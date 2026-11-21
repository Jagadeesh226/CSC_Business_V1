from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.money import PaymentSummary

# Matches jobs.title VARCHAR(200); surrounding whitespace is stripped first.
JobTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


# Integer IDs map to PostgreSQL int4 columns; larger values would fail in SQL.
MAX_DB_INT = 2**31 - 1
ResourceId = Annotated[int, Field(gt=0, le=MAX_DB_INT)]

# Upper bound on free-text description, to keep rows and list pages bounded.
JobDescription = Annotated[str, StringConstraints(max_length=5000)]


class JobStatus(StrEnum):
    """Job statuses approved in docs/contracts/decisions.md section 7."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_DOCUMENTS = "waiting_for_documents"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# Jobs in these statuses are closed: no new assignments, never reopened.
FINAL_STATUSES = (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value)

# Normal moves (docs/contracts/decisions.md section 7). Cancelling is a
# separate admin-only action, allowed from any status that is not final.
STATUS_TRANSITIONS: dict[str, tuple[JobStatus, ...]] = {
    JobStatus.PENDING.value: (JobStatus.IN_PROGRESS,),
    JobStatus.IN_PROGRESS.value: (
        JobStatus.WAITING_FOR_DOCUMENTS,
        JobStatus.SUBMITTED,
    ),
    JobStatus.WAITING_FOR_DOCUMENTS.value: (JobStatus.IN_PROGRESS,),
    JobStatus.SUBMITTED.value: (JobStatus.COMPLETED,),
    JobStatus.COMPLETED.value: (),
    JobStatus.CANCELLED.value: (),
}

Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]


class JobCreate(BaseModel):
    # Unknown fields (job_number, status, created_by, ...) are rejected with 422.
    model_config = ConfigDict(extra="forbid")

    customer_id: ResourceId
    service_id: ResourceId
    title: JobTitle
    description: JobDescription | None = None
    due_date: date | None = None


class JobUpdate(BaseModel):
    """Partial update. Only title, description and due_date may change."""

    model_config = ConfigDict(extra="forbid")

    title: JobTitle | None = None
    description: JobDescription | None = None
    due_date: date | None = None

    @model_validator(mode="after")
    def title_not_null(self) -> "JobUpdate":
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title cannot be null")
        return self


# Statuses in which a job is still gathering documents (before submission).
DOCUMENT_STAGES = (
    JobStatus.PENDING.value,
    JobStatus.IN_PROGRESS.value,
    JobStatus.WAITING_FOR_DOCUMENTS.value,
)


class Readiness(StrEnum):
    """Job list filter by document progress. Only jobs still gathering
    documents (pending, in progress, waiting for documents) match."""

    MISSING_DOCUMENTS = "missing_documents"  # a required one is missing or rejected
    NEEDS_REVIEW = "needs_review"  # a document waits for a person to check it
    PROCESSING = "processing"  # OCR is still reading a document
    READY_TO_SUBMIT = "ready_to_submit"  # in progress, every required one verified
    READY_TO_RESUME = "ready_to_resume"  # waiting for documents, now all verified


class DueFilter(StrEnum):
    """Open jobs by due date, in the centre's calendar (Asia/Kolkata)."""

    OVERDUE = "overdue"
    TODAY = "today"


class JobSort(StrEnum):
    NEWEST = "newest"  # newest first (default)
    URGENCY = "urgency"  # earliest due date first, then oldest; no date last


class DocumentProgress(BaseModel):
    """Required checklist documents of the job, by state."""

    required_total: int
    verified: int
    to_review: int
    processing: int
    rejected: int
    missing: int
    # Every required document is verified (same as the checklist's
    # ``complete``): the job may be submitted.
    complete: bool


class CustomerRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str | None


class ServiceRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str


class AssigneeRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_number: str
    title: str
    description: str | None
    status: str
    due_date: date | None
    created_by: int | None
    created_at: datetime
    customer: CustomerRef
    # Nullable only for legacy rows; jobs created through the API always have one.
    service: ServiceRef | None
    # People currently assigned to the job.
    assignees: list[AssigneeRef]
    # Statuses the job can move to next with POST /jobs/{id}/status.
    # Cancelling (admins only) is separate and possible while not final.
    allowed_next_statuses: list[JobStatus]
    # Always returned; optional only so older clients' schemas stay valid.
    document_progress: DocumentProgress | None = None
    # Fees, received payments and balance; always returned.
    payment_summary: PaymentSummary | None = None


class JobListResponse(BaseModel):
    items: list[JobRead]
    total: int
    page: int
    page_size: int
    total_pages: int


class AssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: ResourceId


class AssignmentRead(BaseModel):
    id: int
    user: AssigneeRef
    assigned_by: AssigneeRef
    assigned_at: datetime
    unassigned_by: AssigneeRef | None
    unassigned_at: datetime | None


class StatusChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: JobStatus
    reason: Reason | None = None
    # Admins only: submit although required documents are not all verified.
    # Needs a reason, and is audited.
    override_document_check: bool = False


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class StatusHistoryRead(BaseModel):
    id: int
    old_status: JobStatus | None
    new_status: JobStatus
    changed_by: AssigneeRef | None
    reason: str | None
    changed_at: datetime


from app.schemas.payment import PaymentSummary

JobRead.model_rebuild()
