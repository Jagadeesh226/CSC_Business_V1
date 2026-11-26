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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.storage import seal, unseal
from app.models import Vehicle, VehicleLookup
from app.repositories import vehicle_repository
from app.repositories.readiness_repository import business_today
from app.services import audit_service, lookup_service, vehicle_service
from app.services.integration import vehicle_lookup as provider_api
from app.workers import registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

TASK_TYPE = lookup_service.TASK_TYPE


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
        lookup.status = "waiting_for_captcha"
        lookup.captcha_png = outcome.image_png
        lookup.captcha_expires_at = lookup_service.captcha_deadline(db)
        lookup.state_sealed = seal(
            json.dumps(outcome.state).encode(), lookup_service.state_context(lookup.id)
        )
        return {"lookup_id": lookup.id, "round": round_, "waiting": "captcha"}

    applied = []
    if vehicle.is_active:
        for kind, valid_until in sorted(outcome.expiries.items()):
            if kind in provider_api.EXPIRY_TYPES and vehicle_service.record_expiry(
                db, vehicle, kind, valid_until, source="lookup"
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
    }
    audit_service.record(
        db,
        action="vehicle_lookup_completed",
        entity_type="vehicle",
        entity_id=vehicle.id,
        details={
            "lookup_id": lookup.id,
            "provider": outcome.provider,
            "applied": applied,
            "challan_status": outcome.challan_status,
        },
    )
    return {"lookup_id": lookup.id, "round": round_, "applied": len(applied)}


def on_lookup_failed(db: Session, info: TaskInfo, error: str) -> None:
    """The round failed for good: record it on the lookup (idempotent)."""
    lookup = _lock_lookup(db, int(info.payload["lookup_id"]))
    if not _current(lookup, int(info.payload["round"])):
        return
    lookup.status = "failed"
    lookup.failure_reason = error[:300]
    lookup.finished_at = vehicle_service._now(db)
    lookup.state_sealed = None
    lookup.captcha_answer_sealed = None
    audit_service.record(
        db,
        action="vehicle_lookup_failed",
        entity_type="vehicle",
        entity_id=lookup.vehicle_id,
        details={"lookup_id": lookup.id, "reason": error[:200]},
    )


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


registry.register(TASK_TYPE, run_vehicle_lookup, on_final_failure=on_lookup_failed)
registry.register_maintenance(expire_unanswered)
