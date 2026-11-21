"""A job's payment position, shared by the job and payment schemas."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class PaymentState(StrEnum):
    """How much of the job's fees has been paid (received payments only)."""

    NO_FEES = "no_fees"
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"
    # Paid more than the fees: only when an admin lowered fees afterwards.
    OVERPAID = "overpaid"


class PaymentSummary(BaseModel):
    fees_total: Decimal
    paid: Decimal
    # fees_total - paid; negative when overpaid.
    balance: Decimal
    state: PaymentState
