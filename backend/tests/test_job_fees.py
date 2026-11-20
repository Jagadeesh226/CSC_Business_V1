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
    Service,
    ServiceCategory,
    User,
)
from app.schemas.fee import FeeCreate, FeeType
from app.schemas.job import JobCreate
from app.services import fee_service, job_service
from app.services.errors import BusinessRuleError

JOBS = "/api/v1/jobs"
FEE = {"fee_type": "third_party", "description": "Stamp paper", "amount": "50.00"}


@pytest.fixture
def customer(make_customer):
    return make_customer()


@pytest.fixture
def variable_service(db_session, make_service):
    service = make_service()
    service.fee_type = "variable"
    service.default_service_fee = None
    db_session.flush()
    return service


@pytest.fixture
def job(client, customer, variable_service):
    """A job with no fees (variable-fee service)."""
    response = client.post(
        JOBS,
        json={
            "customer_id": customer.id,
            "service_id": variable_service.id,
            "title": "Job",
        },
    )
    assert response.status_code == 201
    return response.json()


def fees_url(job_id):
    return f"{JOBS}/{job_id}/fees"


def add(client, job_id, **overrides):
    return client.post(fees_url(job_id), json={**FEE, **overrides})


def assign(client, job_id, user_id):
    response = client.post(f"{JOBS}/{job_id}/assignments", json={"user_id": user_id})
    assert response.status_code == 201


def receive_payment(db_session, job_id, status="received"):
    db_session.add(Payment(job_id=job_id, amount=Decimal("10.00"), status=status))
    db_session.flush()


def set_status(db_session, job_id, status):
    db_session.get(Job, job_id).status = status
    db_session.flush()


# ---------- default service fee ----------


def test_fixed_fee_service_adds_default_fee(client, customer, make_service):
    service = make_service(name="PAN card")  # fixed fee 100.00
    job = client.post(
        JOBS,
        json={"customer_id": customer.id, "service_id": service.id, "title": "J"},
    ).json()

    body = client.get(fees_url(job["id"])).json()
    assert [(f["fee_type"], f["description"], f["amount"]) for f in body["items"]] == [
        ("service", "PAN card", "100.00")
    ]
    assert body["totals"] == {
        "service": "100.00",
        "third_party": "0.00",
        "department": "0.00",
        "total": "100.00",
    }


def test_variable_fee_service_adds_no_fee(client, job):
    body = client.get(fees_url(job["id"])).json()
    assert body["items"] == []
    assert body["totals"]["total"] == "0.00"


# ---------- add ----------


def test_admin_adds_fees_and_totals(client, admin_user, job, db_session):
    first = add(client, job["id"])
    assert first.status_code == 201
    add(client, job["id"], fee_type="department", description="RTO", amount="300.5")
    response = add(client, job["id"], fee_type="service", amount="99.99")

    body = response.json()
    assert body["fee_type"] == "service"
    assert body["amount"] == "99.99"
    assert body["created_by"]["id"] == admin_user.id
    totals = client.get(fees_url(job["id"])).json()["totals"]
    assert totals == {
        "service": "99.99",
        "third_party": "50.00",
        "department": "300.50",
        "total": "450.49",
    }
    entry = db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "fee_added", AuditLog.entity_id == job["id"])
        .order_by(AuditLog.id)
    )
    assert json.loads(entry.details) == {
        "fee_id": first.json()["id"],
        "fee_type": "third_party",
        "amount": "50.00",
    }
    assert entry.user_id == admin_user.id


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "0"},
        {"amount": "-5"},
        {"amount": "1.234"},
        {"amount": "10000000000.00"},
        {"amount": "abc"},
        {"fee_type": "tip"},
        {"description": ""},
        {"description": "x" * 501},
        {"created_by": 1},
    ],
)
def test_add_fee_validation(client, job, overrides):
    assert add(client, job["id"], **overrides).status_code == 422


def test_add_fee_unknown_job(client):
    assert add(client, 999999).status_code == 404


def test_employee_adds_fee_to_assigned_open_job(
    client, employee_client, employee_user, job
):
    assert add(employee_client, job["id"]).status_code == 404  # not assigned
    assign(client, job["id"], employee_user.id)

    response = add(employee_client, job["id"])

    assert response.status_code == 201
    assert response.json()["created_by"]["id"] == employee_user.id


def test_employee_cannot_add_after_payment_received(
    client, employee_client, employee_user, job, db_session
):
    assign(client, job["id"], employee_user.id)
    receive_payment(db_session, job["id"], status="pending")  # not received yet
    assert add(employee_client, job["id"]).status_code == 201

    receive_payment(db_session, job["id"])

    response = add(employee_client, job["id"])
    assert response.status_code == 422
    assert "payment has been received" in response.json()["detail"]
    # Admins can still add.
    assert add(client, job["id"]).status_code == 201


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_employee_cannot_add_to_closed_job(
    client, employee_client, employee_user, job, db_session, status
):
    assign(client, job["id"], employee_user.id)
    set_status(db_session, job["id"], status)

    response = add(employee_client, job["id"])
    assert response.status_code == 422
    assert response.json() == {"detail": f"Fees cannot be added to a {status} job."}
    assert add(client, job["id"]).status_code == 201  # admin correction


# ---------- edit and remove (admin only) ----------


def test_admin_edits_fee(client, admin_user, job, db_session):
    fee = add(client, job["id"]).json()

    response = client.patch(
        f"{fees_url(job['id'])}/{fee['id']}",
        json={"amount": "75.00", "description": "Stamp paper (2)"},
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "75.00"
    assert response.json()["description"] == "Stamp paper (2)"
    entry = db_session.scalar(select(AuditLog).where(AuditLog.action == "fee_updated"))
    assert entry.user_id == admin_user.id
    assert json.loads(entry.details)["changes"]["amount"] == {
        "old": "50.00",
        "new": "75.00",
    }


@pytest.mark.parametrize(
    "body", [{"amount": None}, {"amount": "0"}, {"fee_type": "x"}, {"job_id": 1}]
)
def test_edit_fee_validation(client, job, body):
    fee = add(client, job["id"]).json()
    response = client.patch(f"{fees_url(job['id'])}/{fee['id']}", json=body)
    assert response.status_code == 422


def test_admin_removes_fee_but_keeps_it(client, admin_user, job, db_session):
    fee = add(client, job["id"]).json()
    add(client, job["id"], amount="20.00")

    response = client.delete(f"{fees_url(job['id'])}/{fee['id']}")

    assert response.status_code == 204
    body = client.get(fees_url(job["id"])).json()
    assert [f["amount"] for f in body["items"]] == ["20.00"]
    assert body["totals"]["total"] == "20.00"
    everything = client.get(fees_url(job["id"]), params={"include_removed": True})
    removed = everything.json()["items"][0]
    assert removed["deleted_by"]["id"] == admin_user.id
    assert removed["deleted_at"] is not None
    assert everything.json()["totals"]["total"] == "20.00"
    assert db_session.get(JobFee, fee["id"]) is not None  # never erased
    # Removing or editing it again: it no longer counts as a fee.
    url = f"{fees_url(job['id'])}/{fee['id']}"
    assert client.delete(url).status_code == 404
    assert client.patch(url, json={"amount": "1.00"}).status_code == 404


def test_fee_must_belong_to_job(client, job, customer, variable_service):
    other = client.post(
        JOBS,
        json={
            "customer_id": customer.id,
            "service_id": variable_service.id,
            "title": "Other",
        },
    ).json()
    fee = add(client, other["id"]).json()
    url = f"{fees_url(job['id'])}/{fee['id']}"
    assert client.patch(url, json={"amount": "1.00"}).status_code == 404
    assert client.delete(url).status_code == 404


def test_admin_can_correct_fees_on_closed_job(client, job, db_session):
    fee = add(client, job["id"]).json()
    set_status(db_session, job["id"], "completed")
    url = f"{fees_url(job['id'])}/{fee['id']}"
    assert client.patch(url, json={"amount": "60.00"}).status_code == 200
    assert client.delete(url).status_code == 204


# ---------- access control ----------


def test_fee_routes_require_sign_in(anon_client, job):
    url = fees_url(job["id"])
    assert anon_client.get(url).status_code == 401
    assert anon_client.post(url, json=FEE).status_code == 401
    assert anon_client.patch(f"{url}/1", json={"amount": "1"}).status_code == 401
    assert anon_client.delete(f"{url}/1").status_code == 401


def test_employee_cannot_edit_or_remove_fees(
    client, employee_client, employee_user, job
):
    assign(client, job["id"], employee_user.id)
    fee = add(employee_client, job["id"]).json()
    url = f"{fees_url(job['id'])}/{fee['id']}"

    edit = employee_client.patch(url, json={"amount": "1.00"})
    assert edit.status_code == 403
    assert edit.json() == {"detail": "Admin access required."}
    assert employee_client.delete(url).status_code == 403


def test_employee_sees_fees_of_assigned_jobs_only(
    client, employee_client, employee_user, job
):
    add(client, job["id"])
    assert employee_client.get(fees_url(job["id"])).status_code == 404
    assign(client, job["id"], employee_user.id)
    body = employee_client.get(fees_url(job["id"])).json()
    assert body["totals"]["total"] == "50.00"


def test_fee_write_requires_csrf(client, job):
    del client.headers["X-CSRF-Token"]
    assert add(client, job["id"]).status_code == 403


# ---------- race: payment received while an employee adds a fee ----------


@pytest.fixture
def committed_job(test_engine):
    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE audit_logs, payments, job_fees, job_status_history, "
                    "job_assignments, jobs, sequence_counters, customers, services, "
                    "service_categories, user_sessions, users RESTART IDENTITY "
                    "CASCADE"
                )
            )

    truncate()
    with Session(test_engine) as session:
        category = ServiceCategory(name="Conc", code="CONC")
        session.add(category)
        session.flush()
        service = Service(
            category_id=category.id,
            name="Conc",
            code="CONC-SVC",
            fee_type="variable",
        )
        customer = Customer(name="Conc Customer")
        admin = User(
            name="Admin",
            email="conc.admin@example.com",
            password_hash=hash_password("unused-password"),
            role="admin",
        )
        employee = User(
            name="Employee",
            email="conc.employee@example.com",
            password_hash=hash_password("unused-password"),
            role="employee",
        )
        session.add_all([service, customer, admin, employee])
        session.flush()
        job = job_service.create_job(
            session,
            JobCreate(customer_id=customer.id, service_id=service.id, title="T"),
            creator=employee,  # auto-assigned
        )
        session.commit()
        ids = job.id, admin.id, employee.id
    yield ids
    truncate()


def test_employee_fee_waits_for_payment_being_recorded(test_engine, committed_job):
    """A payment recorded under the job lock (as Phase 3 must) is seen by a
    concurrent employee fee, which is then refused."""
    job_id, _, employee_id = committed_job
    recorder = Session(test_engine, autoflush=False)
    try:
        # What Phase 3 payment recording must do: lock the job, then insert.
        from app.repositories import job_repository

        job_repository.get_job_for_update(recorder, job_id)
        recorder.add(Payment(job_id=job_id, amount=Decimal("10.00"), status="received"))
        recorder.flush()

        result: dict[str, object] = {}

        def add_fee():
            with Session(test_engine, autoflush=False) as session:
                employee = session.get(User, employee_id)
                try:
                    result["value"] = fee_service.add_fee(
                        session,
                        job_id,
                        FeeCreate(
                            fee_type=FeeType.THIRD_PARTY,
                            description="Late",
                            amount=Decimal("5.00"),
                        ),
                        actor=employee,
                    )
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc

        thread = threading.Thread(target=add_fee)
        thread.start()
        thread.join(timeout=1)
        blocked = thread.is_alive()
        recorder.commit()
    finally:
        recorder.close()
    thread.join(timeout=10)

    assert blocked, f"expected to wait on the job lock, got {result}"
    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        assert session.scalars(select(JobFee)).all() == []
