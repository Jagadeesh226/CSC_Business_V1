"""Phase 3.0: payments and receipts."""

import json
import threading
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import (
    AuditLog,
    Customer,
    Job,
    JobFee,
    Payment,
    Receipt,
    Service,
    ServiceCategory,
    User,
)
from app.repositories import job_repository
from app.schemas.job import JobCreate
from app.schemas.payment import PaymentCreate, PaymentMethod
from app.services import job_service, payment_service
from app.services.errors import BusinessRuleError

API = "/api/v1"
_keys = iter(range(1, 100_000))


def key() -> str:
    return f"test-key-{next(_keys):06d}"


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="9876543210")


@pytest.fixture
def job(client, customer, make_service):
    """A job with the service fee (100.00) plus a department fee (50.00)."""
    created = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service(name="RC Renewal").id,
            "title": "RC renewal",
        },
    ).json()
    client.post(
        f"{API}/jobs/{created['id']}/fees",
        json={"fee_type": "department", "description": "RTO fee", "amount": "50"},
    )
    return created


def pay(client, job_id, amount="50.00", method="cash", idem=None, **extra):
    return client.post(
        f"{API}/jobs/{job_id}/payments",
        json={"amount": amount, "method": method, **extra},
        headers={"Idempotency-Key": idem or key()},
    )


def assign(client, job_id, user):
    response = client.post(
        f"{API}/jobs/{job_id}/assignments", json={"user_id": user.id}
    )
    assert response.status_code == 201


def year(db_session) -> int:
    return job_repository.current_year(db_session, job_service.BUSINESS_TIMEZONE)


# ---------- recording ----------


def test_record_payment_issues_a_receipt(client, admin_user, job, db_session):
    response = pay(client, job["id"], "60.00", notes="Paid at counter")

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["amount"], body["method"], body["status"]) == (
        "60.00",
        "cash",
        "received",
    )
    assert body["replayed"] is False
    assert body["recorded_by"]["id"] == admin_user.id
    assert body["notes"] == "Paid at counter"
    receipt = body["receipt"]
    assert receipt["receipt_number"] == f"RCP-{year(db_session)}-000001"
    assert receipt["cancelled"] is False
    assert body["summary"] == {
        "fees_total": "150.00",
        "paid": "60.00",
        "balance": "90.00",
        "state": "partial",
    }

    stored = db_session.scalar(select(Receipt))
    assert stored.snapshot == {
        "customer": {
            "id": job["customer"]["id"],
            "name": "Ravi Kumar",
            "phone": "******3210",
        },
        "job": {
            "id": job["id"],
            "job_number": job["job_number"],
            "title": "RC renewal",
            "service_name": "RC Renewal",
        },
        "fees": [
            {"fee_type": "service", "description": "RC Renewal", "amount": "100.00"},
            {"fee_type": "department", "description": "RTO fee", "amount": "50.00"},
        ],
        "fees_total": "150.00",
        "paid_before": "0.00",
        "amount": "60.00",
        "balance_after": "90.00",
        "method": "cash",
        "reference_number": None,
        "received_by": "Admin Test",
    }
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "payment_recorded")
    )
    assert (entry.entity_type, entry.entity_id) == ("job", job["id"])
    assert json.loads(entry.details) == {
        "payment_id": body["id"],
        "receipt_number": receipt["receipt_number"],
        "amount": "60.00",
        "method": "cash",
    }


def test_part_payments_until_paid(client, job, db_session):
    first = pay(client, job["id"], "100", method="upi", reference_number="UTR123456")
    second = pay(client, job["id"], "50.00", method="card", reference_number="AUTH-9")
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json()["summary"]["state"] == "paid"
    numbers = [
        r.receipt_number
        for r in db_session.scalars(select(Receipt).order_by(Receipt.id))
    ]
    assert numbers == [f"RCP-{year(db_session)}-00000{n}" for n in (1, 2)]
    snapshot = db_session.scalar(
        select(Receipt.snapshot).where(Receipt.payment_id == second.json()["id"])
    )
    assert (snapshot["paid_before"], snapshot["balance_after"]) == ("100.00", "0.00")

    nothing = pay(client, job["id"], "1")
    assert nothing.status_code == 422
    assert nothing.json()["detail"] == "Nothing is due on this job."


def test_amount_above_balance_is_refused(client, job):
    response = pay(client, job["id"], "150.01")
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "The amount is more than the balance due (Rs 150.00)."
    )


@pytest.mark.parametrize(
    "body",
    [
        {"amount": "0", "method": "cash"},
        {"amount": "-5", "method": "cash"},
        {"amount": "1.234", "method": "cash"},
        {"amount": "10", "method": "crypto"},
        {"amount": "10", "method": "upi"},  # reference required
        {"amount": "10", "method": "cheque", "reference_number": "  "},
        {"amount": "10", "method": "cash", "paid_by": "someone"},
        {"amount": "10", "method": "cash", "reference_number": "x" * 101},
    ],
)
def test_payment_validation(client, job, body):
    response = client.post(
        f"{API}/jobs/{job['id']}/payments",
        json=body,
        headers={"Idempotency-Key": key()},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("header", [None, "short", "has space in it", "x" * 101])
def test_idempotency_key_is_required_and_checked(client, job, header):
    headers = {} if header is None else {"Idempotency-Key": header}
    response = client.post(
        f"{API}/jobs/{job['id']}/payments",
        json={"amount": "10", "method": "cash"},
        headers=headers,
    )
    assert response.status_code == 422
    assert not client.get(f"{API}/jobs/{job['id']}/payments").json()["items"]


def test_cancelled_job_takes_no_payments(client, job):
    client.post(f"{API}/jobs/{job['id']}/cancel", json={"reason": "Customer left"})
    response = pay(client, job["id"])
    assert response.status_code == 422
    assert "cancelled" in response.json()["detail"]


def test_completed_job_still_takes_payment(client, job, db_session):
    db_session.get(Job, job["id"]).status = "completed"
    db_session.flush()
    assert pay(client, job["id"]).status_code == 201


# ---------- idempotency ----------


def test_retry_with_the_same_key_returns_the_first_payment(client, job, db_session):
    idem = key()
    first = pay(client, job["id"], "40.00", idem=idem)
    again = pay(client, job["id"], "40.00", idem=idem)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["replayed"] is True
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["receipt"] == first.json()["receipt"]
    assert len(db_session.scalars(select(Payment)).all()) == 1
    assert len(db_session.scalars(select(Receipt)).all()) == 1
    actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.action == "payment_recorded")
    ).all()
    assert actions == ["payment_recorded"]


def test_same_key_for_a_different_payment_is_refused(
    client, job, make_customer, make_service, employee_user, make_client
):
    idem = key()
    assert pay(client, job["id"], "40.00", idem=idem).status_code == 201
    other_job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": make_customer().id,
            "service_id": make_service().id,
            "title": "Other",
        },
    ).json()
    assign(client, job["id"], employee_user)
    employee = make_client(employee_user)

    for response in (
        pay(client, job["id"], "41.00", idem=idem),  # different amount
        pay(client, job["id"], "40.00", method="upi", reference_number="U1", idem=idem),
        pay(client, other_job["id"], "40.00", idem=idem),  # different job
        pay(employee, job["id"], "40.00", idem=idem),  # different person
    ):
        assert response.status_code == 409
        assert "Idempotency-Key" in response.json()["detail"]


def test_replay_checks_access_again(client, job, employee_user, make_client):
    assign(client, job["id"], employee_user)
    employee = make_client(employee_user)
    idem = key()
    assert pay(employee, job["id"], idem=idem).status_code == 201
    client.delete(f"{API}/jobs/{job['id']}/assignments/{employee_user.id}")
    assert pay(employee, job["id"], idem=idem).status_code == 404


# ---------- access ----------


def test_who_can_record_and_see_payments(
    client, job, employee_user, employee_client, anon_client
):
    assert pay(employee_client, job["id"]).status_code == 404
    assert employee_client.get(f"{API}/jobs/{job['id']}/payments").status_code == 404
    assert pay(anon_client, job["id"]).status_code == 401
    assert pay(client, 999_999).status_code == 404

    assign(client, job["id"], employee_user)
    recorded = pay(employee_client, job["id"], "20")
    assert recorded.status_code == 201
    assert recorded.json()["receipt"]["issued_by"]["id"] == employee_user.id
    listing = employee_client.get(f"{API}/jobs/{job['id']}/payments").json()
    assert [p["id"] for p in listing["items"]] == [recorded.json()["id"]]
    assert listing["summary"]["paid"] == "20.00"


def test_payment_needs_csrf_token(client, job):
    del client.headers["X-CSRF-Token"]
    assert pay(client, job["id"]).status_code == 403


# ---------- voiding ----------


def test_admin_voids_a_payment(client, job, employee_user, employee_client, db_session):
    assign(client, job["id"], employee_user)
    payment = pay(employee_client, job["id"], "150.00").json()
    url = f"{API}/payments/{payment['id']}/void"

    assert employee_client.post(url, json={"reason": "Typo"}).status_code == 403
    assert client.post(url, json={"reason": ""}).status_code == 422
    voided = client.post(url, json={"reason": "Entered on the wrong job"})

    assert voided.status_code == 200, voided.text
    body = voided.json()
    assert body["status"] == "voided"
    assert body["void_reason"] == "Entered on the wrong job"
    assert body["receipt"]["cancelled"] is True
    assert body["receipt"]["receipt_number"] == payment["receipt"]["receipt_number"]
    assert body["summary"]["state"] == "unpaid"
    assert client.post(url, json={"reason": "Again"}).status_code == 422
    assert (
        client.post(f"{API}/payments/999999/void", json={"reason": "x"}).status_code
        == 404
    )

    # The number is never reused; the employee may add fees again.
    again = pay(employee_client, job["id"], "10.00").json()
    assert again["receipt"]["receipt_number"].endswith("000002")
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "payment_voided")
    )
    assert json.loads(entry.details)["reason"] == "Entered on the wrong job"


def test_voided_payment_no_longer_blocks_employee_fees(
    client, job, employee_user, employee_client
):
    assign(client, job["id"], employee_user)
    payment = pay(employee_client, job["id"], "10.00").json()
    fee = {"fee_type": "third_party", "description": "Courier", "amount": "20"}
    assert (
        employee_client.post(f"{API}/jobs/{job['id']}/fees", json=fee).status_code
        == 422
    )
    client.post(f"{API}/payments/{payment['id']}/void", json={"reason": "Mistake"})
    assert (
        employee_client.post(f"{API}/jobs/{job['id']}/fees", json=fee).status_code
        == 201
    )


# ---------- money on the job ----------


def test_job_shows_payment_summary(client, job):
    assert client.get(f"{API}/jobs/{job['id']}").json()["payment_summary"] == {
        "fees_total": "150.00",
        "paid": "0.00",
        "balance": "150.00",
        "state": "unpaid",
    }
    pay(client, job["id"], "150")
    listed = client.get(f"{API}/jobs").json()["items"]
    assert listed[0]["payment_summary"]["state"] == "paid"


def test_lowering_fees_after_payment_shows_overpaid(client, job):
    pay(client, job["id"], "150.00")
    fees = client.get(f"{API}/jobs/{job['id']}/fees").json()["items"]
    department = next(f for f in fees if f["fee_type"] == "department")
    corrected = client.patch(
        f"{API}/jobs/{job['id']}/fees/{department['id']}", json={"amount": "30.00"}
    )
    assert corrected.status_code == 200, corrected.text
    summary = client.get(f"{API}/jobs/{job['id']}").json()["payment_summary"]
    assert (summary["balance"], summary["state"]) == ("-20.00", "overpaid")
    assert pay(client, job["id"], "1").status_code == 422


def test_receipt_snapshot_does_not_change_later(client, job, db_session, customer):
    payment = pay(client, job["id"], "10.00").json()
    client.patch(f"{API}/customers/{customer.id}", json={"name": "Ravi K Kumar"})
    client.patch(f"{API}/jobs/{job['id']}", json={"title": "Renamed"})
    snapshot = db_session.scalar(
        select(Receipt.snapshot).where(Receipt.payment_id == payment["id"])
    )
    assert (snapshot["customer"]["name"], snapshot["job"]["title"]) == (
        "Ravi Kumar",
        "RC renewal",
    )


def test_no_fees_job(client, make_customer, make_service, db_session):
    created = client.post(
        f"{API}/jobs",
        json={
            "customer_id": make_customer().id,
            "service_id": make_service().id,
            "title": "Free",
        },
    ).json()
    fee = db_session.scalar(select(JobFee).where(JobFee.job_id == created["id"]))
    fee.deleted_at = fee.created_at  # removed
    db_session.flush()
    summary = client.get(f"{API}/jobs/{created['id']}").json()["payment_summary"]
    assert summary["state"] == "no_fees"
    assert pay(client, created["id"]).status_code == 422


# ---------- races (committed data, real concurrent sessions) ----------


@pytest.fixture
def committed_job(test_engine):
    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE receipts, payments, audit_logs, job_fees, "
                    "job_status_history, job_assignments, job_checklist_items, "
                    "jobs, sequence_counters, customers, services, "
                    "service_categories, user_sessions, users "
                    "RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    with Session(test_engine) as session:
        category = ServiceCategory(name="Race", code="RACE")
        session.add(category)
        session.flush()
        service = Service(
            category_id=category.id,
            name="Race",
            code="RACE-SVC",
            fee_type="fixed",
            default_service_fee=Decimal("100.00"),
        )
        customer = Customer(name="Race Customer", phone="9000000000")
        admin = User(
            name="Admin",
            email="race.admin@example.com",
            password_hash=hash_password("unused-password"),
            role="admin",
        )
        session.add_all([service, customer, admin])
        session.flush()
        job = job_service.create_job(
            session,
            JobCreate(customer_id=customer.id, service_id=service.id, title="T"),
        )
        session.commit()
        ids = job.id, admin.id
    yield ids
    truncate()


def _race(test_engine, job_id, admin_id, requests):
    """Run payment requests at the same moment, each in its own session."""
    barrier = threading.Barrier(len(requests))
    results: list[object] = [None] * len(requests)

    def run(i, amount, idem):
        with Session(test_engine, autoflush=False) as session:
            admin = session.get(User, admin_id)
            barrier.wait()
            try:
                results[i] = payment_service.record_payment(
                    session,
                    job_id,
                    PaymentCreate(amount=Decimal(amount), method=PaymentMethod.CASH),
                    idem,
                    admin,
                )
            except Exception as exc:  # noqa: BLE001
                results[i] = exc

    threads = [
        threading.Thread(target=run, args=(i, amount, idem))
        for i, (amount, idem) in enumerate(requests)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return results


def test_racing_retries_record_once(test_engine, committed_job):
    job_id, admin_id = committed_job
    results = _race(test_engine, job_id, admin_id, [("40.00", "race-same-key")] * 4)

    assert all(not isinstance(r, Exception) for r in results), results
    assert len({r.id for r in results}) == 1
    assert sorted(r.replayed for r in results) == [False, True, True, True]
    with Session(test_engine) as session:
        assert len(session.scalars(select(Payment)).all()) == 1
        assert len(session.scalars(select(Receipt)).all()) == 1


def test_racing_payments_cannot_exceed_the_balance(test_engine, committed_job):
    job_id, admin_id = committed_job
    results = _race(
        test_engine, job_id, admin_id, [("70.00", "race-a"), ("70.00", "race-b")]
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], BusinessRuleError), results
    with Session(test_engine) as session:
        payments = session.scalars(select(Payment)).all()
        assert [p.amount for p in payments] == [Decimal("70.00")]
        numbers = session.scalars(select(Receipt.receipt_number)).all()
        assert len(numbers) == 1
