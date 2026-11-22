from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.job import AssigneeRef
from app.schemas.payment import PaymentMethod

COLLECTIONS_NOTE = (
    "Money received from customers in this period, as known now: a payment "
    "voided later is left out of the day it was received (see 'voided'), and "
    "voids made during the period are listed in 'voids_in_period' so each "
    "day's cash count can be explained. 'Received by' is the person who "
    "recorded the payment. This is collections, not profit: it includes "
    "department and third-party fees passed on, and no expenses."
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
    # Always "Collections": this report is never profit.
    label: Literal["Collections"] = Field(...)
    note: str = Field(...)
    # Inclusive, in the centre's calendar (Asia/Kolkata).
    date_from: date
    date_to: date
    count: int
    total: Decimal
    by_method: list[CollectionsMethodTotal]
    by_day: list[CollectionsDay]
    by_receiver: list[CollectionsReceiverTotal]
    # Payments received in the period and voided since (not in the total).
    voided: CollectionsVoided
    # Voids made during the period, whenever the payment was received:
    # money handed back or a mistake corrected on these days.
    voids_in_period: CollectionsVoided
