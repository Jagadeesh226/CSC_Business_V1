"""Phase 5.0: readiness check, worker heartbeats and the admin's view of
background work."""

import logging
import threading
import time
from datetime import timedelta

import pytest
import redis
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.dependencies import get_db
from app.main import app
from app.models import BackgroundTask, DailyRun, WorkerHeartbeat
from app.repositories import heartbeat_repository
from app.services import ops_service, reminder_service
from app.workers import notifier, registry
from app.workers.worker import Worker

READY = "/api/v1/health/ready"
OVERVIEW = "/api/v1/tasks/overview"
TASK = "vehicle_expiry_lookup"


def ago(minutes: float):
    return func.now() - timedelta(minutes=minutes)


def add_worker(db, name, *, seen=0.0, active=0.0, stopped=False):
    row = WorkerHeartbeat(
        worker_name=name,
        started_at=ago(60),
        last_seen_at=ago(seen),
        last_active_at=None if active is None else ago(active),
        stopped_at=ago(seen) if stopped else None,
        tasks_done=3,
    )
    db.add(row)
    db.flush()
    return row


_keys = iter(range(1_000_000))


def add_task(db, status="queued", *, due_minutes=0.0, finished_minutes=None, **kw):
    """``due_minutes`` > 0: ready that long ago; < 0: waits for a retry."""
    row = BackgroundTask(
        task_type=kw.pop("task_type", TASK),
        idempotency_key=f"ops-test:{next(_keys)}",
        payload=kw.pop("payload", {"lookup_id": 1}),
        status=status,
        attempts=1,
        max_attempts=3,
        timeout_seconds=120,
        run_after=ago(due_minutes),
        finished_at=None if finished_minutes is None else ago(finished_minutes),
        **kw,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def ran_today(db_session):
    """The daily jobs already ran today, so they raise no problem."""
    today = db_session.scalar(
        select(func.date(func.timezone("Asia/Kolkata", func.now())))
    )
    for name in ops_service.DAILY_JOBS:
        db_session.execute(
            text(
                "INSERT INTO daily_runs (name, business_date) VALUES (:n, :d) "
                "ON CONFLICT DO NOTHING"
            ),
            {"n": name, "d": today},
        )
    db_session.flush()


@pytest.fixture(autouse=True)
def fresh_readiness():
    ops_service.clear_readiness_cache()
    yield
    ops_service.clear_readiness_cache()


@pytest.fixture(autouse=True)
def no_leftover_workers(request):
    """Rows committed by other runs would look like real workers here.
    (Tests with committed sessions clean up for themselves: an open test
    transaction holding these rows would block their TRUNCATE.)"""
    if "factory" in request.fixturenames:
        return
    db_session = request.getfixturevalue("db_session")
    db_session.execute(text("DELETE FROM worker_heartbeats"))
    db_session.execute(text("DELETE FROM background_tasks"))
    db_session.flush()


# ---------- readiness ----------


def test_ready_needs_no_sign_in_and_says_only_ok(anon_client):
    response = anon_client.get(READY)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


def test_redis_down_is_degraded_not_down(anon_client):
    class Broken:
        def ping(self):
            raise redis.ConnectionError("connection refused at 10.0.0.5:6379")

    notifier.set_redis_factory(lambda: Broken())
    response = anon_client.get(READY)
    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "database": "ok",
        "redis": "unavailable",
    }
    assert "10.0.0.5" not in response.text


def test_database_down_is_503_without_details(anon_client):
    class DeadSession:
        def execute(self, *_):
            raise ConnectionError("password authentication failed for csc")

    app.dependency_overrides[get_db] = lambda: DeadSession()
    response = anon_client.get(READY)
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["database"] == "unavailable"
    assert "password" not in response.text


def test_readiness_answers_are_reused_briefly(anon_client):
    pings = []

    class Counting:
        def ping(self):
            pings.append(1)

    notifier.set_redis_factory(lambda: Counting())
    for _ in range(5):
        assert anon_client.get(READY).status_code == 200
    assert len(pings) == 1
    ops_service.clear_readiness_cache()
    anon_client.get(READY)
    assert len(pings) == 2


def test_readiness_gives_the_connection_back_before_redis():
    """Review F1: a slow Redis must not hold a database connection."""
    calls = []

    class Session_:
        def execute(self, statement):
            calls.append(str(statement))

        def rollback(self):
            calls.append("rollback")

    class SlowRedis:
        def ping(self):
            calls.append("ping")

    notifier.set_redis_factory(lambda: SlowRedis())
    ops_service.readiness(Session_())
    assert calls == [
        "SET LOCAL statement_timeout = 2000",
        "SELECT 1",
        "rollback",
        "ping",
    ]


# ---------- overview: who may see it ----------


def test_overview_is_for_admins_only(employee_client, anon_client):
    assert anon_client.get(OVERVIEW).status_code == 401
    assert employee_client.get(OVERVIEW).status_code == 403


def test_overview_route_does_not_clash_with_task_ids(client):
    assert client.get(OVERVIEW).status_code == 200
    assert client.get("/api/v1/tasks/999999").status_code == 404


# ---------- overview: workers ----------


def test_no_worker_is_a_problem_and_says_what_waits(client, db_session, ran_today):
    body = client.get(OVERVIEW).json()
    assert body["healthy"] is False
    assert body["problems"] == ["No worker is running."]

    add_task(db_session, due_minutes=1)
    add_task(db_session, due_minutes=-5)  # a retry later: not waiting yet
    assert client.get(OVERVIEW).json()["problems"] == [
        "No worker is running; 1 task is waiting."
    ]


def test_healthy_when_a_worker_runs_and_nothing_is_wrong(client, db_session, ran_today):
    add_worker(db_session, "host-a:101", seen=0.2, active=0.5)
    body = client.get(OVERVIEW).json()
    assert body["problems"] == []
    assert body["healthy"] is True
    [worker] = body["workers"]
    assert (worker["worker_name"], worker["state"], worker["tasks_done"]) == (
        "host-a:101",
        "running",
        3,
    )


def test_worker_states(client, db_session, ran_today):
    add_worker(db_session, "ok:1", seen=0.1, active=1)
    add_worker(db_session, "gone:2", seen=5, active=6)
    add_worker(db_session, "stuck:3", seen=0.1, active=25)
    add_worker(db_session, "done:4", seen=3, active=4, stopped=True)
    add_worker(db_session, "new:5", seen=0.1, active=None)  # just started
    old = add_worker(db_session, "old:6", seen=60 * 30)  # over a day ago
    body = client.get(OVERVIEW).json()
    states = {w["worker_name"]: w["state"] for w in body["workers"]}
    assert states == {
        "ok:1": "running",
        "gone:2": "not_reporting",
        "stuck:3": "stuck",
        "done:4": "stopped",
        "new:5": "stuck",  # started an hour ago and never finished a loop
    }
    assert old.worker_name not in states
    assert sorted(body["problems"]) == [
        "Worker gone:2 stopped reporting without shutting down.",
        "Worker new:5 is reporting but has not finished any work for over 10 minutes.",
        (
            "Worker stuck:3 is reporting but has not finished any work for over "
            "10 minutes."
        ),
    ]


def test_a_long_lost_worker_is_listed_but_no_longer_a_problem(
    client, db_session, ran_today
):
    """Review F3: a crashed worker does not keep the overview red all day."""
    add_worker(db_session, "w:new", seen=0.1, active=0.1)
    add_worker(db_session, "w:crashed", seen=90, active=91)
    body = client.get(OVERVIEW).json()
    states = {w["worker_name"]: w["state"] for w in body["workers"]}
    assert states["w:crashed"] == "not_reporting"
    assert body["problems"] == []


def test_waiting_tasks_with_no_worker_say_so_once(client, db_session, ran_today):
    add_task(db_session, due_minutes=30)
    assert client.get(OVERVIEW).json()["problems"] == [
        "No worker is running; 1 task is waiting."
    ]


def test_a_stopped_worker_alone_means_no_worker(client, db_session, ran_today):
    add_worker(db_session, "done:1", seen=1, active=1, stopped=True)
    assert client.get(OVERVIEW).json()["problems"] == ["No worker is running."]


# ---------- overview: the queue ----------


def test_queue_counts_by_type(client, db_session, ran_today):
    add_worker(db_session, "w:1", seen=0.1, active=0.1)
    add_task(db_session, due_minutes=3)
    add_task(db_session, due_minutes=1)
    add_task(db_session, due_minutes=-10)  # retry later
    add_task(db_session, "running")
    add_task(db_session, "failed", finished_minutes=60)
    add_task(db_session, "failed", finished_minutes=60 * 30)  # over a day ago
    add_task(db_session, "succeeded", finished_minutes=5)
    add_task(db_session, "succeeded", task_type="document_ocr", finished_minutes=1)

    body = client.get(OVERVIEW).json()
    queue = {q["task_type"]: q for q in body["queue"]}
    assert set(queue) == {
        "document_ocr",
        "vehicle_expiry_lookup",
        "vehicle_mobile_lookup",
        "report_export",
    }
    lookup = queue["vehicle_expiry_lookup"]
    assert {k: v for k, v in lookup.items() if k != "oldest_due_seconds"} == {
        "task_type": "vehicle_expiry_lookup",
        "queued": 3,
        "due": 2,
        "running": 1,
        "failed_last_day": 1,
        "succeeded_last_day": 1,
    }
    assert 170 <= lookup["oldest_due_seconds"] <= 190
    assert queue["document_ocr"]["succeeded_last_day"] == 1
    assert queue["vehicle_mobile_lookup"] == {
        "task_type": "vehicle_mobile_lookup",
        "queued": 0,
        "due": 0,
        "running": 0,
        "failed_last_day": 0,
        "succeeded_last_day": 0,
        "oldest_due_seconds": None,
    }
    # Expected failures (an unreadable scan) are a warning, not ill health.
    assert body["problems"] == []
    assert body["warnings"] == ["1 task failed in the last 24 hours."]
    assert body["healthy"] is True


def test_a_task_waiting_long_with_a_worker_running(client, db_session, ran_today):
    add_worker(db_session, "w:1", seen=0.1, active=0.1)
    add_task(db_session, due_minutes=30)
    assert client.get(OVERVIEW).json()["problems"] == [
        "A task has been waiting 30 minutes to start; the workers may be overloaded."
    ]


def test_overview_shows_no_task_contents(client, db_session, ran_today):
    add_task(
        db_session,
        "failed",
        finished_minutes=1,
        payload={"note": "RAVI-KUMAR-9876543210"},
        last_error="Portal said: chassis ZZ9FAKEVIN0123456",
    )
    text_ = client.get(OVERVIEW).text
    assert "RAVI" not in text_ and "ZZ9FAKEVIN" not in text_
    assert "ops-test:" not in text_  # nor idempotency keys


# ---------- overview: daily jobs ----------


def test_daily_check_not_run_today_is_a_problem(
    client, db_session, ran_today, monkeypatch
):
    add_worker(db_session, "w:1", seen=0.1, active=0.1)
    db_session.execute(
        text("DELETE FROM daily_runs WHERE name = :n"),
        {"n": reminder_service.DAILY_JOB},
    )
    db_session.add(
        DailyRun(
            name=reminder_service.DAILY_JOB,
            business_date=func.date(
                func.timezone("Asia/Kolkata", func.now()) - text("interval '1 day'")
            ),
        )
    )
    db_session.flush()
    # Not in the first minutes after midnight, whatever the time now.
    monkeypatch.setattr(ops_service, "DAILY_GRACE", timedelta(seconds=-1))
    body = client.get(OVERVIEW).json()
    job, cleanup = body["daily_jobs"]
    assert cleanup["label"] == "Data clean-up"
    assert (job["label"], job["ran_today"]) == ("Vehicle expiry check", False)
    assert job["last_business_date"] is not None
    assert body["problems"] == ["The vehicle expiry check has not run today."]


def test_the_business_date_is_indian_time(client, db_session, monkeypatch):
    """At 02:00 in India it is still yesterday in UTC: the daily check
    belongs to the Indian date."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    local = datetime(2026, 11, 27, 2, 0)
    utc_now = local.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
    assert utc_now.date().isoformat() == "2026-11-26"
    monkeypatch.setattr(ops_service, "_clock", lambda db: (utc_now, local))
    add_worker(db_session, "w:1", seen=0.1, active=0.1)
    db_session.execute(
        text("DELETE FROM daily_runs WHERE name = :n"),
        {"n": reminder_service.DAILY_JOB},
    )
    db_session.add(
        DailyRun(name=reminder_service.DAILY_JOB, business_date=local.date())
    )
    db_session.flush()
    job = client.get(OVERVIEW).json()["daily_jobs"][0]
    assert job["ran_today"] is True


def test_daily_check_ran_today(client, db_session, ran_today, monkeypatch):
    add_worker(db_session, "w:1", seen=0.1, active=0.1)
    monkeypatch.setattr(ops_service, "DAILY_GRACE", timedelta(seconds=-1))
    body = client.get(OVERVIEW).json()
    assert body["daily_jobs"][0]["ran_today"] is True
    assert body["problems"] == []


# ---------- heartbeats from a real worker ----------


@pytest.fixture
def factory(test_engine):
    """Committed sessions, like the real worker; tables cleaned afterwards."""

    def clean():
        with test_engine.begin() as conn:
            conn.execute(text("TRUNCATE worker_heartbeats"))
            conn.execute(
                text("DELETE FROM background_tasks WHERE idempotency_key LIKE 'ops:%'")
            )

    clean()
    yield sessionmaker(bind=test_engine, autoflush=False)
    clean()


@pytest.fixture
def quick_task():
    previous = registry.get_spec(TASK)
    registry.register(TASK, lambda db, info: {"ok": True}, timeout_seconds=5)
    try:
        yield
    finally:
        registry.unregister(TASK)
        if previous is not None:
            registry._REGISTRY[TASK] = previous


def heartbeat(factory, name) -> WorkerHeartbeat | None:
    with factory() as db:
        return db.scalar(
            select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == name)
        )


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_a_running_worker_reports_and_says_when_it_stops(factory, quick_task):
    from app.services import task_queue

    with factory() as db:
        task_queue.enqueue(db, TASK, "ops:hb:1", {"lookup_id": 1})
        task_queue.enqueue(db, TASK, "ops:hb:2", {"lookup_id": 2})
        db.commit()
    worker = Worker(
        factory, worker_id="hb-test:1", poll_interval=0.01, heartbeat_interval=0.05
    )
    worker._wait_for_wakeup = lambda: time.sleep(0.01)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        assert wait_for(
            lambda: (
                (row := heartbeat(factory, "hb-test:1")) is not None
                and row.tasks_done == 2
                and row.last_active_at is not None
            )
        )
        row = heartbeat(factory, "hb-test:1")
        assert row.stopped_at is None
        first_seen = row.last_seen_at
        assert wait_for(
            lambda: heartbeat(factory, "hb-test:1").last_seen_at > first_seen
        )
    finally:
        worker.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert heartbeat(factory, "hb-test:1").stopped_at is not None


def test_an_idle_loop_counts_as_activity(factory):
    """Review F10: a worker with nothing to do is not stuck."""
    worker = Worker(factory, worker_id="hb-test:idle")
    assert worker.tick() == 0
    assert worker._last_active is not None
    worker.beat()
    assert heartbeat(factory, "hb-test:idle").last_active_at is not None


def test_a_paused_worker_is_not_active(factory):
    """Review F5: with too many hung handlers the worker claims nothing,
    so it must not look busy."""
    from app.workers.worker import MAX_ABANDONED_HANDLERS

    gate = threading.Event()
    hung = [
        threading.Thread(target=gate.wait, daemon=True)
        for _ in range(MAX_ABANDONED_HANDLERS)
    ]
    for t in hung:
        t.start()
    try:
        worker = Worker(factory, worker_id="hb-test:paused")
        worker._abandoned = list(hung)
        worker._last_active = 123.0
        worker.tick()
        assert worker._last_active == 123.0
    finally:
        gate.set()


def test_a_crashing_loop_still_marks_the_worker_stopped(factory):
    worker = Worker(factory, worker_id="hb-test:crash", heartbeat_interval=5)

    def crash():
        raise KeyboardInterrupt

    worker._loop = crash
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever()
    worker.stop()
    assert heartbeat(factory, "hb-test:crash").stopped_at is not None


def test_default_worker_names_are_unique():
    """Review F8: containers often share a host name and pid 1."""
    first, second = Worker(sessionmaker()), Worker(sessionmaker())
    assert first.worker_id != second.worker_id
    assert len(first.worker_id) <= 100


@pytest.mark.parametrize("value", ["0", "4", "31", "120", "soon"])
def test_heartbeat_interval_must_suit_the_overview(monkeypatch, value):
    from app.workers.worker import _heartbeat_seconds

    monkeypatch.setenv("WORKER_HEARTBEAT_SECONDS", value)
    with pytest.raises(SystemExit):
        _heartbeat_seconds()


def test_heartbeat_interval_default(monkeypatch):
    from app.workers.worker import _heartbeat_seconds

    monkeypatch.delenv("WORKER_HEARTBEAT_SECONDS", raising=False)
    assert _heartbeat_seconds() == 30.0


def test_restart_under_the_same_name_starts_afresh(factory):
    worker = Worker(factory, worker_id="hb-test:2")
    with factory() as db:
        heartbeat_repository.register(db, "hb-test:2")
        heartbeat_repository.beat(db, "hb-test:2", idle_for=1, tasks_done=7)
        heartbeat_repository.mark_stopped(db, "hb-test:2")
        db.commit()
    assert worker._report(heartbeat_repository.register, "hb-test:2")
    row = heartbeat(factory, "hb-test:2")
    assert (row.stopped_at, row.tasks_done, row.last_active_at) == (None, 0, None)


def test_a_beat_after_its_row_was_removed_reports_again(factory):
    worker = Worker(factory, worker_id="hb-test:3")
    worker._tasks_done = 4
    assert worker.beat() is True
    row = heartbeat(factory, "hb-test:3")
    assert row.tasks_done == 4
    assert row.last_active_at is None  # it has not finished a loop yet


def test_idle_time_is_reported_from_the_database_clock(factory):
    worker = Worker(factory, worker_id="hb-test:4")
    worker._last_active = time.monotonic() - 120
    worker.beat()
    with factory() as db:
        idle = db.scalar(
            select(
                func.extract("epoch", func.now() - WorkerHeartbeat.last_active_at)
            ).where(WorkerHeartbeat.worker_name == "hb-test:4")
        )
    assert 115 <= idle <= 130


def test_a_failed_beat_is_logged_not_raised(factory, caplog):
    def broken():
        raise ConnectionError("database restarting")

    caplog.set_level(logging.WARNING, logger="app.worker")
    worker = Worker(factory, worker_id="hb-test:5")
    worker._sessions = broken
    assert worker.beat() is False
    assert "Heartbeat not recorded (ConnectionError)" in caplog.text


def test_workers_gone_for_a_week_are_removed(db_session):
    add_worker(db_session, "recent:1", seen=60 * 24 * 6)
    add_worker(db_session, "ancient:2", seen=60 * 24 * 8)
    assert heartbeat_repository.prune(db_session) == 1
    names = db_session.scalars(select(WorkerHeartbeat.worker_name)).all()
    assert names == ["recent:1"]


def test_housekeeping_runs_in_the_worker_loop():
    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    try:
        import importlib

        from app.workers import ops_jobs

        importlib.reload(ops_jobs)
        assert heartbeat_repository.prune in registry.maintenance_jobs()
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved[0])
        registry._MAINTENANCE[:] = saved[1]
