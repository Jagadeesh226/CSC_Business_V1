from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.error import ErrorResponse
from app.schemas.job import (
    MAX_DB_INT,
    JobCreate,
    JobListResponse,
    JobRead,
    JobStatus,
    JobUpdate,
)
from app.services import job_service

# AUTH INTEGRATION POINT: authentication is not implemented yet. When it is,
# add the authenticated-user dependency and per-role permission checks here
# (see docs/contracts/permissions.md) and pass the user id to create_job as
# created_by. Until then these routes are unauthenticated and must not be
# exposed outside local development.
router = APIRouter(prefix="/jobs", tags=["jobs"])

DbSession = Annotated[Session, Depends(get_db)]

JobId = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
# Keeps OFFSET within PostgreSQL bigint range.
MAX_PAGE = 1_000_000

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Job not found"}}

# Business-rule 422s (inactive service) return {"detail": "<message>"}, while
# request-validation 422s return FastAPI's list-shaped detail. Document both.
CREATE_422 = {
    "description": "Validation error, or the selected service is inactive",
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                    {"$ref": "#/components/schemas/ErrorResponse"},
                ]
            }
        }
    },
}


@router.post(
    "",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"model": ErrorResponse, "description": "Customer or service not found"},
        409: {"model": ErrorResponse, "description": "Job could not be created"},
        422: CREATE_422,
    },
)
def create_job(payload: JobCreate, db: DbSession) -> JobRead:
    return job_service.create_job(db, payload, created_by=None)


@router.get("", response_model=JobListResponse)
def list_jobs(
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[
        str | None,
        Query(max_length=200, description="Matches job number or title"),
    ] = None,
    customer_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
    service_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
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
def get_job(job_id: JobId, db: DbSession) -> JobRead:
    return job_service.get_job(db, job_id)


@router.patch("/{job_id}", response_model=JobRead, responses=NOT_FOUND)
def update_job(job_id: JobId, payload: JobUpdate, db: DbSession) -> JobRead:
    return job_service.update_job(db, job_id, payload)
