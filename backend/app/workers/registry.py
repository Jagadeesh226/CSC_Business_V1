"""Task handlers, registered by task type.

A handler receives a database session and a ``TaskInfo`` and returns a small
JSON-able result (ids, counts; never document contents or identity numbers:
results are shown to admins). It cannot commit (its session refuses): the
worker commits the handler's changes together with the task's success, and
only if this claim still owns the task. Handlers must be idempotent, since a
task can run more than once (a crashed worker's task is retried).

Raise ``TaskError`` with a safe message for an expected, retryable failure,
or ``PermanentTaskError`` when retrying cannot help.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session


class TaskError(Exception):
    """Expected failure; its message is stored as the task's last error."""


class PermanentTaskError(TaskError):
    """Retrying cannot help (e.g. the document no longer exists)."""


@dataclass(frozen=True)
class TaskInfo:
    id: int
    task_type: str
    payload: dict[str, Any]
    attempt: int
    # The task's time limit: handlers give external calls a deadline inside it.
    timeout_seconds: int = 120


Handler = Callable[[Session, TaskInfo], dict[str, Any] | None]
# Called once a task has failed for good, in its own transaction after the
# failure is saved (it must not commit), e.g. to send a document to manual
# review. It may run more than once for a task, so it must be idempotent.
FailureHook = Callable[[Session, TaskInfo, str], None]


@dataclass(frozen=True)
class TaskSpec:
    handler: Handler
    timeout_seconds: int = 120
    max_attempts: int = 3
    on_final_failure: FailureHook | None = None


# Limits per task type, known to every process (the API enqueues tasks
# without importing handlers, so limits must not depend on registration).
TASK_LIMITS: dict[str, tuple[int, int]] = {
    # task type: (timeout seconds, max attempts)
    "document_ocr": (180, 3),
    "vehicle_expiry_lookup": (120, 3),
    "vehicle_mobile_lookup": (120, 3),
}
DEFAULT_LIMITS = (120, 3)

_REGISTRY: dict[str, TaskSpec] = {}


def limits_for(task_type: str) -> tuple[int, int]:
    return TASK_LIMITS.get(task_type, DEFAULT_LIMITS)


def register(
    task_type: str,
    handler: Handler,
    *,
    timeout_seconds: int | None = None,
    max_attempts: int | None = None,
    on_final_failure: FailureHook | None = None,
) -> None:
    default_timeout, default_attempts = limits_for(task_type)
    _REGISTRY[task_type] = TaskSpec(
        handler,
        timeout_seconds or default_timeout,
        max_attempts or default_attempts,
        on_final_failure,
    )


def unregister(task_type: str) -> None:
    _REGISTRY.pop(task_type, None)


def get_spec(task_type: str) -> TaskSpec | None:
    return _REGISTRY.get(task_type)


# Periodic maintenance run by every worker on each loop, e.g. recovering
# documents left in 'processing'. Each gets its own session and must not
# commit; it returns how many records it fixed.
Maintenance = Callable[[Session], int]
_MAINTENANCE: list[Maintenance] = []


def register_maintenance(job: Maintenance) -> None:
    if job not in _MAINTENANCE:
        _MAINTENANCE.append(job)


def maintenance_jobs() -> list[Maintenance]:
    return list(_MAINTENANCE)
