from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.error import ErrorResponse
from app.schemas.job import JobCreate, JobListResponse, JobRead, JobStatus, JobUpdate
from app.services import job_service

# AUTH INTEGRATION POINT: authentication is not implemented yet. When it is,
# add the authenticated-user dependency and per-role permission checks here
# (see docs/contracts/permissions.md) and pass the user id to create_job as
# created_by. Until then these routes are unauthenticated and must not be
# exposed outside local development.
router = APIRouter(prefix="/jobs", tags=["jobs"])

DbSession = Annotated[Session, Depends(get_db)]

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Job not found"}}


@router.post(
    "",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"model": ErrorResponse, "description": "Customer or service not found"},
        409: {"model": ErrorResponse, "description": "Job could not be created"},
    },
)
def create_job(payload: JobCreate, db: DbSession) -> JobRead:
    return job_service.create_job(db, payload, created_by=None)


@router.get("", response_model=JobListResponse)
def list_jobs(
    db: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[
        str | None,
        Query(max_length=200, description="Matches job number or title"),
    ] = None,
    customer_id: Annotated[int | None, Query(gt=0)] = None,
    service_id: Annotated[int | None, Query(gt=0)] = None,
    status: JobStatus | None = None,
    due_date: date | None = None,
) -> JobListResponse:
    return job_service.list_jobs(
        db,
        page=page,
        page_size=page_size,
        search=search,
        customer_id=customer_id,
        service_id=service_id,
        status=status,
        due_date=due_date,
    )


@router.get("/{job_id}", response_model=JobRead, responses=NOT_FOUND)
def get_job(job_id: int, db: DbSession) -> JobRead:
    return job_service.get_job(db, job_id)


@router.patch("/{job_id}", response_model=JobRead, responses=NOT_FOUND)
def update_job(job_id: int, payload: JobUpdate, db: DbSession) -> JobRead:
    return job_service.update_job(db, job_id, payload)
