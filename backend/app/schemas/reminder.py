from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.schemas.job import AssigneeRef, Reason

EXPIRY_LABELS = {
    "insurance": "Insurance",
    "pucc": "PUCC",
    "fitness": "Fitness",
    "tax": "Road tax",
    "permit": "Permit",
}


class ReminderStatus(StrEnum):
    PENDING = "pending"  # open, but no WhatsApp number yet
    READY_TO_SEND = "ready_to_send"  # open, with a WhatsApp link
    OPEN = "open"  # filter only: pending or ready_to_send
    DONE = "done"  # the employee contacted the customer
    DISMISSED = "dismissed"  # closed without contact, with a reason
    SUPERSEDED = "superseded"  # the date changed; a new reminder replaced it


class ExpiryBucket(StrEnum):
    EXPIRED = "expired"
    DAYS_0_7 = "0_7"
    DAYS_8_15 = "8_15"
    DAYS_16_30 = "16_30"
    OVER_30 = "over_30"


class ReminderVehicleRef(BaseModel):
    id: int
    registration_number: str
    customer_id: int
    customer_name: str
    assigned_to: AssigneeRef | None


class ReminderRead(BaseModel):
    id: int
    vehicle: ReminderVehicleRef
    expiry_type: str
    valid_until: date
    days_left: int
    status: ReminderStatus
    # Opens WhatsApp with the message filled in (every open expiry of the
    # vehicle); nothing is sent automatically. Null when no valid Indian
    # mobile number is known, or once closed.
    whatsapp_link: str | None
    created_at: datetime
    closed_at: datetime | None
    closed_by: AssigneeRef | None
    close_reason: str | None


class ReminderListResponse(BaseModel):
    items: list[ReminderRead]
    total: int
    page: int
    page_size: int
    total_pages: int


Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class DoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Note | None = None


class DismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ExpiryListItem(BaseModel):
    vehicle_id: int
    registration_number: str
    customer_id: int
    customer_name: str
    expiry_type: str
    valid_until: date
    days_left: int
    bucket: ExpiryBucket
    whatsapp_link: str | None


class ExpiryListResponse(BaseModel):
    as_of: date  # the centre's date (Asia/Kolkata)
    # Count per bucket, for the filters chosen (type, access).
    counts: dict[ExpiryBucket, int]
    items: list[ExpiryListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    reminder_id: int | None
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None


class NotificationListResponse(BaseModel):
    items: list[NotificationRead]
    unread: int
    total: int
    page: int
    page_size: int
    total_pages: int
