from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from app.models import Customer


def get_customer(db: Session, customer_id: int) -> Customer | None:
    return db.get(Customer, customer_id)


def add_customer(db: Session, customer: Customer) -> Customer:
    db.add(customer)
    db.flush()
    return customer


def _phone_digits():
    return func.regexp_replace(func.coalesce(Customer.phone, ""), r"\D", "", "g")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_customers(
    db: Session, *, offset: int, limit: int, search: str | None, search_digits: str
) -> tuple[list[Customer], int]:
    conditions: list[ColumnElement[bool]] = []
    if search:
        matches = [Customer.name.ilike(f"%{_escape_like(search)}%", escape="\\")]
        if len(search_digits) >= 4:  # looks like part of a phone number
            matches.append(_phone_digits().like(f"%{search_digits}%"))
        conditions.append(or_(*matches))
    total = db.scalar(select(func.count()).select_from(Customer).where(*conditions))
    customers = db.scalars(
        select(Customer)
        .where(*conditions)
        .order_by(Customer.name, Customer.id)
        .offset(offset)
        .limit(limit)
    )
    return list(customers), int(total or 0)


def find_possible_duplicates(
    db: Session,
    *,
    name: str,
    phone_last10: str | None,
    exclude_id: int,
    limit: int = 10,
) -> list[Customer]:
    matches: list[ColumnElement[bool]] = [func.lower(Customer.name) == name.lower()]
    if phone_last10:
        matches.append(func.right(_phone_digits(), 10) == phone_last10)
    return list(
        db.scalars(
            select(Customer)
            .where(or_(*matches), Customer.id != exclude_id)
            .order_by(Customer.id)
            .limit(limit)
        )
    )
