"""Worker heartbeats (Phase 5.0). Every time is the database's clock, so
workers on machines with drifting clocks still compare fairly."""

from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat

# Rows of workers gone this long are removed by maintenance.
KEEP_GONE_FOR = "7 days"


def register(db: Session, worker_name: str) -> None:
    """A worker starting (or restarting under the same name)."""
    now = func.now()
    db.execute(
        insert(WorkerHeartbeat)
        .values(worker_name=worker_name)
        .on_conflict_do_update(
            index_elements=[WorkerHeartbeat.worker_name],
            set_={
                "started_at": now,
                "last_seen_at": now,
                "last_active_at": None,
                "stopped_at": None,
                "tasks_done": 0,
            },
        )
    )


def beat(
    db: Session, worker_name: str, *, idle_for: float | None, tasks_done: int
) -> None:
    """``idle_for``: seconds since the worker last finished a task or a
    loop."""
    values = {"last_seen_at": func.now(), "tasks_done": tasks_done}
    if idle_for is not None:
        values["last_active_at"] = func.now() - timedelta(seconds=max(idle_for, 0))
    found = db.execute(
        update(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_name == worker_name)
        .values(**values)
    ).rowcount
    if not found:  # removed while the worker was away: report again
        register(db, worker_name)
        db.execute(
            update(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_name == worker_name)
            .values(**values)
        )


def mark_stopped(db: Session, worker_name: str) -> None:
    db.execute(
        update(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_name == worker_name)
        .values(stopped_at=func.now(), last_seen_at=func.now())
    )


def recent(db: Session) -> list[WorkerHeartbeat]:
    """Workers seen in the last day, newest first."""
    return list(
        db.scalars(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.last_seen_at > func.now() - text("interval '1 day'"))
            .order_by(WorkerHeartbeat.last_seen_at.desc(), WorkerHeartbeat.id)
            .limit(50)
        )
    )


def prune(db: Session) -> int:
    return db.execute(
        delete(WorkerHeartbeat).where(
            WorkerHeartbeat.last_seen_at
            < func.now() - text(f"interval '{KEEP_GONE_FOR}'")
        )
    ).rowcount
