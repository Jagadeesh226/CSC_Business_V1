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
    assert body["voids_in_period"] == {"count": 1, "total": "25.50"}


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


# ---------- review fixes ----------


def test_a_later_void_is_visible_on_the_day_it_happened(client, make_job, db_session):
    """Cash-drawer reconciliation: 1 March's figure changes when an old
    payment is voided today, and today's report shows that void."""
    job = make_job()
    payment = pay(client, job["id"], "100.00")
    db_session.get(Payment, payment["id"]).received_at = datetime(
        2026, 3, 1, 6, 0, tzinfo=UTC
    )
    db_session.flush()
    march = {"date_from": "2026-03-01", "date_to": "2026-03-01"}
    assert report(client, **march)["total"] == "100.00"

    client.post(f"{API}/payments/{payment['id']}/void", json={"reason": "Bounced"})

    then = report(client, **march)
    assert (then["total"], then["voided"]["total"]) == ("0.00", "100.00")
    assert then["voids_in_period"] == {"count": 0, "total": "0.00"}
    today = report(client)
    assert today["voids_in_period"] == {"count": 1, "total": "100.00"}
    assert today["voided"] == {"count": 0, "total": "0.00"}  # not received today


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "9999-12-31", "date_to": "9999-12-31"},
        {"date_from": "1999-12-31", "date_to": "2000-01-02"},
        {"date_to": "2100-01-01"},
    ],
)
def test_dates_outside_the_supported_range_are_refused(client, params):
    response = client.get(f"{API}/reports/collections", params=params)
    assert response.status_code == 422
    assert "between" in response.json()["detail"]


def test_label_and_note_are_required_in_the_contract():
    from app.main import app

    schema = app.openapi()["components"]["schemas"]["CollectionsReport"]
    assert {"label", "note", "voids_in_period"} <= set(schema["required"])


def test_receiver_ties_and_unknown_receivers(
    client, make_job, make_user, make_client, db_session
):
    a = make_user("employee", name="Same Name")
    b = make_user("employee", name="Same Name")
    for user in (b, a):  # paid in reverse id order
        desk = make_client(user)
        pay(desk, make_job(who=desk)["id"], "10.00")
    orphan = pay(client, make_job()["id"], "10.00")
    db_session.get(Payment, orphan["id"]).recorded_by = None
    db_session.flush()

    receivers = report(client)["by_receiver"]
    assert [
        r["received_by"]["id"] if r["received_by"] else None for r in receivers
    ] == [
        min(a.id, b.id),
        max(a.id, b.id),
        None,
    ]


def test_money_on_a_job_with_no_fees_left_is_overpaid(client, make_job, db_session):
    job = make_job()
    pay(client, job["id"], "100.00")
    fee = client.get(f"{API}/jobs/{job['id']}/fees").json()["items"][0]
    client.delete(
        f"{API}/jobs/{job['id']}/fees/{fee['id']}", params={"reason": "Waived"}
    )
    summary = client.get(f"{API}/jobs/{job['id']}").json()["payment_summary"]
    assert summary["state"] == "overpaid"
    assert job["id"] in ids(client, payment="overpaid")
    assert job["id"] not in ids(client, payment="due") | ids(client, payment="paid")


def test_filter_agrees_with_the_job_summary(client, make_job):
    """Whatever the state, ?payment= and payment_summary.state agree."""
    jobs = [make_job(f"j{n}") for n in range(3)]
    pay(client, jobs[1]["id"], "30.00")
    pay(client, jobs[2]["id"], "100.00")
    state_for = {
        "due": {"unpaid", "partial"},
        "paid": {"paid"},
        "overpaid": {"overpaid"},
    }
    listing = client.get(f"{API}/jobs").json()["items"]
    for name, states in state_for.items():
        expected = {j["id"] for j in listing if j["payment_summary"]["state"] in states}
        assert ids(client, payment=name) == expected, name


def test_employee_overpaid_filter_is_scoped(client, make_job, make_user, make_client):
    desk = make_client(make_user("employee"))
    mine, other = make_job(who=desk), make_job()
    for job in (mine, other):
        pay(client, job["id"], "100.00")
        fee = client.get(f"{API}/jobs/{job['id']}/fees").json()["items"][0]
        client.patch(f"{API}/jobs/{job['id']}/fees/{fee['id']}", json={"amount": "50"})
    assert ids(desk, payment="overpaid") == {mine["id"]}
    assert ids(client, payment="overpaid") == {mine["id"], other["id"]}
