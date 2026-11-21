from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.schemas.fee import Amount
from app.schemas.job import AssigneeRef, Reason
from app.schemas.money import PaymentState, PaymentSummary  # noqa: F401 (re-exported)


class PaymentMethod(StrEnum):
    """Matches the ck_payments_method database constraint."""

    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    CHEQUE = "cheque"
    OTHER = "other"


class PaymentStatus(StrEnum):
    RECEIVED = "received"
    VOIDED = "voided"  # an admin cancelled it; its receipt is cancelled


Reference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount
    method: PaymentMethod
    # Transaction / UTR / cheque number; required for every method but cash.
    reference_number: Reference | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def reference_unless_cash(self) -> "PaymentCreate":
        if self.method != PaymentMethod.CASH and not self.reference_number:
            raise ValueError("A transaction reference is required unless paid in cash.")
        if not self.notes:
            self.notes = None
        return self


class VoidRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ReceiptRead(BaseModel):
    id: int
    receipt_number: str
    issued_at: datetime
    issued_by: AssigneeRef | None
    # Cancelled when its payment was voided; the number is never reused.
    cancelled: bool
    cancelled_at: datetime | None
    cancel_reason: str | None


class PaymentRead(BaseModel):
    id: int
    job_id: int
    amount: Decimal
    method: PaymentMethod
    reference_number: str | None
    notes: str | None
    status: PaymentStatus
    received_at: datetime
    recorded_by: AssigneeRef | None
    voided_at: datetime | None
    voided_by: AssigneeRef | None
    void_reason: str | None
    receipt: ReceiptRead | None


class PaymentWithSummary(PaymentRead):
    # The job's payment position after this change.
    summary: PaymentSummary


class PaymentRecorded(PaymentWithSummary):
    # True when this answers a repeat of an earlier request (same
    # Idempotency-Key): nothing new was recorded.
    replayed: bool


class PaymentListResponse(BaseModel):
    items: list[PaymentRead]
    summary: PaymentSummary
