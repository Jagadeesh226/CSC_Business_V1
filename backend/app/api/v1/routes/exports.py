from typing import Annotated

from fastapi import APIRouter, Body, Path, Query, Response, status

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.report_export import ExportListResponse, ExportRead, ExportRequest
from app.services import export_service, report_export_service

# Exports built in the background (Phase 5.2). Only the person who asked
# sees or downloads an export; others get 404.
router = APIRouter(prefix="/exports", tags=["reports"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Export not found"}}


@router.post(
    "",
    response_model=ExportRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {
            "model": ErrorResponse,
            "description": "Three of your exports are still being made",
        }
    },
)
def request_export(
    request: Annotated[ExportRequest, Body()], user: CurrentUser, db: DbSession
) -> ExportRead:
    """Ask for a vehicles or jobs spreadsheet. It is built in the background
    with what you may see; you are notified when it is ready, and can
    download it for 24 hours. Audited."""
    return report_export_service.request_export(db, request, user)


@router.get("", response_model=ExportListResponse)
def list_exports(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ExportListResponse:
    """Your exports, newest first."""
    return report_export_service.list_exports(db, user, page=page, page_size=page_size)


@router.get("/{export_id}", response_model=ExportRead, responses=NOT_FOUND)
def get_export(export_id: Id, user: CurrentUser, db: DbSession) -> ExportRead:
    return report_export_service.get_export(db, export_id, user)


@router.get(
    "/{export_id}/file",
    response_class=Response,
    responses={
        200: {
            "content": {export_service.XLSX: {}},
            "description": "The spreadsheet",
        },
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Not ready, or it failed"},
        410: {"model": ErrorResponse, "description": "Expired: ask for a new one"},
    },
)
def download_export(export_id: Id, user: CurrentUser, db: DbSession) -> Response:
    """Download your finished export. Audited."""
    filename, data = report_export_service.download(db, export_id, user)
    return Response(
        content=data,
        media_type=export_service.XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
