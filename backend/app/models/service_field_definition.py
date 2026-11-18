from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ServiceFieldDefinition(Base):
    __tablename__ = "service_field_definitions"
    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "field_key",
            name="uq_service_field_definition_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id"), nullable=False, index=True
    )

    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    options: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB)
    validation_rules: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
