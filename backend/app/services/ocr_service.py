"""OCR review: show what OCR read, let a person confirm it, apply it.

- Reading: anyone who can see the document sees the OCR fields with
  confidence and flags (missing / low confidence). Sensitive values (e.g.
  Aadhaar, PAN numbers) and the raw text are masked unless the reviewer asks
  to reveal them; revealing needs review rights and is audited.
- Reviewing: the employee assigned to a job using the document (or an admin)
  confirms every field, then verifies or rejects the document. Confirmed
  values are stored encrypted. Values reach the customer record only for the
  fields the reviewer explicitly maps, never automatically (AGENTS.md: OCR
  must not silently overwrite verified data); each change is audited by
  field name, without the values.
- Retrying: a failed OCR run can be queued again for the same version.
"""

import json

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.storage import seal, unseal
from app.models import BackgroundTask, Customer, Document, OcrResult, User
from app.schemas.customer import CustomerUpdate
from app.schemas.ocr import (
    FieldFlag,
    OcrFieldRead,
    OcrRead,
    OcrReviewRequest,
    OcrReviewResponse,
    OcrStatus,
    ReviewDecision,
)
from app.services import audit_service, document_service, task_queue
from app.services.errors import BusinessRuleError, ConflictError
from app.workers import ocr_handler, registry


def mask_value(value: str | None) -> str | None:
    """Keep the last four letters/digits: '1234 5678 9012' -> '**** **** 9012'."""
    if value is None:
        return None
    keep = 4
    seen = sum(ch.isalnum() for ch in value)
    out = []
    for ch in value:
        if ch.isalnum():
            out.append(ch if seen <= keep else "*")
            seen -= 1
        else:
            out.append(ch)
    return "".join(out)


def _result(db: Session, document: Document) -> OcrResult | None:
    return db.scalar(
        select(OcrResult).where(
            OcrResult.document_id == document.id,
            OcrResult.document_version == document.current_version,
        )
    )


def _context(document: Document) -> str:
    return ocr_handler.sealed_context(document.id, document.current_version)


def _open(blob: bytes | None, document: Document) -> dict:
    if not blob:
        return {}
    return json.loads(unseal(blob, _context(document)))


def _read(
    db: Session, document: Document, result: OcrResult | None, reveal: bool
) -> OcrRead:
    specs = ocr_handler.field_specs(db, document)
    if not specs:
        status = OcrStatus.NOT_APPLICABLE
    elif result is None:
        status = OcrStatus.PENDING
    else:
        status = OcrStatus(result.status)
    extraction = _open(result.extraction_sealed, document) if result else {}
    reviewed = _open(result.review_sealed, document) if result else {}
    values = extraction.get("fields", {})
    confidences = result.confidences if result else {}

    fields = []
    for spec in specs:
        confidence = confidences.get(spec.key)
        value = values.get(spec.key)
        if value is None:
            flag = FieldFlag.MISSING
        elif confidence is None or confidence < ocr_handler.LOW_CONFIDENCE:
            flag = FieldFlag.LOW_CONFIDENCE
        else:
            flag = FieldFlag.OK
        hide = spec.sensitive and not reveal
        fields.append(
            OcrFieldRead(
                key=spec.key,
                label=spec.label,
                sensitive=spec.sensitive,
                value=mask_value(value) if hide else value,
                masked=hide and value is not None,
                confidence=confidence,
                flag=flag,
                reviewed_value=mask_value(reviewed.get(spec.key))
                if hide
                else reviewed.get(spec.key),
            )
        )
    reviewer = (
        db.get(User, result.reviewed_by) if result and result.reviewed_by else None
    )
    return OcrRead(
        document_id=document.id,
        document_version=document.current_version,
        document_status=document.status,
        ocr_status=status,
        provider=result.provider if result else None,
        failure_reason=result.failure_reason if result else None,
        fields=fields,
        raw_text=extraction.get("raw_text") if reveal else None,
        reviewed_by={"id": reviewer.id, "name": reviewer.name} if reviewer else None,
        reviewed_at=result.reviewed_at if result else None,
    )


def get_ocr(db: Session, document_id: int, viewer: User, reveal: bool) -> OcrRead:
    document = document_service.get_accessible(db, document_id, viewer)
    if reveal:
        document_service.ensure_can_review(db, document, viewer)
        audit_service.record(
            db,
            action="ocr_sensitive_viewed",
            entity_type="document",
            entity_id=document.id,
            user_id=viewer.id,
            details={"version": document.current_version},
        )
        db.commit()
    return _read(db, document, _result(db, document), reveal)


def _prepare_customer_changes(request: OcrReviewRequest, confirmed: dict) -> dict:
    """Validate the chosen OCR values against the customer field rules,
    before anything is changed."""
    if not request.apply_to_customer:
        return {}
    changes = {}
    for item in request.apply_to_customer:
        value = confirmed.get(item.ocr_field)
        if not value:
            raise BusinessRuleError(
                f"'{item.ocr_field}' has no confirmed value to apply."
            )
        changes[item.customer_field.value] = value
    try:
        validated = CustomerUpdate(**changes).model_dump(exclude_unset=True)
    except ValidationError as exc:
        loc = exc.errors()[0]["loc"]
        field = loc[0] if loc else "a field"
        raise BusinessRuleError(
            f"The value for customer '{field}' is not valid for that field."
        ) from None
    if validated.get("email"):
        validated["email"] = str(validated["email"]).lower()
    return validated


def _apply_to_customer(
    db: Session, document: Document, validated: dict, actor: User
) -> list:
    customer = db.get(Customer, document.customer_id)
    changed = sorted(k for k, v in validated.items() if getattr(customer, k) != v)
    for field in changed:
        setattr(customer, field, validated[field])
    if changed:
        audit_service.record(
            db,
            action="customer_updated_from_ocr",
            entity_type="customer",
            entity_id=customer.id,
            user_id=actor.id,
            details={"fields": changed, "document_id": document.id},
        )
    return changed


def review(
    db: Session, document_id: int, request: OcrReviewRequest, actor: User
) -> OcrReviewResponse:
    document, jobs = document_service.lock_for_write(db, document_id, actor)
    document_service.ensure_can_review(db, document, actor)
    if document.status != "needs_review":
        raise BusinessRuleError(
            f"Only documents waiting for review can be reviewed (this one is "
            f"{document.status})."
        )
    result = _result(db, document)
    if result is None:
        raise BusinessRuleError("This document version has no OCR result yet.")
    keys = [s.key for s in ocr_handler.field_specs(db, document)]
    unknown = sorted(set(request.fields) - set(keys))
    if unknown:
        raise BusinessRuleError(f"Unknown OCR fields: {', '.join(unknown)}.")
    missing = sorted(set(keys) - set(request.fields))
    if missing:
        raise BusinessRuleError(
            f"Confirm every field (use null if absent): {', '.join(missing)}."
        )
    for item in request.apply_to_customer:
        if item.ocr_field not in keys:
            raise BusinessRuleError(f"Unknown OCR field '{item.ocr_field}'.")

    confirmed = {k: (v or None) for k, v in request.fields.items()}
    # Everything is validated before anything changes.
    customer_changes = (
        _prepare_customer_changes(request, confirmed)
        if request.decision == ReviewDecision.VERIFY
        else {}
    )
    result.review_sealed = seal(json.dumps(confirmed).encode(), _context(document))
    result.reviewed_by = actor.id
    result.reviewed_at = document_service._now()
    audit_service.record(
        db,
        action="ocr_reviewed",
        entity_type="document",
        entity_id=document.id,
        user_id=actor.id,
        details={
            "version": document.current_version,
            "decision": request.decision.value,
            # Which fields the reviewer changed from what OCR read (names only).
            "corrected": sorted(
                k
                for k in keys
                if confirmed.get(k)
                != _open(result.extraction_sealed, document).get("fields", {}).get(k)
            ),
        },
    )
    updated: list = []
    if request.decision == ReviewDecision.VERIFY:
        document_service.apply_verify(db, document, jobs, actor)
        updated = _apply_to_customer(db, document, customer_changes, actor)
    else:
        document_service.apply_reject(db, document, jobs, request.reason, actor)
    db.commit()
    return OcrReviewResponse(
        document=document_service.get_detail(db, document),
        ocr=_read(db, document, result, reveal=False),
        customer_fields_updated=updated,
    )


def retry(db: Session, document_id: int, actor: User) -> OcrRead:
    """Run OCR again for the current version after it failed."""
    document, _ = document_service.lock_for_write(db, document_id, actor)
    document_service.ensure_can_review(db, document, actor)
    if document.archived_at is not None:
        raise BusinessRuleError("An archived document cannot be processed.")
    result = _result(db, document)
    if result is None or result.status != "failed":
        raise ConflictError("OCR has not failed for this version; nothing to retry.")
    if document.status != "needs_review":
        raise BusinessRuleError(
            f"A {document.status} document cannot be processed again."
        )
    task = db.scalar(
        select(BackgroundTask)
        .where(
            BackgroundTask.idempotency_key
            == ocr_handler.idempotency_key(document.id, document.current_version)
        )
        .with_for_update()
    )
    if task is None or task.status != "failed":
        raise ConflictError("The OCR task is not in a failed state.")
    # Same rules as the admin retry: attempts keep counting.
    task.status = "queued"
    task.max_attempts = task.attempts + registry.limits_for(task.task_type)[1]
    task.run_after = db.scalar(select(func.clock_timestamp()))
    task.finished_at = None
    document.status = "processing"
    audit_service.record(
        db,
        action="ocr_retried",
        entity_type="document",
        entity_id=document.id,
        user_id=actor.id,
        details={"version": document.current_version, "task_id": task.id},
    )
    task_queue.notify_after_commit(db, [task.id])
    db.commit()
    return _read(db, document, result, reveal=False)
