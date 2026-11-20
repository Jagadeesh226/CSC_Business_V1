import json
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import (
    AuditLog,
    Customer,
    Job,
    JobAssignment,
    Service,
    ServiceCategory,
    User,
)
from app.repositories import job_repository
from app.schemas.job import JobCreate
from app.services import job_service
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError

JOBS = "/api/v1/jobs"


@pytest.fixture
def customer(make_customer):
    return make_customer()


@pytest.fixture
def service(make_service):
    return make_service()


@pytest.fixture
def job(client, customer, service):
    response = client.post(
        JOBS,
        json={"customer_id": customer.id, "service_id": service.id, "title": "Job"},
    )
    assert response.status_code == 201
    return response.json()


def assign(client, job_id, user_id):
    return client.post(f"{JOBS}/{job_id}/assignments", json={"user_id": user_id})


def audits(db_session, action, job_id):
    return db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.entity_id == job_id)
        .order_by(AuditLog.id)
    ).all()


# ---------- assign ----------


def test_assign_employee(client, admin_user, employee_user, job, db_session):
    response = assign(client, job["id"], employee_user.id)

    assert response.status_code == 201
    assert response.json()["assignees"] == [
        {"id": employee_user.id, "name": employee_user.name}
    ]
    entry = audits(db_session, "job_assigned", job["id"])[-1]
    assert entry.user_id == admin_user.id
    assert json.loads(entry.details) == {"user_id": employee_user.id}


def test_assign_admin_and_multiple_people(client, admin_user, make_user, job):
    first, second = make_user(), make_user()
    for user in (first, second, admin_user):
        assert assign(client, job["id"], user.id).status_code == 201

    assignees = client.get(f"{JOBS}/{job['id']}").json()["assignees"]
    assert [a["id"] for a in assignees] == [first.id, second.id, admin_user.id]


def test_assign_twice_is_conflict(client, employee_user, job):
    assign(client, job["id"], employee_user.id)
    response = assign(client, job["id"], employee_user.id)
    assert response.status_code == 409
    assert response.json() == {"detail": "This user is already assigned to the job."}


def test_assign_inactive_user(client, make_user, job):
    inactive = make_user(is_active=False)
    response = assign(client, job["id"], inactive.id)
    assert response.status_code == 422
    assert response.json() == {"detail": "Inactive users cannot be assigned."}


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_assign_to_closed_job(client, employee_user, job, db_session, status):
    db_session.get(Job, job["id"]).status = status
    db_session.flush()
    response = assign(client, job["id"], employee_user.id)
    assert response.status_code == 422
    assert status in response.json()["detail"]


def test_assign_unknown_job_or_user(client, employee_user, job):
    assert assign(client, 999999, employee_user.id).status_code == 404
    response = assign(client, job["id"], 999999)
    assert response.status_code == 404
    assert response.json() == {"detail": "User not found."}


@pytest.mark.parametrize(
    "body", [{}, {"user_id": 0}, {"user_id": 2**31}, {"user_id": 1, "x": 1}]
)
def test_assign_validation(client, job, body):
    response = client.post(f"{JOBS}/{job['id']}/assignments", json=body)
    assert response.status_code == 422


# ---------- unassign and history ----------


def test_unassign_keeps_history(client, admin_user, employee_user, job, db_session):
    assign(client, job["id"], employee_user.id)

    response = client.delete(f"{JOBS}/{job['id']}/assignments/{employee_user.id}")

    assert response.status_code == 200
    assert response.json()["assignees"] == []
    history = client.get(f"{JOBS}/{job['id']}/assignments").json()
    assert len(history) == 1
    assert history[0]["user"]["id"] == employee_user.id
    assert history[0]["assigned_by"]["id"] == admin_user.id
    assert history[0]["unassigned_by"]["id"] == admin_user.id
    assert history[0]["unassigned_at"] is not None
    assert audits(db_session, "job_unassigned", job["id"])[-1].user_id == admin_user.id


def test_reassign_after_unassign(client, employee_user, job):
    assign(client, job["id"], employee_user.id)
    client.delete(f"{JOBS}/{job['id']}/assignments/{employee_user.id}")

    assert assign(client, job["id"], employee_user.id).status_code == 201
    history = client.get(f"{JOBS}/{job['id']}/assignments").json()
    assert [h["unassigned_at"] is None for h in history] == [False, True]


def test_unassign_not_assigned(client, employee_user, job):
    response = client.delete(f"{JOBS}/{job['id']}/assignments/{employee_user.id}")
    assert response.status_code == 404
    assert client.delete(f"{JOBS}/999999/assignments/1").status_code == 404


# ---------- access control ----------


def test_assignment_management_is_admin_only(
    anon_client, employee_client, employee_user, job
):
    url = f"{JOBS}/{job['id']}/assignments"
    for c, expected in ((anon_client, 401), (employee_client, 403)):
        assert c.post(url, json={"user_id": employee_user.id}).status_code == expected
        assert c.delete(f"{url}/{employee_user.id}").status_code == expected


def test_employee_sees_history_only_of_assigned_jobs(
    client, employee_client, employee_user, job
):
    url = f"{JOBS}/{job['id']}/assignments"
    assert employee_client.get(url).status_code == 404

    assign(client, job["id"], employee_user.id)
    assert employee_client.get(url).status_code == 200


def test_employee_loses_access_when_unassigned(
    client, employee_client, employee_user, job
):
    assign(client, job["id"], employee_user.id)
    assert employee_client.get(f"{JOBS}/{job['id']}").status_code == 200

    client.delete(f"{JOBS}/{job['id']}/assignments/{employee_user.id}")

    assert employee_client.get(f"{JOBS}/{job['id']}").status_code == 404
    assert employee_client.get(JOBS).json()["total"] == 0


def test_employee_list_shows_only_assigned_jobs(
    client, make_client, make_user, customer, service
):
    me, other = make_user(), make_user()
    ids = [
        client.post(
            JOBS,
            json={"customer_id": customer.id, "service_id": service.id, "title": t},
        ).json()["id"]
        for t in ("Mine", "Theirs", "Nobody's")
    ]
    assign(client, ids[0], me.id)
    assign(client, ids[1], other.id)

    body = make_client(me).get(JOBS).json()
    assert [j["title"] for j in body["items"]] == ["Mine"]
    assert body["total"] == 1


# ---------- assigned_to filter ----------


def test_assigned_to_filter(client, make_client, admin_user, make_user, job):
    employee = make_user()
    assign(client, job["id"], employee.id)

    by_id = client.get(JOBS, params={"assigned_to": employee.id}).json()
    assert [j["id"] for j in by_id["items"]] == [job["id"]]
    assert client.get(JOBS, params={"assigned_to": "me"}).json()["total"] == 0

    mine = make_client(employee).get(JOBS, params={"assigned_to": "me"}).json()
    assert [j["id"] for j in mine["items"]] == [job["id"]]


def test_employee_cannot_use_filter_to_see_others_jobs(
    client, make_client, make_user, job
):
    me, other = make_user(), make_user()
    assign(client, job["id"], other.id)
    body = make_client(me).get(JOBS, params={"assigned_to": other.id}).json()
    assert body["total"] == 0


@pytest.mark.parametrize("value", ["you", "0", "-1", "2147483648", "1.5"])
def test_assigned_to_validation(client, value):
    assert client.get(JOBS, params={"assigned_to": value}).status_code == 422


# ---------- deactivation ----------


def test_deactivation_removes_user_from_open_jobs_only(
    client, admin_user, make_user, customer, service, db_session
):
    employee = make_user()
    open_job, closed_job = (
        client.post(
            JOBS,
            json={"customer_id": customer.id, "service_id": service.id, "title": t},
        ).json()
        for t in ("Open", "Closed")
    )
    assign(client, open_job["id"], employee.id)
    assign(client, closed_job["id"], employee.id)
    db_session.get(Job, closed_job["id"]).status = "completed"
    db_session.flush()

    client.post(f"/api/v1/users/{employee.id}/deactivate")

    assert client.get(f"{JOBS}/{open_job['id']}").json()["assignees"] == []
    # Completed jobs keep their record of who did the work.
    closed = client.get(f"{JOBS}/{closed_job['id']}").json()
    assert [a["id"] for a in closed["assignees"]] == [employee.id]
    entry = audits(db_session, "job_unassigned", open_job["id"])[-1]
    assert json.loads(entry.details) == {
        "user_id": employee.id,
        "reason": "user_deactivated",
    }
    history = client.get(f"{JOBS}/{open_job['id']}/assignments").json()
    assert history[0]["unassigned_by"]["id"] == admin_user.id


# ---------- performance ----------


def test_job_list_loads_assignees_without_n_plus_one(
    client, make_user, customer, service, test_engine
):
    for i in range(5):
        job = client.post(
            JOBS,
            json={
                "customer_id": customer.id,
                "service_id": service.id,
                "title": f"{i}",
            },
        ).json()
        assign(client, job["id"], make_user().id)

    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(test_engine, "before_cursor_execute", count)
    try:
        body = client.get(JOBS, params={"customer_id": customer.id}).json()
    finally:
        event.remove(test_engine, "before_cursor_execute", count)

    assert len(body["items"]) == 5
    assert all(len(j["assignees"]) == 1 for j in body["items"])
    assignment_queries = [s for s in statements if "job_assignments" in s]
    assert len(assignment_queries) == 1, statements


# ---------- concurrency (real commits) ----------


@pytest.fixture
def committed_job(test_engine):
    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE audit_logs, job_assignments, jobs, sequence_counters, "
                    "customers, services, service_categories, user_sessions, users "
                    "RESTART IDENTITY CASCADE"
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
            fee_type="fixed",
            default_service_fee=Decimal("10.00"),
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
        )
        session.commit()
        ids = job.id, admin.id, employee.id
    yield ids
    truncate()


def test_concurrent_assignment_of_same_user(test_engine, committed_job):
    job_id, admin_id, employee_id = committed_job
    workers = 6
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        with Session(test_engine) as session:
            admin = session.get(User, admin_id)
            barrier.wait(timeout=10)
            try:
                job_service.assign_user(session, job_id, employee_id, actor=admin)
                result = "assigned"
            except ConflictError:
                result = "conflict"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) == ["assigned"] + ["conflict"] * (workers - 1)
    with Session(test_engine) as session:
        active = session.scalars(
            select(JobAssignment).where(
                JobAssignment.job_id == job_id, JobAssignment.unassigned_at.is_(None)
            )
        ).all()
    assert len(active) == 1


def _run_while_locked(holder: Session, target):
    """Run ``target`` in a thread while ``holder`` keeps its row lock, check it
    is waiting on that lock, then commit ``holder`` and return the outcome.

    ``holder`` is always closed, so a failing assertion cannot leave a lock
    behind that would hang the cleanup.
    """
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["value"] = target()
        except Exception as exc:  # noqa: BLE001 - inspected by the caller
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    try:
        thread.join(timeout=1)
        blocked = thread.is_alive()
        holder.commit()
    finally:
        holder.close()
        thread.join(timeout=10)
    assert blocked, f"expected to wait on the row lock, got {result}"
    return result


def test_assign_waits_for_concurrent_deactivation(test_engine, committed_job):
    """Review M-1: a deactivation in flight must not miss a new assignment."""
    job_id, admin_id, employee_id = committed_job
    deactivator = Session(test_engine)
    deactivator.get(User, employee_id).is_active = False
    deactivator.flush()  # row-locks the user, not yet committed

    def assign_in_other_session():
        with Session(test_engine) as session:
            admin = session.get(User, admin_id)
            return job_service.assign_user(session, job_id, employee_id, actor=admin)

    result = _run_while_locked(deactivator, assign_in_other_session)

    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        assert session.scalars(select(JobAssignment)).all() == []


def test_concurrent_unassign_is_recorded_once(test_engine, committed_job):
    """Review L-1: two admins removing the same person: only one succeeds."""
    job_id, admin_id, employee_id = committed_job
    with Session(test_engine) as session:
        admin = session.get(User, admin_id)
        job_service.assign_user(session, job_id, employee_id, actor=admin)

    # The first removal holds the assignment row locked, uncommitted.
    first = Session(test_engine)
    assert job_repository.end_assignment(
        first, job_id, employee_id, admin_id, datetime.now(UTC)
    )

    def second_removal():
        with Session(test_engine) as session:
            admin = session.get(User, admin_id)
            return job_service.unassign_user(session, job_id, employee_id, actor=admin)

    result = _run_while_locked(first, second_removal)

    assert isinstance(result.get("error"), NotFoundError), result
    with Session(test_engine) as session:
        audited = session.scalars(
            select(AuditLog).where(AuditLog.action == "job_unassigned")
        ).all()
    # The first removal was a bare repository call, so no audit row: the
    # second must not have written one either.
    assert audited == []
