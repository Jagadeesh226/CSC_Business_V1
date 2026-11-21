from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OcrResult(Base):
    """OCR of one document version, and the employee's review of it.

    Extracted values and raw text can contain Aadhaar/PAN numbers, so they
    are stored encrypted (``extraction_sealed``, ``review_sealed``). Only the
    field keys with their confidence stay readable, for highlighting.
    """

    __tablename__ = "ocr_results"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "document_version", name="uq_ocr_results_document_version"
        ),
        CheckConstraint(
            "status IN ('completed', 'failed')", name="ck_ocr_results_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("background_tasks.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(100), nullable=False)
    # The fields asked for, frozen with the result ([{key, label, sensitive}]),
    # so later document type edits never change how a stored result is shown
    # or masked.
    field_specs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # {field key: confidence 0..1, or null when the field was not found}
    confidences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Encrypted JSON: {"fields": {key: value}, "raw_text": "..."}
    extraction_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    # Encrypted JSON of the values the reviewer confirmed: {key: value}
    review_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_ocr_results_reviewed_by_users")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
