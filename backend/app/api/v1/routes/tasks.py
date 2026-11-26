from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.v1.routes.jobs import RULE_422
from app.core.dependencies import AUTH_RESPONSES, AdminUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.ops import BackgroundStatus
from app.schemas.task import TaskListResponse, TaskRead, TaskStatus, TaskType
from app.services import ops_service, task_service

# Admin only: watch background processing and re-run failed tasks.
router = APIRouter(prefix="/tasks", tags=["background tasks"], responses=AUTH_RESPONSES)

TaskId = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Task not found"}}


@router.get("", response_model=TaskListResponse)
def list_tasks(
    admin: AdminUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: TaskStatus | None = None,
    task_type: TaskType | None = None,
) -> TaskListResponse:
    return task_service.list_tasks(
        db, page=page, page_size=page_size, status=status, task_type=task_type
    )


@router.get("/overview", response_model=BackgroundStatus)
def background_overview(admin: AdminUser, db: DbSession) -> BackgroundStatus:
    """Is background work healthy? Workers, queue counts by task type,
    the daily jobs, and plain-language problems."""
    return ops_service.background_status(db)


@router.get("/{task_id}", response_model=TaskRead, responses=NOT_FOUND)
def get_task(task_id: TaskId, admin: AdminUser, db: DbSession) -> TaskRead:
    return task_service.get_task(db, task_id)


@router.post(
    "/{task_id}/retry", response_model=TaskRead, responses={**NOT_FOUND, 422: RULE_422}
)
def retry_task(task_id: TaskId, admin: AdminUser, db: DbSession) -> TaskRead:
    return task_service.retry_task(db, task_id, admin)
