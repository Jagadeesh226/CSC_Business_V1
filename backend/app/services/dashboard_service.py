"""Role dashboards (Phase 6.0) and the admin's audit-log review.

Employees see counts for the jobs they are assigned to; admins see every
job plus each active person's workload. Other dashboard tiles come from
existing endpoints: /work-queue (my reminders, notifications, documents
to review), /vehicle-expiries (expiry buckets) and /reports/collections
(admins: collections, which are not profit).
"""

import json
import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, User
from app.repositories import dashboard_repository as repo
from app.repositories.document_repository import users_by_id
from app.repositories.readiness_repository import BUSINESS_TIMEZONE, business_today
from app.schemas.dashboard import (
    AuditEntry,
    AuditLogPage,
    DashboardScope,
    DashboardSummary,
    EmployeeWorkload,
    JobCounts,
    OpenJobCounts,
    RecentJob,
)
from app.schemas.job import AssigneeRef
from app.services import job_service
from app.services.errors import BusinessRuleError

IST = ZoneInfo(BUSINESS_TIMEZONE)
MAX_AUDIT_RANGE_DAYS = 366


def summary(db: Session, viewer: User) -> DashboardSummary:
    today = business_today(db)
    admin = job_service.is_admin(viewer)
    visible_to = None if admin else viewer.id
    counts = repo.job_counts(db, today, visible_to)
    workload = None
    unassigned = None
    if admin:
        workload = [
            EmployeeWorkload(
                user=AssigneeRef.model_validate(user),
                open_jobs=open_jobs,
                overdue=overdue,
                completed_this_month=completed,
            )
            for user, open_jobs, overdue, completed in repo.workload(db, today)
        ]
        unassigned = repo.unassigned_open(db)
    return DashboardSummary(
        as_of=today,
        scope=DashboardScope.ALL if admin else DashboardScope.MINE,
        jobs=JobCounts(
            created_today=counts["created_today"],
            created_this_month=counts["created_this_month"],
            open=OpenJobCounts(
                pending=counts["pending"],
                in_progress=counts["in_progress"],
                waiting_for_documents=counts["waiting_for_documents"],
                submitted=counts["submitted"],
            ),
            overdue=counts["overdue"],
            due_today=counts["due_today"],
            completed_today=counts["completed_today"],
            completed_this_month=counts["completed_this_month"],
            cancelled_this_month=counts["cancelled_this_month"],
        ),
        workload=workload,
        unassigned_open_jobs=unassigned,
        recent_jobs=[
            RecentJob(
                id=job.id,
                job_number=job.job_number,
                title=job.title,
                status=job.status,
                customer_name=customer_name,
                last_activity_at=last_activity_at,
            )
            for job, customer_name, last_activity_at in repo.recent_jobs(db, visible_to)
        ],
    )


# ---------- audit log (admins) ----------


def _details(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return {"text": raw}
    return value if isinstance(value, dict) else {"value": value}


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, IST)


def audit_log(
    db: Session,
    *,
    page: int,
    page_size: int,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> AuditLogPage:
    if date_from and date_to:
        if date_from > date_to:
            raise BusinessRuleError("The start date is after the end date.")
        # Inclusive: from 1 Jan to 31 Dec of a leap year is 366 days.
        if (date_to - date_from).days + 1 > MAX_AUDIT_RANGE_DAYS:
            raise BusinessRuleError("Choose at most one year at a time.")
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if entity_type:
        conditions.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        conditions.append(AuditLog.entity_id == entity_id)
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if date_from:
        conditions.append(AuditLog.created_at >= _start_of(date_from))
    if date_to:
        conditions.append(AuditLog.created_at < _start_of(date_to + timedelta(days=1)))
    total = (
        db.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0
    )
    rows = db.scalars(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    users = users_by_id(db, {r.user_id for r in rows})
    return AuditLogPage(
        items=[
            AuditEntry(
                id=r.id,
                action=r.action,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                user=AssigneeRef.model_validate(users[r.user_id])
                if r.user_id in users
                else None,
                details=_details(r.details),
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )
