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
    returns the existing task, whatever its status, and ignores the new
    payload. Keys must therefore name one piece of work exactly, e.g.
    "document_ocr:<document id>:v<version>"; to run work again, use a new
    key or an admin retry.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unknown task type: {task_type}")
    spec = registry.get_spec(task_type)
    timeout, attempts = registry.limits_for(task_type)
    task_id, created = repo.insert_task(
        db,
        task_type=task_type,
        idempotency_key=idempotency_key,
        payload=payload,
        max_attempts=spec.max_attempts if spec else attempts,
        timeout_seconds=spec.timeout_seconds if spec else timeout,
        created_by=created_by,
    )
    if created:
        db.info.setdefault(_PENDING, []).append(task_id)
    return task_id, created


def notify_after_commit(db: Session, task_ids: list[int]) -> None:
    """Wake workers for these tasks once the current transaction commits."""
    db.info.setdefault(_PENDING, []).extend(task_ids)


_COMMITTED = "task_queue_committed"


# SQLAlchemy fires after_commit both for a real commit and for releasing a
# savepoint, and the two look the same there. after_transaction_end tells
# them apart, so: note the commit, then publish only when the outermost
# transaction ends after a commit. A savepoint release, or an outer rollback
# after one, publishes nothing.
@event.listens_for(Session, "after_commit")
def _note_commit(session: Session) -> None:
    session.info[_COMMITTED] = True


@event.listens_for(Session, "after_transaction_end")
def _publish_or_drop(session: Session, transaction) -> None:
    if transaction.nested:
        session.info.pop(_COMMITTED, None)  # that commit was a savepoint
        return
    if transaction.parent is not None:
        return
    committed = session.info.pop(_COMMITTED, False)
    pending = session.info.pop(_PENDING, [])
    if committed:
        notifier.publish(pending)
