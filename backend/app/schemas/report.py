from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.schemas.job import AssigneeRef
from app.schemas.payment import PaymentMethod

COLLECTIONS_NOTE = (
    "Money received from customers in this period (voided payments "
    "excluded). This is collections, not profit: it includes department "
    "and third-party fees passed on, and no expenses."
)


class CollectionsMethodTotal(BaseModel):
    method: PaymentMethod
    count: int
    total: Decimal


class CollectionsDay(BaseModel):
    date: date
    count: int
    total: Decimal
    by_method: list[CollectionsMethodTotal]


class CollectionsReceiverTotal(BaseModel):
    # Who took the money (for counting the cash drawer).
    received_by: AssigneeRef | None
    count: int
    total: Decimal


class CollectionsVoided(BaseModel):
    count: int
    total: Decimal


class CollectionsReport(BaseModel):
    label: Literal["Collections"] = "Collections"
    note: str = COLLECTIONS_NOTE
    # Inclusive, in the centre's calendar (Asia/Kolkata).
    date_from: date
    date_to: date
    count: int
    total: Decimal
    by_method: list[CollectionsMethodTotal]
    by_day: list[CollectionsDay]
    by_receiver: list[CollectionsReceiverTotal]
    # Payments received in the period and later voided (not in the total).
    voided: CollectionsVoided
