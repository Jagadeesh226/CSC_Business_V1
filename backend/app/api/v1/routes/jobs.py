from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from fastapi.exceptions import RequestValidationError

from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.models import User
from app.schemas.error import ErrorResponse
from app.schemas.job import (
    MAX_DB_INT,
    AssignmentCreate,
    AssignmentRead,
    CancelRequest,
    DueFilter,
    JobCreate,
    JobListResponse,
    JobRead,
    JobSort,
    JobStatus,
    JobUpdate,
    PaymentFilter,
    Readiness,
    StatusChangeRequest,
    StatusHistoryRead,
)
from app.services import job_service

# Access: admins see and edit every job and manage assignments. Employees can
# create jobs (and are assigned to what they create) and can see and edit only
# jobs they are assigned to; other jobs return 404. Rules live in job_service.
router = APIRouter(prefix="/jobs", tags=["jobs"], responses=AUTH_RESPONSES)

JobId = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
# Keeps OFFSET within PostgreSQL bigint range.
MAX_PAGE = 1_000_000

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Job not found"}}

# Business-rule 422s return {"detail": "<message>"}; request-validation 422s
# return FastAPI's list-shaped detail. Document both.
RULE_422 = {
    "description": "Validation error, or a business rule (e.g. the job is closed)",
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
def create_job(payload: JobCreate, user: CurrentUser, db: DbSession) -> JobRead:
    return job_service.create_job(db, payload, creator=user)


@router.get("", response_model=JobListResponse)
def list_jobs(
    user: CurrentUser,
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
    assigned_to: Annotated[
        str | None,
        Query(
            pattern=r"^(me|[1-9][0-9]{0,9})$",
            description="A user id, or 'me' for the signed-in user",
        ),
    ] = None,
    readiness: Annotated[
        Readiness | None,
        Query(description="Document progress (jobs still gathering documents)"),
    ] = None,
    due: Annotated[
        DueFilter | None,
        Query(description="Open jobs overdue or due today (centre's calendar)"),
    ] = None,
    unassigned: Annotated[
        bool,
        Query(description="Open jobs nobody is assigned to (admins)"),
    ] = False,
    sort: JobSort = JobSort.NEWEST,
    payment: Annotated[
        PaymentFilter | None,
        Query(description="Money: due, fully paid or overpaid (not cancelled)"),
    ] = None,
) -> JobListResponse:
    assignee_id = _parse_assigned_to(assigned_to, user)
    return job_service.list_jobs(
        db,
        viewer=user,
        assigned_to=assignee_id,
        page=page,
        page_size=page_size,
        search=search,
        customer_id=customer_id,
        service_id=service_id,
        status=status,
        due_date=due_date,
        readiness=readiness,
        due=due,
        unassigned=unassigned,
        payment=payment,
        sort=sort,
    )


@router.get("/{job_id}", response_model=JobRead, responses=NOT_FOUND)
def get_job(job_id: JobId, user: CurrentUser, db: DbSession) -> JobRead:
    return job_service.get_job(db, job_id, viewer=user)


@router.patch(
    "/{job_id}",
    response_model=JobRead,
    responses={**NOT_FOUND, 422: RULE_422},
)
def update_job(
    job_id: JobId, payload: JobUpdate, user: CurrentUser, db: DbSession
) -> JobRead:
    return job_service.update_job(db, job_id, payload, actor=user)


def _parse_assigned_to(value: str | None, user: User) -> int | None:
    if value is None:
        return None
    if value == "me":
        return user.id
    user_id = int(value)
    if user_id > MAX_DB_INT:
        raise RequestValidationError(
            [
                {
                    "type": "less_than_equal",
                    "loc": ("query", "assigned_to"),
                    "msg": f"Input should be less than or equal to {MAX_DB_INT}",
                    "input": value,
                }
            ]
        )
    return user_id


# ---------- assignments (admin manages; assigned employees can view) ----------

ASSIGN_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Job or user not found"},
    409: {"model": ErrorResponse, "description": "User already assigned"},
    422: RULE_422,
}


@router.get(
    "/{job_id}/assignments",
    response_model=list[AssignmentRead],
    responses=NOT_FOUND,
)
def list_assignments(
    job_id: JobId, user: CurrentUser, db: DbSession
) -> list[AssignmentRead]:
    return job_service.list_assignments(db, job_id, viewer=user)


@router.post(
    "/{job_id}/assignments",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
    responses=ASSIGN_RESPONSES,
)
def assign_user(
    job_id: JobId, payload: AssignmentCreate, admin: AdminUser, db: DbSession
) -> JobRead:
    return job_service.assign_user(db, job_id, payload.user_id, actor=admin)


@router.delete(
    "/{job_id}/assignments/{user_id}",
    response_model=JobRead,
    responses={
        404: {"model": ErrorResponse, "description": "Job or assignment not found"},
        422: RULE_422,
    },
)
def unassign_user(
    job_id: JobId,
    user_id: Annotated[int, Path(gt=0, le=MAX_DB_INT)],
    admin: AdminUser,
    db: DbSession,
) -> JobRead:
    return job_service.unassign_user(db, job_id, user_id, actor=admin)


# ---------- status (employees: assigned jobs; cancel: admins) ----------


@router.post(
    "/{job_id}/status",
    response_model=JobRead,
    responses={**NOT_FOUND, 422: RULE_422},
)
def change_status(
    job_id: JobId, payload: StatusChangeRequest, user: CurrentUser, db: DbSession
) -> JobRead:
    return job_service.change_status(
        db,
        job_id,
        payload.status,
        actor=user,
        reason=payload.reason,
        override_document_check=payload.override_document_check,
    )


@router.post(
    "/{job_id}/cancel",
    response_model=JobRead,
    responses={**NOT_FOUND, 422: RULE_422},
)
def cancel_job(
    job_id: JobId, payload: CancelRequest, admin: AdminUser, db: DbSession
) -> JobRead:
    return job_service.cancel_job(db, job_id, actor=admin, reason=payload.reason)


@router.get(
    "/{job_id}/status-history",
    response_model=list[StatusHistoryRead],
    responses=NOT_FOUND,
)
def list_status_history(
    job_id: JobId, user: CurrentUser, db: DbSession
) -> list[StatusHistoryRead]:
    return job_service.list_status_history(db, job_id, viewer=user)
