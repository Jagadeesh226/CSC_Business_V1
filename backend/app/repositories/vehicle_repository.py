from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import Customer, Job, JobAssignment, Vehicle, VehicleExpiry
from app.schemas.job import FINAL_STATUSES


def add(db: Session, obj):
    db.add(obj)
    db.flush()
    return obj


def get(db: Session, vehicle_id: int) -> Vehicle | None:
    return db.get(Vehicle, vehicle_id)


def lock(db: Session, vehicle_id: int) -> Vehicle | None:
    return db.scalar(
        select(Vehicle)
        .where(Vehicle.id == vehicle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def by_key(db: Session, key: str) -> Vehicle | None:
    return db.scalar(select(Vehicle).where(Vehicle.registration_key == key))


def _open_job_of_customer(user_id: int) -> ColumnElement[bool]:
    return exists().where(
        Job.customer_id == Vehicle.customer_id,
        Job.status.not_in(FINAL_STATUSES),
        JobAssignment.job_id == Job.id,
        JobAssignment.user_id == user_id,
        JobAssignment.unassigned_at.is_(None),
    )


def visible_to(user_id: int) -> ColumnElement[bool]:
    """Employee access: vehicles they are responsible for, and vehicles of
    customers they have an open job for (as with documents)."""
    return or_(Vehicle.assigned_to == user_id, _open_job_of_customer(user_id))


def is_visible(db: Session, vehicle_id: int, user_id: int) -> bool:
    return bool(
        db.scalar(select(exists().where(Vehicle.id == vehicle_id, visible_to(user_id))))
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(
    db: Session,
    *,
    employee_id: int | None,
    key: str | None,
    text: str | None,
    customer_id: int | None,
    assigned_to: int | None,
    category: str | None,
    is_active: bool | None,
    offset: int,
    limit: int,
) -> tuple[list[tuple[Vehicle, Customer]], int]:
    conditions: list[ColumnElement[bool]] = []
    if employee_id is not None:
        conditions.append(visible_to(employee_id))
    if key or text:
        options = []
        if key:
            options.append(
                Vehicle.registration_key.like(f"%{_escape_like(key)}%", escape="\\")
            )
        if text:
            pattern = f"%{_escape_like(text)}%"
            options.append(Vehicle.owner_name.ilike(pattern, escape="\\"))
            options.append(Customer.name.ilike(pattern, escape="\\"))
        conditions.append(or_(*options))
    if customer_id is not None:
        conditions.append(Vehicle.customer_id == customer_id)
    if assigned_to is not None:
        conditions.append(Vehicle.assigned_to == assigned_to)
    if category is not None:
        conditions.append(Vehicle.category == category)
    if is_active is not None:
        conditions.append(Vehicle.is_active.is_(is_active))
    base = select(Vehicle, Customer).join(Customer, Customer.id == Vehicle.customer_id)
    total = db.scalar(
        select(func.count())
        .select_from(Vehicle)
        .join(Customer, Customer.id == Vehicle.customer_id)
        .where(*conditions)
    )
    rows = db.execute(
        base.where(*conditions)
        .order_by(Vehicle.registration_key, Vehicle.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [tuple(r) for r in rows], int(total or 0)


def current_expiries(
    db: Session, vehicle_ids: list[int]
) -> dict[int, list[VehicleExpiry]]:
    result: dict[int, list[VehicleExpiry]] = {v: [] for v in vehicle_ids}
    if vehicle_ids:
        for row in db.scalars(
            select(VehicleExpiry)
            .where(
                VehicleExpiry.vehicle_id.in_(vehicle_ids),
                VehicleExpiry.is_current.is_(True),
            )
            .order_by(VehicleExpiry.expiry_type)
        ):
            result[row.vehicle_id].append(row)
    return result


def current_expiry(
    db: Session, vehicle_id: int, expiry_type: str
) -> VehicleExpiry | None:
    return db.scalar(
        select(VehicleExpiry).where(
            VehicleExpiry.vehicle_id == vehicle_id,
            VehicleExpiry.expiry_type == expiry_type,
            VehicleExpiry.is_current.is_(True),
        )
    )


def history(db: Session, vehicle_id: int, expiry_type: str) -> list[VehicleExpiry]:
    return list(
        db.scalars(
            select(VehicleExpiry)
            .where(
                VehicleExpiry.vehicle_id == vehicle_id,
                VehicleExpiry.expiry_type == expiry_type,
            )
            .order_by(VehicleExpiry.recorded_at.desc(), VehicleExpiry.id.desc())
        )
    )


def refs_for(db: Session, vehicle_ids: set[int]) -> dict[int, Vehicle]:
    ids = {v for v in vehicle_ids if v is not None}
    if not ids:
        return {}
    return {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.id.in_(ids)))}
