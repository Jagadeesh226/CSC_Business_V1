"""Enqueue background tasks from request code.

``enqueue`` adds the task row in the caller's transaction, so a task exists
only if the work that created it is committed. Workers are woken through
Redis after that commit; on rollback nothing is published.
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import TASK_TYPES
from app.repositories import task_repository as repo
from app.workers import notifier, registry

_PENDING = "pending_task_ids"


def enqueue(
    db: Session,
    task_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    *,
    created_by: int | None = None,
) -> tuple[int, bool]:
    """Queue a task once per idempotency key. Returns (task id, created).

    Enqueueing the same key again (a retried request, a duplicate click)
    returns the existing task instead of creating a second one.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unknown task type: {task_type}")
    spec = registry.get_spec(task_type)
    task_id, created = repo.insert_task(
        db,
        task_type=task_type,
        idempotency_key=idempotency_key,
        payload=payload,
        max_attempts=spec.max_attempts if spec else 3,
        timeout_seconds=spec.timeout_seconds if spec else 120,
        created_by=created_by,
    )
    if created:
        db.info.setdefault(_PENDING, []).append(task_id)
    return task_id, created


def notify_after_commit(db: Session, task_ids: list[int]) -> None:
    """Wake workers for these tasks once the current transaction commits."""
    db.info.setdefault(_PENDING, []).extend(task_ids)


@event.listens_for(Session, "after_commit")
def _publish_pending(session: Session) -> None:
    notifier.publish(session.info.pop(_PENDING, []))


@event.listens_for(Session, "after_soft_rollback")
def _drop_pending(session: Session, previous_transaction) -> None:
    if previous_transaction.parent is None:  # the outermost transaction
        session.info.pop(_PENDING, None)
