"""Phase 2.4: document progress on jobs, readiness filters and work queues."""

import itertools
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    BackgroundTask,
    Document,
    DocumentType,
    Job,
    JobChecklistItem,
    JobDocument,
    Service,
    ServiceCategory,
    ServiceDocumentRequirement,
)
from app.repositories.readiness_repository import business_today
from app.workers import ocr_handler
from app.workers.registry import TaskInfo

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n"
_unique = itertools.count(1)


def scan(fields=None, error=None) -> bytes:
    script = {"raw_text": "GOVERNMENT OF INDIA", "n": next(_unique)}
    if fields is not None:
        script["fields"] = fields
    if error:
        script["error"] = error
    return PNG + b"FAKEOCR:" + json.dumps(script).encode()


def photo_bytes() -> bytes:
    return PNG + f"photo-{next(_unique)}".encode()


GOOD_FIELDS = {"name": ["Ravi Kumar", 0.97], "aadhaar_number": ["1234 5678 9012", 0.5]}


@pytest.fixture
def catalogue(db_session):
    category = ServiceCategory(name="Identity", code="IDENTITY")
    db_session.add(category)
    db_session.flush()
    service = Service(
        category_id=category.id, name="PAN Card", code="PAN", fee_type="variable"
    )
    aadhaar = DocumentType(
        code="AADHAAR",
        name="Aadhaar card",
        sensitivity="sensitive",
        ocr_fields=[
            {"key": "name", "label": "Name", "sensitive": False},
            {"key": "aadhaar_number", "label": "Aadhaar number", "sensitive": True},
            {"key": "address", "label": "Address", "sensitive": False},
        ],
    )
    photo = DocumentType(code="PHOTO", name="Photo")  # read by hand
    rc = DocumentType(code="RC", name="RC book")  # optional
    db_session.add_all([service, aadhaar, photo, rc])
    db_session.flush()
    for doc_type, required, multiple in (
        (aadhaar, True, False),
        (photo, True, True),
        (rc, False, False),
    ):
        db_session.add(
            ServiceDocumentRequirement(
                service_id=service.id,
                document_type_id=doc_type.id,
                is_required=required,
                allow_multiple=multiple,
                sort_order=doc_type.id,
            )
        )
    db_session.flush()
    return {"service": service}


@pytest.fixture
def make_job(client, make_customer, catalogue):
    def _make(title="PAN", customer=None, as_client=None):
        customer = customer or make_customer()
        response = (as_client or client).post(
            f"{API}/jobs",
            json={
                "customer_id": customer.id,
                "service_id": catalogue["service"].id,
                "title": title,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make


def upload(client, job, code, data):
    items = client.get(f"{API}/jobs/{job['id']}/checklist").json()["items"]
    item = next(i for i in items if i["document_type_code"] == code)
    response = client.post(
        f"{API}/customers/{job['customer']['id']}/documents",
        files={"file": (f"{code.lower()}.png", data, "image/png")},
        data={"job_id": str(job["id"]), "checklist_item_id": str(item["id"])},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _task(db_session, document_id) -> BackgroundTask:
    return db_session.scalar(
        select(BackgroundTask).where(
            BackgroundTask.idempotency_key
            == ocr_handler.idempotency_key(document_id, 1)
        )
    )


def run_ocr(db_session, document_id):
    task = _task(db_session, document_id)
    info = TaskInfo(
        id=task.id, task_type=task.task_type, payload=task.payload, attempt=1
    )
    ocr_handler.run_document_ocr(db_session, info)
    db_session.flush()


def fail_ocr(db_session, document_id):
    task = _task(db_session, document_id)
    task.status, task.attempts = "failed", 1
    info = TaskInfo(
        id=task.id, task_type=task.task_type, payload=task.payload, attempt=1
    )
    ocr_handler.on_ocr_failed(db_session, info, ocr_handler.UNREADABLE)
    db_session.flush()


def review_aadhaar(client, document_id):
    response = client.post(
        f"{API}/documents/{document_id}/ocr/review",
        json={
            "fields": {
                "name": "Ravi Kumar",
                "aadhaar_number": "1234 5678 9012",
                "address": None,
            },
            "decision": "verify",
        },
    )
    assert response.status_code == 200, response.text


def complete_documents(client, db_session, job):
    """Upload and verify every required document of the job."""
    aadhaar = upload(client, job, "AADHAAR", scan(GOOD_FIELDS))
    run_ocr(db_session, aadhaar["id"])
    review_aadhaar(client, aadhaar["id"])
    photo = upload(client, job, "PHOTO", photo_bytes())
    assert client.post(f"{API}/documents/{photo['id']}/verify").status_code == 200


def set_status(db_session, job, status):
    db_session.get(Job, job["id"]).status = status
    db_session.flush()


def progress(client, job):
    return client.get(f"{API}/jobs/{job['id']}").json()["document_progress"]


def ids(response):
    assert response.status_code == 200, response.text
    return [j["id"] for j in response.json()["items"]]


# ---------- document progress ----------


def test_document_progress_follows_the_document_lifecycle(client, db_session, make_job):
    job = make_job()
    assert job["document_progress"] == {
        "required_total": 2,
        "verified": 0,
        "to_review": 0,
        "processing": 0,
        "rejected": 0,
        "missing": 2,
        "complete": False,
    }

    upload(client, job, "RC", photo_bytes())  # optional: never counted
    aadhaar = upload(client, job, "AADHAAR", scan(GOOD_FIELDS))
    photo = upload(client, job, "PHOTO", photo_bytes())
    p = progress(client, job)
    assert (p["processing"], p["to_review"], p["missing"]) == (1, 1, 0)

    run_ocr(db_session, aadhaar["id"])
    assert progress(client, job)["to_review"] == 2

    client.post(f"{API}/documents/{photo['id']}/reject", json={"reason": "Blurry"})
    p = progress(client, job)
    assert (p["to_review"], p["rejected"]) == (1, 1)

    # A second photo (multiple allowed) replaces the rejected one.
    second = upload(client, job, "PHOTO", photo_bytes())
    client.post(f"{API}/documents/{second['id']}/verify")
    review_aadhaar(client, aadhaar["id"])
    p = progress(client, job)
    assert (p["verified"], p["rejected"], p["complete"]) == (2, 0, True)


def test_progress_agrees_with_the_checklist(client, db_session, make_job):
    job = make_job()
    aadhaar = upload(client, job, "AADHAAR", scan(GOOD_FIELDS))
    run_ocr(db_session, aadhaar["id"])
    review_aadhaar(client, aadhaar["id"])
    archived = upload(client, job, "PHOTO", photo_bytes())
    client.post(f"{API}/documents/{archived['id']}/verify")
    client.post(f"{API}/documents/{archived['id']}/archive", json={"reason": "Wrong"})

    checklist = client.get(f"{API}/jobs/{job['id']}/checklist").json()
    p = progress(client, job)
    assert p["required_total"] == checklist["required_total"] == 2
    assert p["verified"] == checklist["required_verified"] == 1
    assert p["complete"] is checklist["complete"] is False
    assert p["missing"] == 1  # the archived photo no longer counts


def test_job_without_required_documents_is_complete(
    client, make_customer, make_service
):
    job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": make_customer().id,
            "service_id": make_service().id,
            "title": "No documents",
        },
    ).json()
    assert job["document_progress"]["required_total"] == 0
    assert job["document_progress"]["complete"] is True


def test_progress_is_on_every_job_in_the_list(client, db_session, make_job):
    first, second = make_job("A"), make_job("B")
    upload(client, second, "PHOTO", photo_bytes())
    items = client.get(f"{API}/jobs").json()["items"]
    by_id = {j["id"]: j["document_progress"] for j in items}
    assert by_id[first["id"]]["missing"] == 2
    assert by_id[second["id"]]["to_review"] == 1


# ---------- readiness filters ----------


def test_readiness_filters(client, db_session, make_job):
    missing = make_job("missing")
    reviewing = make_job("reviewing")
    upload(client, reviewing, "PHOTO", photo_bytes())
    reading = make_job("reading")
    upload(client, reading, "AADHAAR", scan(GOOD_FIELDS))
    ready = make_job("ready")
    complete_documents(client, db_session, ready)
    set_status(db_session, ready, "in_progress")
    resume = make_job("resume")
    complete_documents(client, db_session, resume)
    set_status(db_session, resume, "waiting_for_documents")
    not_started = make_job("complete but pending")
    complete_documents(client, db_session, not_started)
    submitted = make_job("submitted with missing documents (override)")
    set_status(db_session, submitted, "submitted")
    cancelled = make_job("cancelled")
    set_status(db_session, cancelled, "cancelled")

    def find(readiness):
        return set(ids(client.get(f"{API}/jobs", params={"readiness": readiness})))

    # Closed or submitted jobs are never in a document queue.
    assert find("missing_documents") == {
        missing["id"],
        reviewing["id"],
        reading["id"],
    }
    assert find("needs_review") == {reviewing["id"]}
    assert find("processing") == {reading["id"]}
    assert find("ready_to_submit") == {ready["id"]}
    assert find("ready_to_resume") == {resume["id"]}
    total = client.get(f"{API}/jobs", params={"readiness": "needs_review"}).json()
    assert total["total"] == 1

    assert client.get(f"{API}/jobs", params={"readiness": "nope"}).status_code == 422


def test_readiness_combines_with_other_filters(
    client, db_session, make_job, make_customer
):
    customer = make_customer()
    mine = make_job("mine", customer=customer)
    make_job("other")
    found = ids(
        client.get(
            f"{API}/jobs",
            params={"readiness": "missing_documents", "customer_id": customer.id},
        )
    )
    assert found == [mine["id"]]


def test_due_filters_and_urgency_order(client, db_session, make_job):
    today = business_today(db_session)
    jobs = {}
    for name, offset in (
        ("later", 3),
        ("none", None),
        ("today", 0),
        ("overdue", -2),
        ("very overdue", -9),
    ):
        jobs[name] = make_job(name)
        db_session.get(Job, jobs[name]["id"]).due_date = (
            today + timedelta(days=offset) if offset is not None else None
        )
    closed = make_job("closed overdue")
    db_session.get(Job, closed["id"]).due_date = today - timedelta(days=5)
    set_status(db_session, closed, "completed")
    db_session.flush()

    overdue = ids(client.get(f"{API}/jobs", params={"due": "overdue"}))
    assert set(overdue) == {jobs["overdue"]["id"], jobs["very overdue"]["id"]}
    assert ids(client.get(f"{API}/jobs", params={"due": "today"})) == [
        jobs["today"]["id"]
    ]

    ordered = ids(client.get(f"{API}/jobs", params={"sort": "urgency"}))
    expected = ["very overdue", "closed overdue", "overdue", "today", "later", "none"]
    by_id = {j["id"]: n for n, j in {**jobs, "closed overdue": closed}.items()}
    assert [by_id[i] for i in ordered] == expected


def test_unassigned_filter(client, db_session, make_job, employee_user):
    free = make_job("free")
    taken = make_job("taken")
    client.post(
        f"{API}/jobs/{taken['id']}/assignments", json={"user_id": employee_user.id}
    )
    gone = make_job("cancelled")
    set_status(db_session, gone, "cancelled")
    assert ids(client.get(f"{API}/jobs", params={"unassigned": "true"})) == [free["id"]]


# ---------- employees see only their own work ----------


@pytest.fixture
def two_employees(make_user, make_client):
    first, second = make_user("employee"), make_user("employee")
    return (first, make_client(first)), (second, make_client(second))


def test_employee_queues_cover_only_assigned_jobs(
    client, db_session, make_job, two_employees
):
    (_, mine_client), (_, other_client) = two_employees
    mine = make_job("mine", as_client=mine_client)
    theirs = make_job("theirs", as_client=other_client)
    upload(mine_client, mine, "PHOTO", photo_bytes())
    upload(other_client, theirs, "PHOTO", photo_bytes())
    make_job("nobody's")

    found = ids(mine_client.get(f"{API}/jobs", params={"readiness": "needs_review"}))
    assert found == [mine["id"]]
    assert ids(mine_client.get(f"{API}/jobs", params={"unassigned": "true"})) == []

    summary = mine_client.get(f"{API}/work-queue").json()
    assert summary["documents_to_review"] == 1
    assert summary["jobs_missing_documents"] == 1
    assert summary["unassigned_jobs"] is None

    queue = mine_client.get(f"{API}/review-queue").json()
    assert [i["jobs"][0]["id"] for i in queue["items"]] == [mine["id"]]


# ---------- work queue ----------


def test_work_queue_summary_for_admin(client, db_session, make_job, employee_user):
    today = business_today(db_session)
    waiting = make_job("waiting")
    upload(client, waiting, "PHOTO", photo_bytes())
    db_session.get(Job, waiting["id"]).due_date = today
    late = make_job("late")
    db_session.get(Job, late["id"]).due_date = today - timedelta(days=1)
    db_session.flush()  # before the API call reloads the job under its lock
    client.post(
        f"{API}/jobs/{late['id']}/assignments", json={"user_id": employee_user.id}
    )
    ready = make_job("ready")
    complete_documents(client, db_session, ready)
    set_status(db_session, ready, "in_progress")
    reading = make_job("reading")
    upload(client, reading, "AADHAAR", scan(GOOD_FIELDS))
    db_session.flush()

    summary = client.get(f"{API}/work-queue").json()
    assert summary == {
        "as_of": today.isoformat(),
        "documents_to_review": 1,
        "jobs_missing_documents": 3,
        "jobs_processing": 1,
        "jobs_ready_to_submit": 1,
        "jobs_ready_to_resume": 0,
        "jobs_overdue": 1,
        "jobs_due_today": 1,
        "jobs_with_balance_due": 0,  # this service has no fees
        "unassigned_jobs": 3,
        "jobs_overpaid": 0,
    }


def test_queues_need_sign_in(anon_client):
    assert anon_client.get(f"{API}/work-queue").status_code == 401
    assert anon_client.get(f"{API}/review-queue").status_code == 401


# ---------- review queue ----------


def test_review_queue_lists_waiting_documents_oldest_first(
    client, db_session, make_job
):
    job = make_job()
    photo = upload(client, job, "PHOTO", photo_bytes())
    failed = upload(client, job, "AADHAAR", scan(error="unsupported"))
    fail_ocr(db_session, failed["id"])
    other = make_job("other")
    read = upload(client, other, "AADHAAR", scan(GOOD_FIELDS))
    run_ocr(db_session, read["id"])
    done = upload(client, other, "PHOTO", photo_bytes())
    client.post(f"{API}/documents/{done['id']}/verify")  # leaves the queue

    items = client.get(f"{API}/review-queue").json()["items"]
    assert [i["document_id"] for i in items] == [photo["id"], failed["id"], read["id"]]
    kinds = {i["document_id"]: (i["review_kind"], i["flagged_fields"]) for i in items}
    assert kinds == {
        photo["id"]: ("manual", None),
        failed["id"]: ("ocr_failed", None),
        # aadhaar_number read with low confidence, address not read at all
        read["id"]: ("ocr_check", 2),
    }
    first = items[0]
    assert first["status"] == "uploaded"
    assert first["document_type"]["code"] == "PHOTO"
    assert first["customer"]["id"] == job["customer"]["id"]
    assert first["jobs"] == [
        {
            "id": job["id"],
            "job_number": job["job_number"],
            "title": job["title"],
            "status": "pending",
        }
    ]
    assert first["waiting_since"]


def test_review_queue_filters_and_pages(client, db_session, make_job):
    job, other = make_job(), make_job("other")
    for _ in range(3):
        upload(client, job, "PHOTO", photo_bytes())
    upload(client, other, "PHOTO", photo_bytes())

    by_job = client.get(f"{API}/review-queue", params={"job_id": job["id"]}).json()
    assert by_job["total"] == 3
    by_customer = client.get(
        f"{API}/review-queue", params={"customer_id": other["customer"]["id"]}
    ).json()
    assert by_customer["total"] == 1

    page = client.get(f"{API}/review-queue", params={"page_size": 2, "page": 2}).json()
    assert (page["total"], page["total_pages"], len(page["items"])) == (4, 2, 2)
    assert (
        client.get(f"{API}/review-queue", params={"page_size": 101}).status_code == 422
    )


def test_review_queue_scope(client, db_session, make_job, make_customer, two_employees):
    (_, mine_client), _ = two_employees
    customer = make_customer()
    # A customer document uploaded on its own: admins see it, employees don't.
    loose = client.post(
        f"{API}/customers/{customer.id}/documents",
        files={"file": ("loose.png", photo_bytes(), "image/png")},
    ).json()
    open_job = make_job("open", customer=customer, as_client=mine_client)
    on_open = upload(mine_client, open_job, "PHOTO", photo_bytes())
    closed = make_job("closed", customer=customer, as_client=mine_client)
    on_closed = upload(mine_client, closed, "PHOTO", photo_bytes())
    set_status(db_session, closed, "completed")
    archived = upload(mine_client, open_job, "PHOTO", photo_bytes())
    client.post(f"{API}/documents/{archived['id']}/archive", json={"reason": "Dup"})

    admin_ids = [
        i["document_id"] for i in client.get(f"{API}/review-queue").json()["items"]
    ]
    assert set(admin_ids) == {loose["id"], on_open["id"]}

    mine = mine_client.get(f"{API}/review-queue").json()["items"]
    assert [i["document_id"] for i in mine] == [on_open["id"]]
    assert db_session.get(Document, on_closed["id"]).status == "uploaded"


def test_employee_queue_hides_other_jobs_and_closed_job_documents(
    client, db_session, make_job, make_customer, two_employees
):
    (_, mine_client), (_, other_client) = two_employees
    customer = make_customer()
    mine = make_job("mine", customer=customer, as_client=mine_client)
    theirs = make_job("theirs", customer=customer, as_client=other_client)
    shared = upload(mine_client, mine, "PHOTO", photo_bytes())
    # The same document also linked to a job the employee is not on (set up
    # directly: through the API only verified documents can be shared).
    item = db_session.scalar(
        select(JobChecklistItem).where(
            JobChecklistItem.job_id == theirs["id"],
            JobChecklistItem.document_type_code == "PHOTO",
        )
    )
    db_session.add(
        JobDocument(
            job_id=theirs["id"], document_id=shared["id"], checklist_item_id=item.id
        )
    )
    db_session.flush()

    entry = mine_client.get(f"{API}/review-queue").json()["items"][0]
    assert [j["id"] for j in entry["jobs"]] == [mine["id"]]
    admin_entry = client.get(f"{API}/review-queue").json()["items"][0]
    assert [j["id"] for j in admin_entry["jobs"]] == [mine["id"], theirs["id"]]

    # Once a closed job uses it, the employee can no longer change it.
    set_status(db_session, theirs, "completed")
    assert mine_client.get(f"{API}/review-queue").json()["total"] == 0


# ---------- review findings ----------


def test_employee_cannot_probe_hidden_jobs_through_the_job_filter(
    client, db_session, make_job, make_customer, two_employees
):
    (_, mine_client), (_, other_client) = two_employees
    customer = make_customer()
    mine = make_job("mine", customer=customer, as_client=mine_client)
    theirs = make_job("theirs", customer=customer, as_client=other_client)
    shared = upload(mine_client, mine, "PHOTO", photo_bytes())
    item = db_session.scalar(
        select(JobChecklistItem).where(
            JobChecklistItem.job_id == theirs["id"],
            JobChecklistItem.document_type_code == "PHOTO",
        )
    )
    db_session.add(
        JobDocument(
            job_id=theirs["id"], document_id=shared["id"], checklist_item_id=item.id
        )
    )
    db_session.flush()

    hidden = mine_client.get(f"{API}/review-queue", params={"job_id": theirs["id"]})
    missing = mine_client.get(f"{API}/review-queue", params={"job_id": 999_999})
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    own = mine_client.get(f"{API}/review-queue", params={"job_id": mine["id"]})
    assert own.json()["total"] == 1
    # Admins can filter by any job.
    assert (
        client.get(f"{API}/review-queue", params={"job_id": theirs["id"]}).json()[
            "total"
        ]
        == 1
    )


def test_mixed_documents_on_one_item(client, db_session, make_job):
    job = make_job()
    rejected = upload(client, job, "PHOTO", photo_bytes())
    client.post(f"{API}/documents/{rejected['id']}/reject", json={"reason": "Blurry"})
    upload(client, job, "PHOTO", photo_bytes())  # a new one waits for review
    p = progress(client, job)
    assert (p["to_review"], p["rejected"]) == (1, 0)
    item = next(
        i
        for i in client.get(f"{API}/jobs/{job['id']}/checklist").json()["items"]
        if i["document_type_code"] == "PHOTO"
    )
    assert item["state"] == "pending"


def test_job_needing_no_documents_is_ready_to_submit(
    client, db_session, make_customer, make_service
):
    """Consistent with the submit check: nothing to verify, nothing blocks it."""
    job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": make_customer().id,
            "service_id": make_service().id,
            "title": "No documents",
        },
    ).json()
    set_status(db_session, job, "in_progress")
    found = ids(client.get(f"{API}/jobs", params={"readiness": "ready_to_submit"}))
    assert found == [job["id"]]
    assert (
        ids(client.get(f"{API}/jobs", params={"readiness": "missing_documents"})) == []
    )
