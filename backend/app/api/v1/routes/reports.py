from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.dependencies import AUTH_RESPONSES, AdminUser, DbSession
from app.schemas.report import CollectionsReport
from app.services import report_service

# Admin-only reports.
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
