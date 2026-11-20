from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ServiceDocumentRequirement(Base):
    """Which document types a service needs (required) or accepts (optional)."""

    __tablename__ = "service_document_requirements"
    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "document_type_id",
            name="uq_service_document_requirement_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id"), nullable=False, index=True
    )
    document_type_id: Mapped[int] = mapped_column(
        ForeignKey(
            "document_types.id",
            name="fk_service_document_requirements_document_type_id",
        ),
        nullable=False,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_multiple: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
