from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TASK_TYPES = ("document_ocr", "vehicle_expiry_lookup", "vehicle_mobile_lookup")
TASK_STATUSES = ("queued", "running", "succeeded", "failed")


class BackgroundTask(Base):
    """A unit of background work. PostgreSQL is the source of truth for its
    status; Redis Streams only wakes workers (decisions.md section 10).

    queued -> running -> succeeded
                      -> queued again (retry with backoff) ... -> failed
    """

    __tablename__ = "background_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('document_ocr', 'vehicle_expiry_lookup', "
            "'vehicle_mobile_lookup')",
            name="ck_background_tasks_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_background_tasks_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND timeout_seconds >= 1",
            name="ck_background_tasks_limits",
        ),
        # Workers look for due queued tasks and for expired leases.
        Index("ix_background_tasks_due", "status", "run_after"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Business key that makes enqueueing idempotent, e.g. "document_ocr:12:v2".
    idempotency_key: Mapped[str] = mapped_column(
        String(200), unique=True, nullable=False
    )
    # Only ids and settings: never document contents or personal data.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A short, safe summary of the last failure (no stack traces or data).
    last_error: Mapped[str | None] = mapped_column(String(500))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
