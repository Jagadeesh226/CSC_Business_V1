import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import redis
from sqlalchemy import select, text, update
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import AuditLog, BackgroundTask, User
from app.repositories import task_repository
from app.services import task_queue
from app.workers import notifier, registry
from app.workers.registry import PermanentTaskError, TaskError
from app.workers.worker import Worker, backoff_seconds

# A task type with no real handler yet, so tests can register a fake one.
TASK = "vehicle_expiry_lookup"
API = "/api/v1/tasks"


@pytest.fixture
def factory(test_engine):
    """Committed sessions, like the real worker; tables cleaned afterwards."""

    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE background_tasks, worker_heartbeats, audit_logs, "
                    "user_sessions, users "
                    "RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    yield sessionmaker(bind=test_engine, autoflush=False)
    truncate()


@pytest.fixture
def handler():
    """Register a controllable handler for the test task type."""
    calls: list[int] = []
    behaviour: dict[str, object] = {"result": {"ok": True}}

    def run(db, info):
        calls.append(info.id)
        gates = behaviour.get("gates")  # per call: an Event to wait on, or None
        if gates:
            gate = gates.pop(0)
            if gate is not None:
                gate.wait(timeout=30)
        if "result_per_call" in behaviour:
            behaviour["result"] = {"call": len(calls)}
        if "commit" in behaviour:
            db.add(AuditLog(action="handler_committed", entity_type="test"))
            db.commit()
        if "write" in behaviour:  # a side effect committed with success
            db.add(
                AuditLog(action="handler_wrote", entity_type="test", entity_id=info.id)
            )
        if "raise" in behaviour:
            raise behaviour["raise"]
        return behaviour["result"]

    previous = registry.get_spec(TASK)
    registry.register(TASK, run, timeout_seconds=5, max_attempts=3)
    try:
        yield calls, behaviour
    finally:
        # Put back the real document_ocr handler for other test modules.
        registry.unregister(TASK)
        if previous is not None:
            registry._REGISTRY[TASK] = previous


def enqueue(factory, key="doc:1:v1", payload=None):
    with factory() as db:
        task_id, created = task_queue.enqueue(
            db, TASK, key, payload or {"document_id": 1}
        )
        db.commit()
    return task_id, created


def task(factory, task_id) -> BackgroundTask:
    with factory() as db:
        return db.get(BackgroundTask, task_id)


def make_due(factory, task_id):
    with factory() as db:
        db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id)
            .values(run_after=datetime.now(UTC) - timedelta(seconds=1))
        )
        db.commit()


def stream_ids(client) -> list[int]:
    return [int(f[b"task_id"]) for _, f in client.xrange(notifier.STREAM)]


# ---------- enqueue ----------


def test_enqueue_is_idempotent_and_notifies_after_commit(factory, handler, fake_redis):
    first, created = enqueue(factory)
    again, created_again = enqueue(factory)

    assert created is True and created_again is False
    assert again == first
    assert stream_ids(fake_redis) == [first]  # one wake-up, not two
    row = task(factory, first)
    assert (row.status, row.attempts, row.max_attempts, row.timeout_seconds) == (
        "queued",
        0,
        3,
        5,
    )


def test_rolled_back_enqueue_wakes_nobody(factory, handler, fake_redis):
    with factory() as db:
        task_queue.enqueue(db, TASK, "doc:2:v1", {"document_id": 2})
        db.rollback()
    assert stream_ids(fake_redis) == []
    with factory() as db:
        assert db.scalar(select(BackgroundTask)) is None


def test_unknown_task_type_is_refused(factory):
    with factory() as db, pytest.raises(ValueError):
        task_queue.enqueue(db, "send_spam", "k", {})


def test_enqueue_works_when_redis_is_down(factory, handler):
    def broken():
        raise redis.ConnectionError("down")

    notifier.set_redis_factory(broken)
    task_id, _ = enqueue(factory)  # commit still succeeds
    assert task(factory, task_id).status == "queued"
    assert Worker(factory, worker_id="w1").tick() == 1  # polling finds it


# ---------- running ----------


def test_success_commits_handler_changes_with_result(factory, handler):
    _, behaviour = handler
    behaviour["write"] = True
    task_id, _ = enqueue(factory)

    assert Worker(factory, worker_id="w1").run_one() is True

    row = task(factory, task_id)
    assert row.status == "succeeded"
    assert row.result == {"ok": True}
    assert row.attempts == 1
    assert row.finished_at is not None
    assert row.locked_by is None
    with factory() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "handler_wrote"))
    assert Worker(factory, worker_id="w1").run_one() is False  # nothing left


def test_retries_with_backoff_then_fails(factory, handler):
    calls, behaviour = handler
    behaviour["raise"] = TaskError("Provider busy")
    task_id, _ = enqueue(factory)
    worker = Worker(factory, worker_id="w1")

    worker.run_one()
    row = task(factory, task_id)
    assert (row.status, row.attempts, row.last_error) == ("queued", 1, "Provider busy")
    assert row.run_after > datetime.now(UTC) + timedelta(seconds=20)  # backed off
    assert worker.run_one() is False  # not due yet

    for _ in range(2):
        make_due(factory, task_id)
        worker.run_one()
    row = task(factory, task_id)
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.finished_at is not None
    assert len(calls) == 3


def test_permanent_error_fails_at_once(factory, handler):
    _, behaviour = handler
    behaviour["raise"] = PermanentTaskError("Document no longer exists")
    task_id, _ = enqueue(factory)
    Worker(factory, worker_id="w1").run_one()
    row = task(factory, task_id)
    assert (row.status, row.attempts, row.last_error) == (
        "failed",
        1,
        "Document no longer exists",
    )


def test_unexpected_error_messages_are_not_stored(factory, handler):
    """An exception text could contain document data; only its type is kept."""
    _, behaviour = handler
    behaviour["raise"] = ValueError("Aadhaar 1234 5678 9012 could not be parsed")
    task_id, _ = enqueue(factory)
    Worker(factory, worker_id="w1").run_one()
    assert task(factory, task_id).last_error == "Unexpected error (ValueError)."


def test_missing_handler_fails_permanently(factory, handler):
    task_id, _ = enqueue(factory)
    registry.unregister(TASK)
    Worker(factory, worker_id="w1").run_one()
    row = task(factory, task_id)
    assert (row.status, row.last_error) == ("failed", "No handler for this task type.")


def _set(factory, task_id, **values):
    with factory() as db:
        db.execute(
            update(BackgroundTask).where(BackgroundTask.id == task_id).values(**values)
        )
        db.commit()


def test_timeout_discards_late_work(factory, handler):
    """Deterministic (review L-8): the handler is held until after the
    timeout has been recorded, then released to try its late commit."""
    _, behaviour = handler
    release = threading.Event()
    behaviour["gates"] = [release]
    behaviour["write"] = True
    task_id, _ = enqueue(factory)
    _set(factory, task_id, timeout_seconds=1)
    worker = Worker(factory, worker_id="w1")

    worker.run_one()
    row = task(factory, task_id)
    assert (row.status, row.attempts, row.last_error) == ("queued", 1, "Timed out.")

    release.set()
    worker._abandoned[0].join(timeout=10)
    assert not worker._abandoned[0].is_alive()
    with factory() as db:
        assert (
            db.scalar(select(AuditLog).where(AuditLog.action == "handler_wrote"))
            is None
        )
    assert task(factory, task_id).status == "queued"


def test_stale_attempt_cannot_win_after_admin_retry(factory, handler):
    """Review M-2: attempts keep counting across retries, so a stale timed-out
    attempt can never pass for the new one."""
    from app.services import task_service

    _, behaviour = handler
    release = threading.Event()
    behaviour["gates"] = [release, None]  # first call hangs, second is quick
    behaviour["result_per_call"] = True
    task_id, _ = enqueue(factory)
    _set(factory, task_id, timeout_seconds=1, max_attempts=1)
    worker = Worker(factory, worker_id="w1")

    worker.run_one()  # attempt 1 times out -> failed (no attempts left)
    assert task(factory, task_id).status == "failed"
    with factory() as db:
        admin = User(name="A", email="m2@example.com", password_hash="x", role="admin")
        db.add(admin)
        db.flush()
        task_service.retry_task(db, task_id, admin)
    row = task(factory, task_id)
    assert (row.status, row.attempts, row.max_attempts) == ("queued", 1, 4)

    worker.run_one()  # attempt 2, same worker, succeeds
    release.set()  # now the stale attempt 1 finishes and tries to commit
    worker._abandoned[0].join(timeout=10)
    row = task(factory, task_id)
    assert (row.status, row.attempts, row.result) == ("succeeded", 2, {"call": 2})


def test_handlers_cannot_commit(factory, handler):
    """Review M-4: a handler commit would skip the ownership check."""
    _, behaviour = handler
    behaviour["commit"] = True
    task_id, _ = enqueue(factory)
    Worker(factory, worker_id="w1").run_one()
    row = task(factory, task_id)
    assert row.last_error == "Unexpected error (RuntimeError)."
    with factory() as db:
        assert (
            db.scalar(select(AuditLog).where(AuditLog.action == "handler_committed"))
            is None
        )


def test_lease_outlives_the_time_limit(factory, handler):
    """Review L-7: no second worker takes over a slow but alive handler."""
    from app.repositories.task_repository import LEASE_GRACE_SECONDS

    task_id, _ = enqueue(factory)
    with factory() as db:
        task_repository.claim(db, "w1")
        db.commit()
    row = task(factory, task_id)
    lease = (row.lease_expires_at - row.updated_at).total_seconds()
    assert lease == pytest.approx(row.timeout_seconds + LEASE_GRACE_SECONDS, abs=1)


def test_worker_keeps_running_through_database_errors(factory):
    """Review H-1: a database outage is retried, not fatal."""
    from sqlalchemy.exc import OperationalError

    worker = Worker(factory, worker_id="w1", poll_interval=0.01)
    ticks = []

    def flaky_tick():
        ticks.append(1)
        if len(ticks) <= 2:
            raise OperationalError("SELECT 1", {}, Exception("database restarting"))
        worker.stop()
        return 0

    worker.tick = flaky_tick
    worker._wait_for_wakeup = lambda: None
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(ticks) == 3  # survived two failures


def test_too_many_hung_handlers_pause_claiming(factory, handler):
    from app.workers.worker import MAX_ABANDONED_HANDLERS

    enqueue(factory)
    worker = Worker(factory, worker_id="w1")
    hold = threading.Event()
    worker._abandoned = [
        threading.Thread(target=hold.wait, daemon=True)
        for _ in range(MAX_ABANDONED_HANDLERS)
    ]
    for t in worker._abandoned:
        t.start()
    assert worker.tick() == 0  # paused: nothing claimed
    hold.set()
    for t in worker._abandoned:
        t.join(timeout=5)
    assert worker.tick() == 1


def test_expired_lease_is_requeued_or_failed(factory, handler):
    """A worker that crashed mid-task: its lease runs out and the task returns."""
    task_id, _ = enqueue(factory)
    with factory() as db:
        task_repository.claim(db, "crashed-worker")
        db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        db.commit()

    worker = Worker(factory, worker_id="w2")
    assert worker.requeue_expired() == [task_id]
    row = task(factory, task_id)
    assert (row.status, row.locked_by) == ("queued", None)
    assert "Timed out" in row.last_error

    with factory() as db:  # last attempt crashes too: now it fails
        task_repository.claim(db, "crashed-worker")
        db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id)
            .values(
                attempts=3, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        db.commit()
    assert worker.requeue_expired() == []
    assert task(factory, task_id).status == "failed"


def test_many_workers_run_each_task_exactly_once(factory, handler):
    calls, _ = handler
    ids = [enqueue(factory, key=f"doc:{i}:v1")[0] for i in range(20)]
    barrier = threading.Barrier(4)

    def work(n):
        barrier.wait(timeout=10)
        Worker(factory, worker_id=f"w{n}").tick()

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(calls) == sorted(ids)  # every task once, none twice
    with factory() as db:
        statuses = db.scalars(select(BackgroundTask.status)).all()
    assert set(statuses) == {"succeeded"}


def test_worker_wakes_on_redis_message(factory, handler, fake_redis):
    task_id, _ = enqueue(factory)
    worker = Worker(factory, worker_id="w1", poll_interval=0.1, redis_client=fake_redis)
    worker._wait_for_wakeup()  # reads and acknowledges the wake-up
    pending = fake_redis.xpending(notifier.STREAM, notifier.GROUP)
    assert pending["pending"] == 0
    assert worker.tick() == 1
    assert task(factory, task_id).status == "succeeded"


def test_worker_survives_redis_outage_while_waiting(factory, handler):
    class Down:
        def __getattr__(self, name):
            raise redis.ConnectionError("down")

    worker = Worker(factory, worker_id="w1", poll_interval=0.05, redis_client=Down())
    worker._wait_for_wakeup()  # logs and sleeps instead of crashing


def test_backoff_schedule():
    assert [backoff_seconds(a) for a in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_seconds(20) == 15 * 60


# ---------- admin API ----------


@pytest.fixture
def admin_client(factory, test_engine):
    """Admin client against committed data (the worker needs to see it)."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app
    from tests.conftest import TEST_PASSWORD

    with factory() as db:
        admin = User(
            name="Admin",
            email="tasks.admin@example.com",
            password_hash=hash_password(TEST_PASSWORD),
            role="admin",
        )
        employee = User(
            name="Emp",
            email="tasks.emp@example.com",
            password_hash=hash_password(TEST_PASSWORD),
            role="employee",
        )
        db.add_all([admin, employee])
        db.commit()

    def get_test_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    clients = {}
    for name in ("tasks.admin@example.com", "tasks.emp@example.com"):
        c = TestClient(app)
        login = c.post(
            "/api/v1/auth/login", json={"email": name, "password": TEST_PASSWORD}
        )
        c.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        clients[name.split(".")[1].split("@")[0]] = c
    try:
        yield clients
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_admin_lists_views_and_retries_failed_tasks(
    factory, handler, admin_client, fake_redis
):
    admin = admin_client["admin"]
    _, behaviour = handler
    behaviour["raise"] = PermanentTaskError("Provider rejected the file")
    task_id, _ = enqueue(factory)
    ok_id, _ = enqueue(factory, key="doc:9:v1")
    Worker(factory, worker_id="w1").run_one()  # the first fails
    del behaviour["raise"]

    failed = admin.get(API, params={"status": "failed"}).json()
    assert [t["id"] for t in failed["items"]] == [task_id]
    assert failed["items"][0]["last_error"] == "Provider rejected the file"
    assert admin.get(f"{API}/{task_id}").json()["status"] == "failed"
    assert admin.get(f"{API}/999999").status_code == 404

    not_failed = admin.post(f"{API}/{ok_id}/retry")
    assert not_failed.status_code == 422

    retried = admin.post(f"{API}/{task_id}/retry")
    assert retried.status_code == 200
    body = retried.json()
    assert (body["status"], body["attempts"], body["max_attempts"]) == ("queued", 1, 4)
    assert task_id in stream_ids(fake_redis)  # workers woken
    with factory() as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == "task_retried"))
    assert entry.entity_id == task_id

    Worker(factory, worker_id="w1").tick()
    assert task(factory, task_id).status == "succeeded"


def test_task_api_is_admin_only(admin_client):
    employee = admin_client["emp"]
    assert employee.get(API).status_code == 403
    assert employee.get(f"{API}/1").status_code == 403
    assert employee.post(f"{API}/1/retry").status_code == 403


@pytest.mark.parametrize(
    "params", [{"status": "done"}, {"task_type": "x"}, {"page": 0}]
)
def test_task_list_validation(admin_client, params):
    assert admin_client["admin"].get(API, params=params).status_code == 422


# ---------- review fixes: Redis and notifications ----------


def test_idle_redis_read_is_not_an_outage(factory):
    """Review M-3: a blocking read that simply times out means "no message"."""

    class IdleRedis:
        def xgroup_create(self, *a, **k):
            return True

        def xreadgroup(self, *a, **k):
            raise redis.TimeoutError("read timed out")

    worker = Worker(factory, worker_id="w1", poll_interval=5, redis_client=IdleRedis())
    started = time.monotonic()
    worker._wait_for_wakeup()
    assert time.monotonic() - started < 1  # no extra sleep, no "unavailable"


def test_savepoint_release_does_not_publish(factory, handler, fake_redis):
    """Review L-1: only the outermost commit wakes workers."""
    with factory() as db:
        nested = db.begin_nested()
        task_queue.enqueue(db, TASK, "doc:5:v1", {"document_id": 5})
        nested.commit()
        assert stream_ids(fake_redis) == []
        db.commit()
    assert len(stream_ids(fake_redis)) == 1


def test_malformed_redis_url_does_not_break_commit(factory, handler, monkeypatch):
    """Review L-2."""
    notifier.set_redis_factory(None)
    monkeypatch.setenv("REDIS_URL", "not a url")
    task_id, created = enqueue(factory)
    assert created and task(factory, task_id).status == "queued"


def test_limits_do_not_depend_on_handler_registration(factory, handler):
    """Review L-6: the API enqueues without importing handlers."""
    registry.unregister(TASK)  # the handler fixture restores it afterwards
    task_id, _ = enqueue(factory)
    row = task(factory, task_id)
    assert (row.timeout_seconds, row.max_attempts) == registry.limits_for(TASK)


def test_outer_rollback_after_savepoint_publishes_nothing(factory, handler, fake_redis):
    """Review L-1: a released savepoint followed by an outer rollback."""
    with factory() as db:
        nested = db.begin_nested()
        task_queue.enqueue(db, TASK, "doc:6:v1", {"document_id": 6})
        nested.commit()
        db.rollback()
    assert stream_ids(fake_redis) == []


def test_stop_is_signal_safe_and_prompt(factory):
    """Integration finding: stop() must not take a lock (a second signal
    inside the first handler could deadlock), and waiting ends quickly."""
    import signal as signal_module

    from app.workers.worker import _StopFlag

    worker = Worker(factory, worker_id="w1", poll_interval=5)
    assert isinstance(worker._stop, _StopFlag)
    # Nested call, as a second signal arriving inside the first handler would.
    worker.stop(signal_module.SIGTERM, None)
    worker.stop(signal_module.SIGTERM, None)
    assert worker._stop.is_set()

    flag = _StopFlag()
    threading.Timer(0.1, flag.set).start()
    started = time.monotonic()
    assert flag.wait(5) is True
    assert time.monotonic() - started < 1
