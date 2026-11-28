"""Counts for the dashboard (Phase 6.0). Dates are the centre's calendar
(India); ``visible_to`` limits everything to an employee's jobs."""

from datetime import date

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.orm import Session

from app.models import Customer, Job, JobAssignment, JobStatusHistory, User
from app.repositories.job_repository import _actively_assigned
from app.repositories.readiness_repository import BUSINESS_TIMEZONE
from app.schemas.job import FINAL_STATUSES


def _local_date(column) -> ColumnElement:
    return func.date(func.timezone(BUSINESS_TIMEZONE, column))


def _scope(visible_to: int | None) -> list[ColumnElement[bool]]:
    return [] if visible_to is None else [_actively_assigned(visible_to)]


def job_counts(db: Session, today: date, visible_to: int | None) -> dict[str, int]:
    month_start = today.replace(day=1)
    created = _local_date(Job.created_at)
    is_open = Job.status.not_in(FINAL_STATUSES)

    def count(condition):
        return func.count().filter(condition)

    row = db.execute(
        select(
            count(created == today).label("created_today"),
            count(created >= month_start).label("created_this_month"),
            count(Job.status == "pending").label("pending"),
            count(Job.status == "in_progress").label("in_progress"),
            count(Job.status == "waiting_for_documents").label("waiting_for_documents"),
            count(Job.status == "submitted").label("submitted"),
            count(and_(is_open, Job.due_date < today)).label("overdue"),
            count(and_(is_open, Job.due_date == today)).label("due_today"),
        )
        .select_from(Job)
        .where(*_scope(visible_to))
    ).one()
    changed = _local_date(JobStatusHistory.changed_at)
    closed = db.execute(
        select(
            count(
                and_(JobStatusHistory.new_status == "completed", changed == today)
            ).label("completed_today"),
            count(
                and_(
                    JobStatusHistory.new_status == "completed",
                    changed >= month_start,
                )
            ).label("completed_this_month"),
            count(
                and_(
                    JobStatusHistory.new_status == "cancelled",
                    changed >= month_start,
                )
            ).label("cancelled_this_month"),
        )
        .select_from(JobStatusHistory)
        .join(Job, Job.id == JobStatusHistory.job_id)
        .where(changed >= month_start, *_scope(visible_to))
    ).one()
    return {**row._asdict(), **closed._asdict()}


def workload(db: Session, today: date) -> list[tuple]:
    """(user, open jobs, overdue, completed this month) for active staff."""
    month_start = today.replace(day=1)
    is_open = Job.status.not_in(FINAL_STATUSES)
    completed_this_month = (
        select(func.count())
        .select_from(JobStatusHistory)
        .join(JobAssignment, JobAssignment.job_id == JobStatusHistory.job_id)
        .where(
            JobAssignment.user_id == User.id,
            JobAssignment.unassigned_at.is_(None),
            JobStatusHistory.new_status == "completed",
            _local_date(JobStatusHistory.changed_at) >= month_start,
        )
        .scalar_subquery()
    )
    return db.execute(
        select(
            User,
            func.count(Job.id).filter(is_open),
            func.count(Job.id).filter(and_(is_open, Job.due_date < today)),
            completed_this_month,
        )
        .outerjoin(
            JobAssignment,
            and_(
                JobAssignment.user_id == User.id,
                JobAssignment.unassigned_at.is_(None),
            ),
        )
        .outerjoin(Job, Job.id == JobAssignment.job_id)
        .where(User.is_active.is_(True))
        .group_by(User.id)
        .order_by(User.name, User.id)
    ).all()


def unassigned_open(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status.not_in(FINAL_STATUSES),
                ~select(JobAssignment.id)
                .where(
                    JobAssignment.job_id == Job.id,
                    JobAssignment.unassigned_at.is_(None),
                )
                .exists(),
            )
        )
        or 0
    )


def recent_jobs(db: Session, visible_to: int | None, limit: int = 10) -> list[tuple]:
    last_change = (
        select(func.max(JobStatusHistory.changed_at))
        .where(JobStatusHistory.job_id == Job.id)
        .scalar_subquery()
    )
    activity = func.greatest(Job.created_at, func.coalesce(last_change, Job.created_at))
    return db.execute(
        select(Job, Customer.name, activity.label("last_activity_at"))
        .join(Customer, Customer.id == Job.customer_id)
        .where(*_scope(visible_to))
        .order_by(activity.desc(), Job.id.desc())
        .limit(limit)
    ).all()
