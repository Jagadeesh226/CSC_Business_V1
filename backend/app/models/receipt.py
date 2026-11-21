from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Receipt(Base):
    """The receipt issued for one payment, with a unique number.

    ``snapshot`` freezes what the receipt shows (customer, job, service,
    fees, amounts) at issue time, so later edits never change a receipt that
    was handed over. A voided payment's receipt is marked cancelled; its
    number is never reused.
    """

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id"), unique=True, nullable=False
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    issued_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    print_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_receipts_cancelled_by_users")
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text)
