"""The ``document_ocr`` task: read a document version and store the result.

Flow: the upload queues this task and sets the document to ``processing``.
The handler reads the stored file, asks the OCR provider for the fields the
document type lists, then - holding the document lock and only if the
document is still that version and still processing - saves the encrypted
result and moves the document to ``needs_review`` for a person to check.

A document never stays in ``processing`` once its task has failed for good:
the failure hook sends it to manual review, and a maintenance sweep run by
every worker catches any case the hook missed (worker crash, lock clash).

Idempotent: a stale task (newer version uploaded, document archived or
already reviewed) does nothing. The provider is called before any lock is
taken, so a slow OCR service never holds up people working on the document.
"""

import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.storage import get_storage, seal
from app.models import BackgroundTask, Document, DocumentType, OcrResult
from app.repositories import document_repository
from app.services import audit_service, notification_service
from app.services.integration import ocr as ocr_provider
from app.workers import registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

TASK_TYPE = "document_ocr"
LOW_CONFIDENCE = 0.80
# Safe, fixed messages: provider error text is never stored or shown.
TEMPORARY_FAILURE = "The OCR service is temporarily unavailable."
UNREADABLE = "The OCR service could not read this file."


def idempotency_key(document_id: int, version: int) -> str:
    return f"{TASK_TYPE}:{document_id}:v{version}"


def sealed_context(document_id: int, version: int, part: str) -> str:
    """Distinct per value ('extraction' or 'review'), so sealed values cannot
    be swapped between columns or rows unnoticed."""
    return f"ocr:{document_id}:v{version}:{part}"


def field_specs(db: Session, document: Document) -> list[ocr_provider.FieldSpec]:
    if document.document_type_id is None:
        return []
    doc_type = db.get(DocumentType, document.document_type_id)
    return [
        ocr_provider.FieldSpec(f["key"], f["label"], bool(f.get("sensitive")))
        for f in (doc_type.ocr_fields if doc_type else [])
    ]


def wants_ocr(db: Session, document: Document) -> bool:
    return ocr_provider.configured() and bool(field_specs(db, document))


def _is_current(document: Document | None, version: int) -> bool:
    return (
        document is not None
        and document.archived_at is None
        and document.current_version == version
        and document.status == "processing"
    )


def _existing(db: Session, document_id: int, version: int) -> OcrResult | None:
    return db.scalar(
        select(OcrResult).where(
            OcrResult.document_id == document_id,
            OcrResult.document_version == version,
        )
    )


def _spec_rows(specs: list[ocr_provider.FieldSpec]) -> list[dict]:
    return [{"key": s.key, "label": s.label, "sensitive": s.sensitive} for s in specs]


def run_document_ocr(db: Session, info: TaskInfo) -> dict:
    document_id = int(info.payload["document_id"])
    version = int(info.payload["version"])

    document = db.get(Document, document_id)
    if document is None:
        raise PermanentTaskError("The document no longer exists.")
    if not _is_current(document, version):
        return {"skipped": "stale"}

    stored = document_repository.get_version(db, document_id, version)
    if stored is None:
        raise PermanentTaskError("The document version no longer exists.")
    specs = field_specs(db, document)
    data = get_storage().load(stored.storage_key)
    try:
        output = ocr_provider.get_provider().extract(
            data,
            stored.content_type,
            specs,
            # Leave time to save the result within the task's limit.
            timeout_seconds=max(info.timeout_seconds - 10, 5),
        )
    except ocr_provider.OcrTemporaryError:
        raise TaskError(TEMPORARY_FAILURE) from None
    except ocr_provider.OcrUnsupportedError:
        raise PermanentTaskError(UNREADABLE) from None
    del data

    # Only now take the lock, and re-check nothing changed meanwhile.
    document = document_repository.lock_document(db, document_id)
    if not _is_current(document, version):
        return {"skipped": "stale"}

    values = {
        s.key: output.fields[s.key].value for s in specs if s.key in output.fields
    }
    confidences = {
        s.key: (output.fields[s.key].confidence if s.key in output.fields else None)
        for s in specs
    }
    result = _existing(db, document_id, version) or OcrResult(
        document_id=document_id, document_version=version
    )
    result.task_id = info.id
    result.status = "completed"
    result.provider = output.provider
    result.provider_version = output.provider_version
    result.field_specs = _spec_rows(specs)
    result.confidences = confidences
    result.extraction_sealed = seal(
        json.dumps({"fields": values, "raw_text": output.raw_text}).encode(),
        sealed_context(document_id, version, "extraction"),
    )
    result.failure_reason = None
    db.add(result)
    document.status = "needs_review"

    doc_type = db.get(DocumentType, document.document_type_id)
    if doc_type is not None and doc_type.vehicle_role == "registration_certificate":
        # Phase 4.1: create or fill the customer's vehicle from this RC.
        from app.services import vehicle_intake

        vehicle_intake.process(
            db, document, version, values, confidences, stored.uploaded_by
        )

    missing = sorted(k for k, c in confidences.items() if c is None)
    low = sorted(
        k for k, c in confidences.items() if c is not None and c < LOW_CONFIDENCE
    )
    audit_service.record(
        db,
        action="document_ocr_completed",
        entity_type="document",
        entity_id=document_id,
        details={"version": version, "missing": missing, "low_confidence": low},
    )
    return {
        "document_id": document_id,
        "version": version,
        "fields": len(confidences),
        "missing": len(missing),
        "low_confidence": len(low),
    }


def send_to_manual_review(
    db: Session, document_id: int, version: int, task_id: int, reason: str
) -> bool:
    """OCR failed for good: record it and let a person review the document.
    Waits for the document lock (bounded by lock_timeout). Idempotent."""
    document = db.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not _is_current(document, version):
        return False
    result = _existing(db, document_id, version) or OcrResult(
        document_id=document_id, document_version=version
    )
    if result.status == "completed":
        return False
    result.task_id = task_id
    result.status = "failed"
    result.provider = result.provider or "unknown"
    result.provider_version = result.provider_version or "unknown"
    result.field_specs = result.field_specs or _spec_rows(field_specs(db, document))
    result.confidences = result.confidences or {}
    result.failure_reason = reason[:500]
    db.add(result)
    document.status = "needs_review"
    audit_service.record(
        db,
        action="document_ocr_failed",
        entity_type="document",
        entity_id=document_id,
        details={"version": version, "reason": reason[:200]},
    )
    stored = document_repository.get_version(db, document_id, version)
    task = db.get(BackgroundTask, task_id)
    notification_service.ocr_failed(
        db,
        document,
        version,
        stored.uploaded_by if stored else None,
        # A retry that fails again is a new failure to announce.
        attempt=f":t{task_id}:a{task.attempts if task else 0}",
    )
    return True


def on_ocr_failed(db: Session, info: TaskInfo, error: str) -> None:
    send_to_manual_review(
        db,
        int(info.payload["document_id"]),
        int(info.payload["version"]),
        info.id,
        error,
    )


def recover_stuck_documents(db: Session) -> int:
    """Maintenance: documents still 'processing' whose OCR task for that
    version has failed go to manual review."""
    # "\\:" keeps SQLAlchemy from reading ":v" as a bind parameter.
    rows = db.execute(
        text(
            """
            SELECT d.id, d.current_version, t.id AS task_id, t.last_error
            FROM documents d
            JOIN background_tasks t
              ON t.idempotency_key = 'document_ocr:' || d.id || '\\:v' || d.current_version
            WHERE d.status = 'processing' AND t.status = 'failed'
            ORDER BY d.id
            LIMIT 100
            """
        )
    ).all()
    return sum(
        send_to_manual_review(
            db, r.id, r.current_version, r.task_id, r.last_error or UNREADABLE
        )
        for r in rows
    )


registry.register(TASK_TYPE, run_document_ocr, on_final_failure=on_ocr_failed)
registry.register_maintenance(recover_stuck_documents)
