from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from app.schemas.job import AssigneeRef
from app.schemas.payment import PaymentMethod


class ReceiptStatus(StrEnum):
    ISSUED = "issued"
    CANCELLED = "cancelled"  # its payment was voided; the number stays


class SnapshotCustomer(BaseModel):
    id: int
    name: str
    phone: str | None  # masked: only the last four digits


class SnapshotJob(BaseModel):
    id: int
    job_number: str
    title: str
    service_name: str | None


# Snapshot values are plain strings, not enums: a receipt issued before a
# fee type or method is renamed must still load and print.


class SnapshotFee(BaseModel):
    fee_type: str
    description: str
    amount: Decimal


class ReceiptSnapshot(BaseModel):
    """What the receipt shows, frozen when it was issued."""

    customer: SnapshotCustomer
    job: SnapshotJob
    fees: list[SnapshotFee]
    fees_total: Decimal
    paid_before: Decimal
    amount: Decimal
    balance_after: Decimal
    method: str
    reference_number: str | None
    received_by: str


class ReceiptDetail(BaseModel):
    id: int
    receipt_number: str
    status: ReceiptStatus
    payment_id: int
    job_id: int
    customer_id: int
    issued_at: datetime
    issued_by: AssigneeRef | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    # How many times the PDF was printed; every print after the first is
    # marked as a duplicate copy.
    print_count: int
    snapshot: ReceiptSnapshot
    amount_in_words: str


class ReceiptListItem(BaseModel):
    id: int
    receipt_number: str
    status: ReceiptStatus
    issued_at: datetime
    job_id: int
    job_number: str
    customer_id: int
    customer_name: str  # as printed on the receipt
    amount: Decimal
    method: PaymentMethod


class ReceiptListResponse(BaseModel):
    items: list[ReceiptListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReceiptFilters(BaseModel):
    """Search for GET /receipts (all optional, combined with AND)."""

    search: str | None = None
    customer_id: int | None = None
    job_id: int | None = None
    method: PaymentMethod | None = None
    status: ReceiptStatus | None = None
    # Issue dates in the centre's calendar (Asia/Kolkata), inclusive.
    date_from: date | None = None
    date_to: date | None = None
