"""Phase 2 whole-flow tests, told from the employee's side.

Each test is one day-at-the-counter story driven through the HTTP API, as
the frontend will: the employee finds or creates the customer, opens a job,
uploads documents, the real background worker reads them with OCR, the
employee reviews them from the queue, and the job moves to submission.
Payments and receipts are Phase 3.

The worker runs inside the test's transaction (savepoints instead of
commits), so nothing leaks between tests.
"""

import itertools
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog, BackgroundTask, Customer
from app.workers.worker import HandlerSession, Worker

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n"
_unique = itertools.count(1)


def aadhaar_scan(name="Ravi Kumar", number="1234 5678 9012", address=None, error=None):
    """A fake image whose 'OCR' answer is embedded for the fake provider."""
    fields = {"name": [name, 0.97], "aadhaar_number": [number, 0.93]}
    if address:
        fields["address"] = [address, 0.61]  # low confidence
    script = {"fields": fields, "raw_text": "GOVERNMENT OF INDIA", "n": next(_unique)}
    if error:
        script = {"error": error, "n": next(_unique)}
    return PNG + b"FAKEOCR:" + json.dumps(script).encode()


def photo() -> bytes:
    return PNG + f"passport-photo-{next(_unique)}".encode()


def run_worker(db_session) -> int:
    """One tick of the real worker, on the test's connection."""
    connection = db_session.connection()
    factory = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", autoflush=False
    )
    worker = Worker(factory, worker_id="counter-worker")
    worker._handler_sessions = sessionmaker(
        bind=connection,
        class_=HandlerSession,
        join_transaction_mode="create_savepoint",
        autoflush=False,
    )
    done = worker.tick()
    db_session.expire_all()
    return done


@pytest.fixture
def catalogue(client):
    """The admin sets up a service and what it needs, once."""

    def post(path, body):
        response = client.post(f"{API}{path}", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    category = post("/service-categories", {"name": "Identity", "code": "IDENTITY"})
    aadhaar = post(
        "/document-types",
        {
            "code": "AADHAAR",
            "name": "Aadhaar card",
            "sensitivity": "sensitive",
            "ocr_fields": [
                {"key": "name", "label": "Name"},
                {"key": "aadhaar_number", "label": "Aadhaar number", "sensitive": True},
                {"key": "address", "label": "Address"},
            ],
        },
    )
    photo_type = post("/document-types", {"code": "PHOTO", "name": "Photo"})
    service = post(
        "/services",
        {
            "category_id": category["id"],
            "name": "PAN Card",
            "code": "PAN",
            "fee_type": "fixed",
            "default_service_fee": "107.00",
        },
    )
    for doc_type in (aadhaar, photo_type):
        post(
            f"/services/{service['id']}/document-requirements",
            {"document_type_id": doc_type["id"]},
        )
    return {"service": service}


@pytest.fixture
def employee(make_user, make_client):
    user = make_user("employee", name="Priya Counter")
    return make_client(user)


class Counter:
    """What an employee does at the counter, step by step."""

    def __init__(self, client, service):
        self.c = client
        self.service = service

    def find_customer(self, search):
        response = self.c.get(f"{API}/customers", params={"search": search})
        assert response.status_code == 200, response.text
        return response.json()["items"]

    def new_customer(self, name, phone):
        response = self.c.post(f"{API}/customers", json={"name": name, "phone": phone})
        assert response.status_code == 201, response.text
        return response.json()

    def open_job(self, customer_id, title="PAN card application"):
        response = self.c.post(
            f"{API}/jobs",
            json={
                "customer_id": customer_id,
                "service_id": self.service["id"],
                "title": title,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def item(self, job, code):
        items = self.c.get(f"{API}/jobs/{job['id']}/checklist").json()["items"]
        return next(i for i in items if i["document_type_code"] == code)

    def upload(self, job, code, data):
        response = self.c.post(
            f"{API}/customers/{job['customer']['id']}/documents",
            files={"file": (f"{code.lower()}.png", data, "image/png")},
            data={
                "job_id": str(job["id"]),
                "checklist_item_id": str(self.item(job, code)["id"]),
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def queue(self):
        return self.c.get(f"{API}/work-queue").json()

    def review_queue(self, **params):
        return self.c.get(f"{API}/review-queue", params=params).json()["items"]

    def job(self, job_id):
        return self.c.get(f"{API}/jobs/{job_id}").json()

    def move(self, job_id, status, **extra):
        return self.c.post(
            f"{API}/jobs/{job_id}/status", json={"status": status, **extra}
        )


def audit(db_session, entity_type, entity_id):
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.id)
        )
    )


# ---------- the main story ----------


def test_new_customer_from_counter_to_submission(
    client, employee, catalogue, db_session, fake_redis
):
    desk = Counter(employee, catalogue["service"])

    # 1. A walk-in customer: the employee searches first, finds nobody.
    assert desk.find_customer("98765") == []
    created = desk.new_customer("R Kumar", "+91 98765 43210")
    assert created["possible_duplicates"] == []
    customer = created["customer"]
    assert desk.find_customer("98765")[0]["id"] == customer["id"]

    # 2. Opens the job: assigned to them, checklist and fee ready.
    job = desk.open_job(customer["id"])
    assert [a["name"] for a in job["assignees"]] == ["Priya Counter"]
    assert job["status"] == "pending"
    assert job["document_progress"]["missing"] == 2
    fees = employee.get(f"{API}/jobs/{job['id']}/fees").json()
    assert fees["items"][0]["amount"] == "107.00"
    summary = desk.queue()
    assert (summary["jobs_missing_documents"], summary["documents_to_review"]) == (1, 0)
    desk.move(job["id"], "in_progress")

    # 3. Scans the Aadhaar and the photo; OCR is queued for the Aadhaar only.
    aadhaar = desk.upload(job, "AADHAAR", aadhaar_scan(address="12 MG Road"))
    picture = desk.upload(job, "PHOTO", photo())
    assert (aadhaar["status"], picture["status"]) == ("processing", "uploaded")
    assert fake_redis.xlen("csc:tasks") == 1  # the worker was woken
    assert desk.queue()["jobs_processing"] == 1

    # 4. The background worker reads the Aadhaar.
    assert run_worker(db_session) == 1
    queue = desk.review_queue()
    assert [(i["document_id"], i["review_kind"]) for i in queue] == [
        (aadhaar["id"], "ocr_check"),
        (picture["id"], "manual"),
    ]
    assert queue[0]["flagged_fields"] == 1  # the address was hard to read

    # 5. The review screen: the Aadhaar number is masked until revealed.
    ocr = employee.get(f"{API}/documents/{aadhaar['id']}/ocr").json()
    number = next(f for f in ocr["fields"] if f["key"] == "aadhaar_number")
    assert number["value"] == "**** **** 9012"
    revealed = employee.get(
        f"{API}/documents/{aadhaar['id']}/ocr", params={"reveal": "true"}
    ).json()
    assert any(f["value"] == "1234 5678 9012" for f in revealed["fields"])

    # 6. Confirms the fields (corrects the address), copies name and
    #    address onto the customer, and verifies the photo by hand.
    reviewed = employee.post(
        f"{API}/documents/{aadhaar['id']}/ocr/review",
        json={
            "fields": {
                "name": "Ravi Kumar",
                "aadhaar_number": "1234 5678 9012",
                "address": "12 MG Road, Bengaluru",
            },
            "decision": "verify",
            "apply_to_customer": [
                {"customer_field": "name", "ocr_field": "name"},
                {"customer_field": "address", "ocr_field": "address"},
            ],
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["customer_fields_updated"] == ["address", "name"]
    assert employee.post(f"{API}/documents/{picture['id']}/verify").status_code == 200
    updated = employee.get(f"{API}/customers/{customer['id']}").json()
    assert (updated["name"], updated["address"]) == (
        "Ravi Kumar",
        "12 MG Road, Bengaluru",
    )

    # 7. Everything verified: the queue says submit, and submitting works.
    assert desk.review_queue() == []
    summary = desk.queue()
    assert (summary["jobs_ready_to_submit"], summary["jobs_missing_documents"]) == (
        1,
        0,
    )
    assert desk.job(job["id"])["status"] == "in_progress"  # never automatic
    assert desk.move(job["id"], "submitted").status_code == 200
    assert desk.move(job["id"], "completed").status_code == 200
    assert desk.queue()["jobs_ready_to_submit"] == 0

    # 8. The trail: who did what, with no document contents in it.
    assert audit(db_session, "document", aadhaar["id"]) == [
        "document_uploaded",
        "document_ocr_completed",
        "ocr_viewed",
        "ocr_sensitive_viewed",
        "ocr_reviewed",
        "document_verified",
    ]
    assert audit(db_session, "customer", customer["id"])[-1] == (
        "customer_updated_from_ocr"
    )
    trail = db_session.scalars(select(AuditLog.details)).all()
    assert "1234 5678 9012" not in json.dumps(trail)
    task = db_session.scalar(select(BackgroundTask))
    assert task.status == "succeeded"
    assert "1234" not in json.dumps(task.result)


# ---------- returning customer ----------


def test_returning_customer_reuses_a_verified_aadhaar(
    client, employee, catalogue, db_session
):
    desk = Counter(employee, catalogue["service"])
    customer = desk.new_customer("Ravi Kumar", "9876543210")["customer"]
    first = desk.open_job(customer["id"], "PAN for Ravi")
    aadhaar = desk.upload(first, "AADHAAR", aadhaar_scan())
    run_worker(db_session)
    employee.post(
        f"{API}/documents/{aadhaar['id']}/ocr/review",
        json={
            "fields": {
                "name": "Ravi Kumar",
                "aadhaar_number": "1234 5678 9012",
                "address": None,
            },
            "decision": "verify",
        },
    )

    # Weeks later he is back for another service: found by phone, and the
    # Aadhaar does not have to be scanned again.
    found = desk.find_customer("3210")
    assert [c["id"] for c in found] == [customer["id"]]
    again = desk.new_customer("Ravi K", "9876543210")
    assert [d["id"] for d in again["possible_duplicates"]] == [customer["id"]]
    second = desk.open_job(customer["id"], "Second service")
    linked = employee.post(
        f"{API}/jobs/{second['id']}/checklist/{desk.item(second, 'AADHAAR')['id']}"
        "/documents",
        json={"document_id": aadhaar["id"]},
    )
    assert linked.status_code == 201, linked.text
    progress = desk.job(second["id"])["document_progress"]
    assert (progress["verified"], progress["missing"]) == (1, 1)
    assert run_worker(db_session) == 0  # nothing to read again


# ---------- a bad scan ----------


def test_unreadable_scan_goes_to_manual_review_and_is_replaced(
    client, employee, catalogue, db_session
):
    desk = Counter(employee, catalogue["service"])
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])
    desk.move(job["id"], "in_progress")
    desk.upload(job, "PHOTO", photo())
    employee.post(
        f"{API}/documents/{desk.item(job, 'PHOTO')['documents'][0]['id']}/verify"
    )

    # The scan is unreadable: OCR fails for good, and the document waits for
    # a person instead of being stuck.
    blurry = desk.upload(job, "AADHAAR", aadhaar_scan(error="unsupported"))
    run_worker(db_session)
    entry = desk.review_queue()[0]
    assert (entry["document_id"], entry["review_kind"]) == (blurry["id"], "ocr_failed")

    # The employee rejects it and parks the job until the customer returns.
    rejected = employee.post(
        f"{API}/documents/{blurry['id']}/reject", json={"reason": "Unreadable scan"}
    )
    assert rejected.status_code == 200
    assert desk.job(job["id"])["document_progress"]["rejected"] == 1
    assert desk.move(job["id"], "submitted").status_code == 422  # not verified
    assert desk.move(job["id"], "waiting_for_documents").status_code == 200
    assert desk.queue()["jobs_missing_documents"] == 1

    # The customer brings a clear copy: a new version is read and reviewed.
    replaced = employee.post(
        f"{API}/documents/{blurry['id']}/versions",
        files={"file": ("aadhaar.png", aadhaar_scan(name="Meena"), "image/png")},
        data={"change_reason": "Clear copy from the customer"},
    )
    assert replaced.status_code == 201, replaced.text
    run_worker(db_session)
    assert desk.review_queue()[0]["review_kind"] == "ocr_check"
    employee.post(
        f"{API}/documents/{blurry['id']}/ocr/review",
        json={
            "fields": {
                "name": "Meena",
                "aadhaar_number": "1234 5678 9012",
                "address": None,
            },
            "decision": "verify",
        },
    )
    assert desk.queue()["jobs_ready_to_resume"] == 1
    assert desk.move(job["id"], "in_progress").status_code == 200
    assert desk.move(job["id"], "submitted").status_code == 200
    # The customer record was never touched by OCR.
    assert db_session.get(Customer, customer["id"]).name == "Meena"


# ---------- limits ----------


def test_employee_limits_along_the_way(
    client, employee, catalogue, db_session, make_user, make_client
):
    desk = Counter(employee, catalogue["service"])
    customer = desk.new_customer("Asha", "9000022222")["customer"]
    job = desk.open_job(customer["id"])
    desk.move(job["id"], "in_progress")
    aadhaar = desk.upload(job, "AADHAAR", aadhaar_scan(name="Asha"))
    run_worker(db_session)

    # A colleague sees none of it.
    colleague = make_client(make_user("employee"))
    assert colleague.get(f"{API}/jobs/{job['id']}").status_code == 404
    assert colleague.get(f"{API}/documents/{aadhaar['id']}/ocr").status_code == 404
    assert colleague.get(f"{API}/review-queue").json()["total"] == 0
    assert colleague.get(f"{API}/work-queue").json()["jobs_missing_documents"] == 0

    # The employee cannot skip the document check; only an admin can, with
    # a reason, and it is recorded.
    assert desk.move(job["id"], "submitted").status_code == 422
    override = desk.move(
        job["id"], "submitted", override_document_check=True, reason="Urgent"
    )
    assert override.status_code == 403
    admin_override = client.post(
        f"{API}/jobs/{job['id']}/status",
        json={
            "status": "submitted",
            "override_document_check": True,
            "reason": "Customer travelling; photo sent later",
        },
    )
    assert admin_override.status_code == 200, admin_override.text
    assert "document_check_overridden" in audit(db_session, "job", job["id"])

    # Once the job is completed, its documents are a record.
    assert desk.move(job["id"], "completed").status_code == 200
    late = employee.post(
        f"{API}/documents/{aadhaar['id']}/ocr/review",
        json={
            "fields": {
                "name": "Asha",
                "aadhaar_number": "1234 5678 9012",
                "address": None,
            },
            "decision": "verify",
        },
    )
    assert late.status_code == 422
    assert desk.review_queue() == []  # closed jobs leave the queue
    assert desk.queue()["jobs_missing_documents"] == 0
