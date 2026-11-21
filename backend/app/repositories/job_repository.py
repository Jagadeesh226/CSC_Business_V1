from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Row,
    Select,
    Subquery,
    and_,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from app.models import (
    Customer,
    Job,
    JobAssignment,
    JobStatusHistory,
    SequenceCounter,
    Service,
    ServiceCategory,
    User,
)
from app.repositories import readiness_repository as readiness_repo
from app.schemas.job import DOCUMENT_STAGES, FINAL_STATUSES

JobRow = Row[tuple[Job, Customer, Service | None]]


def get_customer(db: Session, customer_id: int) -> Customer | None:
    return db.get(Customer, customer_id)


def service_category_active(db: Session, service: Service) -> bool:
    category = db.get(ServiceCategory, service.category_id)
    return category is not None and category.is_active


def get_service(db: Session, service_id: int) -> Service | None:
    return db.get(Service, service_id)


def get_job(db: Session, job_id: int) -> Job | None:
    return db.get(Job, job_id)


def get_job_for_update(db: Session, job_id: int) -> Job | None:
    """Row-lock the job until the transaction ends.

    Every write that depends on the job's status takes this lock, so status
    changes, edits and assignment changes on one job happen one at a time.
    """
    return db.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def current_year(db: Session, timezone: str, at: datetime | None = None) -> int:
    """Calendar year in the given time zone, from the database clock by default.

    ``at`` overrides the clock (used by tests for year-boundary checks).
    """
    moment = func.now() if at is None else literal(at, DateTime(timezone=True))
    return int(db.scalar(select(func.extract("year", func.timezone(timezone, moment)))))


def sync_sequence_to_existing_job_numbers(
    db: Session, name: str, year: int, prefix: str
) -> None:
    """Advance a counter past the highest existing job number with ``prefix``.

    Recovers from job numbers the counter did not issue. Never lowers it.
    """
    suffix = func.substring(Job.job_number, len(prefix) + 1)
    highest = db.scalar(
        select(func.max(cast(suffix, BigInteger))).where(
            Job.job_number.startswith(prefix, autoescape=True),
            suffix.regexp_match("^[0-9]+$"),
        )
    )
    if not highest:
        return
    stmt = insert(SequenceCounter).values(name=name, year=year, last_value=highest)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[SequenceCounter.name, SequenceCounter.year],
            set_={
                "last_value": func.greatest(
                    SequenceCounter.last_value, stmt.excluded.last_value
                )
            },
        )
    )


def next_sequence_value(db: Session, name: str, year: int) -> int:
    """Atomically allocate the next value of a per-year counter.

    The upsert locks the counter row until the caller's transaction ends, so
    concurrent callers are serialised and never receive the same value. If the
    transaction rolls back, the increment rolls back with it.
    """
    stmt = (
        insert(SequenceCounter)
        .values(name=name, year=year, last_value=1)
        .on_conflict_do_update(
            index_elements=[SequenceCounter.name, SequenceCounter.year],
            set_={"last_value": SequenceCounter.last_value + 1},
        )
        .returning(SequenceCounter.last_value)
    )
    return int(db.execute(stmt).scalar_one())


def add_job(db: Session, job: Job) -> Job:
    db.add(job)
    db.flush()
    return job


def _with_relations():
    return (
        select(Job, Customer, Service)
        .join(Customer, Job.customer_id == Customer.id)
        .outerjoin(Service, Job.service_id == Service.id)
    )


def get_job_with_relations(db: Session, job_id: int) -> JobRow | None:
    return db.execute(_with_relations().where(Job.id == job_id)).first()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _job_filters(
    *,
    search: str | None = None,
    customer_id: int | None = None,
    service_id: int | None = None,
    status: str | None = None,
    due_date: date | None = None,
    assigned_to: int | None = None,
    visible_to: int | None = None,
    readiness: str | None = None,
    due: str | None = None,
    today: date | None = None,
    unassigned: bool = False,
) -> tuple[list[ColumnElement[bool]], Subquery | None]:
    conditions: list[ColumnElement[bool]] = []
    if search:
        pattern = f"%{_escape_like(search)}%"
        conditions.append(
            or_(
                Job.job_number.ilike(pattern, escape="\\"),
                Job.title.ilike(pattern, escape="\\"),
            )
        )
    if customer_id is not None:
        conditions.append(Job.customer_id == customer_id)
    if service_id is not None:
        conditions.append(Job.service_id == service_id)
    if status is not None:
        conditions.append(Job.status == status)
    if due_date is not None:
        conditions.append(Job.due_date == due_date)
    for user_id in {u for u in (assigned_to, visible_to) if u is not None}:
        conditions.append(_actively_assigned(user_id))
    progress = None
    if readiness is not None:
        progress = readiness_repo.progress_subquery()
        conditions.append(readiness_condition(progress, readiness))
    if due is not None:
        if today is None:
            raise ValueError("The due filter needs today's business date.")
        conditions.append(Job.status.not_in(FINAL_STATUSES))
        conditions.append(
            Job.due_date < today if due == "overdue" else Job.due_date == today
        )
    if unassigned:
        conditions.append(Job.status.not_in(FINAL_STATUSES))
        conditions.append(
            ~exists().where(
                JobAssignment.job_id == Job.id, JobAssignment.unassigned_at.is_(None)
            )
        )
    return conditions, progress


def _count(conditions, progress: Subquery | None) -> Select:
    stmt = select(func.count()).select_from(Job)
    if progress is not None:
        stmt = stmt.outerjoin(progress, progress.c.job_id == Job.id)
    return stmt.where(*conditions)


def count_jobs(db: Session, **filters) -> int:
    """Number of jobs matching the same filters as ``list_jobs``."""
    return int(db.scalar(_count(*_job_filters(**filters))) or 0)


def list_jobs(
    db: Session, *, offset: int, limit: int, sort: str = "newest", **filters
) -> tuple[list[JobRow], int]:
    """Filters (see ``_job_filters``): ``visible_to`` limits results to jobs
    that user is actively assigned to (employee access); ``assigned_to`` is
    the same condition used as a filter; ``due`` needs ``today`` (the
    business date).
    """
    conditions, progress = _job_filters(**filters)
    rows_stmt = _with_relations()
    if progress is not None:
        rows_stmt = rows_stmt.outerjoin(progress, progress.c.job_id == Job.id)
    if sort == "urgency":
        order = (Job.due_date.asc().nulls_last(), Job.created_at, Job.id)
    else:
        order = (Job.created_at.desc(), Job.id.desc())
    total = db.scalar(_count(conditions, progress))
    rows = db.execute(
        rows_stmt.where(*conditions).order_by(*order).offset(offset).limit(limit)
    ).all()
    return list(rows), int(total or 0)


def readiness_condition(progress: Subquery, readiness: str) -> ColumnElement[bool]:
    """Jobs still gathering documents whose progress matches ``readiness``
    (see ``app.schemas.job.Readiness``)."""

    def col(name: str) -> ColumnElement[int]:
        return readiness_repo.column(progress, name)

    complete = readiness_repo.is_complete(progress)
    match readiness:
        case "missing_documents":
            cond = (col("missing") + col("rejected")) > 0
        case "needs_review":
            cond = col("to_review") > 0
        case "processing":
            cond = col("processing") > 0
        case "ready_to_submit":
            return and_(Job.status == "in_progress", complete)
        case "ready_to_resume":
            return and_(Job.status == "waiting_for_documents", complete)
        case _:
            raise ValueError(f"Unknown readiness {readiness!r}")
    return and_(Job.status.in_(DOCUMENT_STAGES), cond)


# ---------- assignments ----------


def _actively_assigned(user_id: int) -> ColumnElement[bool]:
    return exists().where(
        JobAssignment.job_id == Job.id,
        JobAssignment.user_id == user_id,
        JobAssignment.unassigned_at.is_(None),
    )


def is_actively_assigned(db: Session, job_id: int, user_id: int) -> bool:
    return (
        db.scalar(
            select(JobAssignment.id).where(
                JobAssignment.job_id == job_id,
                JobAssignment.user_id == user_id,
                JobAssignment.unassigned_at.is_(None),
            )
        )
        is not None
    )


def get_active_assignment(
    db: Session, job_id: int, user_id: int
) -> JobAssignment | None:
    return db.scalar(
        select(JobAssignment).where(
            JobAssignment.job_id == job_id,
            JobAssignment.user_id == user_id,
            JobAssignment.unassigned_at.is_(None),
        )
    )


def end_assignment(
    db: Session, job_id: int, user_id: int, actor_id: int, now: datetime
) -> bool:
    """End the active assignment in one conditional UPDATE.

    Returns False if there was none, including when a concurrent request
    ended it first, so a removal is recorded and audited exactly once.
    """
    ended = db.scalar(
        update(JobAssignment)
        .where(
            JobAssignment.job_id == job_id,
            JobAssignment.user_id == user_id,
            JobAssignment.unassigned_at.is_(None),
        )
        .values(unassigned_at=now, unassigned_by=actor_id)
        .returning(JobAssignment.id)
    )
    return ended is not None


def add_assignment(db: Session, assignment: JobAssignment) -> JobAssignment:
    db.add(assignment)
    db.flush()
    return assignment


def active_assignees(db: Session, job_ids: list[int]) -> dict[int, list[User]]:
    """Current assignees for a page of jobs, in one query."""
    result: dict[int, list[User]] = {job_id: [] for job_id in job_ids}
    if not job_ids:
        return result
    rows = db.execute(
        select(JobAssignment.job_id, User)
        .join(User, JobAssignment.user_id == User.id)
        .where(JobAssignment.job_id.in_(job_ids), JobAssignment.unassigned_at.is_(None))
        .order_by(JobAssignment.assigned_at, JobAssignment.id)
    )
    for job_id, user in rows:
        result[job_id].append(user)
    return result


AssignmentRow = Row[tuple[JobAssignment, User, User, User | None]]


def list_assignments(db: Session, job_id: int) -> list[AssignmentRow]:
    """Full assignment history of a job, oldest first."""
    assignee, assigner, remover = aliased(User), aliased(User), aliased(User)
    return list(
        db.execute(
            select(JobAssignment, assignee, assigner, remover)
            .join(assignee, JobAssignment.user_id == assignee.id)
            .join(assigner, JobAssignment.assigned_by == assigner.id)
            .outerjoin(remover, JobAssignment.unassigned_by == remover.id)
            .where(JobAssignment.job_id == job_id)
            .order_by(JobAssignment.assigned_at, JobAssignment.id)
        ).all()
    )


def lock_open_jobs_assigned_to(
    db: Session, user_id: int, final_statuses: tuple[str, ...]
) -> list[int]:
    """Row-lock the open jobs the user is actively assigned to, in id order.

    A job being closed concurrently is waited for and then re-checked, so it
    is skipped once it is completed or cancelled.
    """
    return list(
        db.scalars(
            select(Job.id)
            .join(JobAssignment, JobAssignment.job_id == Job.id)
            .where(
                JobAssignment.user_id == user_id,
                JobAssignment.unassigned_at.is_(None),
                Job.status.not_in(final_statuses),
            )
            .order_by(Job.id)
            .with_for_update(of=Job)
        )
    )


def end_assignments_on_jobs(
    db: Session, user_id: int, job_ids: list[int], actor_id: int, now: datetime
) -> list[int]:
    """End the user's active assignments on the given (locked) jobs."""
    if not job_ids:
        return []
    return list(
        db.scalars(
            update(JobAssignment)
            .where(
                JobAssignment.user_id == user_id,
                JobAssignment.unassigned_at.is_(None),
                JobAssignment.job_id.in_(job_ids),
            )
            .values(unassigned_at=now, unassigned_by=actor_id)
            .returning(JobAssignment.job_id)
        )
    )


# ---------- status history ----------


def add_status_history(db: Session, entry: JobStatusHistory) -> None:
    db.add(entry)
    db.flush()


def list_status_history(
    db: Session, job_id: int
) -> list[Row[tuple[JobStatusHistory, User | None]]]:
    return list(
        db.execute(
            select(JobStatusHistory, User)
            .outerjoin(User, JobStatusHistory.changed_by == User.id)
            .where(JobStatusHistory.job_id == job_id)
            .order_by(JobStatusHistory.id)  # ids follow the job lock; times may not
        ).all()
    )
