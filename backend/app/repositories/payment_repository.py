from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JobFee, Payment, Receipt

ZERO = Decimal("0.00")


def add(db: Session, obj):
    db.add(obj)
    db.flush()
    return obj


def get_by_key(db: Session, key: str) -> Payment | None:
    return db.scalar(select(Payment).where(Payment.idempotency_key == key))


def get(db: Session, payment_id: int) -> Payment | None:
    return db.get(Payment, payment_id)


def lock(db: Session, payment_id: int) -> Payment | None:
    """Row-lock a payment (after its job: jobs are always locked first)."""
    return db.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def receipt_for(db: Session, payment_id: int) -> Receipt | None:
    return db.scalar(select(Receipt).where(Receipt.payment_id == payment_id))


def receipts_for(db: Session, payment_ids: list[int]) -> dict[int, Receipt]:
    if not payment_ids:
        return {}
    return {
        r.payment_id: r
        for r in db.scalars(select(Receipt).where(Receipt.payment_id.in_(payment_ids)))
    }


def list_for_job(db: Session, job_id: int) -> list[Payment]:
    return list(
        db.scalars(
            select(Payment)
            .where(Payment.job_id == job_id)
            .order_by(Payment.received_at, Payment.id)
        )
    )


def totals_for_jobs(
    db: Session, job_ids: list[int]
) -> dict[int, tuple[Decimal, Decimal]]:
    """(active fees total, received payments total) per job, in two queries."""
    result = {job_id: (ZERO, ZERO) for job_id in job_ids}
    if not job_ids:
        return result
    fees = dict(
        db.execute(
            select(JobFee.job_id, func.sum(JobFee.amount))
            .where(JobFee.job_id.in_(job_ids), JobFee.deleted_at.is_(None))
            .group_by(JobFee.job_id)
        ).all()
    )
    paid = dict(
        db.execute(
            select(Payment.job_id, func.sum(Payment.amount))
            .where(Payment.job_id.in_(job_ids), Payment.status == "received")
            .group_by(Payment.job_id)
        ).all()
    )
    for job_id in job_ids:
        result[job_id] = (
            Decimal(fees.get(job_id) or ZERO),
            Decimal(paid.get(job_id) or ZERO),
        )
    return result
