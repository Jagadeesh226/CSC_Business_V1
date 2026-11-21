from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobDocument(Base):
    """A document used by a job, optionally for one checklist item.

    The same verified customer document can be linked to many jobs, so it
    never has to be uploaded twice.
    """

    __tablename__ = "job_documents"
    __table_args__ = (
        UniqueConstraint("job_id", "document_id", name="uq_job_documents_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"), nullable=False, index=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), nullable=False, index=True
    )
    checklist_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_checklist_items.id"), index=True
    )
    linked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
