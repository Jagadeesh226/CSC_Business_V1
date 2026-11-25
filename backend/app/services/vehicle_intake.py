"""Registration certificate → vehicle, automatically (Phase 4.1).

When OCR has read a document whose type is the registration certificate,
``process`` runs in the same transaction as saving the OCR result:

- Confident, valid readings are applied with no person involved: a new
  vehicle is created for the document's customer (assigned to the employee
  who uploaded it), or empty fields of the existing vehicle are filled.
- A reading that is low-confidence, missing or invalid (e.g. a chassis
  number that is not 17 characters) is not applied; it waits for a person
  (``check_fields``), and only that field.
- A reading that differs from a value already on the vehicle is never
  applied on its own (AGENTS.md: OCR must not silently overwrite verified
  data); it waits for one "accept" from a person (``pending_changes``).
- A registration already used by another customer's vehicle is a
  ``conflict`` for a person (an admin can move the vehicle).

Every step is audited by field name only; OCR's reading is kept encrypted.
"""

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.storage import seal, unseal
from app.core.vehicle_numbers import mask_identifier, normalize
from app.models import Document, User, Vehicle, VehicleIntake
from app.repositories import vehicle_repository
from app.schemas.job import AssigneeRef
from app.schemas.vehicle import (
    VehicleCategory,
    clean_identifier,
    clean_registration,
)
from app.schemas.vehicle_intake import (
    IntakeCheckField,
    IntakePendingChange,
    IntakeRead,
    IntakeResolve,
    IntakeStatus,
    IntakeVehicleRef,
)
from app.services import audit_service, document_service, job_service
from app.services import vehicle_service as vehicles
from app.services.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)

log = logging.getLogger("app.vehicle_intake")

LOW_CONFIDENCE = 0.80
CHASSIS_LENGTH = 17  # VIN length on vehicles made since 2001
IDENTIFIERS = ("chassis_number", "engine_number")
# OCR keys used, in the order shown to people.
FIELDS = ("registration_number", "owner_name", "chassis_number", "engine_number")
CATEGORY_WORDS = (
    (VehicleCategory.TWO_WHEELER, ("CYCLE", "SCOOTER", "MOPED", "2W", "TWO WHEELER")),
    (
        VehicleCategory.COMMERCIAL,
        (
            "GOODS",
            "TRANSPORT",
            "TAXI",
            "CAB",
            "BUS",
            "TRUCK",
            "LGV",
            "MGV",
            "HGV",
            "3W",
        ),
    ),
    (
        VehicleCategory.PRIVATE_FOUR_WHEELER,
        ("MOTOR CAR", "LMV", "CAR", "JEEP", "SUV", "4W"),
    ),
)


def category_from_class(text: str | None) -> VehicleCategory | None:
    """The RC's 'vehicle class' (e.g. 'M-CYCLE/SCOOTER', 'MOTOR CAR (LMV)')."""
    if not text:
        return None
    upper = text.upper()
    for category, words in CATEGORY_WORDS:
        if any(word in upper for word in words):
            return category
    return VehicleCategory.OTHER


def _clean(key: str, value: str) -> str:
    if key == "registration_number":
        return clean_registration(value)
    if key in IDENTIFIERS:
        return clean_identifier(value)
    name = " ".join(value.split())
    if not 1 <= len(name) <= 200:
        raise ValueError("A name has 1 to 200 characters.")
    return name


def assess(values: dict, confidences: dict) -> tuple[dict[str, str], list[dict]]:
    """Split OCR's reading into values safe to apply and fields for a
    person, with the reason."""
    clean: dict[str, str] = {}
    checks: list[dict] = []
    for key in FIELDS:
        raw = values.get(key)
        if raw is None or not str(raw).strip():
            if key == "registration_number":
                checks.append({"key": key, "reason": "missing"})
            continue
        confidence = confidences.get(key)
        if confidence is None or confidence < LOW_CONFIDENCE:
            checks.append({"key": key, "reason": "low_confidence"})
            continue
        try:
            value = _clean(key, str(raw))
        except ValueError:
            checks.append({"key": key, "reason": "invalid"})
            continue
        if key == "chassis_number" and len(value) != CHASSIS_LENGTH:
            checks.append({"key": key, "reason": "unusual_length"})
            continue
        clean[key] = value
    vehicle_class = values.get("vehicle_class")
    confidence = confidences.get("vehicle_class")
    if vehicle_class and confidence is not None and confidence >= LOW_CONFIDENCE:
        clean["category"] = category_from_class(str(vehicle_class)).value
    return clean, checks


def _context(document_id: int, version: int) -> str:
    return f"vehicle_intake:{document_id}:v{version}"


def _proposal(intake: VehicleIntake) -> dict[str, str]:
    return json.loads(
        unseal(
            intake.proposal_sealed,
            _context(intake.document_id, intake.document_version),
        )
    )


def _audit(db: Session, action: str, entity_type: str, entity_id: int, user_id, **d):
    audit_service.record(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        details={k: v for k, v in d.items() if v is not None} or None,
    )


def _current(vehicle: Vehicle, field: str) -> str | None:
    if field in IDENTIFIERS:
        return vehicles.identifier_value(vehicle, field.removesuffix("_number"))
    return getattr(vehicle, field)


def _set(vehicle: Vehicle, field: str, value: str | None) -> None:
    if field in IDENTIFIERS:
        vehicles.set_identifier(vehicle, field.removesuffix("_number"), value)
    else:
        setattr(vehicle, field, value)


def _create_vehicle(
    db: Session,
    document: Document,
    values: dict[str, str],
    owner: User | None,
    actor_id: int | None,
) -> Vehicle:
    """``owner`` follows up (an active employee); ``actor_id`` is who did it
    for the audit (None when the worker did it on its own)."""
    follow_up = (
        owner.id
        if owner is not None and owner.is_active and not job_service.is_admin(owner)
        else None
    )
    vehicle = vehicle_repository.add(
        db,
        Vehicle(
            customer_id=document.customer_id,
            registration_number=values["registration_number"],
            registration_key=normalize(values["registration_number"]),
            category=values.get("category", VehicleCategory.OTHER.value),
            owner_name=values.get("owner_name"),
            assigned_to=follow_up,
            created_by=actor_id,
        ),
    )
    for field in IDENTIFIERS:
        _set(vehicle, field, values.get(field))
    _audit(
        db,
        "vehicle_created",
        "vehicle",
        vehicle.id,
        actor_id,
        registration_number=vehicle.registration_number,
        customer_id=vehicle.customer_id,
        source="document",
        document_id=document.id,
    )
    return vehicle


def _empty(field: str, current: str | None) -> bool:
    return current is None or (
        field == "category" and current == VehicleCategory.OTHER.value
    )


def _fill(vehicle: Vehicle, values: dict[str, str]) -> tuple[list[str], list[str]]:
    """Fill empty fields; list fields whose reading differs (not applied)."""
    filled, differs = [], []
    for field in ("owner_name", *IDENTIFIERS, "category"):
        new = values.get(field)
        if new is None or (field == "category" and new == VehicleCategory.OTHER.value):
            continue
        current = _current(vehicle, field)
        if _empty(field, current):
            _set(vehicle, field, new)
            filled.append(field)
        elif current != new:
            differs.append(field)
    return filled, differs


def _touch(db: Session, vehicle: Vehicle, fields: list[str], actor_id, document):
    vehicle.updated_at = db.scalar(select(func.clock_timestamp()))
    _audit(
        db,
        "vehicle_updated",
        "vehicle",
        vehicle.id,
        actor_id,
        fields=sorted(fields),
        source="document",
        document_id=document.id,
    )


def _apply(
    db: Session,
    document: Document,
    intake: VehicleIntake,
    clean: dict[str, str],
    checks: list[dict],
    owner: User | None,
) -> None:
    registration = clean.get("registration_number")
    if registration is None:
        intake.status = IntakeStatus.NEEDS_CHECK.value
        return
    vehicle = db.scalar(
        select(Vehicle)
        .where(Vehicle.registration_key == normalize(registration))
        .with_for_update()
    )
    if vehicle is None:
        vehicle = _create_vehicle(db, document, clean, owner, None)
        intake.status = (
            IntakeStatus.NEEDS_CHECK if checks else IntakeStatus.APPLIED
        ).value
    elif vehicle.customer_id != document.customer_id:
        intake.status = IntakeStatus.CONFLICT.value
    else:
        filled, differs = _fill(vehicle, clean)
        if filled:
            _touch(db, vehicle, filled, None, document)
        intake.pending_changes = sorted(differs)
        if checks or differs:
            intake.status = IntakeStatus.NEEDS_CHECK.value
        else:
            intake.status = (
                IntakeStatus.APPLIED if filled else IntakeStatus.NO_CHANGES
            ).value
    intake.vehicle_id = vehicle.id


def process(
    db: Session,
    document: Document,
    version: int,
    values: dict,
    confidences: dict,
    uploaded_by: int | None,
) -> VehicleIntake:
    """Run from the OCR task (inside its transaction). Idempotent.

    Never fails the OCR task: the vehicle work runs in a savepoint. A clash
    with a vehicle created at the same moment (same registration) is
    retried once as an existing vehicle; any other error leaves the reading
    for a person, and the OCR result is kept either way.
    """
    existing = db.scalar(
        select(VehicleIntake).where(
            VehicleIntake.document_id == document.id,
            VehicleIntake.document_version == version,
        )
    )
    if existing is not None:
        return existing
    clean, checks = assess(values, confidences)
    proposal = {
        "read": {
            k: " ".join(str(values[k]).split())
            for k in (*FIELDS, "vehicle_class")
            if values.get(k)
        },
        "clean": clean,
    }
    owner = db.get(User, uploaded_by) if uploaded_by else None
    intake = VehicleIntake(
        document_id=document.id,
        document_version=version,
        check_fields=checks,
        pending_changes=[],
        proposal_sealed=seal(
            json.dumps(proposal).encode(), _context(document.id, version)
        ),
    )
    for attempt in range(2):
        try:
            with db.begin_nested():
                _apply(db, document, intake, clean, checks, owner)
            break
        except IntegrityError:
            intake.vehicle_id, intake.pending_changes = None, []
            if attempt == 0:
                continue  # the other vehicle is visible now: take that path
            intake.status = IntakeStatus.NEEDS_CHECK.value
        except Exception as exc:  # noqa: BLE001 - keep the OCR result
            log.error(
                "Vehicle intake failed (%s); left for a person.", type(exc).__name__
            )
            intake.vehicle_id, intake.pending_changes = None, []
            intake.status = IntakeStatus.NEEDS_CHECK.value
            break
    db.add(intake)
    db.flush()
    _audit(
        db,
        "vehicle_intake",
        "document",
        document.id,
        None,
        version=version,
        status=intake.status,
        vehicle_id=intake.vehicle_id,
        check_fields=[c["key"] for c in checks] or None,
        pending_changes=intake.pending_changes or None,
    )
    return intake


# ---------- the person's side ----------


def _latest(db: Session, document: Document, lock: bool = False) -> VehicleIntake:
    stmt = select(VehicleIntake).where(
        VehicleIntake.document_id == document.id,
        VehicleIntake.document_version == document.current_version,
    )
    intake = db.scalar(stmt.with_for_update() if lock else stmt)
    if intake is None:
        raise NotFoundError("No vehicle reading for this document version.")
    return intake


def _sees_vehicle(db: Session, vehicle: Vehicle | None, viewer: User) -> bool:
    if vehicle is None:
        return False
    return job_service.is_admin(viewer) or vehicle_repository.is_visible(
        db, vehicle.id, viewer.id
    )


def _may_reveal(
    db: Session, document: Document, vehicle: Vehicle | None, viewer: User
) -> bool:
    if job_service.is_admin(viewer):
        return True
    if vehicle is not None:
        return vehicles.can_reveal(vehicle, viewer)
    try:
        document_service.ensure_can_review(db, document, viewer)
    except PermissionDeniedError:
        return document.uploaded_by == viewer.id
    return True


def _shown(field: str, value: str | None, reveal: bool) -> str | None:
    if value is None or reveal or field not in IDENTIFIERS:
        return value
    key = normalize(value)
    # Short readings are hidden completely: four characters would be most
    # of them.
    return mask_identifier(key[-4:]) if len(key) >= 6 else "****"


def _read(
    db: Session,
    intake: VehicleIntake,
    vehicle: Vehicle | None,
    viewer: User,
    reveal: bool,
) -> IntakeRead:
    proposal = _proposal(intake)["read"]
    # Another customer's vehicle (a conflict), or one the viewer may not
    # see, is never described to them.
    visible = vehicle if _sees_vehicle(db, vehicle, viewer) else None
    resolver = db.get(User, intake.resolved_by) if intake.resolved_by else None
    return IntakeRead(
        document_id=intake.document_id,
        document_version=intake.document_version,
        status=intake.status,
        vehicle=IntakeVehicleRef(
            id=visible.id,
            registration_number=visible.registration_number,
            customer_id=visible.customer_id,
        )
        if visible is not None
        else None,
        check_fields=[
            IntakeCheckField(
                key=c["key"],
                reason=c["reason"],
                read_value=_shown(c["key"], proposal.get(c["key"]), reveal),
            )
            for c in intake.check_fields
        ],
        pending_changes=[
            IntakePendingChange(
                key=field,
                current=_shown(field, _current(visible, field), reveal)
                if visible is not None
                else None,
                proposed=_shown(field, proposal.get(field), reveal),
            )
            for field in intake.pending_changes
        ],
        values_masked=not reveal,
        created_at=intake.created_at,
        resolved_by=AssigneeRef.model_validate(resolver) if resolver else None,
        resolved_at=intake.resolved_at,
    )


def get_intake(
    db: Session, document_id: int, viewer: User, reveal: bool = False
) -> IntakeRead:
    document = document_service.get_accessible(db, document_id, viewer)
    intake = _latest(db, document)
    vehicle = db.get(Vehicle, intake.vehicle_id) if intake.vehicle_id else None
    if reveal:
        if not _may_reveal(db, document, vehicle, viewer):
            _audit(
                db, "vehicle_intake_reveal_denied", "document", document.id, viewer.id
            )
            db.commit()
            raise PermissionDeniedError(
                "Only an admin or the employee responsible for this vehicle can "
                "see the full chassis and engine numbers."
            )
        _audit(db, "vehicle_intake_viewed_full", "document", document.id, viewer.id)
        db.commit()
    return _read(db, intake, vehicle, viewer, reveal)


CONFLICT = (
    "This registration belongs to another customer's vehicle. An admin can "
    "move the vehicle to this customer first."
)


def _confirmed(data: IntakeResolve, allowed: set[str]) -> dict[str, str]:
    """The person's values: only for fields that wait for them; a null
    leaves a field as it is."""
    unknown = sorted(str(k) for k in data.values if str(k) not in allowed)
    if unknown:
        raise BusinessRuleError(
            f"These fields do not wait for a person: {', '.join(unknown)}."
        )
    given: dict[str, str] = {}
    for key, value in data.values.items():
        if value is None:
            continue
        try:
            given[str(key)] = _clean(str(key), value)
        except ValueError as exc:
            raise BusinessRuleError(f"{key}: {exc}") from None
    return given


def resolve(
    db: Session, document_id: int, data: IntakeResolve, actor: User
) -> IntakeRead:
    """A person finishes a reading: values for the doubtful fields only,
    and accept (or not) readings that differ from the vehicle. Nothing that
    is already on the vehicle changes without ``accept_changes``."""
    document = document_service.get_accessible(db, document_id, actor)
    if document.archived_at is not None:
        raise BusinessRuleError("The document is archived.")
    intake = _latest(db, document, lock=True)
    if intake.status not in (IntakeStatus.NEEDS_CHECK, IntakeStatus.CONFLICT):
        raise BusinessRuleError("Nothing in this reading waits for a person.")

    allowed = {c["key"] for c in intake.check_fields}
    if intake.vehicle_id is None:
        allowed.add("registration_number")
    given = _confirmed(data, allowed)
    clean: dict[str, str] = dict(_proposal(intake)["clean"])  # confident reads

    vehicle = (
        vehicle_repository.lock(db, intake.vehicle_id) if intake.vehicle_id else None
    )
    if vehicle is not None and not _sees_vehicle(db, vehicle, actor):
        raise ConflictError(CONFLICT)  # someone else's vehicle, or out of reach
    created = False
    if vehicle is None:
        registration = given.get("registration_number") or clean.get(
            "registration_number"
        )
        if not registration:
            raise BusinessRuleError("Confirm the registration number first.")
        vehicle = db.scalar(
            select(Vehicle)
            .where(Vehicle.registration_key == normalize(registration))
            .with_for_update()
        )
        if vehicle is None:
            values = {**clean, **given, "registration_number": registration}
            vehicle = _create_vehicle(db, document, values, actor, actor.id)
            created = True
        elif vehicle.customer_id != document.customer_id or not _sees_vehicle(
            db, vehicle, actor
        ):
            raise ConflictError(CONFLICT)  # nothing is linked or saved
    if vehicle.customer_id != document.customer_id:
        raise ConflictError(CONFLICT)

    if not created:
        given.pop("registration_number", None)
        changes: dict[str, str] = {}
        # Confident readings held back only because the registration
        # needed a person: fill what is empty.
        for field, value in clean.items():
            if field in ("registration_number", "category") or field in given:
                continue
            if _empty(field, _current(vehicle, field)):
                changes[field] = value
        for field, value in given.items():
            current = _current(vehicle, field)
            if (
                not _empty(field, current)
                and current != value
                and not data.accept_changes
            ):
                raise BusinessRuleError(
                    f"{field} already has a value on the vehicle; send "
                    "accept_changes to replace it."
                )
            changes[field] = value
        if data.accept_changes:
            for field in intake.pending_changes:
                value = clean.get(field)
                if value is not None and field not in given:
                    changes[field] = value
        category = clean.get("category")
        if (
            category
            and category != VehicleCategory.OTHER.value
            and (
                _empty("category", vehicle.category)
                or ("category" in intake.pending_changes and data.accept_changes)
            )
        ):
            changes["category"] = category
        if any(f in IDENTIFIERS for f in changes) and not vehicles.can_reveal(
            vehicle, actor
        ):
            raise PermissionDeniedError(
                "Only an admin or the employee responsible for this vehicle can "
                "change its chassis and engine numbers."
            )
        changed = [f for f, v in changes.items() if _current(vehicle, f) != v]
        for field in changed:
            _set(vehicle, field, changes[field])
        if changed:
            _touch(db, vehicle, changed, actor.id, document)
    intake.vehicle_id = vehicle.id
    intake.status = IntakeStatus.RESOLVED.value
    intake.resolved_by = actor.id
    intake.resolved_at = db.scalar(select(func.clock_timestamp()))
    _audit(
        db,
        "vehicle_intake_resolved",
        "document",
        document.id,
        actor.id,
        vehicle_id=vehicle.id,
        accepted_changes=data.accept_changes,
        fields=sorted(given) or None,
    )
    db.commit()
    return _read(db, intake, vehicle, actor, reveal=False)
