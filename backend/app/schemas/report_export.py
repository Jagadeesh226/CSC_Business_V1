"""Background Excel exports (Phase 5.2)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.job import MAX_DB_INT, DueFilter, JobStatus, PaymentFilter, Readiness

Id = Annotated[int, Field(gt=0, le=MAX_DB_INT)]


class ExportKind(StrEnum):
    VEHICLES = "vehicles"
    JOBS = "jobs"


class ExportStatus(StrEnum):
    QUEUED = "queued"
    COMPLETED = "completed"  # the file can be downloaded until expires_at
    FAILED = "failed"
    EXPIRED = "expired"  # the file was deleted


class VehiclesExportRequest(BaseModel):
    """The same filters as GET /reports/vehicles.xlsx."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["vehicles"]
    due_within_days: Annotated[int | None, Field(ge=0, le=366)] = None
    include_inactive: bool = False
    customer_id: Id | None = None
    # Its links carry the full mobile number.
    include_whatsapp: bool = True


class JobsExportRequest(BaseModel):
    """The jobs list's filters (no free-text search: the request is kept
    with the export and audited)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["jobs"]
    status: JobStatus | None = None
    service_id: Id | None = None
    customer_id: Id | None = None
    assigned_to: Id | None = None
    readiness: Readiness | None = None
    due: DueFilter | None = None
    payment: PaymentFilter | None = None
    unassigned: bool = False


ExportRequest = Annotated[
    VehiclesExportRequest | JobsExportRequest, Field(discriminator="kind")
]


class ExportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: ExportKind
    status: ExportStatus
    filters: dict[str, Any]
    row_count: int | None
    filename: str | None
    file_size: int | None
    failure_reason: str | None
    download_count: int
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    last_downloaded_at: datetime | None


class ExportListResponse(BaseModel):
    items: list[ExportRead]
    total: int
    page: int
    page_size: int
    total_pages: int
