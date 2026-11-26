from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.vehicle_lookup import (
    CaptchaAnswer,
    VehicleLookupList,
    VehicleLookupRead,
)
from app.services import lookup_service

# Lookups follow vehicle access. The system never solves a CAPTCHA: a person
# reads the image and types the answer.
router = APIRouter(tags=["vehicles"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}


@router.post(
    "/vehicles/{vehicle_id}/lookups",
    response_model=VehicleLookupRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "A lookup is already running"},
    },
)
def request_lookup(
    vehicle_id: Id, user: CurrentUser, db: DbSession
) -> VehicleLookupRead:
    """Look up the vehicle's validity dates and pending challans from the
    configured source (in the background)."""
    return lookup_service.request_lookup(db, vehicle_id, user)


@router.get(
    "/vehicles/{vehicle_id}/lookups",
    response_model=VehicleLookupList,
    responses=NOT_FOUND,
)
def list_lookups(vehicle_id: Id, user: CurrentUser, db: DbSession) -> VehicleLookupList:
    return lookup_service.list_lookups(db, vehicle_id, user)


@router.get(
    "/vehicle-lookups/{lookup_id}",
    response_model=VehicleLookupRead,
    responses=NOT_FOUND,
)
def get_lookup(lookup_id: Id, user: CurrentUser, db: DbSession) -> VehicleLookupRead:
    return lookup_service.get_lookup(db, lookup_id, user)


@router.get(
    "/vehicle-lookups/{lookup_id}/captcha",
    response_class=Response,
    responses={
        200: {
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "The CAPTCHA to read",
        },
        **NOT_FOUND,
    },
)
def captcha_image(lookup_id: Id, user: CurrentUser, db: DbSession) -> Response:
    return Response(
        content=lookup_service.captcha_image(db, lookup_id, user),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/vehicle-lookups/{lookup_id}/captcha",
    response_model=VehicleLookupRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses=NOT_FOUND,
)
def answer_captcha(
    lookup_id: Id, payload: CaptchaAnswer, user: CurrentUser, db: DbSession
) -> VehicleLookupRead:
    """The person's answer to the CAPTCHA; the lookup continues."""
    return lookup_service.answer_captcha(db, lookup_id, payload.answer, user)
