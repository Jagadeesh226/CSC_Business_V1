"""Phase 5 whole-flow tests: the back office over a day and beyond.

Each test is one story through the HTTP API with the real background
worker: work that needs a person reaches them once and settles when done;
an export is asked for, built, announced, downloaded and finally deleted;
an outage reaches the admins once, counted, and shows on their overview;
the daily clean-up later removes what is past its time and nothing else.
"""

import io
import json
from datetime import timedelta

import pytest
from flow_helpers import API, Counter, aadhaar_scan, ok, run_worker, setup_catalogue
from openpyxl import load_workbook
from sqlalchemy import func, select, text

from app.core.storage import get_storage
from app.models import (
    AuditLog,
    BackgroundTask,
    DailyRun,
    ReportExport,
    WorkerHeartbeat,
)
from app.repositories.readiness_repository import business_today
from app.services import (
    cleanup_service,
    ops_service,
    reminder_service,
    report_export_service,
)

OVERVIEW = f"{API}/tasks/overview"


@pytest.fixture(autouse=True)
def back_office_worker():
    """The export task and the daily jobs, registered as the worker does."""
    from app.workers import registry

    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    from app.workers import export_handler

    registry.register(
        export_handler.TASK_TYPE,
        export_handler.run_report_export,
        on_final_failure=export_handler.on_export_failed,
    )
    registry.register_maintenance(report_export_service.expire_files)
    registry.register_maintenance(cleanup_service.run_daily)
    registry.register_maintenance(reminder_service.run_daily)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved[0])
    registry._MAINTENANCE[:] = saved[1]


@pytest.fixture(autouse=True)
def fresh_day(db_session):
    """Nothing committed elsewhere: no workers, tasks or daily markers."""
    for table in ("worker_heartbeats", "background_tasks", "daily_runs"):
        db_session.execute(text(f"DELETE FROM {table}"))
    db_session.flush()
    ops_service.clear_readiness_cache()


@pytest.fixture
def catalogue(client):
    return setup_catalogue(client)


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya, catalogue):
    return Counter(make_client(priya), catalogue["service"])


def inbox(c, kind=None, unread=False):
    items = ok(c.get(f"{API}/notifications", params={"unread": unread}))["items"]
    return [n for n in items if kind is None or n["kind"] == kind]


def worker_reporting(db, name="counter-pc:1"):
    """A worker that has just reported in (the test worker does not)."""
    db.add(
        WorkerHeartbeat(
            worker_name=name,
            started_at=func.now() - timedelta(hours=1),
            last_seen_at=func.now(),
            last_active_at=func.now(),
        )
    )
    db.flush()


# ---------- the main story ----------


def test_a_day_of_work_reaches_the_right_person_once(
    client, desk, priya, make_user, make_client, db_session
):
    # Morning: the admin gives Priya a job; she is told, nobody else is.
    customer = desk.new_customer("Kavya Rao", "9876543210")["customer"]
    job = ok(
        client.post(
            f"{API}/jobs",
            json={
                "customer_id": customer["id"],
                "service_id": desk.service["id"],
                "title": "PAN card application",
            },
        ),
        201,
    )
    ok(
        client.post(f"{API}/jobs/{job['id']}/assignments", json={"user_id": priya.id}),
        201,
    )
    [assigned] = inbox(desk.c, unread=True)
    assert assigned["kind"] == "job_assigned"
    assert assigned["title"] == f"New job for you: {job['job_number']}"
    colleague = make_client(make_user("employee"))
    assert inbox(colleague) == [] and inbox(client) == []
    # A colleague's own job, which Priya must never see in her export.
    other_desk = Counter(colleague, desk.service)
    other_customer = other_desk.new_customer("Suresh Nair", "9123456780")["customer"]
    other_desk.open_job(other_customer["id"], "Not Priya's job")

    # The customer's Aadhaar scan is unreadable: Priya is asked to check it;
    # an expected outcome, so the admins are not alarmed.
    ok(desk.move(job["id"], "in_progress"))
    blurry = desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session, expect=1)
    [failed] = inbox(desk.c, "ocr_failed", unread=True)
    assert failed["document_id"] == blurry["id"]
    assert inbox(client, "task_failed") == []

    # She rejects it: nothing is left for her to do about it.
    ok(
        desk.c.post(
            f"{API}/documents/{blurry['id']}/reject",
            json={"reason": "Unreadable scan"},
        )
    )
    assert inbox(desk.c, "ocr_failed", unread=True) == []

    # Evening: she asks for her jobs as a spreadsheet; the worker builds it
    # with what she may see, and tells her.
    asked = ok(desk.c.post(f"{API}/exports", json={"kind": "jobs"}), 202)
    assert desk.c.get(f"{API}/exports/{asked['id']}/file").status_code == 409
    run_worker(db_session, expect=1)
    [ready] = inbox(desk.c, "export_ready", unread=True)
    assert ready["report_export_id"] == asked["id"]
    response = desk.c.get(f"{API}/exports/{asked['id']}/file")
    assert response.status_code == 200
    ws = load_workbook(io.BytesIO(response.content))["Jobs"]
    headers = [c.value for c in ws[1]]
    [row] = [dict(zip(headers, r, strict=True)) for r in ws.iter_rows(min_row=2)]
    assert row["Job no."].value == job["job_number"]
    assert row["Mobile"].value == "******3210"
    assert all(c.value != "Suresh Nair" for c in row.values())
    # Downloaded: only the job is still asking for her.
    assert [n["kind"] for n in inbox(desk.c, unread=True)] == ["job_assigned"]
    # Nobody else can fetch her file.
    assert colleague.get(f"{API}/exports/{asked['id']}/file").status_code == 404
    assert client.get(f"{API}/exports/{asked['id']}/file").status_code == 404

    # The job is done: its notice settles too.
    db_session.execute(
        text("UPDATE jobs SET status = 'submitted' WHERE id = :id"), {"id": job["id"]}
    )
    db_session.flush()
    ok(desk.move(job["id"], "completed"))
    assert desk.queue()["unread_notifications"] == 0

    # Every step is in the audit trail, with no customer details in it.
    actions = set(
        db_session.scalars(
            select(AuditLog.action).where(AuditLog.user_id == priya.id)
        ).all()
    )
    assert {
        "document_rejected",
        "export_requested",
        "export_downloaded",
    } <= actions
    trail = json.dumps(db_session.scalars(select(AuditLog.details)).all())
    assert "9876543210" not in trail


# ---------- an outage ----------


def test_an_outage_reaches_the_admins_once_and_shows_on_their_overview(
    client, desk, make_user, make_client, db_session, monkeypatch
):
    other_admin = make_client(make_user("admin"))
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    first = desk.open_job(customer["id"])
    second = desk.open_job(customer["id"], "Second job")
    worker_reporting(db_session)
    # Not in the first minutes after midnight, whatever the time now.
    monkeypatch.setattr(ops_service, "DAILY_GRACE", timedelta(seconds=-1))
    run_worker(db_session, expect=0)  # the day's jobs run on the first loop
    first_look = ok(client.get(OVERVIEW))
    assert first_look["problems"] == [] and first_look["healthy"] is True

    # The OCR service is down: both scans use up their retries.
    for job in (first, second):
        desk.upload(job, "AADHAAR", aadhaar_scan(error="temporary"))
    for _ in range(3):
        run_worker(db_session, expect=2)

    # Each admin: one notice for the hour, counting both; employees: none.
    for admin in (client, other_admin):
        [alert] = inbox(admin, "task_failed")
        assert (alert["occurrences"], alert["title"]) == (
            2,
            "2 document reading (OCR) tasks failed",
        )
    assert inbox(desk.c, "task_failed") == []
    # The uploader is asked to check both by hand.
    assert len(inbox(desk.c, "ocr_failed", unread=True)) == 2

    # The overview: background work itself is fine, the failures a warning.
    body = ok(client.get(OVERVIEW))
    assert body["healthy"] is True and body["problems"] == []
    assert body["warnings"] == ["2 tasks failed in the last 24 hours."]
    ocr = next(q for q in body["queue"] if q["task_type"] == "document_ocr")
    assert ocr["failed_last_day"] == 2
    # Employees cannot look.
    assert desk.c.get(OVERVIEW).status_code == 403

    # The admin runs one again once the service is back; it is queued.
    failed_task = db_session.scalar(
        select(BackgroundTask).where(BackgroundTask.status == "failed")
    )
    # (OCR is run again from its document, which settles the request to
    # check it by hand.)
    document_id = failed_task.payload["document_id"]
    ok(client.post(f"{API}/documents/{document_id}/ocr/retry"))
    assert len(inbox(desk.c, "ocr_failed", unread=True)) == 1

    # The worker stops reporting: the overview says so, plainly.
    db_session.execute(
        text("UPDATE worker_heartbeats SET last_seen_at = now() - interval '5 minutes'")
    )
    # (Inside the test transaction now() stands still; the retry is due.)
    db_session.execute(
        text("UPDATE background_tasks SET run_after = now() WHERE status = 'queued'")
    )
    db_session.flush()
    body = ok(client.get(OVERVIEW))
    assert body["healthy"] is False
    assert body["problems"] == [
        "No worker is running; 1 task is waiting.",
        "Worker counter-pc:1 stopped reporting without shutting down.",
    ]
    # The health check for monitors needs no sign-in and says only this.
    assert ok(client.get(f"{API}/health/ready")) == {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
    }


# ---------- days and months later ----------


def test_time_passes_and_only_working_data_goes(client, desk, priya, db_session):
    customer = desk.new_customer("Asha", "9000022222")["customer"]
    job = desk.open_job(customer["id"])
    asked = ok(desk.c.post(f"{API}/exports", json={"kind": "jobs"}), 202)
    run_worker(db_session, expect=1)
    export = db_session.get(ReportExport, asked["id"])
    stored = get_storage()._path(export.storage_key)
    [ready] = inbox(desk.c, "export_ready")
    ok(desk.c.post(f"{API}/notifications/{ready['id']}/read"))

    # A day later: the file is deleted, the record stays as history.
    export.expires_at = export.finished_at - timedelta(seconds=1)
    db_session.flush()
    assert desk.c.get(f"{API}/exports/{asked['id']}/file").status_code == 410
    run_worker(db_session, expect=0)
    db_session.refresh(export)
    assert (export.status, export.storage_key) == ("expired", None)
    assert not stored.exists()
    assert ok(desk.c.get(f"{API}/exports/{asked['id']}"))["status"] == "expired"

    # Months later (moved back in time here): the next day's clean-up
    # removes the finished task and the read notice, nothing else.
    db_session.execute(
        text(
            "UPDATE background_tasks SET finished_at = now() - interval '40 days' "
            "WHERE status = 'succeeded'"
        )
    )
    db_session.execute(
        text(
            "UPDATE notifications SET read_at = now() - interval '100 days' "
            "WHERE read_at IS NOT NULL"
        )
    )
    db_session.execute(text("DELETE FROM daily_runs WHERE name = 'data_cleanup'"))
    old_audit = db_session.scalar(select(func.count()).select_from(AuditLog))
    db_session.flush()
    run_worker(db_session, expect=0)

    assert db_session.scalar(select(func.count()).select_from(BackgroundTask)) == 0
    assert inbox(desk.c, "export_ready") == []
    # History stays: the export record, the job, the audit trail (plus one
    # entry for the clean-up itself, with counts only).
    assert db_session.get(ReportExport, asked["id"]) is not None
    assert ok(desk.c.get(f"{API}/jobs/{job['id']}"))["id"] == job["id"]
    assert (
        db_session.scalar(select(func.count()).select_from(AuditLog)) == old_audit + 1
    )
    # Today's second run (the first ran with the worker's first loop).
    cleanup = db_session.scalars(
        select(AuditLog.details)
        .where(AuditLog.action == "data_cleanup")
        .order_by(AuditLog.id.desc())
    ).first()
    counts = json.loads(cleanup)
    assert counts["background_tasks"] >= 1 and counts["notifications"] >= 1
    # Marked done for today again: it does not run a third time.
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(DailyRun)
            .where(
                DailyRun.name == "data_cleanup",
                DailyRun.business_date == business_today(db_session),
            )
        )
        == 1
    )
