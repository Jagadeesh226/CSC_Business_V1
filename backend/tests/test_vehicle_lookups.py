"""Phase 4.4: vehicle lookups through the provider adapter (fake provider)."""

import json
from datetime import timedelta

import pytest
from flow_helpers import run_worker
from sqlalchemy import select

import app.workers.lookup_handler  # noqa: F401 - registers the task, as the worker does
from app.models import AuditLog, BackgroundTask, Vehicle, VehicleLookup
from app.repositories.readiness_repository import business_today
from app.services.integration import vehicle_lookup as provider

API = "/api/v1"


@pytest.fixture
def today(db_session):
    return business_today(db_session)


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya):
    return make_client(priya)


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="+91 98765 43210")


def add_vehicle(client, customer_id, registration):
    response = client.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer_id,
            "registration_number": registration,
            "category": "private_four_wheeler",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def start(client, vehicle_id):
    response = client.post(f"{API}/vehicles/{vehicle_id}/lookups")
    assert response.status_code == 202, response.text
    return response.json()


def lookup(client, lookup_id):
    return client.get(f"{API}/vehicle-lookups/{lookup_id}").json()


# ---------- the fake provider ----------


def test_fake_provider_rules(today):
    fake = provider.FakeLookupProvider()
    answer = fake.start(provider.LookupRequest("GA07AB1233"), today)
    assert set(answer.expiries) == {"insurance", "pucc"}
    assert (answer.challan_status, answer.challan_count) == ("pending", 1)
    assert (
        fake.start(provider.LookupRequest("GA07AB1234"), today).challan_status == "none"
    )
    with pytest.raises(provider.LookupFailedError):
        fake.start(provider.LookupRequest("GA07AB0000"), today)
    with pytest.raises(provider.LookupTemporaryError):
        fake.start(provider.LookupRequest("GA07TMP123"), today)
    captcha = fake.start(provider.LookupRequest("GA07CAP123"), today)
    assert isinstance(captcha, provider.CaptchaNeeded)
    again = fake.resume(
        provider.LookupRequest("GA07CAP123"), captcha.state, "wrong", today
    )
    assert isinstance(again, provider.CaptchaNeeded) and again.state["tries"] == 2
    done = fake.resume(
        provider.LookupRequest("GA07CAP123"), captcha.state, "abcd", today
    )
    assert isinstance(done, provider.LookupResult)


# ---------- lookups ----------


def test_lookup_fills_dates_and_challans(desk, priya, customer, today, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 AB 1233")
    started = start(desk, v["id"])
    assert (started["status"], started["requested_by"]["id"]) == ("queued", priya.id)

    run_worker(db_session, expect=1)

    done = lookup(desk, started["id"])
    assert (done["status"], done["provider"]) == ("completed", "fake")
    assert done["result"]["applied"] == ["insurance", "pucc"]
    assert done["result"]["challan_status"] == "pending"
    body = desk.get(f"{API}/vehicles/{v['id']}").json()
    sources = {e["expiry_type"]: e["source"] for e in body["expiries"]}
    assert sources == {"insurance": "lookup", "pucc": "lookup"}
    assert body["challan_status"] == "pending" and body["challan_checked_at"]
    found = {k: v for k, v in done["result"]["expiries"].items()}
    shown = {e["expiry_type"]: e["valid_until"] for e in body["expiries"]}
    assert shown == found
    # Nothing about the vehicle in the task's own record.
    task = db_session.scalar(select(BackgroundTask))
    assert set(task.payload) == {"lookup_id", "round"}
    assert "GA" not in json.dumps(task.result)


def test_dates_found_feed_reminders(desk, customer, today, db_session, monkeypatch):
    class Soon(provider.FakeLookupProvider):
        def start(self, request, today_):
            return provider.LookupResult(
                provider="fake", expiries={"insurance": today_ + timedelta(days=10)}
            )

    monkeypatch.setitem(provider._PROVIDERS, "fake", Soon)
    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    start(desk, v["id"])
    run_worker(db_session, expect=1)
    [r] = desk.get(f"{API}/reminders").json()["items"]
    assert (r["expiry_type"], r["days_left"]) == ("insurance", 10)


def test_captcha_round_trip(desk, customer, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 CAP 123")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)

    waiting = lookup(desk, started["id"])
    assert waiting["status"] == "waiting_for_captcha" and waiting["captcha_expires_at"]
    assert desk.get(f"{API}/work-queue").json()["lookups_waiting_for_captcha"] == 1
    image = desk.get(f"{API}/vehicle-lookups/{started['id']}/captcha")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert image.content.startswith(b"\x89PNG")

    url = f"{API}/vehicle-lookups/{started['id']}/captcha"
    wrong = desk.post(url, json={"answer": "WXYZ"})
    assert (wrong.status_code, wrong.json()["status"]) == (202, "queued")
    run_worker(db_session, expect=1)
    assert lookup(desk, started["id"])["status"] == "waiting_for_captcha"  # asked again

    desk.post(url, json={"answer": "ABCD"})
    run_worker(db_session, expect=1)
    assert lookup(desk, started["id"])["status"] == "completed"
    assert desk.get(f"{API}/work-queue").json()["lookups_waiting_for_captcha"] == 0
    assert desk.get(url).status_code == 404  # nothing waiting now
    assert desk.post(url, json={"answer": "ABCD"}).status_code == 422
    # The answer is never stored in clear nor logged.
    row = db_session.scalar(select(VehicleLookup))
    assert row.captcha_answer_sealed is None and row.state_sealed is None
    trail = json.dumps(db_session.scalars(select(AuditLog.details)).all())
    assert "ABCD" not in trail and "WXYZ" not in trail


def test_unanswered_captcha_expires(desk, customer, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 CAP 124")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    row = db_session.scalar(select(VehicleLookup))
    row.captcha_expires_at = row.created_at - timedelta(minutes=1)
    db_session.flush()
    answered = desk.post(
        f"{API}/vehicle-lookups/{started['id']}/captcha", json={"answer": "ABCD"}
    )
    assert answered.status_code == 422
    expired = lookup(desk, started["id"])
    assert (expired["status"], expired["failure_reason"]) == (
        "expired",
        "The CAPTCHA was not answered in time.",
    )
    # A new lookup can start.
    assert desk.post(f"{API}/vehicles/{v['id']}/lookups").status_code == 202


def test_maintenance_expires_old_captchas(desk, customer, db_session):
    from app.workers import lookup_handler

    v = add_vehicle(desk, customer.id, "GA 07 CAP 125")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    row = db_session.scalar(select(VehicleLookup))
    row.captcha_expires_at = row.created_at - timedelta(minutes=1)
    db_session.flush()
    assert lookup_handler.expire_unanswered(db_session) == 1
    assert lookup(desk, started["id"])["status"] == "expired"


def test_not_found_is_recorded_as_failed(desk, customer, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 AB 0000")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    failed = lookup(desk, started["id"])
    assert (failed["status"], failed["failure_reason"]) == (
        "failed",
        "No vehicle with this registration was found.",
    )
    assert desk.get(f"{API}/vehicles/{v['id']}").json()["expiries"] == []
    assert "vehicle_lookup_failed" in db_session.scalars(select(AuditLog.action)).all()


def test_busy_portal_is_retried(desk, customer, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 TMP 123")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    task = db_session.scalar(select(BackgroundTask))
    assert (task.status, task.attempts) == ("queued", 1)  # will try again
    assert lookup(desk, started["id"])["status"] == "queued"


def test_one_lookup_at_a_time(desk, customer):
    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    start(desk, v["id"])
    again = desk.post(f"{API}/vehicles/{v['id']}/lookups")
    assert again.status_code == 409


def test_lookup_rules(client, desk, customer, make_user, make_client, monkeypatch):
    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    stranger = make_client(make_user("employee"))
    assert stranger.post(f"{API}/vehicles/{v['id']}/lookups").status_code == 404
    started = start(desk, v["id"])
    assert stranger.get(f"{API}/vehicle-lookups/{started['id']}").status_code == 404
    assert stranger.get(f"{API}/vehicles/{v['id']}/lookups").status_code == 404
    listing = desk.get(f"{API}/vehicles/{v['id']}/lookups").json()["items"]
    assert [i["id"] for i in listing] == [started["id"]]

    other = add_vehicle(client, customer.id, "GA 07 AB 1235")
    client.patch(f"{API}/vehicles/{other['id']}", json={"is_active": False})
    assert client.post(f"{API}/vehicles/{other['id']}/lookups").status_code == 422

    monkeypatch.delenv("VEHICLE_LOOKUP_PROVIDER")
    third = add_vehicle(client, customer.id, "GA 07 AB 1236")
    refused = client.post(f"{API}/vehicles/{third['id']}/lookups")
    assert refused.status_code == 422
    assert "not set up" in refused.json()["detail"]


def test_stale_rounds_do_nothing(desk, customer, db_session, monkeypatch):
    from app.workers import lookup_handler
    from app.workers.registry import TaskInfo

    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    started = start(desk, v["id"])
    info = TaskInfo(
        id=1,
        task_type="vehicle_expiry_lookup",
        payload={"lookup_id": started["id"], "round": 7},
        attempt=1,
    )

    def never(*_):
        raise AssertionError("the portal must not be asked for a stale round")

    monkeypatch.setattr(provider.FakeLookupProvider, "start", never)
    assert lookup_handler.run_vehicle_lookup(db_session, info) == {"skipped": "stale"}
    assert db_session.get(Vehicle, v["id"]).challan_status == "unknown"
