from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ServiceDocumentRequirement(Base):
    __tablename__ = "service_document_requirements"
    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "document_code",
            name="uq_service_document_requirement_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id"), nullable=False, index=True
    )
    document_code: Mapped[str] = mapped_column(String(100), nullable=False)
    document_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_multiple: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
