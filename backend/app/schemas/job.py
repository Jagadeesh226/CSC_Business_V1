from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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
    assignees: list[AssigneeRef] = []


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
