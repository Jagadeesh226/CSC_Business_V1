import math
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, JobAssignment, JobStatusHistory, User, Vehicle
from app.repositories import job_repository as repo
from app.repositories import (
    payment_repository,
    readiness_repository,
    user_repository,
    vehicle_repository,
)
from app.repositories.job_repository import JobRow
from app.schemas.job import (
    FINAL_STATUSES,
    STATUS_TRANSITIONS,
    AssigneeRef,
    AssignmentRead,
    CustomerRef,
    DocumentProgress,
    DueFilter,
    JobCreate,
    JobListResponse,
    JobRead,
    JobSort,
    JobStatus,
    JobUpdate,
    JobVehicleRef,
    PaymentFilter,
    Readiness,
    ServiceRef,
    StatusHistoryRead,
)
from app.schemas.user import UserRole
from app.services import audit_service, document_service, fee_service
from app.services.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.services.payment_summary import summarize

JOB_NUMBER_SEQUENCE = "job_number"
JOB_NUMBER_PREFIX = "CSC"
# The job-number year follows the centre's local calendar, not UTC.
BUSINESS_TIMEZONE = readiness_repository.BUSINESS_TIMEZONE

JOB_NOT_FOUND = "Job not found."

# Access rules (docs/contracts/permissions.md and owner decisions):
# admins see and edit every job; employees see and edit only jobs they are
# actively assigned to. For other jobs employees get 404, so they cannot
# discover which jobs exist.


def format_job_number(year: int, value: int) -> str:
    return f"{JOB_NUMBER_PREFIX}-{year}-{value:06d}"


def is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN.value


def ensure_job_access(db: Session, job_id: int, viewer: User | None) -> None:
    """Raise 404 unless ``viewer`` may access the job (None = internal call)."""
    if viewer is None or is_admin(viewer):
        return
    if not repo.is_actively_assigned(db, job_id, viewer.id):
        raise NotFoundError(JOB_NOT_FOUND)


def _progress(counts: dict[str, int]) -> DocumentProgress:
    return DocumentProgress(
        **counts, complete=counts["verified"] == counts["required_total"]
    )


def _to_read(
    row: JobRow,
    assignees: list[User],
    counts: dict[str, int],
    money: tuple[Decimal, Decimal],
    vehicle: Vehicle | None = None,
) -> JobRead:
    job, customer, service = row
    return JobRead(
        id=job.id,
        job_number=job.job_number,
        title=job.title,
        description=job.description,
        status=job.status,
        due_date=job.due_date,
        created_by=job.created_by,
        created_at=job.created_at,
        customer=CustomerRef.model_validate(customer),
        # The name the service had when the job was created (owner decision).
        service=ServiceRef(
            id=service.id,
            name=job.service_name or service.name,
            code=service.code,
        )
        if service is not None
        else None,
        assignees=[AssigneeRef.model_validate(u) for u in assignees],
        allowed_next_statuses=list(STATUS_TRANSITIONS.get(job.status, ())),
        document_progress=_progress(counts),
        payment_summary=summarize(*money),
        vehicle=JobVehicleRef(
            id=vehicle.id, registration_number=vehicle.registration_number
        )
        if vehicle is not None
        else None,
    )


def get_job(db: Session, job_id: int, viewer: User | None = None) -> JobRead:
    row = repo.get_job_with_relations(db, job_id)
    if row is None:
        raise NotFoundError(JOB_NOT_FOUND)
    ensure_job_access(db, job_id, viewer)
    return _to_read(
        row,
        repo.active_assignees(db, [job_id])[job_id],
        readiness_repository.progress_for_jobs(db, [job_id])[job_id],
        payment_repository.totals_for_jobs(db, [job_id])[job_id],
        db.get(Vehicle, row[0].vehicle_id) if row[0].vehicle_id else None,
    )


def create_job(db: Session, data: JobCreate, creator: User | None = None) -> JobRead:
    """Create a job with a server-generated number and status ``pending``.

    ``creator`` is the signed-in user, never taken from the request body. An
    employee who creates a job is assigned to it, so they can keep working on
    it (employees only see their assigned jobs).
    """
    if repo.get_customer(db, data.customer_id) is None:
        raise NotFoundError("Customer not found.")
    service = repo.get_service(db, data.service_id)
    if service is None:
        raise NotFoundError("Service not found.")
    if not service.is_active:
        raise BusinessRuleError("Service is inactive and cannot be used for new jobs.")
    if not repo.service_category_active(db, service):
        raise BusinessRuleError(
            "The service's category is inactive, so it cannot be used for new jobs."
        )

    if data.vehicle_id is not None:
        _check_vehicle(db, data.vehicle_id, data.customer_id)

    created_by = creator.id if creator is not None else None
    # The second attempt runs only after a collision: if a job number exists
    # that the counter did not issue (imported or manually fixed data), the
    # counter is advanced past the highest existing number and allocation is
    # retried once. Without this, the rollback would also undo the increment
    # and every retry would collide with the same number.
    for attempt in range(2):
        try:
            year = repo.current_year(db, BUSINESS_TIMEZONE)
            if attempt:
                repo.sync_sequence_to_existing_job_numbers(
                    db, JOB_NUMBER_SEQUENCE, year, f"{JOB_NUMBER_PREFIX}-{year}-"
                )
            value = repo.next_sequence_value(db, JOB_NUMBER_SEQUENCE, year)
            job = repo.add_job(
                db,
                Job(
                    job_number=format_job_number(year, value),
                    customer_id=data.customer_id,
                    service_id=data.service_id,
                    service_name=service.name,
                    title=data.title,
                    description=data.description,
                    due_date=data.due_date,
                    vehicle_id=data.vehicle_id,
                    status=JobStatus.PENDING.value,
                    created_by=created_by,
                ),
            )
            # Every job's history starts at creation.
            repo.add_status_history(
                db,
                JobStatusHistory(
                    job_id=job.id,
                    old_status=None,
                    new_status=job.status,
                    changed_by=created_by,
                ),
            )
            audit_service.record(
                db,
                action="job_created",
                entity_type="job",
                entity_id=job.id,
                user_id=created_by,
                details={"job_number": job.job_number},
            )
            fee_service.add_default_service_fee(db, job.id, service, created_by)
            # The job's document checklist, frozen from the service today.
            document_service.create_checklist(db, job, service.id)
            if creator is not None and not is_admin(creator):
                _assign(db, job.id, creator, actor=creator, reason="creator")
            db.commit()
            return get_job(db, job.id)
        except IntegrityError:
            db.rollback()

    raise ConflictError("The job could not be created. Please retry.")


def _check_vehicle(db: Session, vehicle_id: int, customer_id: int) -> None:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.customer_id != customer_id:
        raise BusinessRuleError("The vehicle does not belong to this customer.")
    if not vehicle.is_active:
        raise BusinessRuleError("The vehicle is inactive.")


def list_jobs(
    db: Session,
    *,
    page: int,
    page_size: int,
    viewer: User | None = None,
    search: str | None = None,
    customer_id: int | None = None,
    service_id: int | None = None,
    status: JobStatus | None = None,
    due_date: date | None = None,
    assigned_to: int | None = None,
    readiness: Readiness | None = None,
    due: DueFilter | None = None,
    unassigned: bool = False,
    sort: JobSort = JobSort.NEWEST,
    payment: PaymentFilter | None = None,
) -> JobListResponse:
    search = search.strip() if search else None
    rows, total = repo.list_jobs(
        db,
        offset=(page - 1) * page_size,
        limit=page_size,
        search=search or None,
        customer_id=customer_id,
        service_id=service_id,
        status=status.value if status is not None else None,
        due_date=due_date,
        assigned_to=assigned_to,
        visible_to=None if viewer is None or is_admin(viewer) else viewer.id,
        readiness=readiness.value if readiness is not None else None,
        due=due.value if due is not None else None,
        today=readiness_repository.business_today(db) if due is not None else None,
        unassigned=unassigned,
        payment=payment.value if payment is not None else None,
        sort=sort.value,
    )
    job_ids = [row[0].id for row in rows]
    assignees = repo.active_assignees(db, job_ids)
    progress = readiness_repository.progress_for_jobs(db, job_ids)
    money = payment_repository.totals_for_jobs(db, job_ids)
    vehicles = vehicle_repository.refs_for(db, {row[0].vehicle_id for row in rows})
    return JobListResponse(
        items=[
            _to_read(
                row,
                assignees[i],
                progress[i],
                money[i],
                vehicles.get(row[0].vehicle_id),
            )
            for i, row in zip(job_ids, rows, strict=True)
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def update_job(
    db: Session, job_id: int, data: JobUpdate, actor: User | None = None
) -> JobRead:
    job = repo.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    ensure_job_access(db, job_id, actor)
    # Closed jobs are read-only for employees; admins can still correct them.
    if job.status in FINAL_STATUSES and actor is not None and not is_admin(actor):
        raise BusinessRuleError(f"A {job.status} job cannot be edited.")

    changed = {
        field: value
        for field, value in data.model_dump(exclude_unset=True).items()
        if getattr(job, field) != value
    }
    if changed.get("vehicle_id") is not None:
        _check_vehicle(db, changed["vehicle_id"], job.customer_id)
    for field, value in changed.items():
        setattr(job, field, value)
    if changed:
        audit_service.record(
            db,
            action="job_updated",
            entity_type="job",
            entity_id=job.id,
            user_id=actor.id if actor is not None else None,
            details={"fields": sorted(changed)},
        )
    db.commit()

    return get_job(db, job_id)


# ---------- assignments ----------


def _assign(
    db: Session, job_id: int, user: User, *, actor: User, reason: str | None = None
) -> JobAssignment:
    assignment = repo.add_assignment(
        db, JobAssignment(job_id=job_id, user_id=user.id, assigned_by=actor.id)
    )
    details: dict[str, object] = {"user_id": user.id}
    if reason:
        details["reason"] = reason
    audit_service.record(
        db,
        action="job_assigned",
        entity_type="job",
        entity_id=job_id,
        user_id=actor.id,
        details=details,
    )
    return assignment


def assign_user(db: Session, job_id: int, user_id: int, actor: User) -> JobRead:
    """Admin assigns an active user (admin or employee) to an open job."""
    job = repo.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    # Lock order everywhere: the job row (taken above), then the user row.
    # Locking the user means a concurrent deactivation cannot slip between
    # this check and the insert.
    user = user_repository.get_user_for_update(db, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    if not user.is_active:
        raise BusinessRuleError("Inactive users cannot be assigned.")
    if job.status in FINAL_STATUSES:
        raise BusinessRuleError(f"Cannot assign people to a {job.status} job.")
    if repo.get_active_assignment(db, job_id, user_id) is not None:
        raise ConflictError("This user is already assigned to the job.")

    try:
        _assign(db, job_id, user, actor=actor)
        db.commit()
    except IntegrityError:
        # A concurrent request assigned the same user first.
        db.rollback()
        raise ConflictError("This user is already assigned to the job.") from None
    return get_job(db, job_id)


def unassign_user(db: Session, job_id: int, user_id: int, actor: User) -> JobRead:
    """Admin removes a person from an open job; the row is kept as history.

    Closed jobs keep their assignments as the record of who did the work.
    """
    job = repo.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    if job.status in FINAL_STATUSES:
        raise BusinessRuleError(f"Cannot remove people from a {job.status} job.")
    if not repo.end_assignment(db, job_id, user_id, actor.id, datetime.now(UTC)):
        raise NotFoundError("This user is not assigned to the job.")
    audit_service.record(
        db,
        action="job_unassigned",
        entity_type="job",
        entity_id=job_id,
        user_id=actor.id,
        details={"user_id": user_id},
    )
    db.commit()
    return get_job(db, job_id)


def list_assignments(
    db: Session, job_id: int, viewer: User | None = None
) -> list[AssignmentRead]:
    if repo.get_job(db, job_id) is None:
        raise NotFoundError(JOB_NOT_FOUND)
    ensure_job_access(db, job_id, viewer)
    return [
        AssignmentRead(
            id=assignment.id,
            user=AssigneeRef.model_validate(assignee),
            assigned_by=AssigneeRef.model_validate(assigner),
            assigned_at=assignment.assigned_at,
            unassigned_by=AssigneeRef.model_validate(remover) if remover else None,
            unassigned_at=assignment.unassigned_at,
        )
        for assignment, assignee, assigner, remover in repo.list_assignments(db, job_id)
    ]


def lock_open_jobs_of(db: Session, user_id: int) -> list[int]:
    """First step of deactivation: lock the user's open jobs (job rows are
    always locked before user rows)."""
    return repo.lock_open_jobs_assigned_to(db, user_id, FINAL_STATUSES)


def unassign_from_jobs(
    db: Session, user_id: int, job_ids: list[int], actor: User
) -> None:
    """Remove a deactivated user from the given locked open jobs (caller
    commits)."""
    for job_id in repo.end_assignments_on_jobs(
        db, user_id, job_ids, actor.id, datetime.now(UTC)
    ):
        audit_service.record(
            db,
            action="job_unassigned",
            entity_type="job",
            entity_id=job_id,
            user_id=actor.id,
            details={"user_id": user_id, "reason": "user_deactivated"},
        )


# ---------- status changes ----------


def _record_status_change(
    db: Session, job: Job, new_status: str, actor: User, reason: str | None
) -> None:
    old_status = job.status
    job.status = new_status
    repo.add_status_history(
        db,
        JobStatusHistory(
            job_id=job.id,
            old_status=old_status,
            new_status=new_status,
            changed_by=actor.id,
            reason=reason,
        ),
    )
    details: dict[str, object] = {"from": old_status, "to": new_status}
    if reason:
        details["reason"] = reason
    audit_service.record(
        db,
        action="job_cancelled"
        if new_status == JobStatus.CANCELLED.value
        else "job_status_changed",
        entity_type="job",
        entity_id=job.id,
        user_id=actor.id,
        details=details,
    )


def change_status(
    db: Session,
    job_id: int,
    new_status: JobStatus,
    actor: User,
    reason: str | None = None,
    override_document_check: bool = False,
) -> JobRead:
    """Move a job to its next status. Admins: any job; employees: assigned.

    The job row is locked, so concurrent changes are applied one at a time and
    the later one is checked against the status the earlier one left.
    """
    job = repo.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    ensure_job_access(db, job_id, actor)
    if new_status == JobStatus.CANCELLED:
        raise BusinessRuleError("Use the cancel action to cancel a job.")
    allowed = STATUS_TRANSITIONS.get(job.status, ())
    if new_status not in allowed:
        if not allowed:
            raise BusinessRuleError(f"A {job.status} job cannot change status.")
        options = ", ".join(s.value for s in allowed)
        raise BusinessRuleError(f"A {job.status} job can only move to: {options}.")
    if new_status == JobStatus.SUBMITTED:
        _check_documents_before_submit(db, job, actor, reason, override_document_check)
    _record_status_change(db, job, new_status.value, actor, reason)
    db.commit()
    return get_job(db, job_id)


def _check_documents_before_submit(
    db: Session, job: Job, actor: User, reason: str | None, override: bool
) -> None:
    """Owner decision: a job is submitted only when every required checklist
    document is verified. Admins may override, with a reason, and it is
    audited. Runs under the job lock, which document writes also take."""
    checklist = document_service.build_checklist(db, job.id)
    missing = [
        i.document_type_name
        for i in checklist.items
        if i.is_required and i.state != "verified"
    ]
    if not missing:
        if override:
            raise BusinessRuleError(
                "No override is needed: all documents are verified."
            )
        return
    if not override:
        raise BusinessRuleError(
            "Required documents are not verified yet: " + ", ".join(missing) + "."
        )
    if not is_admin(actor):
        raise PermissionDeniedError("Only an admin can override the document check.")
    if not reason:
        raise BusinessRuleError("Overriding the document check needs a reason.")
    audit_service.record(
        db,
        action="document_check_overridden",
        entity_type="job",
        entity_id=job.id,
        user_id=actor.id,
        details={"missing": missing, "reason": reason},
    )


def cancel_job(db: Session, job_id: int, actor: User, reason: str) -> JobRead:
    """Admin-only (enforced by the route): cancel any job that is not final."""
    job = repo.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    if job.status in FINAL_STATUSES:
        raise BusinessRuleError(f"A {job.status} job cannot be cancelled.")
    _record_status_change(db, job, JobStatus.CANCELLED.value, actor, reason)
    db.commit()
    return get_job(db, job_id)


def list_status_history(
    db: Session, job_id: int, viewer: User | None = None
) -> list[StatusHistoryRead]:
    if repo.get_job(db, job_id) is None:
        raise NotFoundError(JOB_NOT_FOUND)
    ensure_job_access(db, job_id, viewer)
    return [
        StatusHistoryRead(
            id=entry.id,
            old_status=entry.old_status,
            new_status=entry.new_status,
            changed_by=AssigneeRef.model_validate(user) if user else None,
            reason=entry.reason,
            changed_at=entry.changed_at,
        )
        for entry, user in repo.list_status_history(db, job_id)
    ]
