from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.catalogue import DocumentTypeRef, Sensitivity
from app.schemas.job import AssigneeRef

Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"  # OCR running (Phase 2.3)
    NEEDS_REVIEW = "needs_review"  # OCR finished, waiting for a person
    VERIFIED = "verified"
    REJECTED = "rejected"


class DocumentVersionRead(BaseModel):
    version: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    page_count: int | None
    change_reason: str | None
    uploaded_by: AssigneeRef | None
    uploaded_at: datetime


class DocumentRead(BaseModel):
    id: int
    customer_id: int
    document_type: DocumentTypeRef | None
    filename: str
    content_type: str | None
    size_bytes: int
    page_count: int | None
    current_version: int
    status: DocumentStatus
    rejection_reason: str | None
    uploaded_by: AssigneeRef | None
    created_at: datetime
    verified_by: AssigneeRef | None
    verified_at: datetime | None
    archived_at: datetime | None
    archive_reason: str | None
    # Jobs this document is used by.
    job_ids: list[int]


class DocumentDetail(DocumentRead):
    versions: list[DocumentVersionRead]


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class LinkDocumentRequest(BaseModel):
    """Reuse an already verified customer document for a checklist item."""

    model_config = ConfigDict(extra="forbid")

    document_id: Annotated[int, Field(gt=0, le=2**31 - 1)]


class ChecklistState(StrEnum):
    MISSING = "missing"  # nothing uploaded yet
    PENDING = "pending"  # uploaded, not yet verified
    REJECTED = "rejected"  # every document for it was rejected
    VERIFIED = "verified"


class ChecklistDocument(BaseModel):
    id: int
    filename: str
    status: DocumentStatus
    current_version: int


class ChecklistItemRead(BaseModel):
    id: int
    document_type_id: int
    document_type_code: str
    document_type_name: str
    sensitivity: Sensitivity
    is_required: bool
    allow_multiple: bool
    state: ChecklistState
    documents: list[ChecklistDocument]


class ChecklistResponse(BaseModel):
    items: list[ChecklistItemRead]
    required_total: int
    required_verified: int
    # True when every required item is verified: the job may be submitted.
    complete: bool
