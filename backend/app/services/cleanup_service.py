"""Daily clean-up of working data (Phase 5.3).

Once per business day (the first worker loop after midnight in India, one
worker only), rows past their retention period are removed:

- background tasks that succeeded over RETENTION_SUCCEEDED_TASKS_DAYS ago
  (default 30) or failed over RETENTION_FAILED_TASKS_DAYS ago (90), except
  those an OCR result still names as its source;
- notifications read over RETENTION_READ_NOTIFICATIONS_DAYS ago (90);
  unread ones stay;
- sign-in sessions that expired or were signed out over
  RETENTION_ENDED_SESSIONS_DAYS ago (30);
- daily-run markers over 180 days old;
- CAPTCHA images, provider state and answers left on finished lookups.

Never touched: audit logs, customers, jobs, documents and their files,
OCR results, payments, receipts, vehicles and their dates, reminders,
exports' records. (Export files are deleted after a day by the exports'
own maintenance.)

Deletes run in batches; if a batch is full, the day's run is marked
unfinished and the next worker loop carries on. Each run is audited with
counts only.
"""

import logging
from datetime import timedelta

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    BackgroundTask,
    DailyRun,
    Notification,
    OcrResult,
    UserSession,
    VehicleLookup,
)
from app.repositories.readiness_repository import business_today
from app.services import audit_service

DAILY_JOB = "data_cleanup"
BATCH = 5000
KEEP_DAILY_RUNS_DAYS = 180
log = logging.getLogger(__name__)


def _days_ago(days: int):
    return func.now() - func.make_interval(0, 0, 0, days)


def _delete_batch(db: Session, model, condition) -> int:
    ids = select(model.id).where(condition).order_by(model.id).limit(BATCH)
    return db.execute(delete(model).where(model.id.in_(ids.scalar_subquery()))).rowcount


def _old_tasks(settings):
    return and_(
        or_(
            and_(
                BackgroundTask.status == "succeeded",
                BackgroundTask.finished_at
                < _days_ago(settings.retention_succeeded_tasks_days),
            ),
            and_(
                BackgroundTask.status == "failed",
                BackgroundTask.finished_at
                < _days_ago(settings.retention_failed_tasks_days),
            ),
        ),
        # An OCR result keeps the task that produced it.
        ~exists().where(OcrResult.task_id == BackgroundTask.id),
    )


def _old_notifications(settings):
    return and_(
        Notification.read_at.is_not(None),
        Notification.read_at < _days_ago(settings.retention_read_notifications_days),
    )


def _ended_sessions(settings):
    cutoff = _days_ago(settings.retention_ended_sessions_days)
    return or_(
        UserSession.expires_at < cutoff,
        and_(UserSession.revoked_at.is_not(None), UserSession.revoked_at < cutoff),
    )


def _clear_lookup_leftovers(db: Session) -> int:
    return db.execute(
        update(VehicleLookup)
        .where(
            VehicleLookup.status.in_(("completed", "failed", "expired")),
            or_(
                VehicleLookup.captcha_png.is_not(None),
                VehicleLookup.state_sealed.is_not(None),
                VehicleLookup.captcha_answer_sealed.is_not(None),
            ),
        )
        .values(captcha_png=None, state_sealed=None, captcha_answer_sealed=None)
    ).rowcount


def _old_daily_runs(db: Session, today) -> int:
    return db.execute(
        delete(DailyRun).where(
            DailyRun.business_date < today - timedelta(days=KEEP_DAILY_RUNS_DAYS)
        )
    ).rowcount


def run_daily(db: Session) -> int:
    """Maintenance job; returns how many rows it removed or cleared."""
    today = business_today(db)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(DAILY_JOB)))):
        return 0  # another worker is doing it right now
    claimed = db.scalar(
        insert(DailyRun)
        .values(name=DAILY_JOB, business_date=today)
        .on_conflict_do_nothing()
        .returning(DailyRun.name)
    )
    if claimed is None:
        return 0
    settings = get_settings()
    counts = {
        "background_tasks": _delete_batch(db, BackgroundTask, _old_tasks(settings)),
        "notifications": _delete_batch(db, Notification, _old_notifications(settings)),
        "user_sessions": _delete_batch(db, UserSession, _ended_sessions(settings)),
        "daily_runs": _old_daily_runs(db, today),
        "lookup_leftovers_cleared": _clear_lookup_leftovers(db),
    }
    unfinished = [
        name
        for name in ("background_tasks", "notifications", "user_sessions")
        if counts[name] >= BATCH
    ]
    if unfinished:
        # More to do: the next worker loop runs it again today.
        db.execute(
            delete(DailyRun).where(
                DailyRun.name == DAILY_JOB, DailyRun.business_date == today
            )
        )
        log.info("Data clean-up continues next loop (%s).", ", ".join(unfinished))
    audit_service.record(
        db,
        action="data_cleanup",
        entity_type="system",
        entity_id=None,
        user_id=None,
        details={**counts, "finished": not unfinished},
    )
    return sum(counts.values())
