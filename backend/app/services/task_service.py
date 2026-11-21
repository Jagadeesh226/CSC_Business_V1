"""Admin view of background tasks: find failures and run them again."""

import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User
from app.repositories import task_repository as repo
from app.schemas.task import TaskListResponse, TaskRead, TaskStatus, TaskType
from app.services import audit_service, task_queue
from app.services.errors import BusinessRuleError, NotFoundError
from app.workers import registry


def list_tasks(
    db: Session,
    *,
    page: int,
    page_size: int,
    status: TaskStatus | None,
    task_type: TaskType | None,
) -> TaskListResponse:
    tasks, total = repo.list_tasks(
        db,
        offset=(page - 1) * page_size,
        limit=page_size,
        status=status.value if status else None,
        task_type=task_type.value if task_type else None,
    )
    return TaskListResponse(
        items=[TaskRead.model_validate(t) for t in tasks],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def get_task(db: Session, task_id: int) -> TaskRead:
    task = repo.get(db, task_id)
    if task is None:
        raise NotFoundError("Task not found.")
    return TaskRead.model_validate(task)


def retry_task(db: Session, task_id: int, actor: User) -> TaskRead:
    """Put a failed task back in the queue with a fresh set of attempts.

    ``attempts`` keeps counting (it identifies each claim, so a stale earlier
    attempt can never pass for the new one); the limit is raised instead.
    """
    task = repo.lock(db, task_id)
    if task is None:
        raise NotFoundError("Task not found.")
    if task.status != TaskStatus.FAILED.value:
        raise BusinessRuleError(
            f"Only failed tasks can be retried (this one is {task.status})."
        )
    now = db.scalar(select(func.clock_timestamp()))
    task.status = TaskStatus.QUEUED.value
    task.max_attempts = task.attempts + registry.limits_for(task.task_type)[1]
    task.run_after = now
    task.finished_at = None
    task.updated_at = now
    audit_service.record(
        db,
        action="task_retried",
        entity_type="background_task",
        entity_id=task.id,
        user_id=actor.id,
        details={"task_type": task.task_type, "last_error": task.last_error},
    )
    task_queue.notify_after_commit(db, [task.id])
    db.commit()
    return TaskRead.model_validate(task)
