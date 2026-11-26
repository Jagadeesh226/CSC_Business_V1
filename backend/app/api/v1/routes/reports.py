from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.schemas.job import MAX_DB_INT
from app.schemas.report import CollectionsReport
from app.services import export_service, report_service

# Reports. Collections are admin-only; exports follow each user's access.
router = APIRouter(prefix="/reports", tags=["reports"], responses=AUTH_RESPONSES)


@router.get("/collections", response_model=CollectionsReport)
def collections(
    user: AdminUser,
    db: DbSession,
    date_from: Annotated[
        date | None,
        Query(description="First day, inclusive (default: date_to)"),
    ] = None,
    date_to: Annotated[
        date | None,
        Query(description="Last day, inclusive (default: today, centre's calendar)"),
    ] = None,
) -> CollectionsReport:
    """Money received per day, method and person: collections, not profit."""
    return report_service.collections(db, date_from, date_to)


@router.get(
    "/vehicles.xlsx",
    response_class=Response,
    responses={
        200: {
            "content": {
                export_service.XLSX: {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "The vehicles spreadsheet",
        },
        422: {
            "description": "Invalid filters, or too many rows (filter further)",
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
        },
    },
)
def vehicles_export(
    user: CurrentUser,
    db: DbSession,
    due_within_days: Annotated[
        int | None,
        Query(
            ge=0,
            le=366,
            description="Only vehicles with a date expiring within this many days "
            "(expired ones included)",
        ),
    ] = None,
    include_inactive: bool = False,
    customer_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
    include_whatsapp: Annotated[
        bool,
        Query(
            description="Add the WhatsApp column (its links carry the full mobile "
            "number)"
        ),
    ] = True,
) -> Response:
    """Vehicles with their expiry dates and a WhatsApp link per row
    (chassis, engine and mobile masked). Employees get the vehicles they
    may see. Audited."""
    filename, data, _ = export_service.vehicles_xlsx(
        db,
        user,
        due_within_days=due_within_days,
        include_inactive=include_inactive,
        customer_id=customer_id,
        include_whatsapp=include_whatsapp,
    )
    return Response(
        content=data,
        media_type=export_service.XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
