import math
from datetime import UTC, date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, JobAssignment, User
from app.repositories import job_repository as repo
from app.repositories import user_repository
from app.repositories.job_repository import JobRow
from app.schemas.job import (
    FINAL_STATUSES,
    AssigneeRef,
    AssignmentRead,
    CustomerRef,
    JobCreate,
    JobListResponse,
    JobRead,
    JobStatus,
    JobUpdate,
    ServiceRef,
)
from app.schemas.user import UserRole
from app.services import audit_service
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError

JOB_NUMBER_SEQUENCE = "job_number"
JOB_NUMBER_PREFIX = "CSC"
# The job-number year follows the centre's local calendar, not UTC.
BUSINESS_TIMEZONE = "Asia/Kolkata"

JOB_NOT_FOUND = "Job not found."

# Access rules (docs/contracts/permissions.md and owner decisions):
# admins see and edit every job; employees see and edit only jobs they are
# actively assigned to. For other jobs employees get 404, so they cannot
# discover which jobs exist.


def format_job_number(year: int, value: int) -> str:
    return f"{JOB_NUMBER_PREFIX}-{year}-{value:06d}"


def _is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN.value


def _ensure_job_access(db: Session, job_id: int, viewer: User | None) -> None:
    """Raise 404 unless ``viewer`` may access the job (None = internal call)."""
    if viewer is None or _is_admin(viewer):
        return
    if not repo.is_actively_assigned(db, job_id, viewer.id):
        raise NotFoundError(JOB_NOT_FOUND)


def _to_read(row: JobRow, assignees: list[User]) -> JobRead:
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
        service=ServiceRef.model_validate(service) if service is not None else None,
        assignees=[AssigneeRef.model_validate(u) for u in assignees],
    )


def get_job(db: Session, job_id: int, viewer: User | None = None) -> JobRead:
    row = repo.get_job_with_relations(db, job_id)
    if row is None:
        raise NotFoundError(JOB_NOT_FOUND)
    _ensure_job_access(db, job_id, viewer)
    return _to_read(row, repo.active_assignees(db, [job_id])[job_id])


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
                    title=data.title,
                    description=data.description,
                    due_date=data.due_date,
                    status=JobStatus.PENDING.value,
                    created_by=created_by,
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
            if creator is not None and not _is_admin(creator):
                _assign(db, job.id, creator, actor=creator, reason="creator")
            db.commit()
            return get_job(db, job.id)
        except IntegrityError:
            db.rollback()

    raise ConflictError("The job could not be created. Please retry.")


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
        visible_to=None if viewer is None or _is_admin(viewer) else viewer.id,
    )
    assignees = repo.active_assignees(db, [row[0].id for row in rows])
    return JobListResponse(
        items=[_to_read(row, assignees[row[0].id]) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def update_job(
    db: Session, job_id: int, data: JobUpdate, actor: User | None = None
) -> JobRead:
    job = repo.get_job(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    _ensure_job_access(db, job_id, actor)

    changed = {
        field: value
        for field, value in data.model_dump(exclude_unset=True).items()
        if getattr(job, field) != value
    }
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
    job = repo.get_job(db, job_id)
    if job is None:
        raise NotFoundError(JOB_NOT_FOUND)
    # Locked so a concurrent deactivation cannot slip between this check and
    # the insert (it would miss the not-yet-committed assignment).
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
    """Admin removes a person from a job; the row is kept as history."""
    if repo.get_job(db, job_id) is None:
        raise NotFoundError(JOB_NOT_FOUND)
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
    _ensure_job_access(db, job_id, viewer)
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


def unassign_from_open_jobs(db: Session, user_id: int, actor: User) -> None:
    """Remove a deactivated user from every open job (caller commits)."""
    for job_id in repo.unassign_from_open_jobs(
        db, user_id, actor.id, datetime.now(UTC), FINAL_STATUSES
    ):
        audit_service.record(
            db,
            action="job_unassigned",
            entity_type="job",
            entity_id=job_id,
            user_id=actor.id,
            details={"user_id": user_id, "reason": "user_deactivated"},
        )
