"""Phase 3.2: money in the job list and work queue; collections report."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Job, Payment
from app.repositories.readiness_repository import business_today

API = "/api/v1"
_keys = itertools.count(1)


@pytest.fixture
def make_job(client, make_customer, make_service):
    service = make_service(name="Certificate")  # fixed fee 100.00

    def _make(title="Job", who=None):
        response = (who or client).post(
            f"{API}/jobs",
            json={
                "customer_id": make_customer().id,
                "service_id": service.id,
                "title": title,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make


def pay(client, job_id, amount, method="cash", **extra):
    if method != "cash":
        extra.setdefault("reference_number", f"REF{next(_keys)}")
    response = client.post(
        f"{API}/jobs/{job_id}/payments",
        json={"amount": amount, "method": method, **extra},
        headers={"Idempotency-Key": f"collections-{next(_keys):06d}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def ids(client, **params):
    response = client.get(f"{API}/jobs", params=params)
    assert response.status_code == 200, response.text
    return {j["id"] for j in response.json()["items"]}


# ---------- job list and work queue ----------


def test_payment_filter(client, make_job, db_session):
    unpaid = make_job("unpaid")
    partial = make_job("partial")
    pay(client, partial["id"], "40.00")
    paid = make_job("paid")
    pay(client, paid["id"], "100.00")
    overpaid = make_job("overpaid")
    pay(client, overpaid["id"], "100.00")
    fee = client.get(f"{API}/jobs/{overpaid['id']}/fees").json()["items"][0]
    client.patch(
        f"{API}/jobs/{overpaid['id']}/fees/{fee['id']}", json={"amount": "80.00"}
    )
    completed_unpaid = make_job("completed, still owes")
    db_session.get(Job, completed_unpaid["id"]).status = "completed"
    cancelled = make_job("cancelled")
    client.post(f"{API}/jobs/{cancelled['id']}/cancel", json={"reason": "Left"})
    db_session.flush()

    assert ids(client, payment="due") == {
        unpaid["id"],
        partial["id"],
        completed_unpaid["id"],
    }
    assert ids(client, payment="paid") == {paid["id"]}
    assert ids(client, payment="overpaid") == {overpaid["id"]}
    assert client.get(f"{API}/jobs", params={"payment": "x"}).status_code == 422
    # A voided payment counts as never received.
    payment = client.get(f"{API}/jobs/{paid['id']}/payments").json()["items"][0]
    client.post(f"{API}/payments/{payment['id']}/void", json={"reason": "Mistake"})
    assert paid["id"] in ids(client, payment="due")


def test_payment_filter_combines_and_is_scoped(
    client, make_job, make_user, make_client
):
    employee = make_user("employee")
    desk = make_client(employee)
    mine = make_job("mine", who=desk)
    make_job("someone else's")
    assert ids(desk, payment="due") == {mine["id"]}
    assert ids(client, payment="due", status="pending") >= {mine["id"]}
    assert ids(client, payment="due", search="mine") == {mine["id"]}


def test_work_queue_money_counts(client, make_job, make_user, make_client):
    employee = make_user("employee")
    desk = make_client(employee)
    mine = make_job("mine", who=desk)
    other = make_job("other")
    pay(client, other["id"], "100.00")
    fee = client.get(f"{API}/jobs/{other['id']}/fees").json()["items"][0]
    client.patch(f"{API}/jobs/{other['id']}/fees/{fee['id']}", json={"amount": "90"})

    admin_view = client.get(f"{API}/work-queue").json()
    assert (admin_view["jobs_with_balance_due"], admin_view["jobs_overpaid"]) == (1, 1)
    desk_view = desk.get(f"{API}/work-queue").json()
    assert desk_view["jobs_with_balance_due"] == 1
    assert desk_view["jobs_overpaid"] is None  # admins settle overpayments
    pay(desk, mine["id"], "100.00")
    assert desk.get(f"{API}/work-queue").json()["jobs_with_balance_due"] == 0


# ---------- collections report ----------


def report(client, **params):
    response = client.get(f"{API}/reports/collections", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_collections_today(client, admin_user, make_job, make_user, make_client):
    employee = make_user("employee", name="Priya Counter")
    desk = make_client(employee)
    first, second, third = make_job(who=desk), make_job(who=desk), make_job()
    pay(desk, first["id"], "100.00")
    pay(desk, second["id"], "60.00", method="upi")
    pay(desk, second["id"], "40.00")
    mistake = pay(client, third["id"], "25.50", method="card")
    client.post(f"{API}/payments/{mistake['id']}/void", json={"reason": "Wrong job"})
    pay(client, third["id"], "10.00", method="upi")

    body = report(client)
    today = body["date_to"]
    assert body["date_from"] == today
    assert body["label"] == "Collections"
    assert "not profit" in body["note"]
    assert (body["count"], body["total"]) == (4, "210.00")
    assert body["by_method"] == [
        {"method": "cash", "count": 2, "total": "140.00"},
        {"method": "upi", "count": 2, "total": "70.00"},
    ]
    assert body["by_day"] == [
        {
            "date": today,
            "count": 4,
            "total": "210.00",
            "by_method": body["by_method"],
        }
    ]
    assert body["by_receiver"] == [
        {
            "received_by": {"id": employee.id, "name": "Priya Counter"},
            "count": 3,
            "total": "200.00",
        },
        {
            "received_by": {"id": admin_user.id, "name": "Admin Test"},
            "count": 1,
            "total": "10.00",
        },
    ]
    assert body["voided"] == {"count": 1, "total": "25.50"}


def test_collections_follow_the_centres_calendar(client, make_job, db_session):
    job = make_job()
    late = pay(client, job["id"], "30.00")
    early = pay(client, job["id"], "20.00")
    # 18:29 UTC = 23:59 India (1 March); 18:30 UTC = 00:00 India (2 March).
    db_session.get(Payment, late["id"]).received_at = datetime(
        2026, 3, 1, 18, 29, tzinfo=UTC
    )
    db_session.get(Payment, early["id"]).received_at = datetime(
        2026, 3, 1, 18, 30, tzinfo=UTC
    )
    db_session.flush()

    first = report(client, date_from="2026-03-01", date_to="2026-03-01")
    second = report(client, date_from="2026-03-02", date_to="2026-03-02")
    both = report(client, date_from="2026-03-01", date_to="2026-03-02")
    assert (first["total"], second["total"]) == ("30.00", "20.00")
    assert [(d["date"], d["total"]) for d in both["by_day"]] == [
        ("2026-03-01", "30.00"),
        ("2026-03-02", "20.00"),
    ]
    empty = report(client, date_from="2026-03-03", date_to="2026-03-03")
    assert (empty["count"], empty["total"], empty["by_day"]) == (0, "0.00", [])


def test_collections_period_rules(client, db_session):
    today = business_today(db_session)

    def status(**params):
        return client.get(f"{API}/reports/collections", params=params).status_code

    assert (
        status(
            date_from=today.isoformat(), date_to=(today - timedelta(days=1)).isoformat()
        )
        == 422
    )
    assert (
        status(
            date_from=(today - timedelta(days=366)).isoformat(),
            date_to=today.isoformat(),
        )
        == 422
    )
    assert (
        status(
            date_from=(today - timedelta(days=365)).isoformat(),
            date_to=today.isoformat(),
        )
        == 200
    )
    assert status(date_from="not-a-date") == 422
    only_from = report(client, date_from=(today - timedelta(days=3)).isoformat())
    # date_to defaults to today; a lone date_from before it is allowed.
    assert only_from["date_to"] == today.isoformat()


def test_collections_are_admin_only(employee_client, anon_client):
    assert employee_client.get(f"{API}/reports/collections").status_code == 403
    assert anon_client.get(f"{API}/reports/collections").status_code == 401
