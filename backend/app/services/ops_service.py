"""Health checks and the admin's view of background work (Phase 5.0).

HealthReadiness says only whether the database and Redis answer, never why not:
it needs no sign-in. The background status is for admins and holds counts,
times and worker names only, never task payloads or errors.
"""

import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from app.models import BackgroundTask, DailyRun
from app.repositories import heartbeat_repository
from app.repositories.readiness_repository import BUSINESS_TIMEZONE
from app.schemas.ops import (
    BackgroundStatus,
    DailyJobStatus,
    HealthCheckState,
    HealthReadiness,
    HealthStatus,
    TaskTypeCounts,
    WorkerState,
    WorkerStatus,
)
from app.schemas.task import TaskType
from app.services import cleanup_service, reminder_service
from app.workers import notifier

log = logging.getLogger(__name__)

# A worker reports every 30 s by default; three missed reports mean trouble.
NOT_REPORTING_AFTER = timedelta(minutes=2)
# A task is ended after its time limit (minutes at most), so a worker with
# no finished task or loop for this long is stuck.
STUCK_AFTER = timedelta(minutes=10)
# Ready to run but not started for this long: workers are missing or busy.
WAITING_TOO_LONG = timedelta(minutes=10)
# The daily jobs run on a worker's first loop of the business day.
DAILY_GRACE = timedelta(minutes=15)
# A worker that vanished longer ago than this is listed, but no longer
# raised as a problem: someone has had the chance to see it.
NOT_REPORTING_PROBLEM_FOR = timedelta(hours=1)
# HealthReadiness needs no sign-in: answers are reused briefly so frequent probes
# cost the database and Redis almost nothing.
READINESS_CACHE_SECONDS = 2.0
_READINESS_DB_TIMEOUT_MS = 2000

DAILY_JOBS = {
    reminder_service.DAILY_JOB: "Vehicle expiry check",
    cleanup_service.DAILY_JOB: "Data clean-up",
}


_cache_lock = threading.Lock()
_cached: tuple[float, HealthReadiness] | None = None


def clear_readiness_cache() -> None:
    global _cached
    _cached = None


def readiness(db: Session) -> HealthReadiness:
    """One probe at a time per process; others reuse its answer for a
    couple of seconds (or wait for it), so probes cannot pile up on the
    connection pool."""
    global _cached
    with _cache_lock:
        if _cached and time.monotonic() - _cached[0] < READINESS_CACHE_SECONDS:
            return _cached[1]
        result = _probe(db)
        _cached = (time.monotonic(), result)
        return result


def _probe(db: Session) -> HealthReadiness:
    database = HealthCheckState.OK
    try:
        db.execute(text(f"SET LOCAL statement_timeout = {_READINESS_DB_TIMEOUT_MS}"))
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log.warning("HealthReadiness: database unavailable (%s).", type(exc).__name__)
        database = HealthCheckState.UNAVAILABLE
    try:
        # Give the connection back before waiting on Redis.
        db.rollback()
    except Exception as exc:  # noqa: BLE001 - the session is closed afterwards
        log.debug("HealthReadiness: rollback failed (%s).", type(exc).__name__)
    redis_state = HealthCheckState.OK
    try:
        notifier.get_redis().ping()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log.warning("HealthReadiness: Redis unavailable (%s).", type(exc).__name__)
        redis_state = HealthCheckState.UNAVAILABLE
    if database is HealthCheckState.UNAVAILABLE:
        status = HealthStatus.UNAVAILABLE
    elif redis_state is HealthCheckState.UNAVAILABLE:
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.OK
    return HealthReadiness(status=status, database=database, redis=redis_state)


def _worker_state(row, now) -> WorkerState:
    if row.stopped_at is not None:
        return WorkerState.STOPPED
    if now - row.last_seen_at > NOT_REPORTING_AFTER:
        return WorkerState.NOT_REPORTING
    if now - (row.last_active_at or row.started_at) > STUCK_AFTER:
        return WorkerState.STUCK
    return WorkerState.RUNNING


def _queue(db: Session) -> list[TaskTypeCounts]:
    day_ago = func.now() - text("interval '1 day'")
    queued = BackgroundTask.status == "queued"
    is_due = queued & (BackgroundTask.run_after <= func.now())

    def count(condition):
        return func.count().filter(condition)

    rows = db.execute(
        select(
            BackgroundTask.task_type,
            count(queued),
            count(is_due),
            count(BackgroundTask.status == "running"),
            count(
                (BackgroundTask.status == "failed")
                & (BackgroundTask.finished_at > day_ago)
            ),
            count(
                (BackgroundTask.status == "succeeded")
                & (BackgroundTask.finished_at > day_ago)
            ),
            func.max(
                case(
                    (
                        is_due,
                        func.extract("epoch", func.now() - BackgroundTask.run_after),
                    )
                )
            ),
        )
        # Only what the counts need: unfinished work and the last day.
        .where(
            BackgroundTask.status.in_(("queued", "running"))
            | (BackgroundTask.finished_at > day_ago)
        )
        .group_by(BackgroundTask.task_type)
    ).all()
    found = {r[0]: r for r in rows}
    result = []
    for task_type in TaskType:
        r = found.get(task_type.value)
        if r is None:
            result.append(
                TaskTypeCounts(
                    task_type=task_type,
                    queued=0,
                    due=0,
                    running=0,
                    failed_last_day=0,
                    succeeded_last_day=0,
                    oldest_due_seconds=None,
                )
            )
            continue
        result.append(
            TaskTypeCounts(
                task_type=task_type,
                queued=r[1],
                due=r[2],
                running=r[3],
                failed_last_day=r[4],
                succeeded_last_day=r[5],
                oldest_due_seconds=None if r[6] is None else int(r[6]),
            )
        )
    return result


def _daily_jobs(db: Session, today, local_now) -> list[DailyJobStatus]:
    rows = {
        r.name: r
        for r in db.execute(
            select(
                DailyRun.name,
                func.max(DailyRun.business_date).label("last_date"),
                func.max(DailyRun.ran_at).label("last_ran_at"),
            )
            .where(DailyRun.name.in_(DAILY_JOBS))
            .group_by(DailyRun.name)
        )
    }
    result = []
    for name, label in DAILY_JOBS.items():
        r = rows.get(name)
        result.append(
            DailyJobStatus(
                name=name,
                label=label,
                last_business_date=r.last_date if r else None,
                last_ran_at=r.last_ran_at if r else None,
                ran_today=bool(r and r.last_date == today),
            )
        )
    return result


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _clock(db: Session):
    """The database's time, and the same moment in the business time zone."""
    return db.execute(
        select(func.now(), func.timezone(BUSINESS_TIMEZONE, func.now()))
    ).one()


def background_status(db: Session) -> BackgroundStatus:
    now, local_now = _clock(db)
    today = local_now.date()
    workers = [
        WorkerStatus(
            worker_name=w.worker_name,
            state=_worker_state(w, now),
            started_at=w.started_at,
            last_seen_at=w.last_seen_at,
            last_active_at=w.last_active_at,
            stopped_at=w.stopped_at,
            tasks_done=w.tasks_done,
        )
        for w in heartbeat_repository.recent(db)
    ]
    queue = _queue(db)
    daily = _daily_jobs(db, today, local_now)

    problems: list[str] = []
    running = [w for w in workers if w.state is WorkerState.RUNNING]
    due = sum(q.due for q in queue)
    if not running:
        waiting = f"; {_plural(due, 'task is', 'tasks are')} waiting" if due else ""
        problems.append(f"No worker is running{waiting}.")
    for w in workers:
        if w.state is WorkerState.STUCK:
            problems.append(
                f"Worker {w.worker_name} is reporting but has not finished any "
                "work for over 10 minutes."
            )
        elif (
            w.state is WorkerState.NOT_REPORTING
            and now - w.last_seen_at <= NOT_REPORTING_PROBLEM_FOR
        ):
            problems.append(
                f"Worker {w.worker_name} stopped reporting without shutting down."
            )
    oldest = max((q.oldest_due_seconds or 0 for q in queue), default=0)
    if running and oldest > WAITING_TOO_LONG.total_seconds():
        problems.append(
            f"A task has been waiting {oldest // 60} minutes to start; "
            "the workers may be overloaded."
        )
    # Some failures are expected (an unreadable scan, an unknown vehicle):
    # worth a look, but not a sign that background work is broken.
    warnings: list[str] = []
    failed = sum(q.failed_last_day for q in queue)
    if failed:
        warnings.append(
            f"{_plural(failed, 'task', 'tasks')} failed in the last 24 hours."
        )
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if local_now - midnight > DAILY_GRACE:
        for job in daily:
            if not job.ran_today:
                problems.append(f"The {job.label.lower()} has not run today.")

    return BackgroundStatus(
        checked_at=now,
        healthy=not problems,
        problems=problems,
        warnings=warnings,
        workers=workers,
        queue=queue,
        daily_jobs=daily,
    )
