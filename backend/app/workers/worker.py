"""The background worker.

Run one or more with ``uv run python -m app.workers.worker``.

Each loop: put back tasks whose lease expired (a worker crashed or hung),
run every due task, then wait for a Redis wake-up (or the poll interval).
Claiming uses ``FOR UPDATE SKIP LOCKED``, so any number of workers can run
side by side without taking the same task.

A handler runs in its own thread and session with the task's time limit.
Its database changes are committed together with "succeeded", and only if
the task is still owned by this attempt; a timed-out handler that finishes
late is rolled back.
"""

import logging
import os
import signal
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.repositories import task_repository as repo
from app.workers import notifier, registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

log = logging.getLogger("app.worker")

MAX_BACKOFF_SECONDS = 15 * 60


def backoff_seconds(attempt: int) -> int:
    """30 s, 60 s, 120 s ... capped at 15 minutes."""
    return min(30 * 2 ** max(attempt - 1, 0), MAX_BACKOFF_SECONDS)


def _safe_error(exc: BaseException) -> str:
    """Only our own TaskError messages are stored; anything else could
    contain document data, so just its type is recorded."""
    if isinstance(exc, TaskError):
        return str(exc)[:500] or type(exc).__name__
    return f"Unexpected error ({type(exc).__name__})."


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
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_interval = poll_interval
        self._redis = redis_client
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="task")

    # ---------- loop ----------

    def stop(self, *_: object) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("Worker %s started.", self.worker_id)
        while not self._stop.is_set():
            self.tick()
            self._wait_for_wakeup()
        self._pool.shutdown(wait=False, cancel_futures=True)
        log.info("Worker %s stopped.", self.worker_id)

    def tick(self) -> int:
        """Requeue expired leases, then run every due task. Returns count."""
        self.requeue_expired()
        done = 0
        while not self._stop.is_set() and self.run_one():
            done += 1
        return done

    def _wait_for_wakeup(self) -> None:
        try:
            client = self._redis or notifier.get_redis()
            notifier.ensure_group(client)
            messages = client.xreadgroup(
                notifier.GROUP,
                self.worker_id,
                {notifier.STREAM: ">"},
                count=50,
                block=int(self.poll_interval * 1000),
            )
            for _, entries in messages or []:
                client.xack(notifier.STREAM, notifier.GROUP, *[e[0] for e in entries])
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
        future = self._pool.submit(self._execute, spec, info)
        try:
            future.result(timeout=row.timeout_seconds)
        except FutureTimeout:
            # Abandon the thread: its late commit is refused by the
            # ownership check. A fresh pool keeps this worker going.
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="task")
            self._record_failure(info, TaskError("Timed out."))
        return True

    def _execute(self, spec: registry.TaskSpec, info: TaskInfo) -> None:
        try:
            with self._sessions() as db:
                result = spec.handler(db, info)
                task = repo.lock_owned(db, info.id, self.worker_id, info.attempt)
                if task is None:
                    db.rollback()  # timed out meanwhile; discard the work
                    return
                task.status = "succeeded"
                task.result = result
                task.last_error = None
                task.locked_by = None
                task.lease_expires_at = None
                task.finished_at = db.scalar(select(func.now()))
                task.updated_at = task.finished_at
                db.commit()
            log.info("Task %s (%s) succeeded.", info.id, info.task_type)
        except Exception as exc:  # noqa: BLE001 - any handler failure is recorded
            self._record_failure(info, exc)

    def _record_failure(self, info: TaskInfo, exc: BaseException) -> None:
        with self._sessions() as db:
            task = repo.lock_owned(db, info.id, self.worker_id, info.attempt)
            if task is None:
                return  # already finished, timed out or taken over
            now = db.scalar(select(func.now()))
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
