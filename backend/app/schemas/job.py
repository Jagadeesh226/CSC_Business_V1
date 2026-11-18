from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Matches jobs.title VARCHAR(200); surrounding whitespace is stripped first.
JobTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class JobStatus(StrEnum):
    """Job statuses approved in docs/contracts/decisions.md section 7."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_DOCUMENTS = "waiting_for_documents"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class JobCreate(BaseModel):
    # Unknown fields (job_number, status, created_by, ...) are rejected with 422.
    model_config = ConfigDict(extra="forbid")

    customer_id: int = Field(gt=0)
    service_id: int = Field(gt=0)
    title: JobTitle
    description: str | None = None
    due_date: date | None = None


class JobUpdate(BaseModel):
    """Partial update. Only title, description and due_date may change."""

    model_config = ConfigDict(extra="forbid")

    title: JobTitle | None = None
    description: str | None = None
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


class JobListResponse(BaseModel):
    items: list[JobRead]
    total: int
    page: int
    page_size: int
    total_pages: int
