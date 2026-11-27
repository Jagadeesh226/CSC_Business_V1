"""Phase 5 whole-flow tests: the back office over a day and beyond.

Each test is one story through the HTTP API with the real background
worker: work that needs a person reaches them once and settles when done;
an export is asked for, built, announced, downloaded and finally deleted;
an outage reaches the admins once, counted, and shows on their overview;
the daily clean-up later removes what is past its time and nothing else.

Inside the test transaction ``now()`` stands still, so "days later" is
made by moving recorded times back.
"""

import importlib
import io
import json
import time
from datetime import timedelta

import pytest
from flow_helpers import API, Counter, aadhaar_scan, ok, run_worker, setup_catalogue
from openpyxl import load_workbook
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.storage import get_storage
from app.models import (
    AuditLog,
    BackgroundTask,
    DailyRun,
    OcrResult,
    ReportExport,
)
from app.repositories import heartbeat_repository
from app.repositories.readiness_repository import business_today
from app.services import (
    cleanup_service,
    ops_service,
    reminder_service,
    report_export_service,
)
from app.workers.worker import Worker

OVERVIEW = f"{API}/tasks/overview"


@pytest.fixture(autouse=True)
def production_worker():
    """Tasks and maintenance registered as the production worker does
    (app.workers.handlers), and put back afterwards."""
    from app.workers import registry

    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    from app.workers import export_handler, ocr_handler, ops_jobs, reminder_jobs

    registry.register(
        ocr_handler.TASK_TYPE,
        ocr_handler.run_document_ocr,
        on_final_failure=ocr_handler.on_ocr_failed,
    )
    registry.register(
        export_handler.TASK_TYPE,
        export_handler.run_report_export,
        on_final_failure=export_handler.on_export_failed,
    )
    registry._MAINTENANCE[:] = []
    registry.register_maintenance(ocr_handler.recover_stuck_documents)
    registry.register_maintenance(report_export_service.expire_files)
    registry.register_maintenance(report_export_service.recover_stuck)
    # These modules only register: reloading them uses their real lines.
    importlib.reload(reminder_jobs)
    importlib.reload(ops_jobs)
    jobs = registry.maintenance_jobs()
    for job in (
        reminder_service.run_daily,
        cleanup_service.run_daily,
        heartbeat_repository.prune,
    ):
        assert job in jobs, job
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


def kinds(c, unread=True):
    return sorted(n["kind"] for n in inbox(c, unread=unread))


def counter_pc(db_session, name="counter-pc:1") -> Worker:
    """The counter PC's worker reporting in, through its own heartbeat
    code, on the test's connection."""
    factory = sessionmaker(
        bind=db_session.connection(),
        join_transaction_mode="create_savepoint",
        autoflush=False,
    )
    worker = Worker(factory, worker_id=name)
    assert worker._report(heartbeat_repository.register, name)
    # Started a while ago and quiet since (now() stands still in the test),
    # so only its own beat shows it running.
    db_session.execute(
        text(
            "UPDATE worker_heartbeats SET started_at = now() - interval '1 hour', "
            "last_seen_at = now() - interval '5 minutes' WHERE worker_name = :n"
        ),
        {"n": name},
    )
    worker._last_active = time.monotonic()
    assert worker.beat()
    return worker


def admin_job(client, desk, customer, title="PAN card application"):
    return ok(
        client.post(
            f"{API}/jobs",
            json={
                "customer_id": customer["id"],
                "service_id": desk.service["id"],
                "title": title,
            },
        ),
        201,
    )


def assign(client, job, user_id):
    ok(
        client.post(f"{API}/jobs/{job['id']}/assignments", json={"user_id": user_id}),
        201,
    )


# ---------- the main story ----------


def test_a_day_of_work_reaches_the_right_person_once(
    client, desk, priya, make_user, make_client, db_session
):
    # Morning: the admin gives Priya a job; she is told, nobody else is.
    customer = desk.new_customer("Kavya Rao", "9876543210")["customer"]
    job = admin_job(client, desk, customer)
    assign(client, job, priya.id)
    [assigned] = inbox(desk.c, unread=True)
    assert assigned["kind"] == "job_assigned"
    assert assigned["title"] == f"New job for you: {job['job_number']}"
    colleague = make_client(make_user("employee"))

    def nobody_else_told():
        assert inbox(colleague) == [] and inbox(client) == []

    nobody_else_told()
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
    nobody_else_told()

    # She rejects it: nothing is left for her to do about it.
    ok(
        desk.c.post(
            f"{API}/documents/{blurry['id']}/reject",
            json={"reason": "Unreadable scan"},
        )
    )
    assert kinds(desk.c) == ["job_assigned"]

    # Evening: she asks for her jobs as a spreadsheet; the worker builds it
    # with what she may see, and tells her (only her).
    asked = ok(desk.c.post(f"{API}/exports", json={"kind": "jobs"}), 202)
    assert desk.c.get(f"{API}/exports/{asked['id']}/file").status_code == 409
    run_worker(db_session, expect=1)
    [ready] = inbox(desk.c, "export_ready", unread=True)
    assert ready["report_export_id"] == asked["id"]
    nobody_else_told()
    response = desk.c.get(f"{API}/exports/{asked['id']}/file")
    assert response.status_code == 200
    ws = load_workbook(io.BytesIO(response.content))["Jobs"]
    headers = [c.value for c in ws[1]]
    [row] = [dict(zip(headers, r, strict=True)) for r in ws.iter_rows(min_row=2)]
    assert row["Job no."].value == job["job_number"]
    assert row["Mobile"].value == "******3210"
    assert all(c.value != "Suresh Nair" for c in row.values())
    assert kinds(desk.c) == ["job_assigned"]  # downloaded: settled
    # Nobody else can fetch her file.
    assert colleague.get(f"{API}/exports/{asked['id']}/file").status_code == 404
    assert client.get(f"{API}/exports/{asked['id']}/file").status_code == 404

    # The customer's second copy is unreadable too; the admin submits the
    # application anyway (an audited override) and Priya completes it.
    # Closing the job settles everything still asking her.
    ok(
        desk.c.post(
            f"{API}/documents/{blurry['id']}/versions",
            files={"file": ("a.png", aadhaar_scan(error="unsupported"), "image/png")},
            data={"change_reason": "Second copy"},
        ),
        201,
    )
    run_worker(db_session, expect=1)
    assert kinds(desk.c) == ["job_assigned", "ocr_failed"]
    ok(
        client.post(
            f"{API}/jobs/{job['id']}/status",
            json={
                "status": "submitted",
                "override_document_check": True,
                "reason": "Verified at the counter from the original card",
            },
        )
    )
    ok(desk.move(job["id"], "completed"))
    assert kinds(desk.c) == []
    nobody_else_told()

    # Every step is in the audit trail, with no customer details in it.
    actions = set(db_session.scalars(select(AuditLog.action)).all())
    assert {
        "job_assigned",
        "document_ocr_failed",
        "document_rejected",
        "document_version_added",
        "document_check_overridden",
        "job_status_changed",
        "export_requested",
        "jobs_exported",
        "export_downloaded",
    } <= actions
    trail = json.dumps(db_session.scalars(select(AuditLog.details)).all())
    for detail in ("9876543210", "Kavya", "Suresh", "9123456780"):
        assert detail not in trail


# ---------- an outage ----------


def test_an_outage_reaches_the_admins_once_and_shows_on_their_overview(
    client, desk, make_user, make_client, db_session, monkeypatch
):
    other_admin = make_client(make_user("admin"))
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    first = desk.open_job(customer["id"])
    second = desk.open_job(customer["id"], "Second job")
    counter_pc(db_session)
    # Not in the first minutes after midnight, whatever the time now.
    monkeypatch.setattr(ops_service, "DAILY_GRACE", timedelta(seconds=-1))
    run_worker(db_session, expect=0)  # the day's jobs run on the first loop
    first_look = ok(client.get(OVERVIEW))
    assert first_look["problems"] == [] and first_look["healthy"] is True
    assert [w["state"] for w in first_look["workers"]] == ["running"]
    assert {j["name"]: j["ran_today"] for j in first_look["daily_jobs"]} == {
        "vehicle_expiry_check": True,
        "data_cleanup": True,
    }

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
    assert (ocr["failed_last_day"], ocr["queued"]) == (2, 0)
    # Employees cannot look.
    assert desk.c.get(OVERVIEW).status_code == 403

    # The admin has OCR run again for one document (from the document:
    # that settles the request to check it by hand). It waits in the queue.
    failed_task = db_session.scalar(
        select(BackgroundTask).where(BackgroundTask.status == "failed")
    )
    document_id = failed_task.payload["document_id"]
    ok(client.post(f"{API}/documents/{document_id}/ocr/retry"))
    assert len(inbox(desk.c, "ocr_failed", unread=True)) == 1
    ocr = next(
        q for q in ok(client.get(OVERVIEW))["queue"] if q["task_type"] == "document_ocr"
    )
    assert ocr["queued"] == 1

    # The worker stops reporting (a while later): the overview says so.
    db_session.execute(
        text("UPDATE worker_heartbeats SET last_seen_at = now() - interval '5 minutes'")
    )
    # (now() stands still in the test, so the retry's time is set to it.)
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
    # Shut down properly instead, it is simply stopped.
    worker = Worker(
        sessionmaker(
            bind=db_session.connection(), join_transaction_mode="create_savepoint"
        ),
        worker_id="counter-pc:1",
    )
    assert worker._report(heartbeat_repository.mark_stopped, "counter-pc:1")
    body = ok(client.get(OVERVIEW))
    assert [w["state"] for w in body["workers"]] == ["stopped"]
    assert body["problems"] == ["No worker is running; 1 task is waiting."]
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
    # A readable Aadhaar (its OCR task stays as the result's source) and an
    # export, both done by the worker.
    desk.upload(job, "AADHAAR", aadhaar_scan(name="Asha"))
    asked = ok(desk.c.post(f"{API}/exports", json={"kind": "jobs"}), 202)
    run_worker(db_session, expect=2)
    export = db_session.get(ReportExport, asked["id"])
    stored = get_storage()._path(export.storage_key)
    [ready] = inbox(desk.c, "export_ready")
    ok(desk.c.post(f"{API}/notifications/{ready['id']}/read"))
    # A job given to her long ago that she never opened: unread, it stays.
    old_job = admin_job(client, desk, customer, "Old job")
    assign(client, old_job, priya.id)
    [unread_old] = inbox(desk.c, "job_assigned", unread=True)

    # A day later: the file is deleted, the record stays as history.
    export.expires_at = export.finished_at - timedelta(seconds=1)
    db_session.flush()
    assert desk.c.get(f"{API}/exports/{asked['id']}/file").status_code == 410
    run_worker(db_session, expect=0)
    db_session.refresh(export)
    assert (export.status, export.storage_key) == ("expired", None)
    assert not stored.exists()
    assert ok(desk.c.get(f"{API}/exports/{asked['id']}"))["status"] == "expired"

    # Months later (times moved back here): every finished task 40 days
    # old, read notices 100 days old; plus two tasks not old enough.
    ocr_task_id = db_session.scalar(select(OcrResult.task_id))
    assert ocr_task_id is not None
    export_task_id = db_session.scalar(
        select(BackgroundTask.id).where(BackgroundTask.task_type == "report_export")
    )
    db_session.execute(
        text(
            "UPDATE background_tasks SET finished_at = now() - interval '40 days' "
            "WHERE status = 'succeeded'"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO background_tasks (task_type, idempotency_key, payload, "
            "status, attempts, max_attempts, timeout_seconds, finished_at) VALUES "
            "('report_export', 'flow:recent-ok', '{}', 'succeeded', 1, 1, 600, "
            "now() - interval '10 days'), "
            "('report_export', 'flow:failed-40', '{}', 'failed', 1, 1, 600, "
            "now() - interval '40 days')"
        )
    )
    db_session.execute(
        text(
            "UPDATE notifications SET read_at = now() - interval '100 days' "
            "WHERE read_at IS NOT NULL"
        )
    )
    db_session.execute(
        text("UPDATE notifications SET created_at = now() - interval '200 days'")
    )
    db_session.execute(text("DELETE FROM daily_runs WHERE name = 'data_cleanup'"))
    db_session.flush()
    audit_before = db_session.scalar(select(func.count()).select_from(AuditLog))
    run_worker(db_session, expect=0)

    # Gone: the export's finished task and its read notice. Kept: the OCR
    # task its result names, the recent task, the failed one (90 days), and
    # the unread notice however old.
    left = set(db_session.scalars(select(BackgroundTask.idempotency_key)))
    assert export_task_id not in set(db_session.scalars(select(BackgroundTask.id)))
    assert ocr_task_id in set(db_session.scalars(select(BackgroundTask.id)))
    assert {"flow:recent-ok", "flow:failed-40"} <= left
    assert inbox(desk.c, "export_ready") == []
    assert [n["id"] for n in inbox(desk.c, unread=True)] == [unread_old["id"]]
    # History stays: the export record, the jobs, the audit trail, plus one
    # entry for the clean-up itself, with counts only.
    assert db_session.get(ReportExport, asked["id"]) is not None
    assert ok(desk.c.get(f"{API}/jobs/{job['id']}"))["id"] == job["id"]
    assert (
        db_session.scalar(select(func.count()).select_from(AuditLog))
        == audit_before + 1
    )
    cleanup = db_session.scalars(
        select(AuditLog.details)
        .where(AuditLog.action == "data_cleanup")
        .order_by(AuditLog.id.desc())
    ).first()
    assert json.loads(cleanup) == {
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

    # Later loops the same day do not run it again.
    runs = db_session.scalar(
        select(func.count()).where(AuditLog.action == "data_cleanup")
    )
    run_worker(db_session, expect=0)
    assert (
        db_session.scalar(select(func.count()).where(AuditLog.action == "data_cleanup"))
        == runs
    )
