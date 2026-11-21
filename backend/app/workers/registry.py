"""Task handlers, registered by task type.

A handler receives a database session and a ``TaskInfo`` and returns a small
JSON-able result (ids, counts; never document contents). It must not commit:
the worker commits the handler's changes together with the task's success,
and only if the task is still owned by this attempt.

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


Handler = Callable[[Session, TaskInfo], dict[str, Any] | None]


@dataclass(frozen=True)
class TaskSpec:
    handler: Handler
    timeout_seconds: int = 120
    max_attempts: int = 3


_REGISTRY: dict[str, TaskSpec] = {}


def register(
    task_type: str,
    handler: Handler,
    *,
    timeout_seconds: int = 120,
    max_attempts: int = 3,
) -> None:
    _REGISTRY[task_type] = TaskSpec(handler, timeout_seconds, max_attempts)


def unregister(task_type: str) -> None:
    _REGISTRY.pop(task_type, None)


def get_spec(task_type: str) -> TaskSpec | None:
    return _REGISTRY.get(task_type)
