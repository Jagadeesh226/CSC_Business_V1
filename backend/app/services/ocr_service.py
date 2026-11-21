"""OCR review: show what OCR read, let a person confirm it, apply it.

- Reading: anyone who can see the document sees the OCR fields with
  confidence and flags (missing / low confidence); views are audited.
  Sensitive values (e.g. Aadhaar, PAN numbers) and the raw text are masked
  unless the reviewer asks to reveal them; revealing needs review rights and
  is audited, and so is a refused attempt.
- Reviewing: the employee assigned to a job using the document (or an admin)
  confirms every field, then verifies or rejects the document. Confirmed
  values are stored encrypted. Values reach the customer record only for the
  fields the reviewer explicitly maps, never automatically (AGENTS.md: OCR
  must not silently overwrite verified data); everything is validated before
  anything changes, and each change is audited by field name only.
- Retrying / starting: OCR can be run again for the current version after it
  failed, or started for a document that was never read.
"""

import json

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.storage import seal, unseal
from app.models import BackgroundTask, Customer, Document, OcrResult, User
from app.schemas.customer import CustomerUpdate
from app.schemas.job import AssigneeRef
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
from app.services.errors import BusinessRuleError, ConflictError, PermissionDeniedError
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


def _task(db: Session, document: Document, lock: bool = False) -> BackgroundTask | None:
    stmt = select(BackgroundTask).where(
        BackgroundTask.idempotency_key
        == ocr_handler.idempotency_key(document.id, document.current_version)
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def _context(document: Document, part: str) -> str:
    return ocr_handler.sealed_context(document.id, document.current_version, part)


def _open(blob: bytes | None, document: Document, part: str) -> dict:
    if not blob:
        return {}
    return json.loads(unseal(blob, _context(document, part)))


def _status(
    db: Session, document: Document, result: OcrResult | None, has_fields: bool
) -> OcrStatus:
    if document.status == "processing":
        task = _task(db, document)
        # A failed task leaves the document here only briefly: the worker's
        # maintenance sweep sends it to manual review.
        return (
            OcrStatus.FAILED if task and task.status == "failed" else OcrStatus.PENDING
        )
    if result is not None:
        return OcrStatus(result.status)
    return OcrStatus.NOT_STARTED if has_fields else OcrStatus.NOT_APPLICABLE


def _read(
    db: Session, document: Document, result: OcrResult | None, reveal: bool
) -> OcrRead:
    # The field list frozen with the result decides labels and masking; the
    # current document type is used only before any result exists.
    specs = (
        result.field_specs
        if result and result.field_specs
        else [
            {"key": s.key, "label": s.label, "sensitive": s.sensitive}
            for s in ocr_handler.field_specs(db, document)
        ]
    )
    extraction = (
        _open(result.extraction_sealed, document, "extraction") if result else {}
    )
    reviewed = _open(result.review_sealed, document, "review") if result else {}
    values = extraction.get("fields", {})
    confidences = result.confidences if result else {}

    fields = []
    for spec in specs:
        key, sensitive = spec["key"], bool(spec.get("sensitive"))
        confidence = confidences.get(key)
        value = values.get(key)
        if value is None:
            flag = FieldFlag.MISSING
        elif confidence is None or confidence < ocr_handler.LOW_CONFIDENCE:
            flag = FieldFlag.LOW_CONFIDENCE
        else:
            flag = FieldFlag.OK
        hide = sensitive and not reveal
        fields.append(
            OcrFieldRead(
                key=key,
                label=spec["label"],
                sensitive=sensitive,
                value=mask_value(value) if hide else value,
                masked=hide and value is not None,
                confidence=confidence,
                flag=flag,
                reviewed_value=mask_value(reviewed.get(key))
                if hide
                else reviewed.get(key),
            )
        )
    reviewer = (
        db.get(User, result.reviewed_by) if result and result.reviewed_by else None
    )
    status = _status(db, document, result, bool(specs))
    # While OCR runs again, an earlier attempt's provider and error are stale.
    settled = result is not None and status != OcrStatus.PENDING
    return OcrRead(
        document_id=document.id,
        document_version=document.current_version,
        document_status=document.status,
        ocr_status=status,
        provider=result.provider if settled else None,
        failure_reason=result.failure_reason if settled else None,
        fields=fields,
        raw_text=extraction.get("raw_text") if reveal else None,
        reviewed_by=AssigneeRef.model_validate(reviewer) if reviewer else None,
        reviewed_at=result.reviewed_at if result else None,
    )


def _audit(db: Session, action: str, document: Document, actor: User, **details):
    audit_service.record(
        db,
        action=action,
        entity_type="document",
        entity_id=document.id,
        user_id=actor.id,
        details={"version": document.current_version, **details},
    )


def get_ocr(db: Session, document_id: int, viewer: User, reveal: bool) -> OcrRead:
    document = document_service.get_accessible(db, document_id, viewer)
    if reveal:
        try:
            document_service.ensure_can_review(db, document, viewer)
        except PermissionDeniedError:
            db.rollback()
            _audit(db, "ocr_reveal_denied", document, viewer)
            db.commit()
            raise
    _audit(db, "ocr_sensitive_viewed" if reveal else "ocr_viewed", document, viewer)
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
            "Only documents waiting for review can be reviewed (this one is "
            f"{document.status})."
        )
    result = _result(db, document)
    if result is None:
        raise BusinessRuleError("This document version has no OCR result yet.")
    keys = [s["key"] for s in result.field_specs]
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
    extracted = _open(result.extraction_sealed, document, "extraction").get(
        "fields", {}
    )
    result.review_sealed = seal(
        json.dumps(confirmed).encode(), _context(document, "review")
    )
    result.reviewed_by = actor.id
    result.reviewed_at = db.scalar(select(func.clock_timestamp()))
    _audit(
        db,
        "ocr_reviewed",
        document,
        actor,
        decision=request.decision.value,
        # Which fields the reviewer changed from what OCR read (names only).
        corrected=sorted(k for k in keys if confirmed.get(k) != extracted.get(k)),
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
    """Run OCR (again) for the current version: after it failed, or for a
    document that was never read (e.g. uploaded before OCR was set up)."""
    document, jobs = document_service.lock_for_write(db, document_id, actor)
    document_service.ensure_can_review(db, document, actor)
    document_service.ensure_no_closed_job(jobs, actor, "process")
    if document.archived_at is not None:
        raise BusinessRuleError("An archived document cannot be processed.")
    if not ocr_handler.wants_ocr(db, document):
        raise BusinessRuleError("This document type is not read by OCR.")
    result = _result(db, document)
    if result is not None and result.status == "completed":
        raise ConflictError("OCR already read this version; nothing to retry.")
    if document.status not in ("processing", "needs_review", "uploaded"):
        raise BusinessRuleError(f"A {document.status} document cannot be processed.")

    task = _task(db, document, lock=True)
    now = db.scalar(select(func.clock_timestamp()))
    if task is None:
        # Never read: start OCR now.
        task_id, _ = task_queue.enqueue(
            db,
            ocr_handler.TASK_TYPE,
            ocr_handler.idempotency_key(document.id, document.current_version),
            {"document_id": document.id, "version": document.current_version},
            created_by=actor.id,
        )
    elif task.status == "failed":
        # Same rules as the admin retry: attempts keep counting.
        task.status = "queued"
        task.max_attempts = task.attempts + registry.limits_for(task.task_type)[1]
        task.run_after = now
        task.finished_at = None
        task.updated_at = now
        task_queue.notify_after_commit(db, [task.id])
        task_id = task.id
    else:
        raise ConflictError(f"OCR is already {task.status} for this version.")
    document.status = "processing"
    _audit(db, "ocr_retried", document, actor, task_id=task_id)
    db.commit()
    return _read(db, document, result, reveal=False)
