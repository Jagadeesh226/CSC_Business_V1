from typing import Annotated

from fastapi import APIRouter, Header, Path, Response, status

from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.payment import (
    PaymentCreate,
    PaymentListResponse,
    PaymentRecorded,
    PaymentWithSummary,
    VoidRequest,
)
from app.services import payment_service

# Access: the employee assigned to the job, or an admin, records payments and
# sees them; only admins void. Rules live in payment_service.
router = APIRouter(tags=["payments"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description=(
            "A new random value (e.g. a UUID) per payment. Send the same value "
            "when retrying the same payment: it is recorded only once."
        ),
    ),
]

WRITE = {
    404: {"model": ErrorResponse, "description": "Job not found"},
    409: {
        "model": ErrorResponse,
        "description": "Idempotency-Key already used for a different payment",
    },
}


@router.post(
    "/jobs/{job_id}/payments",
    response_model=PaymentRecorded,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": PaymentRecorded,
            "description": (
                "Repeat of an earlier request: the original payment, as it is "
                "now (check status: it may have been voided since)"
            ),
        },
        **WRITE,
    },
)
def record_payment(
    job_id: Id,
    payload: PaymentCreate,
    idempotency_key: IdempotencyKey,
    user: CurrentUser,
    db: DbSession,
    response: Response,
) -> PaymentRecorded:
    result = payment_service.record_payment(db, job_id, payload, idempotency_key, user)
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return result


@router.get(
    "/jobs/{job_id}/payments",
    response_model=PaymentListResponse,
    responses={404: {"model": ErrorResponse, "description": "Job not found"}},
)
def list_payments(job_id: Id, user: CurrentUser, db: DbSession) -> PaymentListResponse:
    return payment_service.list_payments(db, job_id, user)


@router.post(
    "/payments/{payment_id}/void",
    response_model=PaymentWithSummary,
    responses={
        404: {"model": ErrorResponse, "description": "Payment not found"},
        409: {"model": ErrorResponse, "description": "Already voided"},
    },
)
def void_payment(
    payment_id: Id, payload: VoidRequest, user: AdminUser, db: DbSession
) -> PaymentWithSummary:
    return payment_service.void_payment(db, payment_id, payload.reason, user)
