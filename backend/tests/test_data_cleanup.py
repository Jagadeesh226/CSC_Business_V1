"""Phase 5.3: the daily clean-up removes working data past its retention
period, and nothing else."""

import itertools
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.core.config import Settings, get_settings
from app.models import (
    AuditLog,
    BackgroundTask,
    DailyRun,
    Document,
    Notification,
    OcrResult,
    UserSession,
    Vehicle,
    VehicleLookup,
)
from app.repositories.readiness_repository import business_today
from app.services import cleanup_service

_n = itertools.count(1)


def ago(days: float):
    return func.now() - timedelta(days=days)


@pytest.fixture(autouse=True)
def fresh_day(db_session):
    """No clean-up has run today, and no committed rows from elsewhere."""
    db_session.execute(text("DELETE FROM daily_runs WHERE name = 'data_cleanup'"))
    db_session.execute(text("DELETE FROM background_tasks"))
    db_session.flush()


def task(db, status="succeeded", finished_days=None):
    row = BackgroundTask(
        task_type="document_ocr",
        idempotency_key=f"cleanup-test:{next(_n)}",
        payload={"document_id": 1},
        status=status,
        attempts=1,
        max_attempts=3,
        timeout_seconds=120,
        finished_at=None if finished_days is None else ago(finished_days),
    )
    db.add(row)
    db.flush()
    return row


def note(db, user, read_days=None):
    row = Notification(
        user_id=user.id,
        kind="job_assigned",
        event_key=f"cleanup-test:{next(_n)}",
        title="New job for you",
        body="A job.",
        read_at=None if read_days is None else ago(read_days),
    )
    db.add(row)
    db.flush()
    return row


def session_row(db, user, expires_days, revoked_days=None):
    row = UserSession(
        user_id=user.id,
        token_hash=f"{next(_n):064d}",
        csrf_token_hash="0" * 64,
        expires_at=func.now() + timedelta(days=expires_days),
        revoked_at=None if revoked_days is None else ago(revoked_days),
    )
    db.add(row)
    db.flush()
    return row


def alive(db, model, row_id) -> bool:
    return db.scalar(select(func.count()).where(model.id == row_id)) == 1


def run(db):
    removed = cleanup_service.run_daily(db)
    db.flush()
    return removed


# ---------- what goes ----------


def test_old_finished_tasks_go(db_session):
    old_ok = task(db_session, "succeeded", finished_days=31)
    new_ok = task(db_session, "succeeded", finished_days=29)
    old_failed = task(db_session, "failed", finished_days=91)
    new_failed = task(db_session, "failed", finished_days=60)  # still kept
    # Retried after an old failure: it keeps the old finish time.
    waiting = task(db_session, "queued", finished_days=100)
    running = task(db_session, "running", finished_days=100)
    run(db_session)
    assert not alive(db_session, BackgroundTask, old_ok.id)
    assert not alive(db_session, BackgroundTask, old_failed.id)
    for kept in (new_ok, new_failed, waiting, running):
        assert alive(db_session, BackgroundTask, kept.id)


def test_a_task_named_by_an_ocr_result_stays(db_session, make_customer):
    customer = make_customer()
    document = Document(
        customer_id=customer.id,
        filename="aadhaar.png",
        storage_key="ab/" + "0" * 32,
        size_bytes=10,
        sha256="0" * 64,
        current_version=1,
        status="verified",
    )
    db_session.add(document)
    db_session.flush()
    source = task(db_session, "succeeded", finished_days=400)
    db_session.add(
        OcrResult(
            document_id=document.id,
            document_version=1,
            task_id=source.id,
            status="completed",
            provider="fake",
            provider_version="1",
            field_specs=[],
            confidences={},
        )
    )
    db_session.flush()
    run(db_session)
    assert alive(db_session, BackgroundTask, source.id)


def test_only_read_notifications_go(db_session, employee_user):
    old_read = note(db_session, employee_user, read_days=91)
    new_read = note(db_session, employee_user, read_days=60)
    unread = note(db_session, employee_user)
    unread.created_at = ago(400)
    db_session.flush()
    run(db_session)
    assert not alive(db_session, Notification, old_read.id)
    assert alive(db_session, Notification, new_read.id)
    assert alive(db_session, Notification, unread.id)


def test_ended_sessions_go(db_session, employee_user):
    expired_long_ago = session_row(db_session, employee_user, expires_days=-31)
    signed_out_long_ago = session_row(
        db_session, employee_user, expires_days=5, revoked_days=31
    )
    expired_lately = session_row(db_session, employee_user, expires_days=-10)
    signed_out_lately = session_row(
        db_session, employee_user, expires_days=5, revoked_days=10
    )
    active = session_row(db_session, employee_user, expires_days=1)
    run(db_session)
    assert not alive(db_session, UserSession, expired_long_ago.id)
    assert not alive(db_session, UserSession, signed_out_long_ago.id)
    for kept in (expired_lately, signed_out_lately, active):
        assert alive(db_session, UserSession, kept.id)


def test_captcha_leftovers_are_cleared_on_finished_lookups(db_session, make_customer):
    customer = make_customer()
    vehicle = Vehicle(
        customer_id=customer.id,
        registration_number="GA 07 AB 1234",
        registration_key="GA07AB1234",
        category="private_four_wheeler",
    )
    db_session.add(vehicle)
    db_session.flush()
    finished = VehicleLookup(
        vehicle_id=vehicle.id,
        status="completed",
        captcha_png=b"png",
        state_sealed=b"state",
        captcha_answer_sealed=b"answer",
    )
    db_session.add(finished)
    db_session.flush()
    others = [
        VehicleLookup(
            vehicle_id=vehicle.id,
            status=status,
            captcha_png=b"png",
            state_sealed=b"state",
            captcha_answer_sealed=b"answer",
        )
        for status in ("failed", "expired")
    ]
    db_session.add_all(others)
    waiting = VehicleLookup(
        vehicle_id=vehicle.id, status="waiting_for_captcha", captcha_png=b"png"
    )
    db_session.add(waiting)
    db_session.flush()
    run(db_session)
    db_session.refresh(finished)
    db_session.refresh(waiting)
    assert (
        finished.captcha_png,
        finished.state_sealed,
        finished.captcha_answer_sealed,
    ) == (None, None, None)
    for other in others:
        db_session.refresh(other)
        assert (other.captcha_png, other.state_sealed) == (None, None)
    assert waiting.captcha_png == b"png"  # someone is about to answer it


def test_old_daily_markers_go(db_session):
    today = business_today(db_session)
    old = DailyRun(name="vehicle_expiry_check", business_date=today - timedelta(181))
    recent = DailyRun(name="vehicle_expiry_check", business_date=today - timedelta(179))
    db_session.add_all([old, recent])
    db_session.flush()
    run(db_session)
    dates = db_session.scalars(
        select(DailyRun.business_date).where(DailyRun.name == "vehicle_expiry_check")
    ).all()
    assert today - timedelta(181) not in dates
    assert today - timedelta(179) in dates


# ---------- what stays ----------


def test_audit_logs_are_never_removed(db_session):
    old = AuditLog(action="document_viewed", entity_type="document", entity_id=1)
    db_session.add(old)
    db_session.flush()
    old.created_at = ago(4000)
    db_session.flush()
    run(db_session)
    assert alive(db_session, AuditLog, old.id)


# ---------- how it runs ----------


def test_once_a_day_and_audited_with_counts(db_session, employee_user):
    task(db_session, "succeeded", finished_days=40)
    note(db_session, employee_user, read_days=100)
    assert run(db_session) == 2
    assert run(db_session) == 0  # already ran today
    [entry] = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "data_cleanup")
    ).all()
    assert entry.user_id is None and entry.entity_type == "system"
    assert json.loads(entry.details) == {
        "background_tasks": 1,
        "notifications": 1,
        "user_sessions": 0,
        "daily_runs": 0,
        "lookup_leftovers_cleared": 0,
        "finished": True,
    }
    assert db_session.scalar(
        select(DailyRun).where(
            DailyRun.name == "data_cleanup",
            DailyRun.business_date == business_today(db_session),
        )
    )


def test_a_big_backlog_continues_on_the_next_loop(db_session, monkeypatch):
    monkeypatch.setattr(cleanup_service, "BATCH", 2)
    today = business_today(db_session)
    db_session.execute(
        text(
            "INSERT INTO daily_runs (name, business_date) "
            "VALUES ('vehicle_expiry_check', :d) ON CONFLICT DO NOTHING"
        ),
        {"d": today},
    )
    for _ in range(5):
        task(db_session, "succeeded", finished_days=40)
    assert run(db_session) == 2
    assert run(db_session) == 2  # not marked done: carries on
    assert run(db_session) == 1
    assert run(db_session) == 0
    finished = [
        json.loads(d)["finished"]
        for d in db_session.scalars(
            select(AuditLog.details)
            .where(AuditLog.action == "data_cleanup")
            .order_by(AuditLog.id)
        )
    ]
    assert finished == [False, False, True]
    # Only its own marker is taken back: the reminder check stays done.
    assert db_session.scalar(
        select(DailyRun).where(
            DailyRun.name == "vehicle_expiry_check", DailyRun.business_date == today
        )
    )


@pytest.mark.parametrize("kind", ["notifications", "sessions"])
def test_every_table_can_continue(db_session, employee_user, monkeypatch, kind):
    monkeypatch.setattr(cleanup_service, "BATCH", 2)
    for _ in range(3):
        if kind == "notifications":
            note(db_session, employee_user, read_days=100)
        else:
            session_row(db_session, employee_user, expires_days=-40)
    assert run(db_session) == 2
    assert run(db_session) == 1  # carried on the same day
    assert run(db_session) == 0


def test_another_worker_running_it_is_left_alone(db_session, test_engine):
    task(db_session, "succeeded", finished_days=40)
    with test_engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(hashtext('data_cleanup'))"))
        try:
            assert run(db_session) == 0
        finally:
            other.execute(text("SELECT pg_advisory_unlock(hashtext('data_cleanup'))"))


def test_retention_periods_come_from_settings(db_session, monkeypatch):
    kept = task(db_session, "succeeded", finished_days=10)
    monkeypatch.setenv("RETENTION_SUCCEEDED_TASKS_DAYS", "7")
    get_settings.cache_clear()
    try:
        run(db_session)
    finally:
        get_settings.cache_clear()
    assert not alive(db_session, BackgroundTask, kept.id)


@pytest.mark.parametrize("value", ["0", "6", "4000"])
def test_retention_periods_have_safe_bounds(monkeypatch, value):
    monkeypatch.setenv("RETENTION_READ_NOTIFICATIONS_DAYS", value)
    with pytest.raises(ValidationError):
        Settings()


def test_it_runs_in_the_worker_loop():
    from app.workers import registry

    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    try:
        import importlib

        from app.workers import ops_jobs

        importlib.reload(ops_jobs)
        assert cleanup_service.run_daily in registry.maintenance_jobs()
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved[0])
        registry._MAINTENANCE[:] = saved[1]


# ---------- review fixes ----------


def reminder_with_note(db, make_customer, user, status, read_days):
    from app.models import VehicleReminder

    customer = make_customer()
    vehicle = Vehicle(
        customer_id=customer.id,
        registration_number=f"GA 07 AB {next(_n):04d}",
        registration_key=f"GA07AB{next(_n):04d}",
        category="private_four_wheeler",
        assigned_to=user.id,
    )
    db.add(vehicle)
    db.flush()
    reminder = VehicleReminder(
        vehicle_id=vehicle.id,
        expiry_type="insurance",
        valid_until=business_today(db) - timedelta(days=80),
        status=status,
    )
    db.add(reminder)
    db.flush()
    row = Notification(
        user_id=user.id,
        kind="vehicle_expiry",
        reminder_id=reminder.id,
        title="GA 07 AB: insurance",
        body="Insurance expires.",
        read_at=ago(read_days),
    )
    db.add(row)
    db.flush()
    return reminder, row


def test_an_open_reminder_keeps_its_read_notification(
    db_session, make_customer, employee_user
):
    """Review M-1: deleting it would make the daily reminder check send it
    again, unread, every three months."""
    from app.services import reminder_service

    reminder, kept = reminder_with_note(
        db_session, make_customer, employee_user, "open", read_days=100
    )
    _, gone = reminder_with_note(
        db_session, make_customer, employee_user, "done", read_days=100
    )
    run(db_session)
    assert alive(db_session, Notification, kept.id)
    assert not alive(db_session, Notification, gone.id)
    # And the reminder check finds its notification, so sends nothing new.
    db_session.execute(
        text("DELETE FROM daily_runs WHERE name = :n"),
        {"n": reminder_service.DAILY_JOB},
    )
    db_session.flush()
    reminder_service.run_daily(db_session)
    db_session.flush()
    assert (
        db_session.scalar(
            select(func.count()).where(Notification.reminder_id == reminder.id)
        )
        == 1
    )
