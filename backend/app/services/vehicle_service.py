"""Vehicles and their validity dates.

Access (permissions.md: vehicles):
- Admins see and change every vehicle.
- Employees see vehicles they are responsible for, and vehicles of
  customers they have an open job for; anything else is 404. They can add
  vehicles (assigned to themselves) and edit the vehicles they see, except
  the owner (customer), the person responsible and active/inactive.
- Chassis and engine numbers are encrypted and masked ("****1234"); the
  full values are shown only to an admin or the responsible employee, and
  every view (and refused attempt) is audited.

Validity dates keep their history: setting a date adds a row and retires
the previous one.
"""

import math
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.storage import seal, unseal
from app.core.vehicle_numbers import mask_identifier, normalize
from app.models import Customer, User, Vehicle, VehicleExpiry
from app.repositories import vehicle_repository as repo
from app.repositories.document_repository import users_by_id
from app.repositories.readiness_repository import business_today
from app.schemas.job import AssigneeRef
from app.schemas.vehicle import (
    ExpiryHistoryResponse,
    ExpiryRead,
    ExpirySet,
    ExpiryType,
    ValueSource,
    VehicleCreate,
    VehicleCustomerRef,
    VehicleListResponse,
    VehicleRead,
    VehicleUpdate,
)
from app.services import audit_service, document_service, job_service
from app.services.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)

VEHICLE_NOT_FOUND = "Vehicle not found."
DUPLICATE = "A vehicle with this registration number already exists."
ADMIN_ONLY_FIELDS = ("customer_id", "assigned_to", "is_active")


def _constraint(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def _now(db: Session) -> datetime:
    return db.scalar(select(func.clock_timestamp()))


def _context(vehicle_id: int, part: str) -> str:
    return f"vehicle:{vehicle_id}:{part}"


def _audit(db: Session, action: str, vehicle: Vehicle, actor: User, **details):
    audit_service.record(
        db,
        action=action,
        entity_type="vehicle",
        entity_id=vehicle.id,
        user_id=actor.id,
        details=details or None,
    )


# ---------- access ----------


def _visible(db: Session, vehicle: Vehicle | None, viewer: User) -> Vehicle:
    if vehicle is None:
        raise NotFoundError(VEHICLE_NOT_FOUND)
    if not job_service.is_admin(viewer) and not repo.is_visible(
        db, vehicle.id, viewer.id
    ):
        raise NotFoundError(VEHICLE_NOT_FOUND)
    return vehicle


def get_visible(db: Session, vehicle_id: int, viewer: User) -> Vehicle:
    return _visible(db, repo.get(db, vehicle_id), viewer)


def can_reveal(vehicle: Vehicle, viewer: User) -> bool:
    return job_service.is_admin(viewer) or vehicle.assigned_to == viewer.id


# ---------- reading ----------


def _expiry_reads(
    rows: list[VehicleExpiry], users: dict[int, User], today: date
) -> list[ExpiryRead]:
    return [
        ExpiryRead(
            expiry_type=row.expiry_type,
            valid_until=row.valid_until,
            days_left=(row.valid_until - today).days if row.valid_until else None,
            source=row.source,
            source_document_id=row.source_document_id,
            recorded_by=AssigneeRef.model_validate(users[row.recorded_by])
            if row.recorded_by in users
            else None,
            recorded_at=row.recorded_at,
        )
        for row in rows
    ]


def _identifier(vehicle: Vehicle, part: str, reveal: bool) -> str | None:
    sealed = getattr(vehicle, f"{part}_sealed")
    if sealed is None:
        return None
    if reveal:
        return unseal(sealed, _context(vehicle.id, part)).decode()
    return mask_identifier(getattr(vehicle, f"{part}_last4"))


def _reads(
    db: Session,
    rows: list[tuple[Vehicle, Customer]],
    reveal_ids: set[int] = frozenset(),
) -> list[VehicleRead]:
    vehicles = [v for v, _ in rows]
    expiries = repo.current_expiries(db, [v.id for v in vehicles])
    user_ids = {v.assigned_to for v in vehicles}
    user_ids |= {e.recorded_by for rows_ in expiries.values() for e in rows_}
    users = users_by_id(db, user_ids)
    today = business_today(db)
    out = []
    for vehicle, customer in rows:
        reveal = vehicle.id in reveal_ids
        out.append(
            VehicleRead(
                id=vehicle.id,
                customer=VehicleCustomerRef(id=customer.id, name=customer.name),
                registration_number=vehicle.registration_number,
                category=vehicle.category,
                owner_name=vehicle.owner_name,
                chassis_number=_identifier(vehicle, "chassis", reveal),
                engine_number=_identifier(vehicle, "engine", reveal),
                identifiers_masked=not reveal,
                assigned_to=AssigneeRef.model_validate(users[vehicle.assigned_to])
                if vehicle.assigned_to in users
                else None,
                contact_phone=vehicle.contact_phone,
                contact_phone_source=vehicle.contact_phone_source,
                challan_status=vehicle.challan_status,
                challan_checked_at=vehicle.challan_checked_at,
                is_active=vehicle.is_active,
                created_at=vehicle.created_at,
                updated_at=vehicle.updated_at,
                expiries=_expiry_reads(expiries[vehicle.id], users, today),
            )
        )
    return out


def _read(db: Session, vehicle: Vehicle, reveal: bool = False) -> VehicleRead:
    customer = db.get(Customer, vehicle.customer_id)
    return _reads(db, [(vehicle, customer)], {vehicle.id} if reveal else set())[0]


def get_vehicle(
    db: Session, vehicle_id: int, viewer: User, reveal: bool = False
) -> VehicleRead:
    vehicle = get_visible(db, vehicle_id, viewer)
    if reveal:
        if not can_reveal(vehicle, viewer):
            _audit(db, "vehicle_identifiers_reveal_denied", vehicle, viewer)
            db.commit()
            raise PermissionDeniedError(
                "Only an admin or the employee responsible for this vehicle can "
                "see the full chassis and engine numbers."
            )
        _audit(db, "vehicle_identifiers_viewed", vehicle, viewer)
        db.commit()
    return _read(db, vehicle, reveal)


def list_vehicles(
    db: Session,
    viewer: User,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    customer_id: int | None = None,
    assigned_to: int | None = None,
    category: str | None = None,
    is_active: bool | None = True,
) -> VehicleListResponse:
    search = (search or "").strip()
    rows, total = repo.search(
        db,
        employee_id=None if job_service.is_admin(viewer) else viewer.id,
        key=normalize(search) or None,
        text=search or None,
        customer_id=customer_id,
        assigned_to=assigned_to,
        category=category,
        is_active=is_active,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return VehicleListResponse(
        items=_reads(db, rows),
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


# ---------- writing ----------


def set_identifier(vehicle: Vehicle, part: str, value: str | None) -> None:
    if value is None:
        setattr(vehicle, f"{part}_sealed", None)
        setattr(vehicle, f"{part}_last4", None)
    else:
        setattr(
            vehicle, f"{part}_sealed", seal(value.encode(), _context(vehicle.id, part))
        )
        setattr(vehicle, f"{part}_last4", value[-4:])


def identifier_value(vehicle: Vehicle, part: str) -> str | None:
    return _identifier(vehicle, part, reveal=True)


def _check_user(db: Session, user_id: int) -> None:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise BusinessRuleError("The person responsible must be an active user.")


def _check_customer(db: Session, customer_id: int) -> None:
    if db.get(Customer, customer_id) is None:
        raise NotFoundError("Customer not found.")


def create_vehicle(db: Session, data: VehicleCreate, actor: User) -> VehicleRead:
    _check_customer(db, data.customer_id)
    admin = job_service.is_admin(actor)
    if data.assigned_to is not None and not admin:
        raise PermissionDeniedError("Only an admin can choose who is responsible.")
    if data.assigned_to is not None:
        _check_user(db, data.assigned_to)
    key = normalize(data.registration_number)
    # Uniqueness means a 409 tells the caller the number is taken somewhere;
    # unavoidable while one vehicle is one record.
    if repo.by_key(db, key) is not None:
        raise ConflictError(DUPLICATE)
    try:
        vehicle = repo.add(
            db,
            Vehicle(
                customer_id=data.customer_id,
                registration_number=data.registration_number,
                registration_key=key,
                category=data.category.value,
                owner_name=data.owner_name,
                assigned_to=data.assigned_to if admin else actor.id,
                contact_phone=data.contact_phone,
                contact_phone_source=ValueSource.MANUAL.value
                if data.contact_phone
                else None,
                created_by=actor.id,
            ),
        )
    except IntegrityError as exc:
        db.rollback()
        if _constraint(exc) == "uq_vehicles_registration_key":
            raise ConflictError(DUPLICATE) from None
        raise
    set_identifier(vehicle, "chassis", data.chassis_number)
    set_identifier(vehicle, "engine", data.engine_number)
    _audit(
        db,
        "vehicle_created",
        vehicle,
        actor,
        registration_number=vehicle.registration_number,
        customer_id=vehicle.customer_id,
    )
    db.commit()
    return _read(db, vehicle)


def update_vehicle(
    db: Session, vehicle_id: int, data: VehicleUpdate, actor: User
) -> VehicleRead:
    _visible(db, repo.get(db, vehicle_id), actor)
    vehicle = repo.lock(db, vehicle_id)
    changes = data.model_dump(exclude_unset=True)
    admin = job_service.is_admin(actor)
    blocked = sorted(f for f in ADMIN_ONLY_FIELDS if f in changes)
    if blocked and not admin:
        raise PermissionDeniedError(f"Only an admin can change: {', '.join(blocked)}.")
    # Changing a number you may not see would let you wipe it, or test
    # guesses against it; only those who may see it can change it.
    if not can_reveal(vehicle, actor) and (
        "chassis_number" in changes or "engine_number" in changes
    ):
        raise PermissionDeniedError(
            "Only an admin or the employee responsible for this vehicle can "
            "change its chassis and engine numbers."
        )
    changed: list[str] = []
    if "registration_number" in changes:
        key = normalize(changes["registration_number"])
        if key != vehicle.registration_key:
            other = repo.by_key(db, key)
            if other is not None and other.id != vehicle.id:
                raise ConflictError(DUPLICATE)
            vehicle.registration_key = key
        if changes["registration_number"] != vehicle.registration_number:
            vehicle.registration_number = changes["registration_number"]
            changed.append("registration_number")
    moves: dict[str, dict] = {}
    if "customer_id" in changes and changes["customer_id"] != vehicle.customer_id:
        _check_customer(db, changes["customer_id"])
        if repo.has_open_jobs(db, vehicle.id):
            raise BusinessRuleError(
                "Finish or unlink the open jobs for this vehicle before "
                "moving it to another customer."
            )
        moves["customer_id"] = {
            "from": vehicle.customer_id,
            "to": changes["customer_id"],
        }
        vehicle.customer_id = changes["customer_id"]
        changed.append("customer_id")
    if "assigned_to" in changes and changes["assigned_to"] != vehicle.assigned_to:
        if changes["assigned_to"] is not None:
            _check_user(db, changes["assigned_to"])
        moves["assigned_to"] = {
            "from": vehicle.assigned_to,
            "to": changes["assigned_to"],
        }
        vehicle.assigned_to = changes["assigned_to"]
        changed.append("assigned_to")
    for field in ("category", "owner_name", "is_active"):
        if field in changes:
            value = changes[field]
            value = value.value if hasattr(value, "value") else value
            if getattr(vehicle, field) != value:
                setattr(vehicle, field, value)
                changed.append(field)
    for part in ("chassis", "engine"):
        field = f"{part}_number"
        if field in changes and changes[field] != identifier_value(vehicle, part):
            set_identifier(vehicle, part, changes[field])
            changed.append(field)
    if "contact_phone" in changes and changes["contact_phone"] != vehicle.contact_phone:
        vehicle.contact_phone = changes["contact_phone"]
        vehicle.contact_phone_source = (
            ValueSource.MANUAL.value if changes["contact_phone"] else None
        )
        changed.append("contact_phone")
    if changed:
        vehicle.updated_at = _now(db)
        # Field names only (plus who/which customer it moved between): no
        # identifier values in the audit trail.
        _audit(db, "vehicle_updated", vehicle, actor, fields=sorted(changed), **moves)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _constraint(exc) == "uq_vehicles_registration_key":
            raise ConflictError(DUPLICATE) from None
        raise
    return _read(db, vehicle)


def set_expiry(
    db: Session,
    vehicle_id: int,
    expiry_type: ExpiryType,
    data: ExpirySet,
    actor: User,
) -> VehicleRead:
    _visible(db, repo.get(db, vehicle_id), actor)
    vehicle = repo.lock(db, vehicle_id)
    source = ValueSource.MANUAL.value
    if data.document_id is not None:
        document = document_service.get_accessible(db, data.document_id, actor)
        if document.customer_id != vehicle.customer_id:
            raise BusinessRuleError("That document belongs to another customer.")
        source = ValueSource.DOCUMENT.value
    current = repo.current_expiry(db, vehicle.id, expiry_type.value)
    unchanged = (
        current is not None
        and current.valid_until == data.valid_until
        and current.source_document_id == data.document_id
    )
    if unchanged or (current is None and data.valid_until is None):
        return _read(db, vehicle)  # nothing to record
    if current is not None:
        current.is_current = False
        db.flush()
    repo.add(
        db,
        VehicleExpiry(
            vehicle_id=vehicle.id,
            expiry_type=expiry_type.value,
            valid_until=data.valid_until,
            source=source,
            source_document_id=data.document_id,
            is_current=True,
            recorded_by=actor.id,
            recorded_at=_now(db),
        ),
    )
    vehicle.updated_at = _now(db)
    _audit(
        db,
        "vehicle_expiry_set",
        vehicle,
        actor,
        expiry_type=expiry_type.value,
        valid_until=data.valid_until.isoformat() if data.valid_until else None,
        previous=current.valid_until.isoformat()
        if current is not None and current.valid_until
        else None,
        source=source,
    )
    db.commit()
    return _read(db, vehicle)


def expiry_history(
    db: Session, vehicle_id: int, expiry_type: ExpiryType, viewer: User
) -> ExpiryHistoryResponse:
    vehicle = get_visible(db, vehicle_id, viewer)
    rows = repo.history(db, vehicle.id, expiry_type.value)
    users = users_by_id(db, {r.recorded_by for r in rows})
    return ExpiryHistoryResponse(
        expiry_type=expiry_type,
        items=_expiry_reads(rows, users, business_today(db)),
    )
