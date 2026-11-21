"""The ``document_ocr`` task: read a document version and store the result.

Flow: the upload queues this task and sets the document to ``processing``.
The handler reads the stored file, asks the OCR provider for the fields the
document type lists, then - holding the document lock and only if the
document is still that version and still processing - saves the encrypted
result and moves the document to ``needs_review`` for a person to check.

Idempotent: a version that already has a completed result is skipped, and a
stale task (newer version uploaded, document archived or reviewed) does
nothing. The provider is called before any lock is taken, so a slow OCR
service never holds up people working on the document.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.storage import get_storage, seal
from app.models import Document, DocumentType, OcrResult
from app.repositories import document_repository
from app.services import audit_service
from app.services.integration import ocr as ocr_provider
from app.workers import registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

TASK_TYPE = "document_ocr"
LOW_CONFIDENCE = 0.80


def idempotency_key(document_id: int, version: int) -> str:
    return f"{TASK_TYPE}:{document_id}:v{version}"


def sealed_context(document_id: int, version: int) -> str:
    return f"ocr:{document_id}:v{version}"


def field_specs(db: Session, document: Document) -> list[ocr_provider.FieldSpec]:
    if document.document_type_id is None:
        return []
    doc_type = db.get(DocumentType, document.document_type_id)
    return [
        ocr_provider.FieldSpec(f["key"], f["label"], bool(f.get("sensitive")))
        for f in (doc_type.ocr_fields if doc_type else [])
    ]


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


def run_document_ocr(db: Session, info: TaskInfo) -> dict:
    document_id = int(info.payload["document_id"])
    version = int(info.payload["version"])

    document = db.get(Document, document_id)
    if document is None:
        raise PermanentTaskError("The document no longer exists.")
    if not _is_current(document, version):
        return {"skipped": "stale"}
    existing = _existing(db, document_id, version)
    if existing is not None and existing.status == "completed":
        return {"skipped": "already_done"}

    stored = document_repository.get_version(db, document_id, version)
    if stored is None:
        raise PermanentTaskError("The document version no longer exists.")
    specs = field_specs(db, document)
    data = get_storage().load(stored.storage_key)
    try:
        output = ocr_provider.get_provider().extract(data, stored.content_type, specs)
    except ocr_provider.OcrTemporaryError as exc:
        raise TaskError(str(exc)) from None
    except ocr_provider.OcrUnsupportedError as exc:
        raise PermanentTaskError(str(exc)) from None
    del data

    # Only now take the lock, and re-check nothing changed meanwhile.
    document = document_repository.lock_document(db, document_id)
    if not _is_current(document, version):
        return {"skipped": "stale"}

    values = {k: f.value for k, f in output.fields.items()}
    confidences = {k: f.confidence for k, f in output.fields.items()}
    sealed = seal(
        json.dumps({"fields": values, "raw_text": output.raw_text}).encode(),
        sealed_context(document_id, version),
    )
    result = existing or OcrResult(document_id=document_id, document_version=version)
    result.task_id = info.id
    result.status = "completed"
    result.provider = output.provider
    result.provider_version = output.provider_version
    result.confidences = confidences
    result.extraction_sealed = sealed
    result.failure_reason = None
    db.add(result)
    document.status = "needs_review"

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


def on_ocr_failed(db: Session, info: TaskInfo, error: str) -> None:
    """OCR failed for good: send the document to manual review instead of
    leaving it in 'processing'. Skips a document locked by a late handler."""
    document_id = int(info.payload["document_id"])
    version = int(info.payload["version"])
    document = db.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if not _is_current(document, version):
        return
    result = _existing(db, document_id, version) or OcrResult(
        document_id=document_id, document_version=version
    )
    if result.status == "completed":
        return
    result.task_id = info.id
    result.status = "failed"
    result.provider = result.provider or "unknown"
    result.provider_version = result.provider_version or "unknown"
    result.confidences = result.confidences or {}
    result.failure_reason = error[:500]
    db.add(result)
    document.status = "needs_review"
    audit_service.record(
        db,
        action="document_ocr_failed",
        entity_type="document",
        entity_id=document_id,
        details={"version": version, "reason": error[:200]},
    )


registry.register(TASK_TYPE, run_document_ocr, on_final_failure=on_ocr_failed)
