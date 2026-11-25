from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentType(Base):
    """A kind of document (Aadhaar, PAN, RC, insurance...) shared by services.

    ``ocr_fields`` lists the fields OCR should extract for this type, as
    ``[{"key": "aadhaar_number", "label": "Aadhaar number", "sensitive": true}]``.
    """

    __tablename__ = "document_types"
    __table_args__ = (
        CheckConstraint(
            "sensitivity IN ('normal', 'sensitive')",
            name="ck_document_types_sensitivity",
        ),
        CheckConstraint(
            "vehicle_role IS NULL OR vehicle_role IN ('registration_certificate')",
            name="ck_document_types_vehicle_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sensitivity: Mapped[str] = mapped_column(
        String(20), default="normal", server_default="normal", nullable=False
    )
    ocr_fields: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    # 'registration_certificate': once OCR reads a document of this type, the
    # customer's vehicle is created or filled automatically (Phase 4.1).
    vehicle_role: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
