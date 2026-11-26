"""Vehicle lookups, the person's side (Phase 4.4).

Anyone who can see a vehicle (admins; the responsible employee; employees
on an open job of its customer) can start a lookup and type a CAPTCHA for
it. The worker does the lookup (``app.workers.lookup_handler``). Results
from the source are recorded on the vehicle as dates with source "lookup"
(history kept, reminders follow), with the time they were retrieved.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.storage import seal
from app.models import User, Vehicle, VehicleLookup
from app.repositories import vehicle_repository
from app.repositories.document_repository import users_by_id
from app.schemas.job import AssigneeRef
from app.schemas.vehicle_lookup import (
    VehicleLookupList,
    VehicleLookupRead,
    VehicleLookupResult,
    VehicleLookupStatus,
)
from app.services import audit_service, task_queue, vehicle_service
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError
from app.services.integration import vehicle_lookup as provider

TASK_TYPE = "vehicle_expiry_lookup"
CAPTCHA_MINUTES = 5
LOOKUP_NOT_FOUND = "Lookup not found."


def _now(db: Session) -> datetime:
    return db.scalar(select(func.clock_timestamp()))


def answer_context(lookup_id: int) -> str:
    return f"vehicle_lookup:{lookup_id}:answer"


def state_context(lookup_id: int) -> str:
    return f"vehicle_lookup:{lookup_id}:state"


def idempotency_key(lookup_id: int, round_: int) -> str:
    return f"vehicle_lookup:{lookup_id}:r{round_}"


def _read(db: Session, lookup: VehicleLookup) -> VehicleLookupRead:
    users = users_by_id(db, {lookup.requested_by})
    result = None
    if lookup.result is not None:
        result = VehicleLookupResult(
            expiries=lookup.result.get("expiries", {}),
            challan_status=lookup.result.get("challan_status"),
            challan_count=lookup.result.get("challan_count"),
            applied=lookup.result.get("applied", []),
            not_applied=lookup.result.get("not_applied", []),
        )
    waiting = lookup.status == VehicleLookupStatus.WAITING_FOR_CAPTCHA
    return VehicleLookupRead(
        id=lookup.id,
        vehicle_id=lookup.vehicle_id,
        status=lookup.status,
        provider=lookup.provider,
        requested_by=AssigneeRef.model_validate(users[lookup.requested_by])
        if lookup.requested_by in users
        else None,
        created_at=lookup.created_at,
        finished_at=lookup.finished_at,
        failure_reason=lookup.failure_reason,
        result=result,
        captcha_expires_at=lookup.captcha_expires_at if waiting else None,
    )


def _queue(db: Session, lookup: VehicleLookup, actor: User) -> None:
    task_queue.enqueue(
        db,
        TASK_TYPE,
        idempotency_key(lookup.id, lookup.round),
        {"lookup_id": lookup.id, "round": lookup.round},
        created_by=actor.id,
    )


def request_lookup(db: Session, vehicle_id: int, actor: User) -> VehicleLookupRead:
    vehicle_service.get_visible(db, vehicle_id, actor)
    vehicle = vehicle_service.repo.lock(db, vehicle_id)
    if not vehicle.is_active:
        raise BusinessRuleError("The vehicle is inactive.")
    if not provider.configured():
        raise BusinessRuleError(
            "Vehicle lookups are not set up yet; enter the dates by hand."
        )
    try:
        with db.begin_nested():
            lookup = VehicleLookup(
                vehicle_id=vehicle.id,
                requested_by=actor.id,
                status=VehicleLookupStatus.QUEUED.value,
                round=1,
            )
            db.add(lookup)
            db.flush()
    except IntegrityError:
        raise ConflictError(
            "A lookup for this vehicle is already in progress."
        ) from None
    _queue(db, lookup, actor)
    audit_service.record(
        db,
        action="vehicle_lookup_requested",
        entity_type="vehicle",
        entity_id=vehicle.id,
        user_id=actor.id,
        details={"lookup_id": lookup.id},
    )
    db.commit()
    return _read(db, lookup)


def _visible_lookup(db: Session, lookup_id: int, viewer: User, lock: bool = False):
    stmt = select(VehicleLookup).where(VehicleLookup.id == lookup_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    lookup = db.scalar(stmt)
    if lookup is None:
        raise NotFoundError(LOOKUP_NOT_FOUND)
    try:
        vehicle_service.get_visible(db, lookup.vehicle_id, viewer)
    except NotFoundError:
        raise NotFoundError(LOOKUP_NOT_FOUND) from None
    return lookup


def get_lookup(db: Session, lookup_id: int, viewer: User) -> VehicleLookupRead:
    return _read(db, _visible_lookup(db, lookup_id, viewer))


def list_lookups(db: Session, vehicle_id: int, viewer: User) -> VehicleLookupList:
    vehicle_service.get_visible(db, vehicle_id, viewer)
    rows = db.scalars(
        select(VehicleLookup)
        .where(VehicleLookup.vehicle_id == vehicle_id)
        .order_by(VehicleLookup.created_at.desc(), VehicleLookup.id.desc())
        .limit(50)
    ).all()
    return VehicleLookupList(items=[_read(db, r) for r in rows])


def _expire_if_late(db: Session, lookup: VehicleLookup) -> bool:
    if (
        lookup.status == VehicleLookupStatus.WAITING_FOR_CAPTCHA
        and lookup.captcha_expires_at is not None
        and lookup.captcha_expires_at <= _now(db)
    ):
        expire(db, lookup)
        return True
    return False


def expire(db: Session, lookup: VehicleLookup) -> None:
    lookup.status = VehicleLookupStatus.EXPIRED.value
    lookup.finished_at = _now(db)
    lookup.captcha_png = None
    lookup.state_sealed = None
    lookup.failure_reason = "The CAPTCHA was not answered in time."
    audit_service.record(
        db,
        action="vehicle_lookup_expired",
        entity_type="vehicle",
        entity_id=lookup.vehicle_id,
        user_id=lookup.requested_by,
        details={"lookup_id": lookup.id},
    )


def captcha_image(db: Session, lookup_id: int, viewer: User) -> bytes:
    lookup = _visible_lookup(db, lookup_id, viewer, lock=True)
    if _expire_if_late(db, lookup):
        db.commit()
    if (
        lookup.status != VehicleLookupStatus.WAITING_FOR_CAPTCHA
        or not lookup.captcha_png
    ):
        raise NotFoundError("No CAPTCHA is waiting for this lookup.")
    return lookup.captcha_png


def answer_captcha(
    db: Session, lookup_id: int, answer: str, actor: User
) -> VehicleLookupRead:
    lookup = _visible_lookup(db, lookup_id, actor, lock=True)
    if _expire_if_late(db, lookup):
        db.commit()
        raise BusinessRuleError("The CAPTCHA expired; start a new lookup.")
    if lookup.status != VehicleLookupStatus.WAITING_FOR_CAPTCHA:
        raise BusinessRuleError("This lookup is not waiting for a CAPTCHA.")
    if not vehicle_service.repo.get(db, lookup.vehicle_id).is_active:
        raise BusinessRuleError("The vehicle is inactive.")
    lookup.captcha_answer_sealed = seal(answer.encode(), answer_context(lookup.id))
    lookup.captcha_png = None
    lookup.captcha_expires_at = None
    lookup.round += 1
    lookup.status = VehicleLookupStatus.QUEUED.value
    _queue(db, lookup, actor)
    # The answer itself is never logged.
    audit_service.record(
        db,
        action="vehicle_lookup_captcha_answered",
        entity_type="vehicle",
        entity_id=lookup.vehicle_id,
        user_id=actor.id,
        details={"lookup_id": lookup.id, "round": lookup.round},
    )
    db.commit()
    return _read(db, lookup)


def captcha_deadline(db: Session) -> datetime:
    return _now(db) + timedelta(minutes=CAPTCHA_MINUTES)


def waiting_count(db: Session, viewer: User) -> int:
    """CAPTCHAs waiting for this person: lookups they started (all of them
    for admins)."""
    stmt = (
        select(func.count())
        .select_from(VehicleLookup)
        .where(VehicleLookup.status == VehicleLookupStatus.WAITING_FOR_CAPTCHA.value)
    )
    if viewer.role != "admin":
        stmt = stmt.join(Vehicle, Vehicle.id == VehicleLookup.vehicle_id).where(
            VehicleLookup.requested_by == viewer.id,
            vehicle_repository.visible_to(viewer.id),
        )
    return int(db.scalar(stmt) or 0)
