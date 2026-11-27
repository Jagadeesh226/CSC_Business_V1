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

EXPORT_KINDS = ("vehicles", "jobs")
EXPORT_STATUSES = ("queued", "completed", "failed", "expired")


class ReportExport(Base):
    """An Excel export built in the background (Phase 5.2).

    queued -> completed (file kept privately for a day) -> expired (file
    deleted); or queued -> failed. Only the person who asked may download
    it; it is built with what they may see when it runs.
    """

    __tablename__ = "report_exports"
    __table_args__ = (
        CheckConstraint("kind IN ('vehicles', 'jobs')", name="ck_report_exports_kind"),
        CheckConstraint(
            "status IN ('queued', 'completed', 'failed', 'expired')",
            name="ck_report_exports_status",
        ),
        CheckConstraint(
            "status <> 'completed' OR (storage_key IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_report_exports_file",
        ),
        Index("ix_report_exports_requester", "requested_by", "created_at"),
        # Maintenance finds files to delete.
        Index(
            "ix_report_exports_expiring",
            "expires_at",
            postgresql_where="status = 'completed'",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # The filters asked for (ids, dates, flags: never personal data).
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    filename: Mapped[str | None] = mapped_column(String(100))
    storage_key: Mapped[str | None] = mapped_column(String(64))
    file_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    # A fixed, safe sentence shown to the person who asked.
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    # The requester's role when it was built: if it changes, the file (built
    # with the old access) is no longer handed out.
    built_role: Mapped[str | None] = mapped_column(String(50))
    download_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
