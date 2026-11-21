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
"""

import logging
import os
import signal
import socket
import threading
from datetime import UTC, datetime

import redis
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.repositories import task_repository as repo
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


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str | None = None,
        poll_interval: float = 5.0,
        redis_client: redis.Redis | None = None,
    ) -> None:
        self._sessions = session_factory
        self._handler_sessions = sessionmaker(
            bind=session_factory.kw["bind"], autoflush=False, class_=HandlerSession
        )
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_interval = poll_interval
        self._redis = redis_client
        self._stop = threading.Event()
        self._abandoned: list[threading.Thread] = []

    # ---------- loop ----------

    def stop(self, *_: object) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("Worker %s started.", self.worker_id)
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
        log.info("Worker %s stopped.", self.worker_id)

    def tick(self) -> int:
        """Requeue expired leases, then run every due task. Returns count."""
        self.requeue_expired()
        done = 0
        while not self._stop.is_set() and self._may_claim() and self.run_one():
            done += 1
        return done

    def _may_claim(self) -> bool:
        self._abandoned = [t for t in self._abandoned if t.is_alive()]
        if len(self._abandoned) >= MAX_ABANDONED_HANDLERS:
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
        with self._sessions() as db:
            ids = repo.requeue_expired(db)
            db.commit()
        return ids

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
            if permanent or task.attempts >= task.max_attempts:
                task.status = "failed"
                task.finished_at = now
            else:
                task.status = "queued"
                task.run_after = datetime.fromtimestamp(
                    now.timestamp() + backoff_seconds(task.attempts), tz=UTC
                )
            db.commit()
        log.warning(
            "Task %s (%s) attempt %s failed: %s",
            info.id,
            info.task_type,
            info.attempt,
            type(exc).__name__,
        )


def main() -> None:
    from app.db.session import get_engine
    from app.workers import handlers  # noqa: F401 - registers task handlers

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    factory = sessionmaker(bind=get_engine(), autoflush=False)
    worker = Worker(factory, poll_interval=float(os.getenv("WORKER_POLL_SECONDS", "5")))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
