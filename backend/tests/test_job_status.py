import json
import threading
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import (
    AuditLog,
    Customer,
    Job,
    JobAssignment,
    JobStatusHistory,
    Service,
    ServiceCategory,
    User,
)
from app.schemas.job import JobCreate, JobStatus, JobUpdate
from app.services import job_service, user_service
from app.services.errors import BusinessRuleError

JOBS = "/api/v1/jobs"

# A path through every normal move, ending in completed.
FULL_PATH = [
    "in_progress",
    "waiting_for_documents",
    "in_progress",
    "submitted",
    "completed",
]


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


def move(client, job_id, status, reason=None):
    body = {"status": status}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"{JOBS}/{job_id}/status", json=body)


def set_status(db_session, job_id, status):
    db_session.get(Job, job_id).status = status
    db_session.flush()


# ---------- moves ----------


def test_new_job_offers_next_status(job):
    assert job["status"] == "pending"
    assert job["allowed_next_statuses"] == ["in_progress"]


def test_full_lifecycle(client, admin_user, job, db_session):
    for status in FULL_PATH:
        response = move(client, job["id"], status)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status

    final = response.json()
    assert final["allowed_next_statuses"] == []
    history = client.get(f"{JOBS}/{job['id']}/status-history").json()
    assert [(h["old_status"], h["new_status"]) for h in history] == [
        (None, "pending"),
        ("pending", "in_progress"),
        ("in_progress", "waiting_for_documents"),
        ("waiting_for_documents", "in_progress"),
        ("in_progress", "submitted"),
        ("submitted", "completed"),
    ]
    assert all(h["changed_by"]["id"] == admin_user.id for h in history)


@pytest.mark.parametrize(
    "current, allowed",
    [
        ("pending", ["in_progress"]),
        ("in_progress", ["waiting_for_documents", "submitted"]),
        ("waiting_for_documents", ["in_progress"]),
        ("submitted", ["completed"]),
        ("completed", []),
        ("cancelled", []),
    ],
)
def test_allowed_moves_match_rules(client, job, db_session, current, allowed):
    set_status(db_session, job["id"], current)
    assert client.get(f"{JOBS}/{job['id']}").json()["allowed_next_statuses"] == allowed

    for target in (
        "pending",
        "in_progress",
        "waiting_for_documents",
        "submitted",
        "completed",
    ):
        if target == current:
            continue
        response = move(client, job["id"], target)
        expected = 200 if target in allowed else 422
        assert response.status_code == expected, (current, target, response.text)
        set_status(db_session, job["id"], current)


def test_blocked_move_explains_options(client, job):
    response = move(client, job["id"], "completed")
    assert response.status_code == 422
    assert response.json() == {"detail": "A pending job can only move to: in_progress."}


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_closed_job_cannot_change_status(client, job, db_session, status):
    set_status(db_session, job["id"], status)
    response = move(client, job["id"], "in_progress")
    assert response.status_code == 422
    assert response.json() == {"detail": f"A {status} job cannot change status."}


def test_cancel_is_not_a_normal_move(client, job):
    response = move(client, job["id"], "cancelled")
    assert response.status_code == 422
    assert response.json() == {"detail": "Use the cancel action to cancel a job."}


def test_reason_is_optional_and_stored(client, job):
    move(client, job["id"], "in_progress", reason="  Customer brought papers  ")
    history = client.get(f"{JOBS}/{job['id']}/status-history").json()
    assert history[-1]["reason"] == "Customer brought papers"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": "unknown"},
        {"status": "in_progress", "reason": ""},
        {"status": "in_progress", "reason": "x" * 1001},
        {"status": "in_progress", "changed_by": 1},
    ],
)
def test_status_change_validation(client, job, body):
    assert client.post(f"{JOBS}/{job['id']}/status", json=body).status_code == 422


def test_status_change_unknown_job(client):
    assert move(client, 999999, "in_progress").status_code == 404


def test_status_changes_are_audited(client, admin_user, job, db_session):
    move(client, job["id"], "in_progress", reason="Started")
    entry = db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "job_status_changed", AuditLog.entity_id == job["id"]
        )
    )
    assert entry.user_id == admin_user.id
    assert json.loads(entry.details) == {
        "from": "pending",
        "to": "in_progress",
        "reason": "Started",
    }


def test_closing_a_job_applies_closed_job_rules(
    client, employee_client, employee_user, job
):
    client.post(f"{JOBS}/{job['id']}/assignments", json={"user_id": employee_user.id})
    for status in FULL_PATH:
        move(client, job["id"], status)

    edit = employee_client.patch(f"{JOBS}/{job['id']}", json={"title": "Late"})
    assert edit.status_code == 422
    remove = client.delete(f"{JOBS}/{job['id']}/assignments/{employee_user.id}")
    assert remove.status_code == 422


# ---------- cancel ----------


@pytest.mark.parametrize("current", ["pending", "in_progress", "submitted"])
def test_admin_cancels_job(client, admin_user, job, db_session, current):
    set_status(db_session, job["id"], current)

    response = client.post(
        f"{JOBS}/{job['id']}/cancel", json={"reason": "Customer withdrew"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["allowed_next_statuses"] == []
    last = client.get(f"{JOBS}/{job['id']}/status-history").json()[-1]
    assert (last["old_status"], last["new_status"]) == (current, "cancelled")
    assert last["reason"] == "Customer withdrew"
    entry = db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "job_cancelled", AuditLog.entity_id == job["id"]
        )
    )
    assert entry.user_id == admin_user.id


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
def test_cancel_requires_reason(client, job, body):
    assert client.post(f"{JOBS}/{job['id']}/cancel", json=body).status_code == 422


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_cannot_cancel_closed_job(client, job, db_session, status):
    set_status(db_session, job["id"], status)
    response = client.post(f"{JOBS}/{job['id']}/cancel", json={"reason": "x"})
    assert response.status_code == 422
    assert response.json() == {"detail": f"A {status} job cannot be cancelled."}


def test_cancel_unknown_job(client):
    assert client.post(f"{JOBS}/999999/cancel", json={"reason": "x"}).status_code == 404


# ---------- access control ----------


def test_status_routes_require_sign_in(anon_client, job):
    assert move(anon_client, job["id"], "in_progress").status_code == 401
    url = f"{JOBS}/{job['id']}"
    assert anon_client.post(f"{url}/cancel", json={"reason": "x"}).status_code == 401
    assert anon_client.get(f"{url}/status-history").status_code == 401


def test_employee_changes_status_of_assigned_job_only(
    client, employee_client, employee_user, job
):
    assert move(employee_client, job["id"], "in_progress").status_code == 404
    history_url = f"{JOBS}/{job['id']}/status-history"
    assert employee_client.get(history_url).status_code == 404

    client.post(f"{JOBS}/{job['id']}/assignments", json={"user_id": employee_user.id})

    for status in FULL_PATH:  # employees may complete their assigned jobs
        assert move(employee_client, job["id"], status).status_code == 200
    history = employee_client.get(history_url).json()
    assert history[-1]["changed_by"]["id"] == employee_user.id


def test_employee_cannot_cancel(client, employee_client, employee_user, job):
    client.post(f"{JOBS}/{job['id']}/assignments", json={"user_id": employee_user.id})
    response = employee_client.post(f"{JOBS}/{job['id']}/cancel", json={"reason": "x"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin access required."}


def test_status_change_requires_csrf(client, job):
    del client.headers["X-CSRF-Token"]
    assert move(client, job["id"], "in_progress").status_code == 403


# ---------- creation entry and database rule ----------


def test_employee_created_job_history_names_creator(
    employee_client, employee_user, customer, service
):
    job = employee_client.post(
        JOBS,
        json={"customer_id": customer.id, "service_id": service.id, "title": "E"},
    ).json()
    history = employee_client.get(f"{JOBS}/{job['id']}/status-history").json()
    assert len(history) == 1
    assert history[0]["old_status"] is None
    assert history[0]["new_status"] == "pending"
    assert history[0]["changed_by"]["id"] == employee_user.id


def test_database_rejects_unknown_status(db_session, job):
    db_session.get(Job, job["id"]).status = "on_hold"
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------- concurrency (real commits) ----------


@pytest.fixture
def committed_job(test_engine):
    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE audit_logs, job_status_history, job_assignments, jobs, "
                    "sequence_counters, customers, services, service_categories, "
                    "user_sessions, users RESTART IDENTITY CASCADE"
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


def _run_while_locked(holder: Session, target):
    """Run ``target`` in a thread while ``holder`` keeps its row lock, check it
    waits on that lock, then commit ``holder``. ``holder`` is always closed."""
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
    assert blocked, f"expected to wait on the job row lock, got {result}"
    return result


def test_concurrent_status_changes_apply_once(test_engine, committed_job):
    job_id, admin_id, _ = committed_job
    first = Session(test_engine)
    first_admin = first.get(User, admin_id)
    # First change applied but not committed: it holds the job row.
    job = first.get(Job, job_id)
    job_service._record_status_change(
        first, job, JobStatus.IN_PROGRESS.value, first_admin, None
    )
    first.flush()

    def second_change():
        with Session(test_engine) as session:
            admin = session.get(User, admin_id)
            return job_service.change_status(
                session, job_id, JobStatus.IN_PROGRESS, actor=admin
            )

    result = _run_while_locked(first, second_change)

    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        moves = session.scalars(
            select(JobStatusHistory).where(
                JobStatusHistory.job_id == job_id,
                JobStatusHistory.new_status == "in_progress",
            )
        ).all()
    assert len(moves) == 1


def test_assign_waits_for_job_being_closed(test_engine, committed_job):
    job_id, admin_id, employee_id = committed_job
    closer = Session(test_engine)
    closer_admin = closer.get(User, admin_id)
    job = closer.get(Job, job_id)
    job_service._record_status_change(
        closer, job, JobStatus.CANCELLED.value, closer_admin, "Closing"
    )
    closer.flush()

    def assign():
        with Session(test_engine) as session:
            admin = session.get(User, admin_id)
            return job_service.assign_user(session, job_id, employee_id, actor=admin)

    result = _run_while_locked(closer, assign)

    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        assert session.scalars(select(JobAssignment)).all() == []


# ---------- races through the real service functions ----------
# Each "holder" runs a real service call in an app-like session
# (autoflush=False), paused just before its commit so it keeps its locks.


def _paused(test_engine, call, *, flush: bool = True):
    """Run ``call(session)`` with its commit deferred; return the open session.

    ``flush=False`` pauses exactly where the real commit would start, before
    anything pending is written. That is the window review M-1 found: with
    autoflush off, a deactivation that only *sets* is_active holds no lock on
    the user until commit. Use ``flush=True`` when the call needs its earlier
    writes visible to later steps (several status moves in one transaction).
    """
    session = Session(test_engine, autoflush=False)
    real_commit = session.commit
    session.commit = session.flush if flush else (lambda: None)
    try:
        call(session)
    except BaseException:
        session.close()
        raise
    session.commit = real_commit
    return session


def _assign_committed(test_engine, job_id, admin_id, user_id):
    with Session(test_engine, autoflush=False) as session:
        admin = session.get(User, admin_id)
        job_service.assign_user(session, job_id, user_id, actor=admin)


def _move_to_completed(session, job_id, admin_id):
    admin = session.get(User, admin_id)
    for status in ("in_progress", "submitted", "completed"):
        job_service.change_status(session, job_id, JobStatus(status), actor=admin)


def test_assign_waits_for_real_deactivation(test_engine, committed_job):
    """Review M-1 (both rounds): assignment during deactivation is refused."""
    job_id, admin_id, employee_id = committed_job
    holder = _paused(
        test_engine,
        lambda s: user_service.deactivate_user(s, s.get(User, admin_id), employee_id),
        flush=False,
    )

    def assign():
        with Session(test_engine, autoflush=False) as session:
            admin = session.get(User, admin_id)
            return job_service.assign_user(session, job_id, employee_id, actor=admin)

    result = _run_while_locked(holder, assign)

    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        assert session.scalars(select(JobAssignment)).all() == []


def test_deactivation_waits_for_job_being_completed(test_engine, committed_job):
    """Review M-2: a job completed meanwhile keeps its assignee."""
    job_id, admin_id, employee_id = committed_job
    _assign_committed(test_engine, job_id, admin_id, employee_id)
    holder = _paused(test_engine, lambda s: _move_to_completed(s, job_id, admin_id))

    def deactivate():
        with Session(test_engine, autoflush=False) as session:
            admin = session.get(User, admin_id)
            return user_service.deactivate_user(session, admin, employee_id)

    result = _run_while_locked(holder, deactivate)

    assert "error" not in result, result
    with Session(test_engine) as session:
        assert session.get(Job, job_id).status == "completed"
        active = session.scalars(
            select(JobAssignment).where(JobAssignment.unassigned_at.is_(None))
        ).all()
        assert [a.user_id for a in active] == [employee_id]
        assert session.get(User, employee_id).is_active is False
        removals = session.scalars(
            select(AuditLog).where(AuditLog.action == "job_unassigned")
        ).all()
        assert removals == []


def test_employee_edit_waits_for_job_being_completed(test_engine, committed_job):
    """Review L-2: an edit racing completion sees the closed job."""
    job_id, admin_id, employee_id = committed_job
    _assign_committed(test_engine, job_id, admin_id, employee_id)
    holder = _paused(test_engine, lambda s: _move_to_completed(s, job_id, admin_id))

    def edit():
        with Session(test_engine, autoflush=False) as session:
            employee = session.get(User, employee_id)
            return job_service.update_job(
                session, job_id, JobUpdate(title="Late edit"), actor=employee
            )

    result = _run_while_locked(holder, edit)

    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        assert session.get(Job, job_id).title == "T"


def test_unassign_waits_for_job_being_completed(test_engine, committed_job):
    """Review L-2: a removal racing completion is refused."""
    job_id, admin_id, employee_id = committed_job
    _assign_committed(test_engine, job_id, admin_id, employee_id)
    holder = _paused(test_engine, lambda s: _move_to_completed(s, job_id, admin_id))

    def unassign():
        with Session(test_engine, autoflush=False) as session:
            admin = session.get(User, admin_id)
            return job_service.unassign_user(session, job_id, employee_id, actor=admin)

    result = _run_while_locked(holder, unassign)

    assert isinstance(result.get("error"), BusinessRuleError), result


def test_status_history_follows_lock_order_not_start_time(test_engine, committed_job):
    """Review L-1: a transaction that started earlier but changed the job
    later is listed after the earlier change."""
    job_id, admin_id, _ = committed_job
    early = Session(test_engine, autoflush=False)
    try:
        early.execute(text("SELECT 1"))  # starts the transaction (fixes now())
        early_admin = early.get(User, admin_id)
        with Session(test_engine, autoflush=False) as late:
            job_service.change_status(
                late, job_id, JobStatus.IN_PROGRESS, actor=late.get(User, admin_id)
            )
        job_service.change_status(early, job_id, JobStatus.SUBMITTED, actor=early_admin)
    finally:
        early.close()

    with Session(test_engine) as session:
        history = job_service.list_status_history(session, job_id)
    assert [h.new_status for h in history] == ["pending", "in_progress", "submitted"]
