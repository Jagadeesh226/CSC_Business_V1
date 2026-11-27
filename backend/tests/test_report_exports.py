"""Phase 5.2: Excel exports built in the background, downloadable for a day
by the person who asked, and only by them."""

import io
from datetime import timedelta

import pytest
from flow_helpers import API, Counter, ok, run_worker, setup_catalogue
from openpyxl import load_workbook
from sqlalchemy import select

from app.core.storage import get_storage
from app.models import AuditLog, BackgroundTask, Notification, ReportExport
from app.services import report_export_service

EXPORTS = f"{API}/exports"


@pytest.fixture(autouse=True)
def export_worker():
    from app.workers import registry

    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    from app.workers import export_handler

    registry.register(
        export_handler.TASK_TYPE,
        export_handler.run_report_export,
        on_final_failure=export_handler.on_export_failed,
    )
    registry.register_maintenance(report_export_service.expire_files)
    registry.register_maintenance(report_export_service.recover_stuck)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved[0])
    registry._MAINTENANCE[:] = saved[1]


@pytest.fixture(autouse=True)
def storage(document_storage):
    return document_storage


@pytest.fixture
def catalogue(client):
    return setup_catalogue(client)


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya, catalogue):
    return Counter(make_client(priya), catalogue["service"])


@pytest.fixture
def colleague(make_user, make_client, catalogue):
    return Counter(make_client(make_user("employee")), catalogue["service"])


def sheet(response, name="Jobs"):
    assert response.status_code == 200, response.text
    ws = load_workbook(io.BytesIO(response.content))[name]
    headers = [c.value for c in ws[1]]
    return [dict(zip(headers, r, strict=True)) for r in ws.iter_rows(min_row=2)]


def ask(c, **body):
    return c.post(EXPORTS, json=body)


def jobs_for(desk, *titles):
    customer = desk.new_customer("Kavya Rao", "9876543210")["customer"]
    return [desk.open_job(customer["id"], title) for title in titles]


# ---------- the main story ----------


def test_jobs_export_from_request_to_download(desk, colleague, priya, db_session):
    jobs_for(desk, "PAN card application", '=HYPERLINK("http://x")')
    jobs_for(colleague, "Not Priya's job")

    asked = ok(ask(desk.c, kind="jobs"), 202)
    assert (asked["status"], asked["kind"], asked["row_count"]) == (
        "queued",
        "jobs",
        None,
    )
    early = desk.c.get(f"{EXPORTS}/{asked['id']}/file")
    assert early.status_code == 409

    run_worker(db_session, expect=1)
    done = ok(desk.c.get(f"{EXPORTS}/{asked['id']}"))
    assert (done["status"], done["row_count"]) == ("completed", 2)
    assert done["filename"].startswith("jobs-") and done["file_size"] > 0
    assert done["expires_at"] > done["finished_at"]

    # Told it is ready, with a link to the export.
    [note] = [
        n
        for n in ok(desk.c.get(f"{API}/notifications"))["items"]
        if n["kind"] == "export_ready"
    ]
    assert note["report_export_id"] == asked["id"]
    assert note["title"] == "Your jobs export is ready"
    assert note["body"].startswith("2 rows. Download it before ")

    response = desk.c.get(f"{EXPORTS}/{asked['id']}/file")
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    )
    assert response.headers["cache-control"] == "no-store"
    rows = sheet(response)
    titles = sorted(r["Title"].value for r in rows)
    # Only her jobs; a formula is kept as inert text.
    assert titles == ['\'=HYPERLINK("http://x")', "PAN card application"]
    assert {r["Mobile"].value for r in rows} == {"******3210"}
    assert {r["Assigned to"].value for r in rows} == {"Priya Counter"}
    assert all("9876543210" not in str(c.value) for r in rows for c in r.values())

    again = ok(desk.c.get(f"{EXPORTS}/{asked['id']}"))
    assert again["download_count"] == 1 and again["last_downloaded_at"]
    assert desk.queue()["unread_notifications"] == 0  # downloaded: settled

    actions = db_session.scalars(
        select(AuditLog.action)
        .where(AuditLog.entity_type == "export", AuditLog.entity_id == asked["id"])
        .order_by(AuditLog.id)
    ).all()
    assert actions == ["export_requested", "jobs_exported", "export_downloaded"]
    assert all(
        row.user_id == priya.id
        for row in db_session.scalars(
            select(AuditLog).where(AuditLog.entity_id == asked["id"])
        )
    )


def test_an_admin_export_has_every_job(client, desk, colleague, db_session):
    jobs_for(desk, "One", "Two")
    jobs_for(colleague, "Three")
    asked = ok(ask(client, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    rows = sheet(client.get(f"{EXPORTS}/{asked['id']}/file"))
    assert len(rows) == 3


def test_jobs_filters(client, desk, db_session):
    first, _ = jobs_for(desk, "One", "Two")
    ok(desk.move(first["id"], "in_progress"))
    asked = ok(ask(client, kind="jobs", status="in_progress"), 202)
    assert asked["filters"]["status"] == "in_progress"
    run_worker(db_session, expect=1)
    [row] = sheet(client.get(f"{EXPORTS}/{asked['id']}/file"))
    assert (row["Job no."].value, row["Status"].value) == (
        first["job_number"],
        "In progress",
    )


def test_requests_are_checked(desk):
    assert ask(desk.c, kind="customers").status_code == 422
    assert ask(desk.c, kind="jobs", search="Kavya").status_code == 422  # no search
    assert ask(desk.c, kind="jobs", status="lost").status_code == 422
    assert ask(desk.c, kind="vehicles", due_within_days=999).status_code == 422


# ---------- vehicles ----------


def test_vehicles_export_in_the_background(client, desk, db_session):
    customer = desk.new_customer("Ravi Kumar", "9876543210")["customer"]
    desk.open_job(customer["id"])
    ok(
        desk.c.post(
            f"{API}/vehicles",
            json={
                "customer_id": customer["id"],
                "registration_number": "GA 07 AB 1234",
                "category": "private_four_wheeler",
                "chassis_number": "ZZ9FAKEVIN0123456",
                "owner_name": "@SUM(A1)",
            },
        ),
        201,
    )
    asked = ok(ask(desk.c, kind="vehicles", include_whatsapp=False), 202)
    run_worker(db_session, expect=1)
    [row] = sheet(desk.c.get(f"{EXPORTS}/{asked['id']}/file"), "Vehicles")
    assert row["Chassis no."].value == "****3456"
    assert row["Owner name (RC)"].value == "'@SUM(A1)"
    assert "WhatsApp" not in row


# ---------- who may see it ----------


def test_only_the_person_who_asked(client, desk, colleague, db_session):
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    for other in (colleague.c, client):  # admins too: it is her export
        assert other.get(f"{EXPORTS}/{asked['id']}").status_code == 404
        assert other.get(f"{EXPORTS}/{asked['id']}/file").status_code == 404
        assert ok(other.get(EXPORTS))["total"] == 0
    assert ok(desk.c.get(EXPORTS))["total"] == 1


def test_exports_need_sign_in(anon_client):
    assert anon_client.get(EXPORTS).status_code == 401
    assert anon_client.post(EXPORTS, json={"kind": "jobs"}).status_code in (401, 403)


def test_the_file_is_stored_encrypted(desk, db_session):
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    row = db_session.get(ReportExport, asked["id"])
    raw = get_storage()._path(row.storage_key).read_bytes()
    assert b"PK" not in raw[:4]  # not a readable zip/xlsx
    assert b"Kavya" not in raw


# ---------- limits, failures, expiry ----------


def test_three_waiting_exports_at_most(desk, colleague):
    for _ in range(3):
        ok(ask(desk.c, kind="jobs"), 202)
    refused = ask(desk.c, kind="jobs")
    assert refused.status_code == 409
    assert "3 exports" in refused.json()["detail"]
    ok(ask(colleague.c, kind="jobs"), 202)  # a limit per person


def test_too_many_rows_fails_and_says_why(client, desk, db_session, monkeypatch):
    monkeypatch.setattr(report_export_service, "MAX_ROWS", 1)
    jobs_for(desk, "One", "Two")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    failed = ok(desk.c.get(f"{EXPORTS}/{asked['id']}"))
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == (
        "More than 1 jobs match; narrow the export with filters."
    )
    assert desk.c.get(f"{EXPORTS}/{asked['id']}/file").status_code == 409
    [note] = [
        n
        for n in ok(desk.c.get(f"{API}/notifications"))["items"]
        if n["kind"] == "export_failed"
    ]
    assert note["title"] == "Your jobs export could not be made"
    assert note["body"] == failed["failure_reason"]
    # An expected outcome: admins are not alarmed.
    assert [
        n
        for n in ok(client.get(f"{API}/notifications"))["items"]
        if n["kind"] == "task_failed"
    ] == []


def test_a_deactivated_requester_gets_nothing(client, desk, priya, db_session):
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    ok(client.post(f"{API}/users/{priya.id}/deactivate"))
    run_worker(db_session, expect=1)
    row = db_session.get(ReportExport, asked["id"])
    assert (row.status, row.storage_key) == ("failed", None)
    assert "can no longer sign in" in row.failure_reason


def test_after_a_day_the_file_is_deleted(desk, db_session):
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    row = db_session.get(ReportExport, asked["id"])
    path = get_storage()._path(row.storage_key)
    assert path.exists()
    row.expires_at = row.finished_at - timedelta(seconds=1)
    db_session.flush()
    assert desk.c.get(f"{EXPORTS}/{asked['id']}/file").status_code == 410

    run_worker(db_session, expect=0)  # maintenance
    db_session.refresh(row)
    assert (row.status, row.storage_key) == ("expired", None)
    assert not path.exists()
    assert desk.c.get(f"{EXPORTS}/{asked['id']}/file").status_code == 410
    assert desk.queue()["unread_notifications"] == 0


def test_a_damaged_file_is_not_served(desk, db_session):
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    row = db_session.get(ReportExport, asked["id"])
    row.sha256 = "0" * 64
    db_session.flush()
    assert desk.c.get(f"{EXPORTS}/{asked['id']}/file").status_code == 410


def test_a_stuck_export_fails(desk, db_session):
    asked = ok(ask(desk.c, kind="jobs"), 202)
    # Its task vanished (deleted by hand) and it is old.
    for task in db_session.scalars(select(BackgroundTask)):
        db_session.delete(task)
    row = db_session.get(ReportExport, asked["id"])
    row.created_at = row.created_at - timedelta(minutes=11)
    db_session.flush()
    run_worker(db_session, expect=0)
    db_session.refresh(row)
    assert row.status == "failed"
    assert row.failure_reason == "The export stopped unexpectedly; ask for a new one."


def test_a_stale_task_does_nothing(desk, db_session):
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    row = db_session.get(ReportExport, asked["id"])
    row.status = "failed"
    row.failure_reason = "Stopped by hand."
    db_session.flush()
    run_worker(db_session, expect=1)
    task = db_session.scalar(select(BackgroundTask))
    assert task.result == {"skipped": "stale"}
    assert db_session.get(ReportExport, asked["id"]).storage_key is None


def test_payloads_hold_only_the_export_id(desk, db_session):
    asked = ok(ask(desk.c, kind="jobs", status="pending"), 202)
    task = db_session.scalar(select(BackgroundTask))
    assert task.payload == {"export_id": asked["id"]}
    assert db_session.scalars(select(Notification)).all() == []


# ---------- review fixes ----------


def files_in(root):
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_a_job_without_a_service_is_exported(client, desk, db_session):
    """Review F1: older jobs may have no service."""
    from app.models import Job

    [job] = jobs_for(desk, "Old job")
    db_session.get(Job, job["id"]).service_id = None
    db_session.flush()
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    [row] = sheet(desk.c.get(f"{EXPORTS}/{asked['id']}/file"))
    assert (row["Title"].value, row["Service"].value) == ("Old job", None)


def test_rows_are_read_in_chunks_newest_first(desk, db_session, monkeypatch):
    """Review F3/F4/F5: one list of ids, read a chunk at a time."""
    from app.services import export_service

    monkeypatch.setattr(export_service, "EXPORT_CHUNK", 2)
    made = jobs_for(desk, "One", "Two", "Three", "Four", "Five")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    rows = sheet(desk.c.get(f"{EXPORTS}/{asked['id']}/file"))
    assert [r["Job no."].value for r in rows] == [
        j["job_number"] for j in reversed(made)
    ]


def test_vehicle_dates_are_read_in_chunks(desk, db_session, monkeypatch):
    from app.services import export_service

    monkeypatch.setattr(export_service, "EXPORT_CHUNK", 1)
    customer = desk.new_customer("Ravi Kumar", "9876543210")["customer"]
    desk.open_job(customer["id"])
    for registration in ("GA 07 AB 1001", "GA 07 AB 1002", "GA 07 AB 1003"):
        vehicle = ok(
            desk.c.post(
                f"{API}/vehicles",
                json={
                    "customer_id": customer["id"],
                    "registration_number": registration,
                    "category": "private_four_wheeler",
                },
            ),
            201,
        )
        ok(
            desk.c.put(
                f"{API}/vehicles/{vehicle['id']}/expiries/insurance",
                json={"valid_until": "2027-01-31"},
            )
        )
    asked = ok(ask(desk.c, kind="vehicles"), 202)
    run_worker(db_session, expect=1)
    rows = sheet(desk.c.get(f"{EXPORTS}/{asked['id']}/file"), "Vehicles")
    assert len(rows) == 3
    assert all(r["Insurance valid until"].value is not None for r in rows)


def test_a_run_that_is_not_saved_leaves_no_file(desk, db_session, storage, monkeypatch):
    """Review F2: timed out or taken over after saving its file."""
    from app.repositories import task_repository

    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    before = files_in(storage)
    monkeypatch.setattr(task_repository, "lock_owned", lambda *a, **k: None)
    run_worker(db_session, expect=1)
    assert files_in(storage) == before
    assert db_session.get(ReportExport, asked["id"]).status == "queued"


def test_a_stale_run_deletes_its_file(desk, db_session, storage, monkeypatch):
    from sqlalchemy import update

    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    real_build = report_export_service.build

    def build_then_cancelled(db, export, user):
        result = real_build(db, export, user)
        db.execute(
            update(ReportExport)
            .where(ReportExport.id == asked["id"])
            .values(status="failed", failure_reason="Stopped by hand.")
        )
        return result

    monkeypatch.setattr(report_export_service, "build", build_then_cancelled)
    before = files_in(storage)
    run_worker(db_session, expect=1)
    assert files_in(storage) == before
    assert db_session.scalar(select(BackgroundTask)).result == {"skipped": "stale"}


def test_recovery_leaves_waiting_tasks_alone(desk, db_session):
    asked = ok(ask(desk.c, kind="jobs"), 202)
    row = db_session.get(ReportExport, asked["id"])
    row.created_at = row.created_at - timedelta(minutes=30)
    db_session.flush()
    assert report_export_service.recover_stuck(db_session) == 0
    assert row.status == "queued"


def test_the_failure_hook_leaves_a_finished_export(desk, db_session):
    from app.workers import export_handler
    from app.workers.registry import TaskInfo

    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    info = TaskInfo(
        id=1, task_type="report_export", payload={"export_id": asked["id"]}, attempt=1
    )
    export_handler.on_export_failed(db_session, info, "Timed out.")
    assert db_session.get(ReportExport, asked["id"]).status == "completed"


def test_finished_exports_do_not_count_as_waiting(desk, db_session):
    made = [ok(ask(desk.c, kind="jobs"), 202) for _ in range(3)]
    for m in made:
        db_session.get(ReportExport, m["id"]).status = "failed"
    db_session.flush()
    ok(ask(desk.c, kind="jobs"), 202)


def test_twenty_a_day(desk, db_session, monkeypatch):
    """Review F10."""
    monkeypatch.setattr(report_export_service, "MAX_PER_DAY", 2)
    for _ in range(2):
        made = ok(ask(desk.c, kind="jobs"), 202)
        db_session.get(ReportExport, made["id"]).status = "failed"
        db_session.flush()
    refused = ask(desk.c, kind="jobs")
    assert refused.status_code == 409
    assert "2 exports today" in refused.json()["detail"]


def test_corrupted_file_bytes_are_refused(desk, db_session):
    """Review F7: the storage's own check catches tampering."""
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    path = get_storage()._path(db_session.get(ReportExport, asked["id"]).storage_key)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    response = desk.c.get(f"{EXPORTS}/{asked['id']}/file")
    assert response.status_code == 410
    assert "damaged" in response.json()["detail"]


def test_a_changed_role_withdraws_the_file(client, admin_user, db_session, desk):
    """Review F6: built with admin access, no longer an admin."""
    jobs_for(desk, "One")
    asked = ok(ask(client, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    assert db_session.get(ReportExport, asked["id"]).built_role == "admin"
    admin_user.role = "employee"
    db_session.flush()
    response = client.get(f"{EXPORTS}/{asked['id']}/file")
    assert response.status_code == 410
    assert "access has changed" in response.json()["detail"]


def test_system_failures_get_a_plain_reason(desk, db_session):
    """Review F9: "Timed out." is not shown to the person."""
    asked = ok(ask(desk.c, kind="jobs"), 202)
    row = db_session.get(ReportExport, asked["id"])
    report_export_service.fail(db_session, row, "Timed out.")
    assert row.failure_reason == report_export_service.GENERIC_FAILURE
    report_export_service.fail(db_session, row, "More than 5 jobs match; narrow it.")
    assert row.failure_reason == "More than 5 jobs match; narrow it."


def test_an_undeleted_file_keeps_its_record(desk, db_session, monkeypatch):
    """Review F8: kept, to try again, rather than forgetting the file."""
    jobs_for(desk, "One")
    asked = ok(ask(desk.c, kind="jobs"), 202)
    run_worker(db_session, expect=1)
    row = db_session.get(ReportExport, asked["id"])
    row.expires_at = row.finished_at - timedelta(seconds=1)
    db_session.flush()
    storage = get_storage()
    monkeypatch.setattr(type(storage), "delete", lambda self, key: None)
    assert report_export_service.expire_files(db_session) == 0
    assert row.status == "completed" and row.storage_key is not None
