from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint(
            "fee_type IN ('fixed', 'variable')",
            name="ck_services_fee_type",
        ),
        CheckConstraint(
            "default_service_fee IS NULL OR default_service_fee >= 0",
            name="ck_services_default_fee_nonnegative",
        ),
        CheckConstraint(
            "fee_type = 'variable' OR default_service_fee IS NOT NULL",
            name="ck_services_fixed_fee_required",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("service_categories.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)

    fee_type: Mapped[str] = mapped_column(String(20), default="fixed", nullable=False)
    default_service_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    requires_vehicle: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
