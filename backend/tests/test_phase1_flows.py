"""Phase 1 whole-flow tests.

Each test drives one realistic story end to end through the HTTP API, as the
frontend will: sign-in, users, jobs, assignment, status, fees and the audit
trail together (payments, from Phase 3, through the API as well).
"""

import json

import pytest
from sqlalchemy import select

from app.models import AuditLog

API = "/api/v1"
TEMP_PASSWORD = "temporary-pass-1"
NEW_PASSWORD = "my-own-password-1"


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="9000000123")


@pytest.fixture
def service(make_service):
    return make_service(name="RC Renewal")  # fixed fee 100.00


def onboard_employee(admin, make_client, name, email):
    """Admin creates an employee, who signs in and sets their own password."""
    created = admin.post(
        f"{API}/users",
        json={"name": name, "email": email, "temporary_password": TEMP_PASSWORD},
    )
    assert created.status_code == 201, created.text
    assert created.json()["must_change_password"] is True

    employee = make_client()
    login = employee.post(
        f"{API}/auth/login", json={"email": email, "password": TEMP_PASSWORD}
    )
    assert login.status_code == 200
    employee.headers["X-CSRF-Token"] = login.json()["csrf_token"]

    blocked = employee.get(f"{API}/jobs")
    assert blocked.status_code == 403  # must change password first
    changed = employee.post(
        f"{API}/auth/change-password",
        json={"current_password": TEMP_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200
    assert employee.get(f"{API}/jobs").status_code == 200
    return employee, created.json()


def job_audit(db_session, job_id):
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_type == "job", AuditLog.entity_id == job_id)
            .order_by(AuditLog.id)
        )
    )


def move(client, job_id, status, reason=None):
    body = {"status": status} | ({"reason": reason} if reason else {})
    response = client.post(f"{API}/jobs/{job_id}/status", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_job_from_creation_to_completion(
    client, admin_user, make_client, customer, service, db_session
):
    # 1. An admin onboards an employee.
    employee, emp = onboard_employee(client, make_client, "Priya", "priya@example.com")

    # 2. The employee creates a job: auto-assigned, default fee added.
    created = employee.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": service.id,
            "title": "RC renewal for KA-01-AB-1234",
            "due_date": "2026-12-01",
        },
    )
    assert created.status_code == 201
    job = created.json()
    job_id = job["id"]
    assert job["status"] == "pending"
    assert job["created_by"] == emp["id"]
    assert job["assignees"] == [{"id": emp["id"], "name": "Priya"}]
    assert job["allowed_next_statuses"] == ["in_progress"]

    fees = employee.get(f"{API}/jobs/{job_id}/fees").json()
    assert fees["totals"]["service"] == "100.00"

    # 3. Work happens: status moves and fees.
    move(employee, job_id, "in_progress", reason="Customer documents received")
    move(employee, job_id, "waiting_for_documents", reason="Need insurance copy")
    for fee in (
        {"fee_type": "department", "description": "RTO fee", "amount": "450"},
        {"fee_type": "third_party", "description": "Courier", "amount": "60.5"},
    ):
        assert employee.post(f"{API}/jobs/{job_id}/fees", json=fee).status_code == 201
    move(employee, job_id, "in_progress")
    employee.patch(f"{API}/jobs/{job_id}", json={"description": "Insurance received"})

    totals = employee.get(f"{API}/jobs/{job_id}/fees").json()["totals"]
    assert totals == {
        "service": "100.00",
        "third_party": "60.50",
        "department": "450.00",
        "total": "610.50",
    }

    # 4. The customer pays the full amount.
    paid = employee.post(
        f"{API}/jobs/{job_id}/payments",
        json={"amount": "610.50", "method": "cash"},
        headers={"Idempotency-Key": "phase1-flow-payment"},
    )
    assert paid.status_code == 201, paid.text
    late_fee = employee.post(
        f"{API}/jobs/{job_id}/fees",
        json={"fee_type": "third_party", "description": "Late", "amount": "10"},
    )
    assert late_fee.status_code == 422
    assert "payment has been received" in late_fee.json()["detail"]

    # 5. Submitted and completed; the job is now closed.
    move(employee, job_id, "submitted")
    done = move(employee, job_id, "completed", reason="RC issued")
    assert done["allowed_next_statuses"] == []
    assert (
        employee.patch(f"{API}/jobs/{job_id}", json={"title": "Too late"}).status_code
        == 422
    )
    assert (
        client.post(f"{API}/jobs/{job_id}/cancel", json={"reason": "x"}).status_code
        == 422
    )
    assert (
        client.delete(f"{API}/jobs/{job_id}/assignments/{emp['id']}").status_code == 422
    )

    # 6. The record is complete.
    history = client.get(f"{API}/jobs/{job_id}/status-history").json()
    assert [(h["old_status"], h["new_status"]) for h in history] == [
        (None, "pending"),
        ("pending", "in_progress"),
        ("in_progress", "waiting_for_documents"),
        ("waiting_for_documents", "in_progress"),
        ("in_progress", "submitted"),
        ("submitted", "completed"),
    ]
    assert {h["changed_by"]["id"] for h in history} == {emp["id"]}
    assert history[-1]["reason"] == "RC issued"

    assert job_audit(db_session, job_id) == [
        "job_created",
        "fee_added",  # automatic service fee
        "job_assigned",  # creator auto-assigned
        "job_status_changed",
        "job_status_changed",
        "fee_added",
        "fee_added",
        "job_status_changed",
        "job_updated",
        "payment_recorded",
        "job_status_changed",
        "job_status_changed",
    ]
    details = " ".join(
        d or "" for d in db_session.scalars(select(AuditLog.details)).all()
    )
    for secret in (TEMP_PASSWORD, NEW_PASSWORD, "password_hash", "csrf"):
        assert secret not in details


def test_cancelled_job(client, admin_user, make_client, customer, service, db_session):
    employee, emp = onboard_employee(client, make_client, "Arun", "arun@example.com")
    job = client.post(
        f"{API}/jobs",
        json={"customer_id": customer.id, "service_id": service.id, "title": "PAN"},
    ).json()
    job_id = job["id"]
    assert job["assignees"] == []  # admins are not auto-assigned

    # Not assigned yet: invisible to the employee.
    assert employee.get(f"{API}/jobs/{job_id}").status_code == 404
    assert employee.get(f"{API}/jobs").json()["total"] == 0

    client.post(f"{API}/jobs/{job_id}/assignments", json={"user_id": emp["id"]})
    move(employee, job_id, "in_progress")

    # Employees can't cancel; the admin cancels with a reason.
    assert (
        employee.post(f"{API}/jobs/{job_id}/cancel", json={"reason": "x"}).status_code
        == 403
    )
    cancelled = client.post(
        f"{API}/jobs/{job_id}/cancel", json={"reason": "Customer withdrew"}
    )
    assert cancelled.json()["status"] == "cancelled"

    # Closed: employee is read-only; the admin can still correct fees.
    assert employee.get(f"{API}/jobs/{job_id}").status_code == 200
    assert (
        employee.post(
            f"{API}/jobs/{job_id}/status", json={"status": "submitted"}
        ).status_code
        == 422
    )
    assert (
        employee.post(
            f"{API}/jobs/{job_id}/fees",
            json={"fee_type": "service", "description": "x", "amount": "1"},
        ).status_code
        == 422
    )
    fee_id = client.get(f"{API}/jobs/{job_id}/fees").json()["items"][0]["id"]
    assert client.delete(f"{API}/jobs/{job_id}/fees/{fee_id}").status_code == 204
    assert client.get(f"{API}/jobs/{job_id}/fees").json()["totals"]["total"] == "0.00"

    last = client.get(f"{API}/jobs/{job_id}/status-history").json()[-1]
    assert (last["new_status"], last["reason"]) == ("cancelled", "Customer withdrew")
    assert last["changed_by"]["id"] == admin_user.id
    assert job_audit(db_session, job_id)[-2:] == ["job_cancelled", "fee_removed"]


def test_employee_leaves(client, make_client, customer, service, db_session):
    employee, emp = onboard_employee(client, make_client, "Meena", "meena@example.com")
    open_job, finished_job = (
        employee.post(
            f"{API}/jobs",
            json={"customer_id": customer.id, "service_id": service.id, "title": t},
        ).json()["id"]
        for t in ("Open work", "Finished work")
    )
    for status in ("in_progress", "submitted", "completed"):
        move(employee, finished_job, status)

    deactivated = client.post(f"{API}/users/{emp['id']}/deactivate")
    assert deactivated.json()["is_active"] is False

    # Signed out everywhere and unable to sign back in.
    assert employee.get(f"{API}/auth/me").status_code == 401
    relogin = make_client().post(
        f"{API}/auth/login",
        json={"email": "meena@example.com", "password": NEW_PASSWORD},
    )
    assert relogin.status_code == 401

    # Removed from open work; completed work keeps the record.
    assert client.get(f"{API}/jobs/{open_job}").json()["assignees"] == []
    finished = client.get(f"{API}/jobs/{finished_job}").json()
    assert [a["id"] for a in finished["assignees"]] == [emp["id"]]
    unassigned = db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "job_unassigned", AuditLog.entity_id == open_job
        )
    )
    assert json.loads(unassigned.details)["reason"] == "user_deactivated"

    # Coming back does not silently restore old open work.
    client.post(f"{API}/users/{emp['id']}/reactivate")
    back = make_client()
    login = back.post(
        f"{API}/auth/login",
        json={"email": "meena@example.com", "password": NEW_PASSWORD},
    )
    assert login.status_code == 200
    visible = [j["id"] for j in back.get(f"{API}/jobs").json()["items"]]
    assert visible == [finished_job]


def test_employees_cannot_see_each_others_work(client, make_client, customer, service):
    alice, _ = onboard_employee(client, make_client, "Alice", "alice@example.com")
    bob, _ = onboard_employee(client, make_client, "Bob", "bob@example.com")
    job_id = alice.post(
        f"{API}/jobs",
        json={"customer_id": customer.id, "service_id": service.id, "title": "A1"},
    ).json()["id"]
    job = f"{API}/jobs/{job_id}"

    assert bob.get(f"{API}/jobs").json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "total_pages": 0,
    }
    assert bob.get(f"{API}/jobs", params={"search": "A1"}).json()["total"] == 0
    for path in ("", "/assignments", "/status-history", "/fees"):
        assert bob.get(job + path).status_code == 404, path
    assert bob.patch(job, json={"title": "x"}).status_code == 404
    assert bob.post(f"{job}/status", json={"status": "in_progress"}).status_code == 404
    assert (
        bob.post(
            f"{job}/fees",
            json={"fee_type": "service", "description": "x", "amount": "1"},
        ).status_code
        == 404
    )
    # Admin-only actions are refused outright, before any lookup.
    assert bob.post(f"{job}/cancel", json={"reason": "x"}).status_code == 403
    assert bob.post(f"{job}/assignments", json={"user_id": 1}).status_code == 403
    assert bob.get(f"{API}/users").status_code == 403
