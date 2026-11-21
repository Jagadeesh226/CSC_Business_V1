from typing import Annotated

from fastapi import APIRouter, Query

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.job import MAX_DB_INT
from app.schemas.queue import ReviewQueueResponse, WorkQueueSummary
from app.services import queue_service

# Read-only queues for the signed-in user. Employees see only open jobs they
# are assigned to (and those jobs' documents); admins see everything.
router = APIRouter(tags=["queues"], responses=AUTH_RESPONSES)

# Keeps OFFSET within PostgreSQL bigint range.
MAX_PAGE = 1_000_000


@router.get("/work-queue", response_model=WorkQueueSummary)
def work_queue(user: CurrentUser, db: DbSession) -> WorkQueueSummary:
    return queue_service.work_queue(db, user)


@router.get("/review-queue", response_model=ReviewQueueResponse)
def review_queue(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    customer_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
    job_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
) -> ReviewQueueResponse:
    return queue_service.review_queue(
        db,
        user,
        page=page,
        page_size=page_size,
        customer_id=customer_id,
        job_id=job_id,
    )
