"""Phase 6.0: the dashboard summary and the admin's audit-log review."""

import itertools
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, text

from app.models import AuditLog, Job, JobAssignment, JobStatusHistory
from app.repositories.readiness_repository import business_today

DASHBOARD = "/api/v1/dashboard"
AUDIT = "/api/v1/audit-logs"
_n = itertools.count(1)


@pytest.fixture
def today(db_session):
    return business_today(db_session)


@pytest.fixture(autouse=True)
def no_other_jobs(db_session):
    """Counts are global for admins: start from no committed jobs."""
    assert db_session.scalar(text("SELECT count(*) FROM jobs")) == 0


def job(
    db, customer, *, status="pending", days_old=0, due=None, assignees=(), title=None
):
    row = Job(
        job_number=f"CSC-T-{next(_n):06d}",
        customer_id=customer.id,
        title=title or "PAN card application",
        status=status,
        due_date=due,
        created_at=func.now() - timedelta(days=days_old),
    )
    db.add(row)
    db.flush()
    for user in assignees:
        db.add(JobAssignment(job_id=row.id, user_id=user.id, assigned_by=user.id))
    db.flush()
    return row


def changed(db, row, new_status, days_ago=0):
    db.add(
        JobStatusHistory(
            job_id=row.id,
            old_status=row.status,
            new_status=new_status,
            changed_at=func.now() - timedelta(days=days_ago),
        )
    )
    row.status = new_status
    db.flush()


def get(client, url=DASHBOARD, **params):
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------- the dashboard ----------


def test_an_employee_sees_counts_for_her_jobs_only(
    employee_client, employee_user, make_user, make_customer, db_session, today
):
    customer = make_customer(name="Kavya Rao")
    other = make_user("employee")
    job(db_session, customer, assignees=[employee_user])
    job(
        db_session,
        customer,
        status="in_progress",
        assignees=[employee_user],
        due=today - timedelta(days=1),
    )
    job(
        db_session, customer, status="in_progress", assignees=[employee_user], due=today
    )
    job(db_session, customer, status="waiting_for_documents", assignees=[other])
    job(db_session, customer)  # nobody's

    body = get(employee_client)
    assert (body["as_of"], body["scope"]) == (today.isoformat(), "mine")
    jobs = body["jobs"]
    assert jobs["created_today"] == 3
    assert jobs["open"] == {
        "pending": 1,
        "in_progress": 2,
        "waiting_for_documents": 0,
        "submitted": 0,
    }
    assert (jobs["overdue"], jobs["due_today"]) == (1, 1)
    # Admin-only parts are not there for her.
    assert body["workload"] is None and body["unassigned_open_jobs"] is None
    assert len(body["recent_jobs"]) == 3


def test_an_admin_sees_every_job_and_the_workload(
    client, employee_user, make_user, make_customer, db_session, today
):
    customer = make_customer()
    sam = make_user("employee", name="Sam Desk")
    gone = make_user("employee", name="Gone Person", is_active=False)
    job(db_session, customer, assignees=[employee_user], due=today - timedelta(days=2))
    job(db_session, customer, status="in_progress", assignees=[employee_user, sam])
    done = job(db_session, customer, status="submitted", assignees=[sam])
    changed(db_session, done, "completed")
    job(db_session, customer)  # unassigned and open
    job(db_session, customer, assignees=[gone])

    body = get(client)
    assert body["scope"] == "all"
    assert body["jobs"]["created_today"] == 5
    assert body["jobs"]["completed_today"] == 1
    assert body["unassigned_open_jobs"] == 1
    workload = {w["user"]["name"]: w for w in body["workload"]}
    assert "Gone Person" not in workload
    assert workload[employee_user.name] == {
        "user": {"id": employee_user.id, "name": employee_user.name},
        "open_jobs": 2,
        "overdue": 1,
        "completed_this_month": 0,
    }
    assert (
        workload["Sam Desk"]["open_jobs"],
        workload["Sam Desk"]["completed_this_month"],
    ) == (1, 1)


def test_today_and_this_month_follow_the_centres_calendar(
    client, make_customer, db_session, today
):
    customer = make_customer()
    old = job(db_session, customer, days_old=40)
    job(db_session, customer, days_old=today.day)  # last day of last month
    if today.day > 1:
        job(db_session, customer, days_old=today.day - 1)  # the 1st
    changed(db_session, old, "cancelled", days_ago=40)
    cancelled = job(db_session, customer)
    changed(db_session, cancelled, "cancelled")
    finished = job(db_session, customer, status="submitted")
    changed(db_session, finished, "completed", days_ago=0)
    body = get(client)["jobs"]
    # This month: the 1st (unless today is the 1st), and the two made today.
    assert body["created_this_month"] == 2 + (1 if today.day > 1 else 0)
    assert body["cancelled_this_month"] == 1
    assert (body["completed_today"], body["completed_this_month"]) == (1, 1)


def test_recent_jobs_by_latest_activity(client, make_customer, db_session):
    customer = make_customer(name="Asha")
    older = job(db_session, customer, days_old=10, title="Old but just updated")
    job(db_session, customer, days_old=5, title="Middle")
    for i in range(10):
        job(db_session, customer, days_old=20 + i, title=f"Ancient {i}")
    changed(db_session, older, "in_progress", days_ago=0)
    recent = get(client)["recent_jobs"]
    assert len(recent) == 10
    assert [r["title"] for r in recent[:2]] == ["Old but just updated", "Middle"]
    assert recent[0]["customer_name"] == "Asha"
    assert recent[0]["status"] == "in_progress"


def test_the_dashboard_needs_sign_in(anon_client):
    assert anon_client.get(DASHBOARD).status_code == 401


# ---------- the audit log ----------


def entry(
    db,
    action="document_viewed",
    entity_type="document",
    entity_id=1,
    user=None,
    details=None,
    days_ago=0,
):
    row = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user.id if user else None,
        details=details,
        created_at=func.now() - timedelta(days=days_ago),
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def fresh_audit(db_session):
    db_session.execute(text("DELETE FROM audit_logs"))
    db_session.flush()


def test_only_admins_read_the_audit_log(employee_client, anon_client):
    assert employee_client.get(AUDIT).status_code == 403
    assert anon_client.get(AUDIT).status_code == 401


def test_audit_entries_newest_first_with_people_and_details(
    client, admin_user, employee_user, db_session, fresh_audit
):
    entry(
        db_session, user=employee_user, details=json.dumps({"version": 2}), days_ago=1
    )
    entry(db_session, action="export_downloaded", entity_type="export", user=admin_user)
    entry(
        db_session,
        action="data_cleanup",
        entity_type="system",
        entity_id=None,
        details="not json",
    )
    body = get(client, AUDIT)
    actions = [e["action"] for e in body["items"]]
    # The admin's own sign-in is in there too; the seeded ones in order.
    seeded = [
        a
        for a in actions
        if a in ("document_viewed", "export_downloaded", "data_cleanup")
    ]
    # Same moment: the later entry first.
    assert seeded == ["data_cleanup", "export_downloaded", "document_viewed"]
    by_action = {e["action"]: e for e in body["items"]}
    viewed = by_action["document_viewed"]
    assert viewed["user"] == {"id": employee_user.id, "name": employee_user.name}
    assert viewed["details"] == {"version": 2}
    assert by_action["data_cleanup"]["user"] is None
    assert by_action["data_cleanup"]["details"] == {"text": "not json"}


def test_audit_filters(client, employee_user, db_session, fresh_audit, today):
    entry(
        db_session,
        entity_type="customer",
        entity_id=7,
        action="customer_updated",
        user=employee_user,
    )
    entry(db_session, entity_type="customer", entity_id=8, action="customer_updated")
    entry(
        db_session, entity_type="job", entity_id=7, action="job_assigned", days_ago=40
    )

    def actions(**params):
        return [
            (e["entity_type"], e["entity_id"])
            for e in get(client, AUDIT, **params)["items"]
        ]

    assert actions(entity_type="customer", entity_id=7) == [("customer", 7)]
    assert actions(action="customer_updated", user_id=employee_user.id) == [
        ("customer", 7)
    ]
    old_day = (today - timedelta(days=40)).isoformat()
    assert actions(date_from=old_day, date_to=old_day) == [("job", 7)]
    assert actions(entity_type="job", date_from=today.isoformat()) == []


def test_audit_paging_and_bad_ranges(client, db_session, fresh_audit, today):
    for i in range(5):
        entry(db_session, entity_id=i)
    body = get(client, AUDIT, entity_type="document", page_size=2, page=3)
    assert (body["total"], body["total_pages"], len(body["items"])) == (5, 3, 1)
    later = (today + timedelta(days=1)).isoformat()
    bad = client.get(AUDIT, params={"date_from": later, "date_to": today.isoformat()})
    assert bad.status_code == 422
    too_long = client.get(
        AUDIT,
        params={
            "date_from": (today - timedelta(days=400)).isoformat(),
            "date_to": today.isoformat(),
        },
    )
    assert too_long.status_code == 422
    assert client.get(AUDIT, params={"page_size": 101}).status_code == 422


def test_just_after_midnight_in_india_is_today(
    client, make_customer, db_session, today
):
    """00:30 in India is still yesterday in UTC."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    customer = make_customer()
    row = job(db_session, customer)
    row.created_at = datetime.combine(today, time(0, 30), ZoneInfo("Asia/Kolkata"))
    db_session.flush()
    assert get(client)["jobs"]["created_today"] == 1


def test_closed_jobs_are_never_overdue(client, make_customer, db_session, today):
    customer = make_customer()
    for status in ("completed", "cancelled"):
        job(db_session, customer, status=status, due=today - timedelta(days=3))
    job(db_session, customer, status="completed", due=today)
    jobs = get(client)["jobs"]
    assert (jobs["overdue"], jobs["due_today"]) == (0, 0)


def test_someone_taken_off_a_job_no_longer_carries_it(
    client, employee_client, employee_user, make_customer, db_session, today
):
    customer = make_customer()
    row = job(
        db_session, customer, assignees=[employee_user], due=today - timedelta(days=1)
    )
    db_session.execute(
        text("UPDATE job_assignments SET unassigned_at = now() WHERE job_id = :j"),
        {"j": row.id},
    )
    db_session.flush()
    mine = get(employee_client)
    assert mine["jobs"]["open"]["pending"] == 0 and mine["recent_jobs"] == []
    workload = {w["user"]["id"]: w for w in get(client)["workload"]}
    assert (
        workload[employee_user.id]["open_jobs"],
        workload[employee_user.id]["overdue"],
    ) == (0, 0)
    assert get(client)["unassigned_open_jobs"] == 1


# ---------- review fixes ----------


def test_an_employee_sees_only_her_completions_and_cancellations(
    employee_client, employee_user, make_user, make_customer, db_session
):
    """Review F1: closed-job counts follow her jobs too."""
    customer = make_customer()
    other = make_user("employee")
    mine = job(db_session, customer, status="submitted", assignees=[employee_user])
    changed(db_session, mine, "completed")
    theirs = job(db_session, customer, status="submitted", assignees=[other])
    changed(db_session, theirs, "completed")
    dropped = job(db_session, customer, assignees=[other])
    changed(db_session, dropped, "cancelled")
    jobs = get(employee_client)["jobs"]
    assert (jobs["completed_today"], jobs["completed_this_month"]) == (1, 1)
    assert jobs["cancelled_this_month"] == 0


def test_workload_and_unassigned_count_open_jobs_only(
    client, make_user, make_customer, db_session, today
):
    """Review F2."""
    customer = make_customer()
    sam = make_user("employee", name="Sam Desk")
    alex = make_user("employee", name="Alex Desk")
    job(db_session, customer, status="completed")  # unassigned but closed
    job(
        db_session,
        customer,
        status="completed",
        assignees=[sam],
        due=today - timedelta(days=5),
    )
    # Alex was on it, then Sam took over and completed it.
    handed = job(db_session, customer, status="submitted", assignees=[alex])
    db_session.execute(
        text("UPDATE job_assignments SET unassigned_at = now() WHERE job_id = :j"),
        {"j": handed.id},
    )
    db_session.add(JobAssignment(job_id=handed.id, user_id=sam.id, assigned_by=sam.id))
    db_session.flush()
    changed(db_session, handed, "completed")
    body = get(client)
    assert body["unassigned_open_jobs"] == 0
    workload = {w["user"]["name"]: w for w in body["workload"]}
    assert (workload["Sam Desk"]["overdue"], workload["Sam Desk"]["open_jobs"]) == (
        0,
        0,
    )
    assert workload["Sam Desk"]["completed_this_month"] == 1
    assert workload["Alex Desk"]["completed_this_month"] == 0


def test_a_whole_year_is_allowed_but_not_more(client, today):
    """Review F6: at most 366 days, counted inclusively."""
    start = today - timedelta(days=365)
    assert (
        client.get(
            AUDIT,
            params={"date_from": start.isoformat(), "date_to": today.isoformat()},
        ).status_code
        == 200
    )
    too_long = client.get(
        AUDIT,
        params={
            "date_from": (start - timedelta(days=1)).isoformat(),
            "date_to": today.isoformat(),
        },
    )
    assert too_long.status_code == 422
