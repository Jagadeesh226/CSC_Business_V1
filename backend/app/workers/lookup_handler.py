"""The ``vehicle_expiry_lookup`` task (Phase 4.4).

One round per task: ask the provider (no locks held while it works), then,
holding the vehicle and the lookup row and only if this round is still the
current one, record what happened:

- an answer: validity dates are recorded on the vehicle (source "lookup",
  history kept, reminders follow) and the challan status is updated;
- a CAPTCHA: the lookup waits for a person (a few minutes), with the
  provider's state kept encrypted; their answer starts the next round;
- a temporary error: retried with backoff; a permanent one: the lookup is
  marked failed with a fixed, safe reason.

Idempotent: a stale round (answered again, expired, finished) does nothing.
No lookup data goes into the task's payload or result beyond ids and counts.
"""

import json

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from app.core.storage import seal, unseal
from app.models import BackgroundTask, Vehicle, VehicleLookup
from app.repositories import vehicle_repository
from app.repositories.readiness_repository import business_today
from app.services import audit_service, lookup_service, vehicle_service
from app.services.integration import vehicle_lookup as provider_api
from app.workers import registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

TASK_TYPE = lookup_service.TASK_TYPE


MAX_ROUNDS = 5


def _may_apply(current, found) -> bool:
    """AGENTS.md spirit (never silently overwrite verified data): a found
    date is written when there is none, when the current one also came
    from a lookup, or when it is later than the current one."""
    if current is None or current.valid_until is None:
        return True
    if current.source == "lookup":
        return True
    return found > current.valid_until


def _finish_failed(db: Session, lookup: VehicleLookup, reason: str) -> None:
    lookup.status = "failed"
    lookup.failure_reason = reason[:300]
    lookup.finished_at = vehicle_service._now(db)
    lookup.state_sealed = None
    lookup.captcha_answer_sealed = None
    lookup.captcha_png = None
    lookup.captcha_expires_at = None
    audit_service.record(
        db,
        action="vehicle_lookup_failed",
        entity_type="vehicle",
        entity_id=lookup.vehicle_id,
        user_id=lookup.requested_by,
        details={"lookup_id": lookup.id, "reason": reason[:200]},
    )


def _current(lookup: VehicleLookup | None, round_: int) -> bool:
    return lookup is not None and lookup.round == round_ and lookup.status == "queued"


def _lock_lookup(db: Session, lookup_id: int) -> VehicleLookup | None:
    return db.scalar(
        select(VehicleLookup)
        .where(VehicleLookup.id == lookup_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def run_vehicle_lookup(db: Session, info: TaskInfo) -> dict:
    lookup_id = int(info.payload["lookup_id"])
    round_ = int(info.payload["round"])
    lookup = db.get(VehicleLookup, lookup_id)
    if lookup is None:
        raise PermanentTaskError("The lookup no longer exists.")
    if not _current(lookup, round_):
        return {"skipped": "stale"}
    vehicle = db.get(Vehicle, lookup.vehicle_id)
    request = provider_api.LookupRequest(
        registration_number=vehicle.registration_number,
        chassis_number=vehicle_service.identifier_value(vehicle, "chassis"),
        engine_number=vehicle_service.identifier_value(vehicle, "engine"),
    )
    today = business_today(db)
    try:
        provider = provider_api.get_provider()
        if lookup.state_sealed and lookup.captcha_answer_sealed:
            state = json.loads(
                unseal(lookup.state_sealed, lookup_service.state_context(lookup.id))
            )
            answer = unseal(
                lookup.captcha_answer_sealed, lookup_service.answer_context(lookup.id)
            ).decode()
            outcome = provider.resume(request, state, answer, today)
        else:
            outcome = provider.start(request, today)
    except provider_api.LookupTemporaryError as exc:
        if lookup.captcha_answer_sealed:
            # A CAPTCHA answer is usually good for one try only: resending it
            # on a retry would not work, so this lookup ends here.
            raise PermanentTaskError(
                "The vehicle portal was busy after the CAPTCHA; start a new lookup."
            ) from None
        raise TaskError(str(exc)[:300]) from None
    except provider_api.LookupFailedError as exc:
        raise PermanentTaskError(str(exc)[:300]) from None

    # Lock order: vehicle, then lookup.
    vehicle = vehicle_repository.lock(db, lookup.vehicle_id)
    lookup = _lock_lookup(db, lookup_id)
    if not _current(lookup, round_):
        return {"skipped": "stale"}
    lookup.provider = outcome.provider
    lookup.captcha_answer_sealed = None

    if isinstance(outcome, provider_api.CaptchaNeeded):
        if round_ >= MAX_ROUNDS:
            # Many wrong answers look like guessing to a portal.
            _finish_failed(db, lookup, "Too many CAPTCHA attempts; start a new lookup.")
            return {"lookup_id": lookup.id, "round": round_, "failed": "captcha"}
        lookup.status = "waiting_for_captcha"
        lookup.captcha_png = outcome.image_png
        lookup.captcha_expires_at = lookup_service.captcha_deadline(db)
        lookup.state_sealed = seal(
            json.dumps(outcome.state).encode(), lookup_service.state_context(lookup.id)
        )
        return {"lookup_id": lookup.id, "round": round_, "waiting": "captcha"}

    applied, not_applied = [], []
    if vehicle.is_active:
        current = {
            e.expiry_type: e
            for e in vehicle_repository.current_expiries(db, [vehicle.id])[vehicle.id]
        }
        for kind, valid_until in sorted(outcome.expiries.items()):
            if kind not in provider_api.EXPIRY_TYPES:
                continue
            if not _may_apply(current.get(kind), valid_until):
                not_applied.append(kind)
                continue
            if vehicle_service.record_expiry(
                db,
                vehicle,
                kind,
                valid_until,
                source="lookup",
                actor_id=lookup.requested_by,
            ):
                applied.append(kind)
        if outcome.challan_status in ("none", "pending"):
            vehicle.challan_status = outcome.challan_status
            vehicle.challan_checked_at = vehicle_service._now(db)
    lookup.status = "completed"
    lookup.finished_at = vehicle_service._now(db)
    lookup.state_sealed = None
    lookup.result = {
        "expiries": {k: v.isoformat() for k, v in sorted(outcome.expiries.items())},
        "challan_status": outcome.challan_status,
        "challan_count": outcome.challan_count,
        "applied": applied,
        # Found but not written: the vehicle has a later date entered by staff
        # or read from a document; a person decides.
        "not_applied": not_applied,
    }
    audit_service.record(
        db,
        action="vehicle_lookup_completed",
        entity_type="vehicle",
        entity_id=vehicle.id,
        user_id=lookup.requested_by,
        details={
            "lookup_id": lookup.id,
            "provider": outcome.provider,
            "applied": applied,
            "not_applied": not_applied,
            "challan_status": outcome.challan_status,
        },
    )
    return {"lookup_id": lookup.id, "round": round_, "applied": len(applied)}


def on_lookup_failed(db: Session, info: TaskInfo, error: str) -> None:
    """The round failed for good: record it on the lookup (idempotent)."""
    lookup = _lock_lookup(db, int(info.payload["lookup_id"]))
    if not _current(lookup, int(info.payload["round"])):
        return
    _finish_failed(db, lookup, error)


def expire_unanswered(db: Session) -> int:
    """Maintenance: CAPTCHAs nobody answered in time end the lookup."""
    rows = db.scalars(
        select(VehicleLookup)
        .where(
            VehicleLookup.status == "waiting_for_captcha",
            VehicleLookup.captcha_expires_at <= vehicle_service._now(db),
        )
        .with_for_update(skip_locked=True)
        .limit(100)
    ).all()
    for lookup in rows:
        lookup_service.expire(db, lookup)
    return len(rows)


def recover_stuck_lookups(db: Session) -> int:
    """Maintenance: a lookup still 'queued' whose task for this round has
    finished (or vanished) would block the vehicle for good; it fails."""
    # Bound parameters: ":r" here is plain text, not a placeholder.
    key = func.concat("vehicle_lookup:", VehicleLookup.id, ":r", VehicleLookup.round)
    rows = db.scalars(
        select(VehicleLookup)
        .where(
            VehicleLookup.status == "queued",
            ~exists().where(
                BackgroundTask.idempotency_key == key,
                BackgroundTask.status.in_(("queued", "running")),
            ),
            VehicleLookup.created_at < func.now() - text("interval '5 minutes'"),
        )
        .with_for_update(skip_locked=True)
        .limit(100)
    ).all()
    for lookup in rows:
        _finish_failed(db, lookup, "The lookup stopped unexpectedly; start a new one.")
    return len(rows)


registry.register(TASK_TYPE, run_vehicle_lookup, on_final_failure=on_lookup_failed)
registry.register_maintenance(expire_unanswered)
registry.register_maintenance(recover_stuck_lookups)
