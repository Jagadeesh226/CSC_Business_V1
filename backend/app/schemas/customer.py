import re
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    StringConstraints,
    model_validator,
)

from app.core.masking import digits

CustomerName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]

_PHONE_CHARS = re.compile(r"^\+?[0-9 ()-]+$")


def _check_phone(value: str) -> str:
    value = value.strip()
    if not _PHONE_CHARS.match(value) or not 10 <= len(digits(value)) <= 13:
        raise ValueError("Enter a phone number with 10 to 13 digits.")
    return value


# Matches customers.phone VARCHAR(30). Not unique: families share numbers.
Phone = Annotated[str, StringConstraints(max_length=30), AfterValidator(_check_phone)]


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: CustomerName
    phone: Phone | None = None
    email: EmailStr | None = None
    address: Notes | None = None
    notes: Notes | None = None


class CustomerUpdate(BaseModel):
    """Partial update. Name cannot be cleared; other fields can be set to null."""

    model_config = ConfigDict(extra="forbid")

    name: CustomerName | None = None
    phone: Phone | None = None
    email: EmailStr | None = None
    address: Notes | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def name_not_null(self) -> "CustomerUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class CustomerRead(BaseModel):
    """Full customer details (single-customer views)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str | None
    email: str | None
    address: str | None
    notes: str | None
    created_at: datetime


class CustomerSummary(BaseModel):
    """List and search results: the phone number is masked."""

    id: int
    name: str
    phone_masked: str | None
    created_at: datetime


class CustomerCreated(BaseModel):
    customer: CustomerRead
    # Existing customers with the same phone number or the same name, so staff
    # can spot an accidental duplicate. The new customer is still created.
    possible_duplicates: list[CustomerSummary]


class CustomerListResponse(BaseModel):
    items: list[CustomerSummary]
    total: int
    page: int
    page_size: int
    total_pages: int
