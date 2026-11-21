from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskType(StrEnum):
    DOCUMENT_OCR = "document_ocr"
    VEHICLE_EXPIRY_LOOKUP = "vehicle_expiry_lookup"
    VEHICLE_MOBILE_LOOKUP = "vehicle_mobile_lookup"


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_type: TaskType
    idempotency_key: str
    payload: dict[str, Any]
    status: TaskStatus
    attempts: int
    max_attempts: int
    timeout_seconds: int
    run_after: datetime
    last_error: str | None
    result: dict[str, Any] | None
    created_by: int | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class TaskListResponse(BaseModel):
    items: list[TaskRead]
    total: int
    page: int
    page_size: int
    total_pages: int
