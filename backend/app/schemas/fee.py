from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.job import AssigneeRef


class FeeType(StrEnum):
    """Matches the ck_job_fees_fee_type database constraint."""

    SERVICE = "service"
    THIRD_PARTY = "third_party"
    DEPARTMENT = "department"


# Matches job_fees.amount NUMERIC(12, 2); fees are positive amounts in rupees.
Amount = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
FeeDescription = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]


class FeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fee_type: FeeType
    description: FeeDescription
    amount: Amount


class FeeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fee_type: FeeType | None = None
    description: FeeDescription | None = None
    amount: Amount | None = None

    @model_validator(mode="after")
    def no_nulls(self) -> "FeeUpdate":
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class FeeRead(BaseModel):
    id: int
    fee_type: FeeType
    description: str
    amount: Decimal
    created_by: AssigneeRef | None
    created_at: datetime
    deleted_by: AssigneeRef | None
    deleted_at: datetime | None


class FeeTotals(BaseModel):
    service: Decimal
    third_party: Decimal
    department: Decimal
    total: Decimal


class FeeListResponse(BaseModel):
    items: list[FeeRead]
    # Totals of fees that have not been removed.
    totals: FeeTotals
