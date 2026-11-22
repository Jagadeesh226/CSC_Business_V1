from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import Customer, Job, JobAssignment, Payment, Receipt
from app.repositories.readiness_repository import BUSINESS_TIMEZONE
from app.schemas.receipt import ReceiptFilters


def get(db: Session, receipt_id: int) -> Receipt | None:
    return db.get(Receipt, receipt_id)


def lock(db: Session, receipt_id: int) -> Receipt | None:
    return db.scalar(
        select(Receipt)
        .where(Receipt.id == receipt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conditions(
    filters: ReceiptFilters, employee_id: int | None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if employee_id is not None:
        # Employees see receipts of jobs they are assigned to (open or closed).
        conditions.append(
            exists().where(
                JobAssignment.job_id == Receipt.job_id,
                JobAssignment.user_id == employee_id,
                JobAssignment.unassigned_at.is_(None),
            )
        )
    if filters.search:
        pattern = f"%{_escape_like(filters.search)}%"
        conditions.append(
            or_(
                Receipt.receipt_number.ilike(pattern, escape="\\"),
                Job.job_number.ilike(pattern, escape="\\"),
                Customer.name.ilike(pattern, escape="\\"),
            )
        )
    if filters.customer_id is not None:
        conditions.append(Receipt.customer_id == filters.customer_id)
    if filters.job_id is not None:
        conditions.append(Receipt.job_id == filters.job_id)
    if filters.method is not None:
        conditions.append(Payment.payment_method == filters.method.value)
    if filters.status is not None:
        conditions.append(
            Receipt.cancelled_at.is_not(None)
            if filters.status.value == "cancelled"
            else Receipt.cancelled_at.is_(None)
        )
    issued_on = func.date(func.timezone(BUSINESS_TIMEZONE, Receipt.issued_at))
    if filters.date_from is not None:
        conditions.append(issued_on >= filters.date_from)
    if filters.date_to is not None:
        conditions.append(issued_on <= filters.date_to)
    return conditions


def _joined(stmt):
    return (
        stmt.join(Payment, Payment.id == Receipt.payment_id)
        .join(Job, Job.id == Receipt.job_id)
        .join(Customer, Customer.id == Receipt.customer_id)
    )


def search(
    db: Session,
    filters: ReceiptFilters,
    *,
    employee_id: int | None,
    offset: int,
    limit: int,
) -> tuple[list[tuple[Receipt, Payment, Job]], int]:
    """Newest first."""
    conditions = _conditions(filters, employee_id)
    total = db.scalar(
        _joined(select(func.count()).select_from(Receipt)).where(*conditions)
    )
    rows = db.execute(
        _joined(select(Receipt, Payment, Job).select_from(Receipt))
        .where(*conditions)
        .order_by(Receipt.issued_at.desc(), Receipt.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [tuple(r) for r in rows], int(total or 0)
