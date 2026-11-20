from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobFee(Base):
    __tablename__ = "job_fees"
    __table_args__ = (
        CheckConstraint(
            "fee_type IN ('service', 'third_party', 'department')",
            name="ck_job_fees_fee_type",
        ),
        CheckConstraint(
            "amount >= 0",
            name="ck_job_fees_amount_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"), nullable=False, index=True
    )
    fee_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Fees are never erased: removal marks the row, so totals already used by
    # payments and receipts can always be traced.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_job_fees_deleted_by_users")
    )
