"""Phase 4.4: vehicle lookups through the provider adapter (fake provider)."""

import json
from datetime import timedelta

import pytest
from flow_helpers import run_worker
from sqlalchemy import select

from app.models import AuditLog, BackgroundTask, Vehicle, VehicleLookup
from app.repositories.readiness_repository import business_today
from app.services.integration import vehicle_lookup as provider

API = "/api/v1"


@pytest.fixture(autouse=True)
def lookup_worker():
    """Register the lookup task and its maintenance as the worker does,
    for this module only."""
    from app.workers import registry

    # Saved before the import: importing the module registers the task.
    saved = (dict(registry._REGISTRY), list(registry._MAINTENANCE))
    from app.workers import lookup_handler

    registry.register(
        lookup_handler.TASK_TYPE,
        lookup_handler.run_vehicle_lookup,
        on_final_failure=lookup_handler.on_lookup_failed,
    )
    registry.register_maintenance(lookup_handler.expire_unanswered)
    registry.register_maintenance(lookup_handler.recover_stuck_lookups)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved[0])
    registry._MAINTENANCE[:] = saved[1]


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


# ---------- review fixes ----------


def set_date(client, vehicle_id, kind, when):
    response = client.put(
        f"{API}/vehicles/{vehicle_id}/expiries/{kind}",
        json={"valid_until": when.isoformat()},
    )
    assert response.status_code == 200, response.text


def answering(found):
    class Fixed(provider.FakeLookupProvider):
        def start(self, request, today_):
            return provider.LookupResult(provider="fake", expiries=found(today_))

    return Fixed


def test_staff_dates_are_not_replaced_by_older_ones(
    desk, priya, customer, today, db_session, monkeypatch
):
    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    renewed = today + timedelta(days=360)
    set_date(desk, v["id"], "insurance", renewed)  # entered by staff
    set_date(desk, v["id"], "pucc", today + timedelta(days=2))  # an old PUCC
    monkeypatch.setitem(
        provider._PROVIDERS,
        "fake",
        answering(
            lambda t: {
                "insurance": t + timedelta(days=11),  # older than staff's
                "pucc": t + timedelta(days=180),  # later: a renewal
            }
        ),
    )
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)

    result = lookup(desk, started["id"])["result"]
    assert (result["applied"], result["not_applied"]) == (["pucc"], ["insurance"])
    body = desk.get(f"{API}/vehicles/{v['id']}").json()
    by_type = {e["expiry_type"]: e for e in body["expiries"]}
    assert by_type["insurance"]["valid_until"] == renewed.isoformat()
    assert by_type["insurance"]["source"] == "manual"
    assert by_type["pucc"]["source"] == "lookup"
    # Applied by the lookup in the requester's name.
    assert by_type["pucc"]["recorded_by"]["id"] == priya.id
    # No reminder for the false "11 days left".
    open_ = desk.get(f"{API}/reminders", params={"status": "open"}).json()
    assert open_["total"] == 0


def test_lookup_dates_follow_newer_lookups(
    desk, customer, today, db_session, monkeypatch
):
    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    monkeypatch.setitem(
        provider._PROVIDERS,
        "fake",
        answering(lambda t: {"tax": t + timedelta(days=100)}),
    )
    start(desk, v["id"])
    run_worker(db_session, expect=1)
    monkeypatch.setitem(
        provider._PROVIDERS,
        "fake",
        answering(lambda t: {"tax": t + timedelta(days=50)}),
    )
    start(desk, v["id"])
    run_worker(db_session, expect=1)
    [tax] = desk.get(f"{API}/vehicles/{v['id']}").json()["expiries"]
    assert tax["days_left"] == 50  # a lookup may correct a lookup


def test_stuck_lookup_is_recovered(desk, customer, db_session):
    from app.workers import lookup_handler

    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    started = start(desk, v["id"])
    row = db_session.scalar(select(VehicleLookup))
    task = db_session.scalar(select(BackgroundTask))
    # Its task ended, but the lookup never heard (e.g. the failure hook failed).
    task.status = "failed"
    row.created_at = row.created_at - timedelta(minutes=10)
    db_session.flush()
    assert lookup_handler.recover_stuck_lookups(db_session) == 1
    stuck = lookup(desk, started["id"])
    assert stuck["status"] == "failed"
    assert desk.post(f"{API}/vehicles/{v['id']}/lookups").status_code == 202


def test_running_lookups_are_left_alone(desk, customer, db_session):
    from app.workers import lookup_handler

    v = add_vehicle(desk, customer.id, "GA 07 AB 1234")
    start(desk, v["id"])
    row = db_session.scalar(select(VehicleLookup))
    row.created_at = row.created_at - timedelta(minutes=10)
    db_session.flush()
    assert lookup_handler.recover_stuck_lookups(db_session) == 0  # task still queued


def test_expiry_is_audited(desk, priya, customer, db_session):
    from app.workers import lookup_handler

    v = add_vehicle(desk, customer.id, "GA 07 CAP 126")
    start(desk, v["id"])
    run_worker(db_session, expect=1)
    row = db_session.scalar(select(VehicleLookup))
    row.captcha_expires_at = row.created_at - timedelta(minutes=1)
    db_session.flush()
    assert lookup_handler.expire_unanswered(db_session) == 1
    db_session.flush()
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "vehicle_lookup_expired")
    )
    assert entry.user_id == priya.id


def test_busy_portal_after_an_answer_ends_the_lookup(
    desk, customer, db_session, monkeypatch
):
    class BusyAfterAnswer(provider.FakeLookupProvider):
        def resume(self, *args):
            raise provider.LookupTemporaryError("busy")

    monkeypatch.setitem(provider._PROVIDERS, "fake", BusyAfterAnswer)
    v = add_vehicle(desk, customer.id, "GA 07 CAP 127")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    desk.post(f"{API}/vehicle-lookups/{started['id']}/captcha", json={"answer": "ABCD"})
    run_worker(db_session, expect=1)
    ended = lookup(desk, started["id"])
    assert ended["status"] == "failed"
    assert "after the CAPTCHA" in ended["failure_reason"]


def test_too_many_wrong_answers(desk, customer, db_session):
    from app.workers import lookup_handler

    v = add_vehicle(desk, customer.id, "GA 07 CAP 128")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    url = f"{API}/vehicle-lookups/{started['id']}/captcha"
    for _ in range(lookup_handler.MAX_ROUNDS - 1):
        assert desk.post(url, json={"answer": "nope"}).status_code == 202
        run_worker(db_session, expect=1)
    ended = lookup(desk, started["id"])
    assert (ended["status"], ended["failure_reason"]) == (
        "failed",
        "Too many CAPTCHA attempts; start a new lookup.",
    )


def test_portal_busy_three_times_fails_the_lookup(desk, customer, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 TMP 124")
    started = start(desk, v["id"])
    for _ in range(3):
        run_worker(db_session, expect=1)  # run_worker makes the retry due
    failed = lookup(desk, started["id"])
    assert (failed["status"], failed["failure_reason"]) == (
        "failed",
        "The vehicle portal is busy; try again later.",
    )


def test_captcha_for_an_inactive_vehicle_is_refused(client, desk, customer, db_session):
    v = add_vehicle(desk, customer.id, "GA 07 CAP 129")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    client.patch(f"{API}/vehicles/{v['id']}", json={"is_active": False})
    answered = client.post(
        f"{API}/vehicle-lookups/{started['id']}/captcha", json={"answer": "ABCD"}
    )
    assert answered.status_code == 422


def test_strangers_cannot_see_or_answer_captchas(
    desk, customer, make_user, make_client, db_session
):
    v = add_vehicle(desk, customer.id, "GA 07 CAP 130")
    started = start(desk, v["id"])
    run_worker(db_session, expect=1)
    stranger = make_client(make_user("employee"))
    url = f"{API}/vehicle-lookups/{started['id']}/captcha"
    assert stranger.get(url).status_code == 404
    assert stranger.post(url, json={"answer": "ABCD"}).status_code == 404
    assert stranger.get(f"{API}/work-queue").json()["lookups_waiting_for_captcha"] == 0


def test_waiting_count_drops_when_access_is_lost(
    client, desk, customer, make_user, db_session
):
    v = add_vehicle(desk, customer.id, "GA 07 CAP 131")
    start(desk, v["id"])
    run_worker(db_session, expect=1)
    assert desk.get(f"{API}/work-queue").json()["lookups_waiting_for_captcha"] == 1
    other = make_user("employee")
    client.patch(f"{API}/vehicles/{v['id']}", json={"assigned_to": other.id})
    assert desk.get(f"{API}/work-queue").json()["lookups_waiting_for_captcha"] == 0
