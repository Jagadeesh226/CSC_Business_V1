"""Phase 5.1: work notifications. The right person hears once when work
needs them, and the notification settles when it no longer does."""

from datetime import timedelta

import pytest
from flow_helpers import API, Counter, aadhaar_scan, ok, run_worker, setup_catalogue
from sqlalchemy import select

from app.models import Document, Notification, VehicleLookup
from app.services import notification_service


@pytest.fixture
def catalogue(client):
    return setup_catalogue(client)


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya, catalogue):
    return Counter(make_client(priya), catalogue["service"])


def notes(c, kind=None, unread=False):
    items = ok(c.get(f"{API}/notifications", params={"unread": unread}))["items"]
    return [n for n in items if kind is None or n["kind"] == kind]


def assign(client, job, user_id):
    return ok(
        client.post(f"{API}/jobs/{job['id']}/assignments", json={"user_id": user_id}),
        201,
    )


def unread_of(c):
    return ok(c.get(f"{API}/work-queue"))["unread_notifications"]


# ---------- jobs ----------


def test_assigned_by_someone_else(client, desk, priya, make_user, make_client):
    customer = client.post(
        f"{API}/customers", json={"name": "Kavya Rao", "phone": "9000044444"}
    ).json()["customer"]
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
    assign(client, job, priya.id)

    [note] = notes(desk.c, "job_assigned")
    assert note["title"] == f"New job for you: {job['job_number']}"
    assert note["body"] == "PAN card application for Kavya Rao."
    assert (note["job_id"], note["read_at"], note["occurrences"]) == (
        job["id"],
        None,
        1,
    )
    assert unread_of(desk.c) == 1
    # The admin who assigned is not told, nor is anyone else.
    assert notes(client, "job_assigned") == []
    other = make_client(make_user("employee"))
    assert notes(other) == []

    # Taken off the job: the notification settles (kept, marked read);
    # a colleague still on the job keeps theirs.
    colleague = make_user("employee")
    assign(client, job, colleague.id)
    ok(client.delete(f"{API}/jobs/{job['id']}/assignments/{priya.id}"))
    [note] = notes(desk.c, "job_assigned")
    assert note["read_at"] is not None
    assert unread_of(desk.c) == 0
    assert unread_of(make_client(colleague)) == 1

    # Put back on later: a new assignment, a new notification.
    assign(client, job, priya.id)
    assert len(notes(desk.c, "job_assigned", unread=True)) == 1
    assert len(notes(desk.c, "job_assigned")) == 2


def test_no_notification_for_assigning_yourself(client, desk, admin_user):
    customer = desk.new_customer("Asha", "9000055555")["customer"]
    desk.open_job(customer["id"])  # the creator is assigned to it
    assert notes(desk.c) == []
    job = desk.open_job(customer["id"], "Second job")
    assign(client, job, admin_user.id)
    assert notes(client, "job_assigned") == []


def test_closing_the_job_settles_it(client, desk, priya):
    customer = desk.new_customer("Asha", "9000055555")["customer"]
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
    assign(client, job, priya.id)
    assert unread_of(desk.c) == 1
    ok(client.post(f"{API}/jobs/{job['id']}/cancel", json={"reason": "Customer left"}))
    assert unread_of(desk.c) == 0


# ---------- OCR ----------


def test_unreadable_scan_tells_the_uploader(
    client, desk, priya, make_user, make_client, db_session
):
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    colleague = make_user("employee")
    assign(client, job, colleague.id)
    blurry = desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session, expect=1)

    [note] = notes(desk.c, "ocr_failed")
    assert note["document_id"] == blurry["id"]
    assert note["title"] == "Could not read: Aadhaar card for Meena"
    assert "by hand" in note["body"]
    # Only the person who uploaded it; the colleague hears about the job only.
    assert [n["kind"] for n in notes(make_client(colleague))] == ["job_assigned"]

    # Recording the failure again (maintenance, a repeated hook) adds nothing.
    notification_service.ocr_failed(
        db_session, db_session.get(Document, blurry["id"]), 1, priya.id
    )
    db_session.flush()
    assert len(notes(desk.c, "ocr_failed")) == 1

    # Rejected: nothing left to do about the failed reading.
    ok(
        desk.c.post(
            f"{API}/documents/{blurry['id']}/reject", json={"reason": "Unreadable"}
        )
    )
    assert notes(desk.c, "ocr_failed")[0]["read_at"] is not None


def test_a_new_copy_settles_the_failed_reading(desk, db_session):
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    blurry = desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session, expect=1)
    assert len(notes(desk.c, "ocr_failed", unread=True)) == 1
    ok(
        desk.c.post(
            f"{API}/documents/{blurry['id']}/versions",
            files={"file": ("a.png", aadhaar_scan(name="Meena"), "image/png")},
            data={"change_reason": "Clear copy"},
        ),
        201,
    )
    assert notes(desk.c, "ocr_failed", unread=True) == []


def test_notifications_hold_no_document_contents(desk, db_session):
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session, expect=1)
    text_ = desk.c.get(f"{API}/notifications").text
    assert "1234 5678 9012" not in text_ and "FAKEOCR" not in text_


# ---------- vehicle lookups ----------


@pytest.fixture
def lookup_worker():
    from app.workers import registry

    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    from app.workers import lookup_handler

    registry.register(
        lookup_handler.TASK_TYPE,
        lookup_handler.run_vehicle_lookup,
        on_final_failure=lookup_handler.on_lookup_failed,
    )
    registry.register_maintenance(lookup_handler.expire_unanswered)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved[0])
    registry._MAINTENANCE[:] = saved[1]


def vehicle_lookup(desk, registration):
    customer = desk.new_customer("Ravi Kumar", "9876543210")["customer"]
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
    return ok(desk.c.post(f"{API}/vehicles/{vehicle['id']}/lookups"), 202)


def test_a_captcha_waits_for_the_person_who_asked(
    lookup_worker, client, desk, db_session
):
    started = vehicle_lookup(desk, "GA 07 CAP 321")
    run_worker(db_session, expect=1)
    [note] = notes(desk.c, "lookup_captcha")
    assert note["vehicle_lookup_id"] == started["id"]
    assert note["title"] == "Type the CAPTCHA for GA 07 CAP 321"
    assert notes(client, "lookup_captcha") == []

    url = f"{API}/vehicle-lookups/{started['id']}/captcha"
    # Answering settles it; a wrong answer's next CAPTCHA adds nothing,
    # the person is at the screen.
    ok(desk.c.post(url, json={"answer": "WXYZ"}), 202)
    assert unread_of(desk.c) == 0
    run_worker(db_session, expect=1)
    assert len(notes(desk.c, "lookup_captcha")) == 1
    assert unread_of(desk.c) == 0
    ok(desk.c.post(url, json={"answer": "ABCD"}), 202)
    run_worker(db_session, expect=1)
    assert notes(desk.c, "lookup_captcha")[0]["read_at"] is not None


def test_an_expired_captcha_settles(lookup_worker, desk, db_session):
    started = vehicle_lookup(desk, "GA 07 CAP 322")
    run_worker(db_session, expect=1)
    assert unread_of(desk.c) == 1
    row = db_session.get(VehicleLookup, started["id"])
    row.captcha_expires_at = row.created_at - timedelta(minutes=1)
    db_session.flush()
    run_worker(db_session, expect=0)  # maintenance expires it
    assert db_session.get(VehicleLookup, started["id"]).status == "expired"
    assert unread_of(desk.c) == 0


def test_a_failed_lookup_settles(lookup_worker, desk, db_session):
    started = vehicle_lookup(desk, "GA 07 CAP 323")
    run_worker(db_session, expect=1)
    url = f"{API}/vehicle-lookups/{started['id']}/captcha"
    from app.workers.lookup_handler import MAX_ROUNDS

    for _ in range(MAX_ROUNDS - 1):
        ok(desk.c.post(url, json={"answer": "WRONG"}), 202)
        run_worker(db_session, expect=1)
    assert db_session.get(VehicleLookup, started["id"]).status == "failed"
    assert unread_of(desk.c) == 0


# ---------- background tasks (admins) ----------


def test_admins_hear_once_an_hour_with_a_count(
    client, desk, make_user, make_client, db_session
):
    other_admin = make_client(make_user("admin"))
    gone_admin = make_user("admin", is_active=False)
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    blurry = desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session, expect=1)

    for c in (client, other_admin):
        [note] = notes(c, "task_failed")
        assert note["title"] == "1 document reading (OCR) task failed"
        assert note["task_type"] == "document_ocr"
        assert note["occurrences"] == 1
    # Each admin has their own: one cannot mark another's.
    assert client.post(f"{API}/notifications/{note['id']}/read").status_code == 404
    assert notes(desk.c, "task_failed") == []
    assert (
        db_session.scalar(
            select(Notification).where(Notification.user_id == gone_admin.id)
        )
        is None
    )

    # The first admin reads theirs; a second failure brings it back, counted.
    [mine] = notes(client, "task_failed")
    ok(client.post(f"{API}/notifications/{mine['id']}/read"))
    ok(
        desk.c.post(
            f"{API}/documents/{blurry['id']}/versions",
            files={"file": ("a.png", aadhaar_scan(error="unsupported"), "image/png")},
            data={"change_reason": "Another try"},
        ),
        201,
    )
    run_worker(db_session, expect=1)
    [mine] = notes(client, "task_failed")
    assert (mine["occurrences"], mine["read_at"]) == (2, None)
    assert mine["title"] == "2 document reading (OCR) tasks failed"
    assert "after all retries" in mine["body"]


def test_a_broken_admin_notice_never_blocks_the_failure(desk, db_session, monkeypatch):
    def broken(db, task_type):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(notification_service, "task_failed", broken)
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    blurry = desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session, expect=1)
    # The document still went to manual review, and its uploader was told.
    assert desk.review_queue()[0]["document_id"] == blurry["id"]
    assert len(notes(desk.c, "ocr_failed")) == 1
