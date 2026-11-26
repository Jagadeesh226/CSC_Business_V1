"""Health and background-work monitoring (Phase 5.0)."""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

from app.schemas.task import TaskType


class HealthCheckState(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class HealthStatus(StrEnum):
    OK = "ok"
    # Redis is down: the API works and workers poll the database, slower.
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class HealthReadiness(BaseModel):
    status: HealthStatus
    database: HealthCheckState
    redis: HealthCheckState


class WorkerState(StrEnum):
    RUNNING = "running"
    # Reporting, but no task or loop finished for a long time.
    STUCK = "stuck"
    # Stopped reporting without saying it stopped (crashed, killed, cut off).
    NOT_REPORTING = "not_reporting"
    STOPPED = "stopped"


class WorkerStatus(BaseModel):
    worker_name: str
    state: WorkerState
    started_at: datetime
    last_seen_at: datetime
    last_active_at: datetime | None
    stopped_at: datetime | None
    tasks_done: int


class TaskTypeCounts(BaseModel):
    task_type: TaskType
    queued: int
    # Queued and ready to run now (the rest wait for a retry time).
    due: int
    running: int
    failed_last_day: int
    succeeded_last_day: int
    # How long the oldest due task has been ready, in seconds.
    oldest_due_seconds: int | None


class DailyJobStatus(BaseModel):
    name: str
    label: str
    last_business_date: date | None
    last_ran_at: datetime | None
    ran_today: bool


class BackgroundStatus(BaseModel):
    checked_at: datetime
    healthy: bool
    # Plain sentences for the admin, most urgent first; empty when healthy.
    problems: list[str]
    # Worth a look, but not a sign that background work is broken (such as
    # tasks that failed for good: a scan nobody could read).
    warnings: list[str]
    workers: list[WorkerStatus]
    queue: list[TaskTypeCounts]
    daily_jobs: list[DailyJobStatus]
