from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PAYMENT_METHODS = ("cash", "upi", "card", "bank_transfer", "cheque", "other")


class Payment(Base):
    """Money received for a job. Never deleted: a mistake is voided by an
    admin, with a reason, and its receipt is marked cancelled.

    ``idempotency_key`` (unique) makes recording safe to retry: the same key
    returns the first payment; ``request_hash`` detects a key reused for a
    different payment.
    """

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        Index("ix_payments_received_at", "received_at"),
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint("status IN ('received', 'voided')", name="ck_payments_status"),
        CheckConstraint(
            "payment_method IN ('cash', 'upi', 'card', 'bank_transfer', "
            "'cheque', 'other')",
            name="ck_payments_method",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL) = (request_hash IS NULL)",
            name="ck_payments_idempotency_pair",
        ),
        CheckConstraint(
            "(status = 'voided') = (voided_at IS NOT NULL)",
            name="ck_payments_void_fields",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(50), nullable=False)
    reference_number: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="received", nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_payments_voided_by_users")
    )
    void_reason: Mapped[str | None] = mapped_column(Text)
