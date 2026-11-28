from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.routes.jobs import RULE_422
from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.schemas.dashboard import AuditLogPage, DashboardSummary
from app.schemas.job import MAX_DB_INT
from app.services import dashboard_service

router = APIRouter(tags=["dashboard"], responses=AUTH_RESPONSES)

Name = Annotated[str | None, Query(min_length=1, max_length=100)]
OptionalId = Annotated[int | None, Query(gt=0, le=MAX_DB_INT)]


@router.get("/dashboard", response_model=DashboardSummary)
def dashboard(user: CurrentUser, db: DbSession) -> DashboardSummary:
    """Job counts for the dashboard: an employee's own jobs, or (admins)
    every job with each person's workload. Dates are the centre's
    calendar."""
    return dashboard_service.summary(db, user)


@router.get("/audit-logs", response_model=AuditLogPage, responses={422: RULE_422})
def audit_logs(
    admin: AdminUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    action: Name = None,
    entity_type: Name = None,
    entity_id: OptionalId = None,
    user_id: OptionalId = None,
    date_from: Annotated[
        date | None, Query(description="On or after (centre's calendar)")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="On or before (centre's calendar)")
    ] = None,
) -> AuditLogPage:
    """Admins: review security-relevant activity, newest first."""
    return dashboard_service.audit_log(
        db,
        page=page,
        page_size=page_size,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
    )
