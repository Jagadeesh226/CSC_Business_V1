"""Phase 4 whole-flow tests: a vehicle's life at the counter.

Each test is one story driven through the HTTP API from the employee's
side, with the real background worker: an RC upload that OCR turns into a
vehicle, a lookup that stops for a CAPTCHA the employee types, the expiry
reminder with its WhatsApp link, and the Excel export.
"""

import io
import itertools
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from flow_helpers import API, Counter, ok, run_worker
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import AuditLog, VehicleReminder
from app.repositories.readiness_repository import business_today
from app.services.integration import vehicle_lookup as provider

PNG = b"\x89PNG\r\n\x1a\n"
CHASSIS = "MA3FJEB1S00123456"
ENGINE = "K12MN1234567"
_unique = itertools.count(1)


def rc_scan(registration="GA 07 AB 1234", **overrides) -> bytes:
    fields = {
        "registration_number": [registration, 0.98],
        "owner_name": ["RAVI KUMAR", 0.95],
        "chassis_number": [CHASSIS, 0.96],
        "engine_number": [ENGINE, 0.94],
        "vehicle_class": ["MOTOR CAR (LMV)", 0.9],
        **overrides,
    }
    script = {"fields": fields, "raw_text": "FORM 23", "n": next(_unique)}
    return PNG + b"FAKEOCR:" + json.dumps(script).encode()


class PortalWithCaptcha(provider.FakeLookupProvider):
    """Asks for a CAPTCHA, then answers with insurance due in 12 days."""

    def start(self, request, today):
        return provider.CaptchaNeeded("fake", provider._PNG, {"tries": 1})

    def resume(self, request, state, answer, today):
        if answer != provider.FAKE_CAPTCHA_ANSWER:
            return provider.CaptchaNeeded("fake", provider._PNG, {"tries": 2})
        return provider.LookupResult(
            provider="fake",
            expiries={
                "insurance": today + timedelta(days=12),
                "pucc": today + timedelta(days=200),
            },
            challan_status="none",
            challan_count=0,
        )


@pytest.fixture(autouse=True)
def lookup_worker(monkeypatch):
    """The worker's lookup task, registered for this module only, with the
    portal above."""
    from app.workers import lookup_handler, registry

    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    registry.register(
        lookup_handler.TASK_TYPE,
        lookup_handler.run_vehicle_lookup,
        on_final_failure=lookup_handler.on_lookup_failed,
    )
    monkeypatch.setitem(provider._PROVIDERS, "fake", PortalWithCaptcha)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved[0])
    registry._MAINTENANCE[:] = saved[1]


@pytest.fixture
def rto(client):
    """The admin sets up an RTO service that needs the RC, read by OCR."""

    def post(path, body):
        return ok(client.post(f"{API}{path}", json=body), 201)

    category = post("/service-categories", {"name": "RTO", "code": "RTO"})
    rc = post(
        "/document-types",
        {
            "code": "RC",
            "name": "Registration certificate",
            "sensitivity": "sensitive",
            "vehicle_role": "registration_certificate",
            "ocr_fields": [
                {"key": "registration_number", "label": "Registration no."},
                {"key": "owner_name", "label": "Owner"},
                {"key": "chassis_number", "label": "Chassis no.", "sensitive": True},
                {"key": "engine_number", "label": "Engine no.", "sensitive": True},
                {"key": "vehicle_class", "label": "Vehicle class"},
            ],
        },
    )
    service = post(
        "/services",
        {
            "category_id": category["id"],
            "name": "Insurance renewal",
            "code": "INS-RENEW",
            "fee_type": "fixed",
            "default_service_fee": "150.00",
            "requires_vehicle": True,
        },
    )
    post(
        f"/services/{service['id']}/document-requirements",
        {"document_type_id": rc["id"]},
    )
    return service


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya, rto):
    return Counter(make_client(priya), rto)


@pytest.fixture
def today(db_session):
    return business_today(db_session)


def vehicle_of(desk, customer_id):
    items = ok(desk.c.get(f"{API}/vehicles", params={"customer_id": customer_id}))[
        "items"
    ]
    assert len(items) == 1, items
    return items[0]


def text_of(link):
    return parse_qs(urlparse(link).query)["text"][0]


# ---------- the main story ----------


def test_rc_to_reminder_to_export(client, desk, priya, today, db_session, fake_redis):
    # Morning: a customer brings their RC for an insurance renewal.
    customer = desk.new_customer("Ravi Kumar", "+91 98765 43210")["customer"]
    job = desk.open_job(customer["id"], "Insurance renewal")
    rc = desk.upload(job, "RC", rc_scan())
    run_worker(db_session, expect=1)  # OCR reads the RC

    # The vehicle exists without anyone typing it in.
    vehicle = vehicle_of(desk, customer["id"])
    assert vehicle["registration_number"] == "GA 07 AB 1234"
    assert (vehicle["chassis_number"], vehicle["engine_number"]) == (
        "****3456",
        "****4567",
    )
    assert vehicle["assigned_to"]["id"] == priya.id
    assert (
        ok(desk.c.get(f"{API}/documents/{rc['id']}/vehicle-intake"))["status"]
        == "applied"
    )
    ok(desk.c.patch(f"{API}/jobs/{job['id']}", json={"vehicle_id": vehicle["id"]}))
    assert desk.job(job["id"])["vehicle"]["registration_number"] == "GA 07 AB 1234"

    # The lookup stops for a CAPTCHA; only the employee can read it.
    lookup = ok(desk.c.post(f"{API}/vehicles/{vehicle['id']}/lookups"), 202)
    run_worker(db_session, expect=1)
    assert desk.queue()["lookups_waiting_for_captcha"] == 1
    image = desk.c.get(f"{API}/vehicle-lookups/{lookup['id']}/captcha")
    assert image.content.startswith(PNG)
    ok(
        desk.c.post(
            f"{API}/vehicle-lookups/{lookup['id']}/captcha",
            json={"answer": provider.FAKE_CAPTCHA_ANSWER},
        ),
        202,
    )
    run_worker(db_session, expect=1)
    done = ok(desk.c.get(f"{API}/vehicle-lookups/{lookup['id']}"))
    assert done["status"] == "completed"
    assert done["result"]["applied"] == ["insurance", "pucc"]

    # The insurance due in 12 days: a reminder and a notification.
    summary = desk.queue()
    assert (summary["reminders_to_send"], summary["unread_notifications"]) == (1, 1)
    [reminder] = ok(desk.c.get(f"{API}/reminders"))["items"]
    assert (reminder["expiry_type"], reminder["days_left"]) == ("insurance", 12)
    assert reminder["status"] == "ready_to_send"
    message = text_of(reminder["whatsapp_link"])
    assert urlparse(reminder["whatsapp_link"]).path == "/919876543210"
    assert "GA 07 AB 1234" in message and "(12 days left)" in message

    # The employee sends it on WhatsApp, then says so.
    ok(
        desk.c.post(
            f"{API}/reminders/{reminder['id']}/done", json={"note": "Sent on WhatsApp"}
        )
    )
    summary = desk.queue()
    assert (summary["reminders_to_send"], summary["unread_notifications"]) == (0, 0)

    # Evening: the export has the vehicle, masked, with its link.
    response = desk.c.get(
        f"{API}/reports/vehicles.xlsx", params={"due_within_days": 30}
    )
    ws = load_workbook(io.BytesIO(response.content))["Vehicles"]
    headers = [c.value for c in ws[1]]
    [row] = [dict(zip(headers, r, strict=False)) for r in ws.iter_rows(min_row=2)]
    assert row["Registration no."].value == "GA 07 AB 1234"
    assert row["Chassis no."].value == "****3456"
    assert row["Insurance days left"].value == 12
    assert "Insurance expires on" in text_of(row["WhatsApp"].hyperlink.target)

    # The trail: who did what, and nothing sensitive in it.
    trail = json.dumps(db_session.scalars(select(AuditLog.details)).all())
    for secret in (CHASSIS, ENGINE, "9876543210", provider.FAKE_CAPTCHA_ANSWER):
        assert secret not in trail
    actions = set(db_session.scalars(select(AuditLog.action)).all())
    assert {
        "vehicle_created",
        "vehicle_intake",
        "vehicle_lookup_requested",
        "vehicle_lookup_captcha_answered",
        "vehicle_lookup_completed",
        "reminder_completed",
        "vehicles_exported",
    } <= actions


# ---------- renewal ----------


def test_renewal_retires_the_reminder_and_is_kept(desk, today, db_session):
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    desk.upload(job, "RC", rc_scan("KA 01 CD 5678"))
    run_worker(db_session, expect=1)
    vehicle = vehicle_of(desk, customer["id"])
    lookup = ok(desk.c.post(f"{API}/vehicles/{vehicle['id']}/lookups"), 202)
    run_worker(db_session, expect=1)
    desk.c.post(
        f"{API}/vehicle-lookups/{lookup['id']}/captcha",
        json={"answer": provider.FAKE_CAPTCHA_ANSWER},
    )
    run_worker(db_session, expect=1)
    assert ok(desk.c.get(f"{API}/reminders", params={"status": "open"}))["total"] == 1

    # The customer renews here; the new policy runs for a year.
    renewed = today + timedelta(days=377)
    ok(
        desk.c.put(
            f"{API}/vehicles/{vehicle['id']}/expiries/insurance",
            json={"valid_until": renewed.isoformat()},
        )
    )
    assert ok(desk.c.get(f"{API}/reminders", params={"status": "open"}))["total"] == 0
    assert desk.queue()["unread_notifications"] == 0
    [old] = db_session.scalars(select(VehicleReminder)).all()
    assert old.status == "superseded"

    # A later lookup still sees the old policy: the renewal is kept.
    second = ok(desk.c.post(f"{API}/vehicles/{vehicle['id']}/lookups"), 202)
    run_worker(db_session, expect=1)
    desk.c.post(
        f"{API}/vehicle-lookups/{second['id']}/captcha",
        json={"answer": provider.FAKE_CAPTCHA_ANSWER},
    )
    run_worker(db_session, expect=1)
    result = ok(desk.c.get(f"{API}/vehicle-lookups/{second['id']}"))["result"]
    assert result["not_applied"] == ["insurance"]
    insurance = next(
        e
        for e in ok(desk.c.get(f"{API}/vehicles/{vehicle['id']}"))["expiries"]
        if e["expiry_type"] == "insurance"
    )
    assert (insurance["valid_until"], insurance["source"]) == (
        renewed.isoformat(),
        "manual",
    )


# ---------- doubtful RC, then a handover ----------


def test_doubtful_rc_and_handover(
    client, desk, priya, make_user, make_client, rto, db_session, today
):
    customer = desk.new_customer("Asha", "9000022222")["customer"]
    job = desk.open_job(customer["id"])
    rc = desk.upload(job, "RC", rc_scan("MH 12 XY 9999", engine_number=[ENGINE, 0.4]))
    run_worker(db_session, expect=1)

    # Only the unsure engine number waits for a person.
    reading = ok(desk.c.get(f"{API}/documents/{rc['id']}/vehicle-intake"))
    assert [c["key"] for c in reading["check_fields"]] == ["engine_number"]
    assert desk.queue()["vehicle_readings_to_check"] == 1
    ok(
        desk.c.post(
            f"{API}/documents/{rc['id']}/vehicle-intake/resolve",
            json={"values": {"engine_number": ENGINE}},
        )
    )
    vehicle = vehicle_of(desk, customer["id"])
    full = ok(desk.c.get(f"{API}/vehicles/{vehicle['id']}", params={"reveal": "true"}))
    assert full["engine_number"] == ENGINE

    # A date in 9 days, then Priya hands the vehicle to Sameer.
    ok(
        desk.c.put(
            f"{API}/vehicles/{vehicle['id']}/expiries/pucc",
            json={"valid_until": (today + timedelta(days=9)).isoformat()},
        )
    )
    sameer_user = make_user("employee", name="Sameer Desk")
    sameer = Counter(make_client(sameer_user), rto)
    ok(
        client.patch(
            f"{API}/vehicles/{vehicle['id']}", json={"assigned_to": sameer_user.id}
        )
    )
    assert sameer.queue()["unread_notifications"] == 1
    assert desk.queue()["unread_notifications"] == 0
    [reminder] = ok(sameer.c.get(f"{API}/reminders"))["items"]
    assert reminder["vehicle"]["assigned_to"]["name"] == "Sameer Desk"
    # Sameer, not Priya, may see the full numbers now (Priya still sees the
    # vehicle, masked, through her open job for the customer).
    assert (
        desk.c.get(
            f"{API}/vehicles/{vehicle['id']}", params={"reveal": "true"}
        ).status_code
        == 403
    )
    assert (
        sameer.c.get(
            f"{API}/vehicles/{vehicle['id']}", params={"reveal": "true"}
        ).json()["engine_number"]
        == ENGINE
    )


# ---------- limits ----------


def test_colleagues_and_strangers_see_nothing(
    desk, make_user, make_client, rto, db_session
):
    customer = desk.new_customer("Kiran", "9000033333")["customer"]
    job = desk.open_job(customer["id"])
    desk.upload(job, "RC", rc_scan("TN 01 AB 1001"))
    run_worker(db_session, expect=1)
    vehicle = vehicle_of(desk, customer["id"])
    lookup = ok(desk.c.post(f"{API}/vehicles/{vehicle['id']}/lookups"), 202)
    run_worker(db_session, expect=1)

    stranger = make_client(make_user("employee"))
    for url in (
        f"{API}/vehicles/{vehicle['id']}",
        f"{API}/vehicle-lookups/{lookup['id']}/captcha",
    ):
        assert stranger.get(url).status_code == 404
    assert ok(stranger.get(f"{API}/vehicles"))["total"] == 0
    assert ok(stranger.get(f"{API}/reminders"))["total"] == 0
    export = stranger.get(f"{API}/reports/vehicles.xlsx")
    ws = load_workbook(io.BytesIO(export.content))["Vehicles"]
    assert ws.max_row == 1  # headers only
    # A stranger's search for the number finds no customer either.
    assert (
        ok(stranger.get(f"{API}/customers", params={"search": "TN01AB"}))["total"] == 0
    )
