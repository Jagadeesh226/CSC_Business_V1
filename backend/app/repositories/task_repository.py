from typing import Any

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import BackgroundTask


def insert_task(
    db: Session,
    *,
    task_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    max_attempts: int,
    timeout_seconds: int,
    created_by: int | None,
) -> tuple[int, bool]:
    """Insert unless the key exists. Returns (task id, created)."""
    new_id = db.scalar(
        insert(BackgroundTask)
        .values(
            task_type=task_type,
            idempotency_key=idempotency_key,
            payload=payload,
            status="queued",
            attempts=0,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            created_by=created_by,
        )
        .on_conflict_do_nothing(index_elements=[BackgroundTask.idempotency_key])
        .returning(BackgroundTask.id)
    )
    if new_id is not None:
        return new_id, True
    existing = db.scalar(
        select(BackgroundTask.id).where(
            BackgroundTask.idempotency_key == idempotency_key
        )
    )
    return existing, False


# The lease outlives the handler's time limit, so a slow but alive handler is
# stopped by its worker before another worker could take the task over.
LEASE_GRACE_SECONDS = 60

_CLAIM = text(
    """
    UPDATE background_tasks t
    SET status = 'running',
        attempts = t.attempts + 1,
        locked_by = :worker,
        lease_expires_at = now() + make_interval(secs => t.timeout_seconds + :grace),
        updated_at = now()
    WHERE t.id = (
        SELECT id FROM background_tasks
        WHERE status = 'queued' AND run_after <= now()
          AND (CAST(:task_id AS integer) IS NULL OR id = :task_id)
        ORDER BY run_after, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING t.id, t.task_type, t.payload, t.attempts, t.timeout_seconds
    """
)


def claim(db: Session, worker: str, task_id: int | None = None):
    """Atomically take one due task (or the given one). SKIP LOCKED means
    concurrent workers never take the same task."""
    return db.execute(
        _CLAIM, {"worker": worker, "task_id": task_id, "grace": LEASE_GRACE_SECONDS}
    ).first()


def lock_owned(db: Session, task_id: int, worker: str, attempt: int):
    """The task row, locked, if this worker's attempt still owns it."""
    return db.scalar(
        select(BackgroundTask)
        .where(
            BackgroundTask.id == task_id,
            BackgroundTask.status == "running",
            BackgroundTask.locked_by == worker,
            BackgroundTask.attempts == attempt,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def requeue_expired(db: Session):
    """Tasks whose worker vanished or hung past the lease: retry or fail.
    Returns (id, status, task_type, payload, attempts, last_error) rows."""
    rows = db.execute(
        text(
            """
            UPDATE background_tasks
            SET status = CASE WHEN attempts >= max_attempts
                              THEN 'failed' ELSE 'queued' END,
                finished_at = CASE WHEN attempts >= max_attempts
                                   THEN now() ELSE NULL END,
                last_error = 'Timed out: the worker did not finish in time.',
                locked_by = NULL,
                lease_expires_at = NULL,
                run_after = now(),
                updated_at = now()
            WHERE status = 'running' AND lease_expires_at < now()
            RETURNING id, status, task_type, payload, attempts, last_error
            """
        )
    ).all()
    return list(rows)


def list_tasks(
    db: Session,
    *,
    offset: int,
    limit: int,
    status: str | None,
    task_type: str | None,
) -> tuple[list[BackgroundTask], int]:
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(BackgroundTask.status == status)
    if task_type:
        conditions.append(BackgroundTask.task_type == task_type)
    total = db.scalar(
        select(func.count()).select_from(BackgroundTask).where(*conditions)
    )
    tasks = db.scalars(
        select(BackgroundTask)
        .where(*conditions)
        .order_by(BackgroundTask.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(tasks), int(total or 0)


def get(db: Session, task_id: int) -> BackgroundTask | None:
    return db.get(BackgroundTask, task_id)


def lock(db: Session, task_id: int) -> BackgroundTask | None:
    return db.scalar(
        select(BackgroundTask)
        .where(BackgroundTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
