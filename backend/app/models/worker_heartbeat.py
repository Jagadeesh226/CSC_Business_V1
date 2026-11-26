from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkerHeartbeat(Base):
    """One row per worker process (Phase 5.0): it reports every
    ``WORKER_HEARTBEAT_SECONDS`` so an admin can see that background work
    is running. Times come from the database clock."""

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint("tasks_done >= 0", name="ck_worker_heartbeats_tasks_done"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # "<host>:<pid>" unless set otherwise; the worker's id in task leases.
    worker_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # When it last finished a task or a loop: a worker that reports but
    # makes no progress is stuck.
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tasks_done: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
