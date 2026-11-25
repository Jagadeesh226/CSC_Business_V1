"""Phase 4.2: expiry reminders, notifications, buckets and WhatsApp links."""

import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from flow_helpers import run_worker
from sqlalchemy import select

from app.core.config import get_settings
from app.core.whatsapp import describe, link, renewal_message, whatsapp_number
from app.models import AuditLog, DailyRun, Notification, VehicleReminder
from app.repositories.readiness_repository import business_today
from app.services import reminder_service

API = "/api/v1"


# ---------- WhatsApp ----------


@pytest.mark.parametrize(
    "phone, number",
    [
        ("+91 98765 43210", "919876543210"),
        ("098765 43210", None),  # leading 0: could be an STD landline
        ("080 2345 6789", None),  # Bengaluru landline
        ("0612 234 5678", None),  # Patna landline
        ("0712-2345678", None),  # Nagpur landline
        ("9876543210", "919876543210"),
        ("919876543210", "919876543210"),
        ("5876543210", None),  # not a mobile
        ("12345", None),
        (None, None),
    ],
)
def test_whatsapp_number(phone, number):
    assert whatsapp_number(phone) == number


def test_message_and_link():
    assert describe("PUCC", date(2026, 11, 20), -5) == "PUCC expired on 20-11-2026"
    assert describe("Tax", date(2026, 11, 25), 0) == "Tax expires today (25-11-2026)"
    assert describe("Permit", date(2026, 11, 26), 1) == (
        "Permit expires on 26-11-2026 (1 day left)"
    )
    message = renewal_message(
        "Ravi & Sons",
        "6A06U4313",
        [("Insurance", date(2026, 12, 25), 30), ("PUCC", date(2026, 12, 1), 6)],
        "Sample CSC",
    )
    assert message == (
        "Hello Ravi & Sons, for your vehicle 6A06U4313: Insurance expires on "
        "25-12-2026 (30 days left); PUCC expires on 01-12-2026 (6 days left). "
        "Please contact us for renewal. - Sample CSC"
    )
    url = link("919876543210", message)
    assert url.startswith("https://wa.me/919876543210?text=")
    assert "&" not in url.split("?text=")[1]  # every special character encoded
    assert parse_qs(urlparse(url).query)["text"] == [message]


# ---------- fixtures ----------


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya):
    return make_client(priya)


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="+91 98765 43210")


@pytest.fixture
def today(db_session):
    # A live run against the test database may have left today's marker;
    # each test starts without one (inside its own rolled-back transaction).
    db_session.query(DailyRun).delete()
    db_session.flush()
    return business_today(db_session)


def add_vehicle(client, customer_id, registration="GA 07 AB 1234", **extra):
    response = client.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer_id,
            "registration_number": registration,
            "category": "private_four_wheeler",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def set_date(client, vehicle_id, kind, when):
    response = client.put(
        f"{API}/vehicles/{vehicle_id}/expiries/{kind}",
        json={"valid_until": when.isoformat() if when else None},
    )
    assert response.status_code == 200, response.text


def reminders(client, **params):
    response = client.get(f"{API}/reminders", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------- reminders ----------


def test_date_within_30_days_reminds_the_responsible_employee(
    desk, priya, customer, today, db_session
):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=20))

    [r] = reminders(desk)["items"]
    assert (r["expiry_type"], r["days_left"], r["status"]) == (
        "insurance",
        20,
        "ready_to_send",
    )
    assert r["vehicle"]["assigned_to"]["id"] == priya.id
    text = parse_qs(urlparse(r["whatsapp_link"]).query)["text"][0]
    assert urlparse(r["whatsapp_link"]).path == "/919876543210"
    assert "GA 07 AB 1234" in text and "(20 days left)" in text
    assert text.endswith(f"- {get_settings().centre_name}")

    [n] = desk.get(f"{API}/notifications").json()["items"]
    due = (today + timedelta(days=20)).strftime("%d-%m-%Y")
    assert n["title"] == f"GA 07 AB 1234: Insurance expires on {due} (20 days left)"
    assert n["reminder_id"] == r["id"] and n["read_at"] is None
    summary = desk.get(f"{API}/work-queue").json()
    assert (summary["reminders_to_send"], summary["unread_notifications"]) == (1, 1)


@pytest.mark.parametrize("offset", [-1, 31, 365])
def test_dates_outside_the_window_get_no_reminder(desk, customer, today, offset):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "pucc", today + timedelta(days=offset))
    assert reminders(desk)["total"] == 0
    assert desk.get(f"{API}/notifications").json()["total"] == 0


def test_every_expiry_type_is_watched(desk, customer, today):
    v = add_vehicle(desk, customer.id)
    for n, kind in enumerate(("insurance", "pucc", "fitness", "tax", "permit")):
        set_date(desk, v["id"], kind, today + timedelta(days=n * 7))
    items = reminders(desk)["items"]
    assert [i["expiry_type"] for i in items] == [
        "insurance",
        "pucc",
        "fitness",
        "tax",
        "permit",
    ]
    # One message per vehicle lists everything due.
    text = parse_qs(urlparse(items[0]["whatsapp_link"]).query)["text"][0]
    assert "Insurance expires today" in text and "Permit expires on" in text


def test_changing_a_date_replaces_the_reminder(desk, customer, today, db_session):
    v = add_vehicle(desk, customer.id)
    first, second = today + timedelta(days=10), today + timedelta(days=25)
    set_date(desk, v["id"], "insurance", first)
    set_date(desk, v["id"], "insurance", second)
    rows = db_session.scalars(
        select(VehicleReminder).order_by(VehicleReminder.id)
    ).all()
    assert [(r.valid_until, r.status) for r in rows] == [
        (first, "superseded"),
        (second, "open"),
    ]
    # Renewed for a year: nothing left to remind about.
    set_date(desk, v["id"], "insurance", today + timedelta(days=380))
    assert reminders(desk, status="open")["total"] == 0
    # Back to the earlier date (a correction): its reminder comes back.
    set_date(desk, v["id"], "insurance", first)
    assert [r["valid_until"] for r in reminders(desk, status="open")["items"]] == [
        first.isoformat()
    ]
    assert len(db_session.scalars(select(VehicleReminder)).all()) == 2


def test_no_mobile_number_means_pending(desk, make_customer, today):
    landline_only = make_customer(name="Meena", phone="080 2345 6789x")
    landline_only.phone = None
    v = add_vehicle(desk, landline_only.id)
    set_date(desk, v["id"], "tax", today + timedelta(days=3))
    [r] = reminders(desk)["items"]
    assert (r["status"], r["whatsapp_link"]) == ("pending", None)
    assert reminders(desk, status="pending")["total"] == 1
    assert reminders(desk, status="ready_to_send")["total"] == 0

    desk.patch(f"{API}/vehicles/{v['id']}", json={"contact_phone": "9000012345"})
    [r] = reminders(desk)["items"]
    assert r["status"] == "ready_to_send"
    assert urlparse(r["whatsapp_link"]).path == "/919000012345"


def test_marking_done_and_dismissing(desk, priya, customer, today, db_session):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    set_date(desk, v["id"], "pucc", today + timedelta(days=6))
    insurance, pucc = reminders(desk)["items"]

    done = desk.post(
        f"{API}/reminders/{insurance['id']}/done", json={"note": "Sent on WhatsApp"}
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["status"], body["closed_by"]["id"], body["whatsapp_link"]) == (
        "done",
        priya.id,
        None,
    )
    assert (
        desk.post(f"{API}/reminders/{insurance['id']}/done", json={}).status_code == 422
    )

    assert (
        desk.post(f"{API}/reminders/{pucc['id']}/dismiss", json={}).status_code == 422
    )
    dismissed = desk.post(
        f"{API}/reminders/{pucc['id']}/dismiss", json={"reason": "Renewed elsewhere"}
    )
    assert dismissed.json()["close_reason"] == "Renewed elsewhere"
    assert reminders(desk, status="open")["total"] == 0
    assert reminders(desk, status="done")["total"] == 1

    # Their notifications are settled too.
    assert (
        desk.get(f"{API}/notifications", params={"unread": "true"}).json()["total"] == 0
    )
    actions = db_session.scalars(
        select(AuditLog.action)
        .where(AuditLog.action.in_(("reminder_completed", "reminder_dismissed")))
        .order_by(AuditLog.id)
    ).all()
    assert actions == ["reminder_completed", "reminder_dismissed"]


def test_reminders_follow_vehicle_access(
    client, desk, customer, make_user, make_client, today
):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    [r] = reminders(desk)["items"]
    stranger = make_client(make_user("employee"))
    assert reminders(stranger)["total"] == 0
    assert stranger.post(f"{API}/reminders/{r['id']}/done", json={}).status_code == 404
    assert stranger.get(f"{API}/vehicle-expiries").json()["total"] == 0
    assert reminders(client)["total"] == 1  # admins see all


def test_inactive_vehicles_have_no_open_reminders(
    client, desk, customer, today, db_session
):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    client.patch(f"{API}/vehicles/{v['id']}", json={"is_active": False})
    assert reminders(client, status="open")["total"] == 0
    client.patch(f"{API}/vehicles/{v['id']}", json={"is_active": True})
    assert reminders(client, status="open")["total"] == 1


def test_unassigned_vehicles_notify_no_one(client, customer, today, db_session):
    v = add_vehicle(client, customer.id)  # an admin's vehicle: unassigned
    set_date(client, v["id"], "insurance", today + timedelta(days=5))
    assert reminders(client)["total"] == 1
    assert db_session.scalars(select(Notification)).all() == []


# ---------- the daily check ----------


def test_daily_check_creates_due_reminders_once(
    desk, customer, today, db_session, monkeypatch
):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=31))
    assert reminders(desk)["total"] == 0

    tomorrow = today + timedelta(days=1)
    monkeypatch.setattr(reminder_service, "business_today", lambda db: tomorrow)
    assert reminder_service.run_daily(db_session) == 1
    assert reminder_service.run_daily(db_session) == 0  # same day: nothing
    db_session.flush()
    monkeypatch.setattr(
        reminder_service, "business_today", lambda db: tomorrow + timedelta(days=1)
    )
    assert reminder_service.run_daily(db_session) == 0  # already reminded
    assert len(db_session.scalars(select(VehicleReminder)).all()) == 1
    assert len(db_session.scalars(select(Notification)).all()) == 1
    assert len(db_session.scalars(select(DailyRun)).all()) == 2


def test_daily_check_retires_stale_reminders(
    desk, customer, today, db_session, monkeypatch
):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    reminder = db_session.scalar(select(VehicleReminder))
    # The date changes outside the normal path (e.g. an import).
    from app.models import VehicleExpiry

    expiry = db_session.scalar(select(VehicleExpiry).where(VehicleExpiry.is_current))
    expiry.valid_until = today + timedelta(days=200)
    db_session.flush()
    monkeypatch.setattr(
        reminder_service, "business_today", lambda db: today + timedelta(days=1)
    )
    reminder_service.run_daily(db_session)
    db_session.refresh(reminder)
    assert reminder.status == "superseded"


def test_daily_check_runs_in_the_real_worker(desk, customer, today, db_session):
    from app.workers import registry

    before = registry.maintenance_jobs()
    import app.workers.handlers  # noqa: F401 - registers jobs, as the worker does

    assert reminder_service.run_daily in registry.maintenance_jobs()
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    # As if the date was set before Phase 4.2.
    db_session.query(Notification).delete()
    db_session.query(VehicleReminder).delete()
    db_session.flush()
    try:
        run_worker(db_session, expect=0)  # maintenance runs on every tick
    finally:
        # Leave the registry as other tests expect it.
        registry._MAINTENANCE[:] = before
    assert len(db_session.scalars(select(VehicleReminder)).all()) == 1
    assert db_session.scalar(select(DailyRun)).business_date == today


# ---------- expiry buckets ----------


def test_expiry_buckets(desk, customer, make_customer, today):
    for n, (offset, reg) in enumerate(
        [
            (-2, "GA01A0001"),
            (3, "GA01A0002"),
            (10, "GA01A0003"),
            (20, "GA01A0004"),
            (60, "GA01A0005"),
        ]
    ):
        v = add_vehicle(desk, customer.id, registration=reg)
        set_date(desk, v["id"], "insurance", today + timedelta(days=offset))
    body = desk.get(f"{API}/vehicle-expiries").json()
    assert body["as_of"] == today.isoformat()
    assert body["counts"] == {
        "expired": 1,
        "0_7": 1,
        "8_15": 1,
        "16_30": 1,
        "over_30": 1,
    }
    assert [i["bucket"] for i in body["items"]] == [
        "expired",
        "0_7",
        "8_15",
        "16_30",
        "over_30",
    ]
    expired = body["items"][0]
    assert expired["days_left"] == -2
    text = parse_qs(urlparse(expired["whatsapp_link"]).query)["text"][0]
    assert "Insurance expired on" in text  # no reminder, but a link to chase it
    only = desk.get(f"{API}/vehicle-expiries", params={"bucket": "8_15"}).json()
    assert [i["registration_number"] for i in only["items"]] == ["GA01A0003"]
    assert only["total"] == 1
    assert (
        desk.get(f"{API}/vehicle-expiries", params={"bucket": "soon"}).status_code
        == 422
    )


# ---------- notifications ----------


def test_notifications_are_private(
    desk, priya, customer, make_user, make_client, today, db_session
):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    set_date(desk, v["id"], "pucc", today + timedelta(days=9))
    first, _ = desk.get(f"{API}/notifications").json()["items"]

    other = make_client(make_user("employee"))
    assert other.get(f"{API}/notifications").json()["total"] == 0
    assert other.post(f"{API}/notifications/{first['id']}/read").status_code == 404

    assert desk.post(f"{API}/notifications/{first['id']}/read").json() == {"marked": 1}
    listing = desk.get(f"{API}/notifications", params={"unread": "true"}).json()
    assert (listing["total"], listing["unread"]) == (1, 1)
    assert desk.post(f"{API}/notifications/read-all").json() == {"marked": 1}
    assert desk.get(f"{API}/work-queue").json()["unread_notifications"] == 0
    # No identifier or phone number in a notification.
    stored = json.dumps(
        [(n.title, n.body) for n in db_session.scalars(select(Notification))]
    )
    assert "9876543210" not in stored


def test_second_run_on_the_same_day_does_nothing(
    desk, customer, today, db_session, monkeypatch
):
    from app.models import VehicleExpiry

    monkeypatch.setattr(reminder_service, "business_today", lambda db: today)
    assert reminder_service.run_daily(db_session) == 0  # today's run: nothing due
    v = add_vehicle(desk, customer.id)
    # A date arrives outside the normal path (e.g. an import) later that day.
    db_session.add(
        VehicleExpiry(
            vehicle_id=v["id"],
            expiry_type="tax",
            valid_until=today + timedelta(days=4),
            source="manual",
            is_current=True,
        )
    )
    db_session.flush()
    assert reminder_service.run_daily(db_session) == 0  # already ran today
    assert db_session.scalars(select(VehicleReminder)).all() == []
    monkeypatch.setattr(
        reminder_service, "business_today", lambda db: today + timedelta(days=1)
    )
    assert reminder_service.run_daily(db_session) == 1  # tomorrow's run picks it up


def test_inactive_employees_are_not_notified(
    client, desk, priya, customer, today, db_session
):
    v = add_vehicle(desk, customer.id)
    client.post(f"{API}/users/{priya.id}/deactivate")
    set_date(client, v["id"], "insurance", today + timedelta(days=5))
    assert reminders(client)["total"] == 1
    assert db_session.scalars(select(Notification)).all() == []


@pytest.mark.parametrize(
    "offset, bucket",
    [
        (-1, "expired"),
        (0, "0_7"),
        (7, "0_7"),
        (8, "8_15"),
        (15, "8_15"),
        (16, "16_30"),
        (30, "16_30"),
        (31, "over_30"),
    ],
)
def test_bucket_edges(desk, customer, today, offset, bucket):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "pucc", today + timedelta(days=offset))
    [item] = desk.get(f"{API}/vehicle-expiries").json()["items"]
    assert item["bucket"] == bucket
    for name in ("expired", "0_7", "8_15", "16_30", "over_30"):
        filtered = desk.get(f"{API}/vehicle-expiries", params={"bucket": name}).json()
        assert filtered["total"] == (1 if name == bucket else 0), name


# ---------- review fixes ----------


def test_assigning_a_vehicle_later_notifies_the_new_person(
    client, priya, desk, customer, make_user, make_client, today, db_session
):
    v = add_vehicle(client, customer.id)  # an admin's vehicle: nobody yet
    set_date(client, v["id"], "insurance", today + timedelta(days=5))
    assert db_session.scalars(select(Notification)).all() == []

    client.patch(f"{API}/vehicles/{v['id']}", json={"assigned_to": priya.id})
    [n] = desk.get(f"{API}/notifications").json()["items"]
    assert n["title"].startswith("GA 07 AB 1234: Insurance expires on")

    sameer = make_user("employee", name="Sameer")
    client.patch(f"{API}/vehicles/{v['id']}", json={"assigned_to": sameer.id})
    assert make_client(sameer).get(f"{API}/notifications").json()["unread"] == 1
    # Priya's is settled: the vehicle is no longer hers to chase.
    assert desk.get(f"{API}/notifications").json()["unread"] == 0


def test_daily_check_backfills_missing_notifications(
    client, priya, desk, customer, today, db_session, monkeypatch
):
    v = add_vehicle(client, customer.id)
    set_date(client, v["id"], "insurance", today + timedelta(days=5))
    # Assigned without going through the API (e.g. a data fix).
    from app.models import Vehicle

    db_session.get(Vehicle, v["id"]).assigned_to = priya.id
    db_session.flush()
    monkeypatch.setattr(reminder_service, "business_today", lambda db: today)
    reminder_service.run_daily(db_session)
    assert desk.get(f"{API}/notifications").json()["unread"] == 1


def test_expiry_row_links_match_the_row(desk, customer, today):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    set_date(desk, v["id"], "pucc", today + timedelta(days=200))
    set_date(desk, v["id"], "tax", today - timedelta(days=3))
    rows = {
        i["expiry_type"]: i for i in desk.get(f"{API}/vehicle-expiries").json()["items"]
    }

    def text(item):
        return parse_qs(urlparse(item["whatsapp_link"]).query)["text"][0]

    assert "Insurance expires on" in text(rows["insurance"])
    assert rows["pucc"]["whatsapp_link"] is None  # nothing to chase yet
    tax = text(rows["tax"])
    assert "Road tax expired on" in tax and "Insurance" not in tax


def test_notification_wording(desk, customer, today):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "permit", today)
    set_date(desk, v["id"], "tax", today + timedelta(days=1))
    titles = sorted(
        n["title"] for n in desk.get(f"{API}/notifications").json()["items"]
    )
    assert titles[0].endswith(
        "Permit expires today (" + today.strftime("%d-%m-%Y") + ")"
    )
    assert titles[1].endswith("(1 day left)")


def test_a_stale_date_never_gets_a_reminder(desk, customer, today, db_session):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=200))
    from app.models import Vehicle

    vehicle = db_session.get(Vehicle, v["id"])
    # A date that is not (or no longer) the current one is ignored.
    assert (
        reminder_service._ensure(
            db_session, vehicle, "insurance", today + timedelta(days=5), today
        )
        is False
    )
    assert db_session.scalars(select(VehicleReminder)).all() == []


def test_done_reminder_is_not_reopened(desk, customer, today, db_session):
    v = add_vehicle(desk, customer.id)
    when = today + timedelta(days=5)
    set_date(desk, v["id"], "insurance", when)
    [r] = reminders(desk)["items"]
    desk.post(f"{API}/reminders/{r['id']}/done", json={})
    set_date(desk, v["id"], "insurance", when + timedelta(days=1))
    set_date(desk, v["id"], "insurance", when)  # back again
    statuses = sorted(
        (x.valid_until, x.status) for x in db_session.scalars(select(VehicleReminder))
    )
    assert statuses == [(when, "done"), (when + timedelta(days=1), "superseded")]


def test_mark_read_response_is_typed():
    from app.main import app

    spec = app.openapi()["paths"]["/api/v1/notifications/read-all"]["post"]
    schema = spec["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/MarkedResponse"}


def test_replaced_reminders_settle_their_notification(desk, customer, today):
    v = add_vehicle(desk, customer.id)
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))
    assert desk.get(f"{API}/notifications").json()["unread"] == 1
    set_date(desk, v["id"], "insurance", today + timedelta(days=300))  # renewed
    assert desk.get(f"{API}/notifications").json()["unread"] == 0
    set_date(desk, v["id"], "insurance", today + timedelta(days=5))  # a mistake
    assert desk.get(f"{API}/notifications").json()["unread"] == 1
