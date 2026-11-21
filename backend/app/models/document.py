from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DOCUMENT_STATUSES = ("uploaded", "processing", "needs_review", "verified", "rejected")


class Document(Base):
    """A customer document. Its file content lives in document_versions; the
    current version's details are mirrored here for listing.

    Lifecycle: uploaded -> processing -> needs_review -> verified | rejected.
    Archived documents are kept but no longer count anywhere.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded', 'processing', 'needs_review', 'verified', "
            "'rejected')",
            name="ck_documents_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    # The job the document was first uploaded for, if any (links: job_documents).
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    document_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_types.id", name="fk_documents_document_type_id"),
        index=True,
    )
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="uploaded", nullable=False, index=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_documents_verified_by_users")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_documents_archived_by_users")
    )
    archive_reason: Mapped[str | None] = mapped_column(Text)
    # Set when a verified document shared by several jobs is rejected: the
    # jobs already using it keep it verified, but it cannot be used for new
    # jobs (owner decision).
    reuse_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reuse_blocked_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_documents_reuse_blocked_by_users")
    )
    reuse_block_reason: Mapped[str | None] = mapped_column(Text)
    # Legacy column from the first schema; OCR results get their own table.
    ocr_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
