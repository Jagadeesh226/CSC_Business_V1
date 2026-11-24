from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    StringConstraints,
    model_validator,
)

from app.core.vehicle_numbers import normalize
from app.schemas.customer import Phone
from app.schemas.job import AssigneeRef, ResourceId


class VehicleCategory(StrEnum):
    TWO_WHEELER = "two_wheeler"
    PRIVATE_FOUR_WHEELER = "private_four_wheeler"
    COMMERCIAL = "commercial"
    OTHER = "other"


class ExpiryType(StrEnum):
    INSURANCE = "insurance"
    PUCC = "pucc"
    FITNESS = "fitness"
    TAX = "tax"
    PERMIT = "permit"


class ValueSource(StrEnum):
    MANUAL = "manual"  # typed by staff
    DOCUMENT = "document"  # read from a document (OCR, confirmed)
    LOOKUP = "lookup"  # fetched from an authorised lookup


class ChallanStatus(StrEnum):
    UNKNOWN = "unknown"  # never checked
    NONE = "none"  # checked: nothing pending
    PENDING = "pending"  # checked: challans pending


def _registration(value: str) -> str:
    key = normalize(value)
    if not 4 <= len(key) <= 15:
        raise ValueError("A registration number has 4 to 15 letters and digits.")
    if not (any(c.isalpha() for c in key) and any(c.isdigit() for c in key)):
        raise ValueError("A registration number has both letters and digits.")
    # Shown as typed, with spacing tidied (fits the 20-character column).
    shown = " ".join(value.upper().split())
    return shown if len(shown) <= 20 else key


def _identifier(value: str) -> str:
    key = normalize(value)
    # At least 6, so the masked form ("****" + last 4) never shows it whole.
    if not 6 <= len(key) <= 25:
        raise ValueError("Use 6 to 25 letters and digits.")
    return key


Registration = Annotated[
    str, StringConstraints(max_length=30), AfterValidator(_registration)
]
Identifier = Annotated[
    str, StringConstraints(max_length=40), AfterValidator(_identifier)
]
OwnerName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class VehicleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: ResourceId
    registration_number: Registration
    category: VehicleCategory
    owner_name: OwnerName | None = None
    chassis_number: Identifier | None = None
    engine_number: Identifier | None = None
    # Reminder number, when it is not the customer's own phone.
    contact_phone: Phone | None = None
    # Admins only; an employee's new vehicle is assigned to them.
    assigned_to: ResourceId | None = None


class VehicleUpdate(BaseModel):
    """Partial update. Owner name, chassis, engine and contact phone can be
    cleared with null. Admins only: customer_id (ownership transfer),
    assigned_to and is_active."""

    model_config = ConfigDict(extra="forbid")

    registration_number: Registration | None = None
    category: VehicleCategory | None = None
    owner_name: OwnerName | None = None
    chassis_number: Identifier | None = None
    engine_number: Identifier | None = None
    contact_phone: Phone | None = None
    customer_id: ResourceId | None = None
    assigned_to: ResourceId | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def required_not_null(self) -> "VehicleUpdate":
        for field in ("registration_number", "category", "customer_id", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ExpirySet(BaseModel):
    """Set (or clear, with null) the current date of one expiry type."""

    model_config = ConfigDict(extra="forbid")

    valid_until: date | None
    # A customer document this date was read from (e.g. the policy).
    document_id: ResourceId | None = None


class ExpiryRead(BaseModel):
    expiry_type: ExpiryType
    valid_until: date | None
    # Days from today (centre's calendar); negative once expired.
    days_left: int | None
    source: ValueSource
    source_document_id: int | None
    recorded_by: AssigneeRef | None
    recorded_at: datetime


class VehicleCustomerRef(BaseModel):
    id: int
    name: str


class VehicleRead(BaseModel):
    id: int
    customer: VehicleCustomerRef
    registration_number: str
    category: VehicleCategory
    owner_name: str | None
    # Masked ("****1234") unless revealed by someone allowed to.
    chassis_number: str | None
    engine_number: str | None
    identifiers_masked: bool
    assigned_to: AssigneeRef | None
    contact_phone: str | None
    contact_phone_source: ValueSource | None
    challan_status: ChallanStatus
    challan_checked_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    # Current date per type that has one recorded.
    expiries: list[ExpiryRead]


class VehicleListResponse(BaseModel):
    items: list[VehicleRead]
    total: int
    page: int
    page_size: int
    total_pages: int


class ExpiryHistoryResponse(BaseModel):
    expiry_type: ExpiryType
    items: list[ExpiryRead]  # newest first
