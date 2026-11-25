from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.vehicle_intake import IntakeRead, IntakeResolve
from app.services import vehicle_intake

# What OCR did with a registration certificate, and the person's answer for
# anything that waited for them. Access follows the document.
router = APIRouter(tags=["vehicles"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
RESPONSES = {
    404: {"model": ErrorResponse, "description": "Document or reading not found"},
    403: {"model": ErrorResponse, "description": "Not allowed for this user"},
}


@router.get(
    "/documents/{document_id}/vehicle-intake",
    response_model=IntakeRead,
    responses=RESPONSES,
)
def get_intake(
    document_id: Id,
    user: CurrentUser,
    db: DbSession,
    reveal: Annotated[
        bool,
        Query(
            description=(
                "Show full chassis and engine numbers (admin or the responsible "
                "employee; audited)"
            )
        ),
    ] = False,
) -> IntakeRead:
    return vehicle_intake.get_intake(db, document_id, user, reveal)


@router.post(
    "/documents/{document_id}/vehicle-intake/resolve",
    response_model=IntakeRead,
    responses={
        **RESPONSES,
        409: {"model": ErrorResponse, "description": "Registration owned elsewhere"},
    },
)
def resolve_intake(
    document_id: Id, payload: IntakeResolve, user: CurrentUser, db: DbSession
) -> IntakeRead:
    return vehicle_intake.resolve(db, document_id, payload, user)
