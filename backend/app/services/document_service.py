"""Customer documents: upload, versions, verification, archive and access.

Access (owner decisions):
- Admins: every document.
- Employees: documents used by a job they are assigned to, plus all of a
  customer's documents while they are assigned to one of that customer's
  open jobs (so they can find and reuse them).
- Verifying or rejecting: the employee assigned to a job the document is used
  by, or an admin. Archiving: admins only (documents are never deleted).

Locks: writes lock the related job rows first (id order), then the document,
matching the job -> user lock order used elsewhere. Every file access and
change is audited; audit details never contain file contents or OCR values.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.storage import get_storage
from app.models import (
    Customer,
    Document,
    DocumentType,
    DocumentVersion,
    Job,
    JobChecklistItem,
    JobDocument,
    User,
)
from app.repositories import document_repository as repo
from app.schemas.catalogue import DocumentTypeRef
from app.schemas.document import (
    ChecklistDocument,
    ChecklistItemRead,
    ChecklistResponse,
    ChecklistState,
    DocumentDetail,
    DocumentRead,
    DocumentStatus,
    DocumentVersionRead,
)
from app.schemas.job import FINAL_STATUSES, AssigneeRef
from app.services import audit_service, job_service, task_queue
from app.services.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.services.file_validation import CheckedFile

DOCUMENT_NOT_FOUND = "Document not found."
# States in which a document still occupies a single-document checklist item.
_ACTIVE_STATES = (
    DocumentStatus.UPLOADED.value,
    DocumentStatus.PROCESSING.value,
    DocumentStatus.NEEDS_REVIEW.value,
    DocumentStatus.VERIFIED.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(db: Session, action: str, document_id: int, actor: User, **details):
    audit_service.record(
        db,
        action=action,
        entity_type="document",
        entity_id=document_id,
        user_id=actor.id,
        details={k: v for k, v in details.items() if v is not None} or None,
    )


# ---------- access ----------


def can_access(db: Session, document: Document, user: User) -> bool:
    if job_service.is_admin(user):
        return True
    return repo.assigned_to_linked_job(
        db, user.id, document.id
    ) or repo.assigned_to_open_job_of_customer(
        db, user.id, document.customer_id, FINAL_STATUSES
    )


def _deny(db: Session, document: Document | None, user: User) -> None:
    """404 for a missing or forbidden document; forbidden attempts on real
    documents are audited (requirements: audit denied access)."""
    if document is not None:
        db.rollback()
        _audit(db, "document_access_denied", document.id, user)
        db.commit()
    raise NotFoundError(DOCUMENT_NOT_FOUND)


def get_accessible(db: Session, document_id: int, user: User) -> Document:
    document = repo.get_document(db, document_id)
    if document is None or not can_access(db, document, user):
        _deny(db, document, user)
    return document


def lock_for_write(db: Session, document_id: int, user: User):
    """Lock the document's jobs then the document; check read access.

    If the document gained or lost a job link between reading the links and
    locking it, the caller is asked to retry, so the closed-job and submit
    checks always see every job the document belongs to.
    """
    document = get_accessible(db, document_id, user)
    job_ids = repo.linked_job_ids(db, document.id)
    jobs = repo.lock_jobs(db, job_ids)
    document = repo.lock_document(db, document_id)
    if repo.linked_job_ids(db, document_id) != job_ids:
        raise ConflictError("The document changed while saving. Please retry.")
    return document, jobs


def ensure_can_review(db: Session, document: Document, user: User) -> None:
    if job_service.is_admin(user):
        return
    if not repo.assigned_to_linked_job(db, user.id, document.id):
        raise PermissionDeniedError(
            "Only the employee assigned to a job using this document, or an "
            "admin, can verify or reject it."
        )


def ensure_no_closed_job(jobs: list[Job], user: User, action: str) -> None:
    """Documents of completed/cancelled jobs are a record; employees cannot
    change them (admins can, to correct mistakes)."""
    if job_service.is_admin(user):
        return
    closed = [j.job_number for j in jobs if j.status in FINAL_STATUSES]
    if closed:
        raise BusinessRuleError(
            f"Cannot {action} a document used by a closed job ({closed[0]})."
        )


# ---------- reading ----------


def _type_refs(db: Session, docs: list[Document]) -> dict[int, DocumentType]:
    ids = {d.document_type_id for d in docs if d.document_type_id}
    return {i: db.get(DocumentType, i) for i in ids}


def _ref(users: dict[int, User], user_id: int | None) -> AssigneeRef | None:
    user = users.get(user_id) if user_id else None
    return AssigneeRef.model_validate(user) if user else None


def _reads(db: Session, docs: list[Document]) -> list[DocumentRead]:
    links = repo.links_for_documents(db, [d.id for d in docs])
    types = _type_refs(db, docs)
    users = repo.users_by_id(
        db, {u for d in docs for u in (d.uploaded_by, d.verified_by)}
    )
    return [
        DocumentRead(
            id=d.id,
            customer_id=d.customer_id,
            document_type=DocumentTypeRef.model_validate(types[d.document_type_id])
            if d.document_type_id
            else None,
            filename=d.filename,
            content_type=d.content_type,
            size_bytes=d.size_bytes,
            page_count=d.page_count,
            current_version=d.current_version,
            status=d.status,
            rejection_reason=d.rejection_reason,
            uploaded_by=_ref(users, d.uploaded_by),
            created_at=d.created_at,
            verified_by=_ref(users, d.verified_by),
            verified_at=d.verified_at,
            archived_at=d.archived_at,
            archive_reason=d.archive_reason,
            reusable=d.reuse_blocked_at is None,
            reuse_block_reason=d.reuse_block_reason,
            job_ids=links[d.id],
        )
        for d in docs
    ]


def get_document(db: Session, document_id: int, viewer: User) -> DocumentDetail:
    document = get_accessible(db, document_id, viewer)
    _audit(db, "document_viewed", document.id, viewer)
    db.commit()
    return _detail(db, document)


def get_detail(db: Session, document: Document) -> DocumentDetail:
    return _detail(db, document)


def _detail(db: Session, document: Document) -> DocumentDetail:
    versions = repo.list_versions(db, document.id)
    users = repo.users_by_id(db, {v.uploaded_by for v in versions})
    return DocumentDetail(
        **_reads(db, [document])[0].model_dump(),
        versions=[
            DocumentVersionRead(
                version=v.version,
                original_filename=v.original_filename,
                content_type=v.content_type,
                size_bytes=v.size_bytes,
                sha256=v.sha256,
                page_count=v.page_count,
                change_reason=v.change_reason,
                uploaded_by=_ref(users, v.uploaded_by),
                uploaded_at=v.uploaded_at,
            )
            for v in versions
        ],
    )


def list_customer_documents(
    db: Session,
    customer_id: int,
    viewer: User,
    *,
    include_archived: bool = False,
    status: DocumentStatus | None = None,
) -> list[DocumentRead]:
    if db.get(Customer, customer_id) is None:
        raise NotFoundError("Customer not found.")
    only_for = None
    if not job_service.is_admin(viewer) and not repo.assigned_to_open_job_of_customer(
        db, viewer.id, customer_id, FINAL_STATUSES
    ):
        only_for = viewer.id  # just the documents of their own jobs
    docs = repo.list_customer_documents(
        db,
        customer_id,
        include_archived=include_archived,
        status=status.value if status else None,
        only_for_user_jobs=only_for,
    )
    return _reads(db, docs)


def read_file(
    db: Session, document_id: int, viewer: User, version: int | None, inline: bool
) -> tuple[DocumentVersion, bytes]:
    """Decrypt a version for preview/download; always audited."""
    document = get_accessible(db, document_id, viewer)
    number = version or document.current_version
    stored = repo.get_version(db, document.id, number)
    if stored is None:
        raise NotFoundError("Version not found.")
    data = get_storage().load(stored.storage_key)
    _audit(
        db,
        "document_previewed" if inline else "document_downloaded",
        document.id,
        viewer,
        version=number,
    )
    db.commit()
    return stored, data


# ---------- writing ----------


def _store(
    db: Session,
    document: Document,
    file: CheckedFile,
    version: int,
    actor: User,
    change_reason: str | None,
) -> str:
    storage_key = get_storage().save(file.data)
    repo.add(
        db,
        DocumentVersion(
            document_id=document.id,
            version=version,
            storage_key=storage_key,
            original_filename=file.filename,
            content_type=file.content_type,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            page_count=file.page_count,
            change_reason=change_reason,
            uploaded_by=actor.id,
        ),
    )
    document.filename = file.filename
    document.storage_key = storage_key
    document.content_type = file.content_type
    document.size_bytes = file.size_bytes
    document.sha256 = file.sha256
    document.page_count = file.page_count
    document.current_version = version
    return storage_key


def _commit_or_discard(db: Session, storage_key: str) -> None:
    """Commit; if that fails, remove the file just written so no orphan
    encrypted file is left without a database row."""
    try:
        db.commit()
    except Exception:
        db.rollback()
        get_storage().delete(storage_key)
        raise


def _occupied(db: Session, item: JobChecklistItem) -> bool:
    """A single-document checklist item already has a live document."""
    return any(
        item_id == item.id and doc.status in _ACTIVE_STATES
        for item_id, doc in repo.documents_for_job_items(db, item.job_id)
    )


def upload(
    db: Session,
    customer_id: int,
    file: CheckedFile,
    actor: User,
    *,
    document_type_id: int | None = None,
    job_id: int | None = None,
    checklist_item_id: int | None = None,
) -> DocumentDetail:
    if db.get(Customer, customer_id) is None:
        raise NotFoundError("Customer not found.")
    if checklist_item_id is not None and job_id is None:
        raise BusinessRuleError("A checklist item needs its job_id.")

    item = None
    if job_id is not None:
        job = repo.lock_jobs(db, [job_id])
        if not job:
            raise NotFoundError(job_service.JOB_NOT_FOUND)
        job = job[0]
        job_service.ensure_job_access(db, job.id, actor)
        if job.customer_id != customer_id:
            raise BusinessRuleError("That job belongs to another customer.")
        ensure_no_closed_job([job], actor, "add")
        if checklist_item_id is not None:
            item = repo.get_checklist_item(db, checklist_item_id)
            if item is None or item.job_id != job.id:
                raise NotFoundError("Checklist item not found.")
            if (
                document_type_id is not None
                and document_type_id != item.document_type_id
            ):
                raise BusinessRuleError(
                    "The document type does not match the checklist item."
                )
            document_type_id = item.document_type_id
            if not item.allow_multiple and _occupied(db, item):
                raise ConflictError(
                    "This checklist item already has a document. Upload a new "
                    "version of it instead."
                )
    elif not job_service.is_admin(actor) and not repo.assigned_to_open_job_of_customer(
        db, actor.id, customer_id, FINAL_STATUSES
    ):
        raise PermissionDeniedError(
            "You can add customer documents only while assigned to one of the "
            "customer's open jobs."
        )

    if document_type_id is not None and item is None:
        doc_type = db.get(DocumentType, document_type_id)
        if doc_type is None:
            raise NotFoundError("Document type not found.")
        if not doc_type.is_active:
            raise BusinessRuleError("That document type is no longer in use.")

    document = repo.add(
        db,
        Document(
            customer_id=customer_id,
            job_id=job_id,
            document_type_id=document_type_id,
            uploaded_by=actor.id,
            filename=file.filename,
            storage_key="pending",
            content_type=file.content_type,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            page_count=file.page_count,
            current_version=1,
            status=DocumentStatus.UPLOADED.value,
        ),
    )
    storage_key = _store(db, document, file, 1, actor, None)
    if job_id is not None:
        repo.add(
            db,
            JobDocument(
                job_id=job_id,
                document_id=document.id,
                checklist_item_id=checklist_item_id,
                linked_by=actor.id,
            ),
        )
    _audit(
        db,
        "document_uploaded",
        document.id,
        actor,
        job_id=job_id,
        checklist_item_id=checklist_item_id,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
    )
    _queue_ocr(db, document, actor)
    _commit_or_discard(db, storage_key)
    return _detail(db, document)


def _queue_ocr(db: Session, document: Document, actor: User) -> None:
    """If the document's type lists OCR fields, read it in the background:
    the document waits in 'processing' until OCR puts it up for review.
    The task is queued in this transaction (it exists only if the upload is
    saved) and keyed by document version, so it is queued once."""
    from app.workers import ocr_handler

    if not ocr_handler.wants_ocr(db, document):
        return
    document.status = DocumentStatus.PROCESSING.value
    task_queue.enqueue(
        db,
        ocr_handler.TASK_TYPE,
        ocr_handler.idempotency_key(document.id, document.current_version),
        {"document_id": document.id, "version": document.current_version},
        created_by=actor.id,
    )


def add_version(
    db: Session, document_id: int, file: CheckedFile, change_reason: str, actor: User
) -> DocumentDetail:
    document, jobs = lock_for_write(db, document_id, actor)
    # Replacing a file un-verifies it, so it needs the same right as
    # verifying or rejecting (review M-1).
    ensure_can_review(db, document, actor)
    if document.archived_at is not None:
        raise BusinessRuleError("An archived document cannot get new versions.")
    ensure_no_closed_job(jobs, actor, "replace")
    if document.status == DocumentStatus.REJECTED.value:
        # Reviving a rejected document must not give a single-document
        # checklist item a second live document (review L-1).
        for item in repo.single_items_of_document(db, document.id):
            if _occupied(db, item):
                raise ConflictError(
                    f"'{item.document_type_name}' already has another document "
                    "for this job."
                )
    version = document.current_version + 1
    storage_key = _store(db, document, file, version, actor, change_reason)
    # A new file must be checked again.
    document.status = DocumentStatus.UPLOADED.value
    document.verified_by = None
    document.verified_at = None
    document.rejection_reason = None
    _audit(db, "document_version_added", document.id, actor, version=version)
    _queue_ocr(db, document, actor)
    _commit_or_discard(db, storage_key)
    return _detail(db, document)


def apply_verify(db: Session, document: Document, jobs: list[Job], actor: User) -> None:
    """Verify an already locked document (caller commits)."""
    ensure_can_review(db, document, actor)
    if document.archived_at is not None:
        raise BusinessRuleError("An archived document cannot be verified.")
    if document.status not in (
        DocumentStatus.UPLOADED.value,
        DocumentStatus.NEEDS_REVIEW.value,
    ):
        raise BusinessRuleError(
            f"A {document.status} document cannot be verified."
            + (
                " Upload a new version instead."
                if document.status == "rejected"
                else ""
            )
        )
    ensure_no_closed_job(jobs, actor, "verify")
    document.status = DocumentStatus.VERIFIED.value
    document.verified_by = actor.id
    document.verified_at = _now()
    _audit(
        db, "document_verified", document.id, actor, version=document.current_version
    )


def verify(db: Session, document_id: int, actor: User) -> DocumentDetail:
    document, jobs = lock_for_write(db, document_id, actor)
    ensure_can_review(db, document, actor)
    # A document read by OCR is verified through the OCR review, so its
    # extracted values are always looked at (review L-6).
    from app.models import OcrResult

    pending_review = db.scalar(
        select(OcrResult.id).where(
            OcrResult.document_id == document.id,
            OcrResult.document_version == document.current_version,
            OcrResult.reviewed_at.is_(None),
        )
    )
    if pending_review is not None:
        raise BusinessRuleError(
            "This document was read by OCR: confirm its fields in the OCR "
            "review to verify it."
        )
    apply_verify(db, document, jobs, actor)
    db.commit()
    return _detail(db, document)


def apply_reject(
    db: Session, document: Document, jobs: list[Job], reason: str, actor: User
) -> None:
    """Reject an already locked document (caller commits)."""
    ensure_can_review(db, document, actor)
    if document.archived_at is not None:
        raise BusinessRuleError("An archived document cannot be rejected.")
    if document.status == DocumentStatus.REJECTED.value:
        raise BusinessRuleError("The document is already rejected.")
    if document.status == DocumentStatus.PROCESSING.value:
        raise BusinessRuleError("Wait for processing to finish before rejecting.")
    if document.status == DocumentStatus.VERIFIED.value and len(jobs) >= 2:
        # Owner decision: a verified document shared by several jobs is not
        # un-verified for the jobs already using it; it is flagged so it
        # cannot be used for any new job.
        if document.reuse_blocked_at is not None:
            raise BusinessRuleError(
                "The document is already flagged and cannot be reused."
            )
        document.reuse_blocked_at = _now()
        document.reuse_blocked_by = actor.id
        document.reuse_block_reason = reason
        _audit(
            db,
            "document_reuse_blocked",
            document.id,
            actor,
            reason=reason,
            job_ids=[j.id for j in jobs],
        )
        return
    ensure_no_closed_job(jobs, actor, "reject")
    document.status = DocumentStatus.REJECTED.value
    document.rejection_reason = reason
    document.verified_by = None
    document.verified_at = None
    _audit(db, "document_rejected", document.id, actor, reason=reason)


def reject(db: Session, document_id: int, reason: str, actor: User) -> DocumentDetail:
    document, jobs = lock_for_write(db, document_id, actor)
    apply_reject(db, document, jobs, reason, actor)
    db.commit()
    return _detail(db, document)


def archive(db: Session, document_id: int, reason: str, actor: User) -> DocumentDetail:
    """Admin only (enforced by the route)."""
    document, _ = lock_for_write(db, document_id, actor)
    if document.archived_at is not None:
        raise BusinessRuleError("The document is already archived.")
    document.archived_at = _now()
    document.archived_by = actor.id
    document.archive_reason = reason
    _audit(db, "document_archived", document.id, actor, reason=reason)
    db.commit()
    return _detail(db, document)


# ---------- checklists ----------


def _item_state(docs: list[Document]) -> ChecklistState:
    statuses = {d.status for d in docs}
    if DocumentStatus.VERIFIED.value in statuses:
        return ChecklistState.VERIFIED
    if statuses & set(_ACTIVE_STATES):
        return ChecklistState.PENDING
    if statuses:
        return ChecklistState.REJECTED
    return ChecklistState.MISSING


def build_checklist(db: Session, job_id: int) -> ChecklistResponse:
    items = repo.checklist_items(db, job_id)
    by_item: dict[int, list[Document]] = {i.id: [] for i in items}
    for item_id, doc in repo.documents_for_job_items(db, job_id):
        by_item.setdefault(item_id, []).append(doc)
    reads = [
        ChecklistItemRead(
            id=i.id,
            document_type_id=i.document_type_id,
            document_type_code=i.document_type_code,
            document_type_name=i.document_type_name,
            sensitivity=i.sensitivity,
            is_required=i.is_required,
            allow_multiple=i.allow_multiple,
            state=_item_state(by_item[i.id]),
            documents=[
                ChecklistDocument(
                    id=d.id,
                    filename=d.filename,
                    status=d.status,
                    current_version=d.current_version,
                )
                for d in by_item[i.id]
            ],
        )
        for i in items
    ]
    required = [r for r in reads if r.is_required]
    verified = [r for r in required if r.state == ChecklistState.VERIFIED]
    return ChecklistResponse(
        items=reads,
        required_total=len(required),
        required_verified=len(verified),
        complete=len(required) == len(verified),
    )


def get_checklist(db: Session, job_id: int, viewer: User) -> ChecklistResponse:
    if job_service.repo.get_job(db, job_id) is None:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    job_service.ensure_job_access(db, job_id, viewer)
    return build_checklist(db, job_id)


def link_document(
    db: Session, job_id: int, item_id: int, document_id: int, actor: User
) -> ChecklistResponse:
    """Reuse a verified customer document for a job's checklist item."""
    jobs = repo.lock_jobs(db, [job_id])
    if not jobs:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    job = jobs[0]
    job_service.ensure_job_access(db, job.id, actor)
    ensure_no_closed_job([job], actor, "add")
    item = repo.get_checklist_item(db, item_id)
    if item is None or item.job_id != job.id:
        raise NotFoundError("Checklist item not found.")
    document = repo.get_document(db, document_id)
    if (
        document is None
        or document.customer_id != job.customer_id
        or not can_access(db, document, actor)
    ):
        raise NotFoundError(DOCUMENT_NOT_FOUND)
    document = repo.lock_document(db, document_id)
    if document.archived_at is not None:
        raise BusinessRuleError("An archived document cannot be used.")
    if document.status != DocumentStatus.VERIFIED.value:
        raise BusinessRuleError("Only verified documents can be reused.")
    if document.reuse_blocked_at is not None:
        raise BusinessRuleError(
            "This document was flagged and cannot be used for new jobs: "
            f"{document.reuse_block_reason}"
        )
    if document.document_type_id != item.document_type_id:
        raise BusinessRuleError("The document type does not match the checklist item.")
    if repo.link_exists(db, job.id, document.id):
        raise ConflictError("This document is already used by the job.")
    if not item.allow_multiple and _occupied(db, item):
        raise ConflictError("This checklist item already has a document.")
    repo.add(
        db,
        JobDocument(
            job_id=job.id,
            document_id=document.id,
            checklist_item_id=item.id,
            linked_by=actor.id,
        ),
    )
    _audit(
        db,
        "document_linked",
        document.id,
        actor,
        job_id=job.id,
        checklist_item_id=item.id,
    )
    db.commit()
    return build_checklist(db, job.id)


def create_checklist(db: Session, job: Job, service_id: int) -> None:
    """Copy the service's active requirements onto a new job (caller commits)."""
    from app.repositories import catalogue_repository

    for req, doc_type in catalogue_repository.list_requirements(db, service_id, True):
        db.add(
            JobChecklistItem(
                job_id=job.id,
                document_type_id=doc_type.id,
                document_type_code=doc_type.code,
                document_type_name=doc_type.name,
                sensitivity=doc_type.sensitivity,
                ocr_fields=doc_type.ocr_fields,
                is_required=req.is_required,
                allow_multiple=req.allow_multiple,
                sort_order=req.sort_order,
            )
        )
    db.flush()
