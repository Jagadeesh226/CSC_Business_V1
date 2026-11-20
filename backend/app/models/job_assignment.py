from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobAssignment(Base):
    __tablename__ = "job_assignments"
    __table_args__ = (
        Index(
            "uq_job_assignments_active",
            "job_id",
            "user_id",
            unique=True,
            postgresql_where=mapped_column("unassigned_at").is_(None),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    assigned_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unassigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Who removed the person; set together with unassigned_at.
    unassigned_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_job_assignments_unassigned_by_users")
    )
