"""Dashboard summary and audit-log review (Phase 6.0)."""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.schemas.job import AssigneeRef, JobStatus


class DashboardScope(StrEnum):
    ALL = "all"  # admins: every job
    MINE = "mine"  # employees: the jobs they are assigned to


class OpenJobCounts(BaseModel):
    pending: int
    in_progress: int
    waiting_for_documents: int
    submitted: int


class JobCounts(BaseModel):
    created_today: int
    created_this_month: int
    open: OpenJobCounts
    # Open jobs past their due date / due today (centre's calendar).
    overdue: int
    due_today: int
    completed_today: int
    completed_this_month: int
    cancelled_this_month: int


class EmployeeWorkload(BaseModel):
    """Every active person (admins too) and the jobs they are on now. A job
    completed by two people counts for each of them, so these totals can
    add up to more than the centre's completed count."""

    user: AssigneeRef
    open_jobs: int
    overdue: int
    completed_this_month: int


class RecentJob(BaseModel):
    id: int
    job_number: str
    title: str
    status: JobStatus
    customer_name: str
    # Created, or its status last changed, whichever is later.
    last_activity_at: datetime


class DashboardSummary(BaseModel):
    as_of: date
    scope: DashboardScope
    jobs: JobCounts
    # Admins only (null for employees): active staff and their jobs.
    workload: list[EmployeeWorkload] | None
    unassigned_open_jobs: int | None
    recent_jobs: list[RecentJob]


class AuditEntry(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: int | None
    user: AssigneeRef | None
    details: dict[str, Any] | None
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditEntry]
    total: int
    page: int
    page_size: int
    total_pages: int
