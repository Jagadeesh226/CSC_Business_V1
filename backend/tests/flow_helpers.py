"""Shared helpers for the whole-flow tests (Phase 2 and Phase 3): the
counter-desk actions an employee takes, fake scans, and running the real
background worker inside the test's transaction."""

import io
import itertools
import json

from pypdf import PdfReader
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog, BackgroundTask
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


def run_worker(db_session, expect: int) -> None:
    """One tick of the real worker, on the test's connection; ``expect`` is
    how many tasks it must run.

    Inside the test transaction ``now()`` never moves, so a task scheduled
    for "later" (a retry, a backoff) is made due first, as if time passed.
    Each step is committed (a savepoint release) so a later request's
    rollback cannot undo the worker's work, as in production.
    """
    db_session.commit()
    connection = db_session.connection()
    connection.execute(
        update(BackgroundTask)
        .where(BackgroundTask.status == "queued", BackgroundTask.run_after > func.now())
        .values(run_after=func.now())
    )
    factory = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", autoflush=False
    )
    worker = Worker(factory, worker_id="counter-worker")
    # The handler thread shares this connection safely: run_one waits for
    # it (the fake provider never comes near the task's time limit).
    worker._handler_sessions = sessionmaker(
        bind=connection,
        class_=HandlerSession,
        join_transaction_mode="create_savepoint",
        autoflush=False,
    )
    assert worker.tick() == expect
    # The handler's SET LOCAL limits would otherwise last for the rest of
    # the test transaction.
    for setting in (
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
    ):
        connection.execute(text(f"SET LOCAL {setting} TO DEFAULT"))
    db_session.commit()
    db_session.expire_all()


def ok(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


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

    def review(self, document_id, name, decision="verify", **extra):
        """Confirm every OCR field of an Aadhaar and decide."""
        return self.c.post(
            f"{API}/documents/{document_id}/ocr/review",
            json={
                "fields": {
                    "name": name,
                    "aadhaar_number": "1234 5678 9012",
                    "address": None,
                },
                "decision": decision,
                **extra,
            },
        )

    def take_payment(self, job, amount, method="cash", key=None, **extra):
        """Record a payment; ``key`` is what the counter app would resend
        on a retry (a new one is made per payment otherwise)."""
        return self.c.post(
            f"{API}/jobs/{job['id']}/payments",
            json={"amount": amount, "method": method, **extra},
            headers={"Idempotency-Key": key or f"counter-pay-{next(_unique):06d}"},
        )

    def print_receipt(self, receipt_id):
        return self.c.post(f"{API}/receipts/{receipt_id}/print")

    def move(self, job_id, status, **extra):
        return self.c.post(
            f"{API}/jobs/{job_id}/status", json={"status": status, **extra}
        )


def pdf_text(response) -> str:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() for page in reader.pages)


def audit(db_session, entity_type, entity_id):
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.id)
        )
    )


def setup_catalogue(client):
    """The admin sets up a service (fixed fee 107.00) needing an Aadhaar,
    read by OCR, and a photo, checked by hand."""

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
