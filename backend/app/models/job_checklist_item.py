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


class JobChecklistItem(Base):
    """A document a job needs, copied from its service when the job is made.

    The copy (name, sensitivity, OCR fields, required flag) is frozen, so
    later catalogue edits never change an existing job's checklist.
    """

    __tablename__ = "job_checklist_items"
    __table_args__ = (
        UniqueConstraint("job_id", "document_type_id", name="uq_job_checklist_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"), nullable=False, index=True
    )
    document_type_id: Mapped[int] = mapped_column(
        ForeignKey("document_types.id"), nullable=False
    )
    document_type_code: Mapped[str] = mapped_column(String(50), nullable=False)
    document_type_name: Mapped[str] = mapped_column(String(150), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(20), nullable=False)
    ocr_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    allow_multiple: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
