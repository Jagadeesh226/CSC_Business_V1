from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'waiting_for_documents', "
            "'submitted', 'completed', 'cancelled')",
            name="ck_jobs_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_number: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", name="fk_jobs_service_id_services"),
        nullable=True,
        index=True,
    )
    # The service name when the job was created: renaming a service later
    # does not change existing jobs (owner decision).
    service_name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(50), default="pending", nullable=False, index=True
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
