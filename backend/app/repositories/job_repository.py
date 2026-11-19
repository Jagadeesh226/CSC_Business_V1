from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Row,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Customer, Job, SequenceCounter, Service

JobRow = Row[tuple[Job, Customer, Service | None]]


def get_customer(db: Session, customer_id: int) -> Customer | None:
    return db.get(Customer, customer_id)


def get_service(db: Session, service_id: int) -> Service | None:
    return db.get(Service, service_id)


def get_job(db: Session, job_id: int) -> Job | None:
    return db.get(Job, job_id)


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


def list_jobs(
    db: Session,
    *,
    offset: int,
    limit: int,
    search: str | None = None,
    customer_id: int | None = None,
    service_id: int | None = None,
    status: str | None = None,
    due_date: date | None = None,
) -> tuple[list[JobRow], int]:
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

    total = db.scalar(select(func.count()).select_from(Job).where(*conditions))
    rows = db.execute(
        _with_relations()
        .where(*conditions)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return list(rows), int(total or 0)
