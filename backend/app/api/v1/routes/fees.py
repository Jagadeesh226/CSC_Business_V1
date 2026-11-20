from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.v1.routes.jobs import NOT_FOUND, RULE_422, JobId
from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.fee import FeeCreate, FeeListResponse, FeeRead, FeeUpdate
from app.schemas.job import MAX_DB_INT
from app.services import fee_service

# Admins manage fees on any job. Employees view fees of jobs they are
# assigned to and add fees to open assigned jobs until a payment is received.
router = APIRouter(prefix="/jobs", tags=["job fees"], responses=AUTH_RESPONSES)

FeeId = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
FEE_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Job or fee not found"}}


@router.get("/{job_id}/fees", response_model=FeeListResponse, responses=NOT_FOUND)
def list_fees(
    job_id: JobId,
    user: CurrentUser,
    db: DbSession,
    include_removed: Annotated[
        bool, Query(description="Also list removed fees (totals never include them)")
    ] = False,
) -> FeeListResponse:
    return fee_service.list_fees(db, job_id, user, include_removed=include_removed)


@router.post(
    "/{job_id}/fees",
    response_model=FeeRead,
    status_code=status.HTTP_201_CREATED,
    responses={**NOT_FOUND, 422: RULE_422},
)
def add_fee(
    job_id: JobId, payload: FeeCreate, user: CurrentUser, db: DbSession
) -> FeeRead:
    return fee_service.add_fee(db, job_id, payload, actor=user)


@router.patch(
    "/{job_id}/fees/{fee_id}",
    response_model=FeeRead,
    responses=FEE_NOT_FOUND,
)
def update_fee(
    job_id: JobId, fee_id: FeeId, payload: FeeUpdate, admin: AdminUser, db: DbSession
) -> FeeRead:
    return fee_service.update_fee(db, job_id, fee_id, payload, actor=admin)


@router.delete(
    "/{job_id}/fees/{fee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=FEE_NOT_FOUND,
)
def remove_fee(
    job_id: JobId, fee_id: FeeId, admin: AdminUser, db: DbSession
) -> Response:
    fee_service.remove_fee(db, job_id, fee_id, actor=admin)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
