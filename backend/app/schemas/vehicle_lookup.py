from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.schemas.job import AssigneeRef


class LookupStatus(StrEnum):
    QUEUED = "queued"  # waiting for the worker
    WAITING_FOR_CAPTCHA = "waiting_for_captcha"  # a person must type it
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"  # the CAPTCHA was not answered in time


class LookupResultRead(BaseModel):
    # Dates found, by expiry type ("YYYY-MM-DD").
    expiries: dict[str, str]
    challan_status: str | None
    challan_count: int | None
    # Expiry types whose date on the vehicle changed because of this lookup.
    applied: list[str]


class LookupRead(BaseModel):
    id: int
    vehicle_id: int
    status: LookupStatus
    provider: str | None
    requested_by: AssigneeRef | None
    created_at: datetime
    finished_at: datetime | None
    failure_reason: str | None
    result: LookupResultRead | None
    # While waiting: GET /vehicle-lookups/{id}/captcha shows the image.
    captcha_expires_at: datetime | None


class LookupListResponse(BaseModel):
    items: list[LookupRead]


Answer = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)
]


class CaptchaAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Answer
