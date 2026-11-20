from decimal import Decimal

from sqlalchemy import Row, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.models import JobFee, Payment, User

FeeRow = Row[tuple[JobFee, User | None, User | None]]


def add_fee(db: Session, fee: JobFee) -> JobFee:
    db.add(fee)
    db.flush()
    return fee


def get_active_fee(db: Session, job_id: int, fee_id: int) -> JobFee | None:
    return db.scalar(
        select(JobFee).where(
            JobFee.id == fee_id, JobFee.job_id == job_id, JobFee.deleted_at.is_(None)
        )
    )


def list_fees(db: Session, job_id: int, include_removed: bool) -> list[FeeRow]:
    creator, remover = aliased(User), aliased(User)
    stmt = (
        select(JobFee, creator, remover)
        .outerjoin(creator, JobFee.created_by == creator.id)
        .outerjoin(remover, JobFee.deleted_by == remover.id)
        .where(JobFee.job_id == job_id)
        .order_by(JobFee.id)
    )
    if not include_removed:
        stmt = stmt.where(JobFee.deleted_at.is_(None))
    return list(db.execute(stmt).all())


def active_totals(db: Session, job_id: int) -> dict[str, Decimal]:
    rows = db.execute(
        select(JobFee.fee_type, func.coalesce(func.sum(JobFee.amount), 0))
        .where(JobFee.job_id == job_id, JobFee.deleted_at.is_(None))
        .group_by(JobFee.fee_type)
    )
    return {fee_type: Decimal(total) for fee_type, total in rows}


def has_received_payment(db: Session, job_id: int) -> bool:
    """True once any payment with status 'received' exists for the job."""
    return bool(
        db.scalar(
            select(
                exists().where(Payment.job_id == job_id, Payment.status == "received")
            )
        )
    )
