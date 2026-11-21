from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.catalogue import DocumentTypeRef
from app.schemas.document import DocumentStatus
from app.schemas.job import CustomerRef


class WorkQueueSummary(BaseModel):
    """What the signed-in user has to do next. Employees count only open
    jobs they are assigned to; admins count every open job."""

    # The centre's date (Asia/Kolkata) used for "overdue" and "due today".
    as_of: date
    # Documents waiting for a person (GET /review-queue).
    documents_to_review: int
    # GET /jobs?readiness=... for each of these.
    jobs_missing_documents: int
    jobs_processing: int
    jobs_ready_to_submit: int
    jobs_ready_to_resume: int
    # GET /jobs?due=overdue and ?due=today.
    jobs_overdue: int
    jobs_due_today: int
    # Admins only (GET /jobs?unassigned=true); null for employees.
    unassigned_jobs: int | None


class ReviewKind(StrEnum):
    OCR_CHECK = "ocr_check"  # OCR read it: confirm the fields
    OCR_FAILED = "ocr_failed"  # OCR could not read it: read it by hand
    MANUAL = "manual"  # not read by OCR: check and verify it by hand


class ReviewJobRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_number: str
    title: str
    status: str


class ReviewQueueItem(BaseModel):
    document_id: int
    filename: str
    status: DocumentStatus
    current_version: int
    document_type: DocumentTypeRef | None
    customer: CustomerRef
    # Open jobs using this document that the viewer can see.
    jobs: list[ReviewJobRef]
    review_kind: ReviewKind
    # OCR fields missing or read with low confidence (ocr_check only).
    flagged_fields: int | None
    # When the current version was uploaded: the queue is oldest first.
    waiting_since: datetime


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItem]
    total: int
    page: int
    page_size: int
    total_pages: int
