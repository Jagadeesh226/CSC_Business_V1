"""The background worker.

Run one or more with ``uv run python -m app.workers.worker``.

Each loop: put back tasks whose lease expired (a worker crashed or hung),
run every due task, then wait for a Redis wake-up (or the poll interval).
Claiming uses ``FOR UPDATE SKIP LOCKED``, so any number of workers can run
side by side without taking the same task.

Resilience:
- Any error in a loop iteration (database restarting, pool exhausted, Redis
  gone) is logged and retried with backoff; the worker never exits on its
  own. Running it under a supervisor with a restart policy is still advised.
- A handler runs in a daemon thread with its own guarded session (it cannot
  commit) and PostgreSQL time limits on its transaction, so a hung handler
  is ended by the database and never blocks shutdown.
- Its changes are committed together with "succeeded" only while this exact
  claim still owns the task; a timed-out handler that finishes late is
  rolled back.
- While too many timed-out handlers are still running, the worker pauses
  claiming new tasks instead of exhausting database connections.

Heartbeat (Phase 5.0): a separate thread reports every
``WORKER_HEARTBEAT_SECONDS`` (default 30) that the process is alive, when
it last finished a task or a loop and how many tasks it has run, so an admin can see
a stopped or stuck worker. A failed report is logged and tried again.
"""

import logging
import os
import secrets
import signal
import socket
import threading
import time
from datetime import UTC, datetime

import redis
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.repositories import heartbeat_repository
from app.repositories import task_repository as repo
from app.services import notification_service
from app.workers import notifier, registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

log = logging.getLogger("app.worker")

MAX_BACKOFF_SECONDS = 15 * 60
MAX_ABANDONED_HANDLERS = 3
LOCK_TIMEOUT_MS = 10_000


def backoff_seconds(attempt: int) -> int:
    """30 s, 60 s, 120 s ... capped at 15 minutes."""
    return min(30 * 2 ** max(attempt - 1, 0), MAX_BACKOFF_SECONDS)


def _safe_error(exc: BaseException) -> str:
    """Only our own TaskError messages are stored; anything else could
    contain document data, so just its type is recorded."""
    if isinstance(exc, TaskError):
        return str(exc)[:500] or type(exc).__name__
    return f"Unexpected error ({type(exc).__name__})."


class HandlerSession(Session):
    """Handlers must not commit: the worker commits their changes together
    with the task's success, after checking this claim still owns the task."""

    def commit(self) -> None:
        raise RuntimeError(
            "Task handlers must not commit; return and the worker commits."
        )

    def _worker_commit(self) -> None:
        super().commit()


class _StopFlag:
    """Signal-safe stop flag.

    ``threading.Event.set`` takes a lock, so a second signal arriving while
    the first handler holds it can deadlock the process. Assigning a bool is
    atomic and lock-free; waiting sleeps in short slices instead.
    """

    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._set:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))
        return self._set


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str | None = None,
        poll_interval: float = 5.0,
        redis_client: redis.Redis | None = None,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._sessions = session_factory
        self._handler_sessions = sessionmaker(
            bind=session_factory.kw["bind"], autoflush=False, class_=HandlerSession
        )
        # A random part: containers often share a host name and pid 1.
        self.worker_id = (
            worker_id
            or f"{socket.gethostname()[:60]}:{os.getpid()}:{secrets.token_hex(3)}"
        )
        self.poll_interval = poll_interval
        self._redis = redis_client
        self._stop = _StopFlag()
        self._abandoned: list[threading.Thread] = []
        self.heartbeat_interval = heartbeat_interval
        self._tasks_done = 0
        self._last_active: float | None = None  # time.monotonic()
        self._paused = False

    # ---------- loop ----------

    def stop(self, *_: object) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("Worker %s started.", self.worker_id)
        self._report(heartbeat_repository.register, self.worker_id)
        beats = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        beats.start()
        try:
            self._loop()
        finally:
            self._report(heartbeat_repository.mark_stopped, self.worker_id)
        log.info("Worker %s stopped.", self.worker_id)

    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self.tick()
                self._wait_for_wakeup()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                failures += 1
                delay = min(self.poll_interval * 2**failures, 60.0)
                log.error(
                    "Worker loop error (%s); retrying in %.0f s.",
                    type(exc).__name__,
                    delay,
                )
                self._stop.wait(delay)

    # ---------- heartbeat ----------

    def _report(self, action, *args, **kwargs) -> bool:
        try:
            with self._sessions() as db:
                action(db, *args, **kwargs)
                db.commit()
            return True
        except Exception as exc:  # noqa: BLE001 - reported again shortly
            log.warning("Heartbeat not recorded (%s).", type(exc).__name__)
            return False

    def beat(self) -> bool:
        last = self._last_active
        return self._report(
            heartbeat_repository.beat,
            self.worker_id,
            idle_for=None if last is None else time.monotonic() - last,
            tasks_done=self._tasks_done,
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            self.beat()

    def tick(self) -> int:
        """Requeue expired leases, then run every due task. Returns count."""
        self.requeue_expired()
        self.run_maintenance()
        done = 0
        while not self._stop.is_set() and self._may_claim() and self.run_one():
            done += 1
            self._tasks_done += 1
            self._last_active = time.monotonic()
        # While claiming is paused, the worker does no work: it is not active,
        # and will show as stuck if it stays that way.
        if not self._paused:
            self._last_active = time.monotonic()
        return done

    def _may_claim(self) -> bool:
        self._abandoned = [t for t in self._abandoned if t.is_alive()]
        self._paused = len(self._abandoned) >= MAX_ABANDONED_HANDLERS
        if self._paused:
            log.warning(
                "%s timed-out handlers still running; not claiming new tasks.",
                len(self._abandoned),
            )
            return False
        return True

    def _wait_for_wakeup(self) -> None:
        block_ms = int(self.poll_interval * 1000)
        try:
            client = self._redis or notifier.get_redis(
                read_timeout=self.poll_interval + 5
            )
            notifier.ensure_group(client)
            messages = client.xreadgroup(
                notifier.GROUP,
                self.worker_id,
                {notifier.STREAM: ">"},
                count=50,
                block=block_ms,
            )
            for _, entries in messages or []:
                client.xack(notifier.STREAM, notifier.GROUP, *[e[0] for e in entries])
        except redis.TimeoutError:
            return  # nothing arrived; the database poll covers it
        except redis.RedisError as exc:
            log.warning("Redis unavailable (%s); polling.", type(exc).__name__)
            self._stop.wait(self.poll_interval)

    # ---------- one task ----------

    def requeue_expired(self) -> list[int]:
        """Returns the ids put back in the queue. Tasks that ran out of
        attempts this way fail, and their failure hooks run."""
        with self._sessions() as db:
            rows = repo.requeue_expired(db)
            db.commit()
        for row in rows:
            if row.status == "failed":
                info = TaskInfo(
                    id=row.id,
                    task_type=row.task_type,
                    payload=row.payload,
                    attempt=row.attempts,
                )
                self._run_failure_hook(info, row.last_error or "Timed out.")
        return [r.id for r in rows if r.status == "queued"]

    def run_maintenance(self) -> int:
        fixed = 0
        for job in registry.maintenance_jobs():
            try:
                with self._handler_sessions() as db:
                    db.execute(text(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}"))
                    fixed += job(db) or 0
                    db._worker_commit()
            except Exception as exc:  # noqa: BLE001 - try again next loop
                log.error("Maintenance job failed (%s).", type(exc).__name__)
        return fixed

    def run_one(self, task_id: int | None = None) -> bool:
        with self._sessions() as db:
            row = repo.claim(db, self.worker_id, task_id)
            db.commit()
        if row is None:
            return False
        info = TaskInfo(
            id=row.id,
            task_type=row.task_type,
            payload=row.payload,
            attempt=row.attempts,
            timeout_seconds=row.timeout_seconds,
        )
        spec = registry.get_spec(info.task_type)
        if spec is None:
            self._record_failure(
                info, PermanentTaskError("No handler for this task type.")
            )
            return True
        thread = threading.Thread(
            target=self._execute,
            args=(spec, info, row.timeout_seconds),
            name=f"task-{info.id}",
            daemon=True,  # never blocks shutdown
        )
        thread.start()
        thread.join(timeout=row.timeout_seconds)
        if thread.is_alive():
            # The thread's late commit is refused by the ownership check and
            # its transaction is ended by PostgreSQL's time limits.
            self._abandoned.append(thread)
            self._record_failure(info, TaskError("Timed out."))
        return True

    def _execute(self, spec: registry.TaskSpec, info: TaskInfo, timeout: int) -> None:
        try:
            with self._handler_sessions() as db:
                # Bound everything the handler does in the database: it cannot
                # outlive its task by much, wait long on locks or sit idle in
                # an open transaction while holding them.
                limit_ms = timeout * 1000
                db.execute(text(f"SET LOCAL statement_timeout = {limit_ms}"))
                db.execute(text(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}"))
                db.execute(
                    text(f"SET LOCAL idle_in_transaction_session_timeout = {limit_ms}")
                )
                result = spec.handler(db, info)
                task = repo.lock_owned(db, info.id, self.worker_id, info.attempt)
                if task is None:
                    db.rollback()  # timed out meanwhile; discard the work
                    return
                finished = db.scalar(select(func.clock_timestamp()))
                task.status = "succeeded"
                task.result = result
                task.last_error = None
                task.locked_by = None
                task.lease_expires_at = None
                task.finished_at = finished
                task.updated_at = finished
                db._worker_commit()
            log.info("Task %s (%s) succeeded.", info.id, info.task_type)
        except Exception as exc:  # noqa: BLE001 - any handler failure is recorded
            try:
                self._record_failure(info, exc)
            except Exception as record_exc:  # noqa: BLE001 - database may be down
                log.error(
                    "Could not record failure of task %s (%s); its lease will "
                    "expire and it will be retried.",
                    info.id,
                    type(record_exc).__name__,
                )

    def _record_failure(self, info: TaskInfo, exc: BaseException) -> None:
        with self._sessions() as db:
            task = repo.lock_owned(db, info.id, self.worker_id, info.attempt)
            if task is None:
                return  # already finished, timed out or taken over
            now = db.scalar(select(func.clock_timestamp()))
            permanent = isinstance(exc, PermanentTaskError)
            task.last_error = _safe_error(exc)
            task.locked_by = None
            task.lease_expires_at = None
            task.updated_at = now
            final = permanent or task.attempts >= task.max_attempts
            error = task.last_error
            if final:
                task.status = "failed"
                task.finished_at = now
            else:
                task.status = "queued"
                task.run_after = datetime.fromtimestamp(
                    now.timestamp() + backoff_seconds(task.attempts), tz=UTC
                )
            db.commit()
        if final:
            # After the failure is saved, in its own transaction: it may wait
            # for locks the timed-out handler still holds without blocking
            # the task row.
            self._run_failure_hook(info, error)
        log.warning(
            "Task %s (%s) attempt %s failed: %s",
            info.id,
            info.task_type,
            info.attempt,
            type(exc).__name__,
        )

    def _tell_admins(self, info: TaskInfo) -> None:
        """Phase 5.1: admins hear that a task failed for good (grouped per
        type and hour). Its own transaction: it never blocks the failure
        being recorded or the task's own failure hook."""
        try:
            with self._sessions() as db:
                db.execute(text(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}"))
                notification_service.task_failed(db, info.task_type)
                db.commit()
        except Exception as exc:  # noqa: BLE001 - the failure itself is saved
            log.error(
                "Could not notify admins about task %s (%s).",
                info.id,
                type(exc).__name__,
            )

    def _run_failure_hook(self, info: TaskInfo, error: str) -> None:
        self._tell_admins(info)
        spec = registry.get_spec(info.task_type)
        if spec is None or spec.on_final_failure is None:
            return
        try:
            with self._handler_sessions() as db:
                db.execute(text(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}"))
                spec.on_final_failure(db, info, error)
                db._worker_commit()
        except Exception as exc:  # noqa: BLE001 - maintenance retries later
            log.error(
                "Failure hook of task %s failed (%s); maintenance will retry.",
                info.id,
                type(exc).__name__,
            )


HEARTBEAT_RANGE = (5.0, 30.0)


def _heartbeat_seconds() -> float:
    """The overview treats 2 minutes of silence as a lost worker, so the
    interval must stay well below that."""
    raw = os.getenv("WORKER_HEARTBEAT_SECONDS", "30")
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    low, high = HEARTBEAT_RANGE
    if not low <= value <= high:
        raise SystemExit(
            f"WORKER_HEARTBEAT_SECONDS must be between {low:.0f} and {high:.0f}."
        )
    return value


def main() -> None:
    from app.db.session import get_engine
    from app.workers import handlers  # noqa: F401 - registers task handlers

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    factory = sessionmaker(bind=get_engine(), autoflush=False)
    worker = Worker(
        factory,
        poll_interval=float(os.getenv("WORKER_POLL_SECONDS", "5")),
        heartbeat_interval=_heartbeat_seconds(),
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
