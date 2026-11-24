from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import Customer, Vehicle
from app.repositories import vehicle_repository


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
    db: Session,
    *,
    offset: int,
    limit: int,
    name_query: str | None,
    phone_digits: str | None,
    registration_key: str | None = None,
    vehicles_visible_to: int | None = None,
) -> tuple[list[Customer], int]:
    """Search either by name or by phone digits, never both at once. A name
    query that looks like a registration number also finds the customers
    owning a matching vehicle."""
    conditions: list[ColumnElement[bool]] = []
    if name_query:
        by_name = Customer.name.ilike(f"%{_escape_like(name_query)}%", escape="\\")
        if registration_key:
            vehicle_match = [
                Vehicle.customer_id == Customer.id,
                Vehicle.registration_key.like(
                    f"%{_escape_like(registration_key)}%", escape="\\"
                ),
            ]
            if vehicles_visible_to is not None:
                vehicle_match.append(vehicle_repository.visible_to(vehicles_visible_to))
            by_vehicle = exists().where(*vehicle_match)
            conditions.append(or_(by_name, by_vehicle))
        else:
            conditions.append(by_name)
    elif phone_digits:
        if len(phone_digits) >= 10:
            # A full number: compare the last 10 digits, ignoring +91 or 0.
            conditions.append(func.right(_phone_digits(), 10) == phone_digits[-10:])
        else:
            conditions.append(_phone_digits().like(f"%{phone_digits}%"))
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
