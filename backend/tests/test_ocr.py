import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import (
    AuditLog,
    BackgroundTask,
    Customer,
    Document,
    DocumentType,
    OcrResult,
    Service,
    ServiceCategory,
    ServiceDocumentRequirement,
    User,
)
from app.schemas.job import JobCreate
from app.services import document_service, job_service
from app.services.file_validation import check_upload
from app.services.ocr_service import mask_value
from app.workers import ocr_handler
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n"
AADHAAR_NUMBER = "1234 5678 9012"


def scan(fields=None, raw_text="GOVERNMENT OF INDIA ...", error=None) -> bytes:
    """A fake image whose 'OCR' result is embedded for the fake provider."""
    script = {"raw_text": raw_text}
    if fields is not None:
        script["fields"] = fields
    if error:
        script["error"] = error
    return PNG + b"FAKEOCR:" + json.dumps(script).encode()


GOOD = scan(
    {
        "name": ["Ravi Kumar", 0.97],
        "aadhaar_number": [AADHAAR_NUMBER, 0.95],
        "date_of_birth": ["01/01/1990", 0.62],
    }
)


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
            {"key": "date_of_birth", "label": "Date of birth", "sensitive": False},
            {"key": "address", "label": "Address", "sensitive": False},
        ],
    )
    photo = DocumentType(code="PHOTO", name="Photo")  # no OCR fields
    db_session.add_all([service, aadhaar, photo])
    db_session.flush()
    for doc_type in (aadhaar, photo):
        db_session.add(
            ServiceDocumentRequirement(
                service_id=service.id,
                document_type_id=doc_type.id,
                sort_order=doc_type.id,
            )
        )
    db_session.flush()
    return {"service": service, "aadhaar": aadhaar, "photo": photo}


@pytest.fixture
def customer(make_customer):
    return make_customer(name="R Kumar", phone="9000000001")


@pytest.fixture
def job(client, customer, catalogue):
    return client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": catalogue["service"].id,
            "title": "PAN",
        },
    ).json()


def upload_aadhaar(client, job, data=GOOD):
    items = client.get(f"{API}/jobs/{job['id']}/checklist").json()["items"]
    item = next(i for i in items if i["document_type_code"] == "AADHAAR")
    response = client.post(
        f"{API}/customers/{job['customer']['id']}/documents",
        files={"file": ("aadhaar.png", data, "image/png")},
        data={"job_id": str(job["id"]), "checklist_item_id": str(item["id"])},
    )
    assert response.status_code == 201, response.text
    return response.json()


def ocr_task(db_session, document_id, version=1) -> BackgroundTask:
    return db_session.scalar(
        select(BackgroundTask).where(
            BackgroundTask.idempotency_key
            == ocr_handler.idempotency_key(document_id, version)
        )
    )


def run_ocr(db_session, document_id, version=1):
    task = ocr_task(db_session, document_id, version)
    info = TaskInfo(
        id=task.id, task_type=task.task_type, payload=task.payload, attempt=1
    )
    result = ocr_handler.run_document_ocr(db_session, info)
    db_session.flush()
    return result


def review(client, document_id, fields=None, decision="verify", **extra):
    body = {
        "fields": fields
        or {
            "name": "Ravi Kumar",
            "aadhaar_number": AADHAAR_NUMBER,
            "date_of_birth": "01/01/1990",
            "address": "12 MG Road, Bengaluru",
        },
        "decision": decision,
        **extra,
    }
    return client.post(f"{API}/documents/{document_id}/ocr/review", json=body)


# ---------- unit ----------


@pytest.mark.parametrize(
    "value, masked",
    [
        ("1234 5678 9012", "**** **** 9012"),
        ("ABCDE1234F", "******234F"),
        ("12", "12"),
        (None, None),
    ],
)
def test_mask_value(value, masked):
    assert mask_value(value) == masked


# ---------- upload queues OCR ----------


def test_upload_queues_ocr_and_waits_in_processing(client, admin_user, job, db_session):
    doc = upload_aadhaar(client, job)

    assert doc["status"] == "processing"
    task = ocr_task(db_session, doc["id"])
    assert (task.status, task.payload) == (
        "queued",
        {"document_id": doc["id"], "version": 1},
    )
    assert task.created_by == admin_user.id
    ocr = client.get(f"{API}/documents/{doc['id']}/ocr").json()
    assert ocr["ocr_status"] == "pending"
    # Cannot be verified or rejected while being read.
    assert client.post(f"{API}/documents/{doc['id']}/verify").status_code == 422


def test_document_without_ocr_fields_skips_ocr(client, job, db_session):
    items = client.get(f"{API}/jobs/{job['id']}/checklist").json()["items"]
    photo = next(i for i in items if i["document_type_code"] == "PHOTO")
    doc = client.post(
        f"{API}/customers/{job['customer']['id']}/documents",
        files={"file": ("p.png", PNG + b"pixels", "image/png")},
        data={"job_id": str(job["id"]), "checklist_item_id": str(photo["id"])},
    ).json()
    assert doc["status"] == "uploaded"
    assert ocr_task(db_session, doc["id"]) is None
    ocr = client.get(f"{API}/documents/{doc['id']}/ocr").json()
    assert ocr["ocr_status"] == "not_applicable"


# ---------- the OCR task ----------


def test_ocr_result_is_stored_encrypted_and_sent_for_review(client, job, db_session):
    doc = upload_aadhaar(client, job)

    outcome = run_ocr(db_session, doc["id"])

    assert outcome == {
        "document_id": doc["id"],
        "version": 1,
        "fields": 4,
        "missing": 1,
        "low_confidence": 1,
    }
    assert db_session.get(Document, doc["id"]).status == "needs_review"
    result = db_session.scalar(
        select(OcrResult).where(OcrResult.document_id == doc["id"])
    )
    assert result.status == "completed"
    assert result.provider == "fake"
    assert result.confidences == {
        "name": 0.97,
        "aadhaar_number": 0.95,
        "date_of_birth": 0.62,
        "address": None,
    }
    assert AADHAAR_NUMBER.encode() not in result.extraction_sealed
    assert b"Ravi" not in result.extraction_sealed
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "document_ocr_completed")
    )
    assert json.loads(entry.details) == {
        "version": 1,
        "missing": ["address"],
        "low_confidence": ["date_of_birth"],
    }
    assert AADHAAR_NUMBER not in entry.details

    # Running the same task again changes nothing.
    assert run_ocr(db_session, doc["id"]) == {"skipped": "stale"}


def test_ocr_skips_stale_versions(client, job, db_session):
    doc = upload_aadhaar(client, job)
    client.post(
        f"{API}/documents/{doc['id']}/versions",
        files={"file": ("v2.png", GOOD, "image/png")},
        data={"change_reason": "clearer"},
    )
    assert run_ocr(db_session, doc["id"], version=1) == {"skipped": "stale"}
    assert ocr_task(db_session, doc["id"], version=2) is not None
    run_ocr(db_session, doc["id"], version=2)
    assert db_session.get(Document, doc["id"]).status == "needs_review"


def test_ocr_provider_errors(client, job, db_session):
    busy = upload_aadhaar(client, job, scan(error="temporary"))
    with pytest.raises(TaskError, match="temporarily unavailable"):
        run_ocr(db_session, busy["id"])


def test_unreadable_file_fails_permanently(client, job, db_session):
    doc = upload_aadhaar(client, job, scan(error="unsupported"))
    with pytest.raises(PermanentTaskError):
        run_ocr(db_session, doc["id"])


def test_failed_ocr_goes_to_manual_review_and_can_be_retried(client, job, db_session):
    doc = upload_aadhaar(client, job, scan(error="unsupported"))
    task = ocr_task(db_session, doc["id"])
    task.status, task.attempts = "failed", 1
    info = TaskInfo(
        id=task.id, task_type=task.task_type, payload=task.payload, attempt=1
    )

    ocr_handler.on_ocr_failed(
        db_session, info, "The OCR service cannot read this file."
    )
    db_session.flush()

    assert db_session.get(Document, doc["id"]).status == "needs_review"
    ocr = client.get(f"{API}/documents/{doc['id']}/ocr").json()
    assert ocr["ocr_status"] == "failed"
    assert ocr["failure_reason"] == "The OCR service cannot read this file."
    assert all(f["flag"] == "missing" for f in ocr["fields"])

    retried = client.post(f"{API}/documents/{doc['id']}/ocr/retry")
    assert retried.status_code == 200
    db_session.refresh(task)
    assert (task.status, task.attempts, task.max_attempts) == ("queued", 1, 4)
    assert db_session.get(Document, doc["id"]).status == "processing"
    again = client.post(f"{API}/documents/{doc['id']}/ocr/retry")
    assert again.status_code == 409  # already queued


# ---------- reading results ----------


def test_review_screen_masks_sensitive_values(client, job, db_session):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])

    ocr = client.get(f"{API}/documents/{doc['id']}/ocr").json()

    by_key = {f["key"]: f for f in ocr["fields"]}
    assert ocr["ocr_status"] == "completed"
    assert by_key["name"] == {
        "key": "name",
        "label": "Name",
        "sensitive": False,
        "value": "Ravi Kumar",
        "masked": False,
        "confidence": 0.97,
        "flag": "ok",
        "reviewed_value": None,
    }
    assert by_key["aadhaar_number"]["value"] == "**** **** 9012"
    assert by_key["aadhaar_number"]["masked"] is True
    assert by_key["date_of_birth"]["flag"] == "low_confidence"
    assert by_key["address"]["flag"] == "missing"
    assert ocr["raw_text"] is None


def test_reveal_needs_review_rights_and_is_audited(
    client, make_client, make_user, job, db_session
):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    url = f"{API}/documents/{doc['id']}/ocr"

    revealed = client.get(url, params={"reveal": True}).json()
    assert {f["key"]: f["value"] for f in revealed["fields"]}[
        "aadhaar_number"
    ] == AADHAAR_NUMBER
    assert revealed["raw_text"] == "GOVERNMENT OF INDIA ..."
    assert db_session.scalar(
        select(AuditLog).where(AuditLog.action == "ocr_sensitive_viewed")
    )

    outsider = make_client(make_user())
    assert outsider.get(url).status_code == 404


# ---------- review ----------


def test_review_verifies_and_applies_chosen_fields_to_customer(
    client, admin_user, job, customer, db_session
):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])

    response = review(
        client,
        doc["id"],
        apply_to_customer=[
            {"customer_field": "name", "ocr_field": "name"},
            {"customer_field": "address", "ocr_field": "address"},
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["status"] == "verified"
    assert body["customer_fields_updated"] == ["address", "name"]
    db_session.refresh(customer)
    assert (customer.name, customer.address) == ("Ravi Kumar", "12 MG Road, Bengaluru")
    assert customer.phone == "9000000001"  # untouched: not chosen
    by_key = {f["key"]: f for f in body["ocr"]["fields"]}
    assert by_key["address"]["reviewed_value"] == "12 MG Road, Bengaluru"
    assert by_key["aadhaar_number"]["reviewed_value"] == "**** **** 9012"

    result = db_session.scalar(
        select(OcrResult).where(OcrResult.document_id == doc["id"])
    )
    assert result.reviewed_by == admin_user.id
    assert AADHAAR_NUMBER.encode() not in result.review_sealed
    customer_audit = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "customer_updated_from_ocr")
    )
    assert json.loads(customer_audit.details) == {
        "fields": ["address", "name"],
        "document_id": doc["id"],
    }
    reviewed = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "ocr_reviewed")
    )
    assert json.loads(reviewed.details)["corrected"] == ["address"]
    item = next(
        i
        for i in client.get(f"{API}/jobs/{job['id']}/checklist").json()["items"]
        if i["document_type_code"] == "AADHAAR"
    )
    assert item["state"] == "verified"


def test_review_without_applying_changes_no_customer_data(
    client, job, customer, db_session
):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    body = review(client, doc["id"]).json()
    assert body["customer_fields_updated"] == []
    db_session.refresh(customer)
    assert customer.name == "R Kumar"


def test_review_reject(client, job, db_session):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    body = review(
        client, doc["id"], decision="reject", reason="Not the customer"
    ).json()
    assert body["document"]["status"] == "rejected"
    assert body["document"]["rejection_reason"] == "Not the customer"


@pytest.mark.parametrize(
    "change, status",
    [
        ({"fields": {"name": "Ravi"}}, 422),  # not every field confirmed
        ({"decision": "reject"}, 422),  # rejection needs a reason
        (
            {
                "decision": "reject",
                "reason": "x",
                "apply_to_customer": [{"customer_field": "name", "ocr_field": "name"}],
            },
            422,
        ),
        (
            {"apply_to_customer": [{"customer_field": "phone", "ocr_field": "name"}]},
            422,
        ),
        ({"apply_to_customer": [{"customer_field": "name", "ocr_field": "nope"}]}, 422),
        ({"extra": 1}, 422),
    ],
)
def test_review_validation(client, job, customer, db_session, change, status):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    full = {
        "fields": {
            "name": "Ravi Kumar",
            "aadhaar_number": AADHAAR_NUMBER,
            "date_of_birth": "01/01/1990",
            "address": None,
        },
        "decision": "verify",
    }
    response = client.post(
        f"{API}/documents/{doc['id']}/ocr/review", json={**full, **change}
    )
    assert response.status_code == status
    db_session.refresh(customer)
    assert customer.name == "R Kumar"  # nothing applied
    assert db_session.get(Document, doc["id"]).status == "needs_review"


def test_review_only_when_waiting_for_review(client, job):
    doc = upload_aadhaar(client, job)  # still processing
    assert review(client, doc["id"]).status_code == 422


def test_review_needs_review_rights(
    client, make_client, make_user, job, db_session, customer
):
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    # On another open job of the customer: can see the document, not review it.
    other = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": job["service"]["id"],
            "title": "2",
        },
    ).json()
    viewer_user = make_user()
    client.post(
        f"{API}/jobs/{other['id']}/assignments", json={"user_id": viewer_user.id}
    )
    viewer = make_client(viewer_user)
    assert viewer.get(f"{API}/documents/{doc['id']}/ocr").status_code == 200
    assert (
        viewer.get(
            f"{API}/documents/{doc['id']}/ocr", params={"reveal": True}
        ).status_code
        == 403
    )
    assert review(viewer, doc["id"]).status_code == 403


# ---------- end to end with the real worker ----------


@pytest.fixture
def committed(test_engine):
    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE ocr_results, background_tasks, audit_logs, job_documents, "
                    "document_versions, documents, job_checklist_items, job_fees, "
                    "job_status_history, job_assignments, jobs, sequence_counters, "
                    "customers, service_document_requirements, document_types, "
                    "services, service_categories, user_sessions, users "
                    "RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    factory = sessionmaker(bind=test_engine, autoflush=False)
    with factory() as session:
        category = ServiceCategory(name="C", code="C")
        session.add(category)
        session.flush()
        service = Service(
            category_id=category.id, name="S", code="S", fee_type="variable"
        )
        doc_type = DocumentType(
            code="AADHAAR",
            name="Aadhaar",
            sensitivity="sensitive",
            ocr_fields=[{"key": "name", "label": "Name", "sensitive": False}],
        )
        customer = Customer(name="E2E Customer")
        admin = User(
            name="Admin",
            email="ocr.admin@example.com",
            password_hash=hash_password("unused-password"),
            role="admin",
        )
        session.add_all([service, doc_type, customer, admin])
        session.flush()
        session.add(
            ServiceDocumentRequirement(
                service_id=service.id, document_type_id=doc_type.id
            )
        )
        session.flush()
        job = job_service.create_job(
            session,
            JobCreate(customer_id=customer.id, service_id=service.id, title="T"),
        )
        session.commit()
        ids = customer.id, job.id, admin.id
    yield factory, ids
    truncate()


def test_upload_to_review_through_the_real_worker(committed, fake_redis):
    from app.workers.worker import Worker

    factory, (customer_id, job_id, admin_id) = committed
    with factory() as session:
        admin = session.get(User, admin_id)
        item_id = document_service.build_checklist(session, job_id).items[0].id
        doc = document_service.upload(
            session,
            customer_id,
            check_upload(scan({"name": ["E2E Name", 0.99]}), "a.png"),
            admin,
            job_id=job_id,
            checklist_item_id=item_id,
        )
    assert doc.status == "processing"
    assert fake_redis.xlen("csc:tasks") == 1  # the worker was woken

    assert Worker(factory, worker_id="w1").tick() == 1

    with factory() as session:
        assert session.get(Document, doc.id).status == "needs_review"
        task = session.scalar(select(BackgroundTask))
        assert (task.status, task.result["fields"]) == ("succeeded", 1)


# ---------- review fixes ----------


def test_document_ocr_cannot_be_retried_from_the_task_list(client, job, db_session):
    """Review M-1: the document's own retry also resets the document."""
    from app.services import task_service

    doc = upload_aadhaar(client, job, scan(error="unsupported"))
    task = ocr_task(db_session, doc["id"])
    task.status = "failed"
    db_session.flush()
    admin = db_session.scalar(select(User).where(User.role == "admin"))
    with pytest.raises(Exception, match="Retry OCR from the document"):
        task_service.retry_task(db_session, task.id, admin)


def test_plain_verify_needs_the_ocr_review(client, job, db_session):
    """Review L-6: OCR-read values are always looked at before verifying."""
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    response = client.post(f"{API}/documents/{doc['id']}/verify")
    assert response.status_code == 422
    assert "OCR review" in response.json()["detail"]


def test_validation_errors_do_not_echo_submitted_values(client, job, db_session):
    """Review L-2: a rejected review must not send the Aadhaar number back."""
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    response = client.post(
        f"{API}/documents/{doc['id']}/ocr/review",
        json={"fields": {"aadhaar_number": "1" * 600}, "decision": "verify"},
    )
    assert response.status_code == 422
    assert "1" * 600 not in response.text
    assert all("input" not in e for e in response.json()["detail"])


def test_no_ocr_without_a_configured_provider(client, job, db_session, monkeypatch):
    """Review L-3 and L-1: unconfigured means 'not started', never 'fake'."""
    monkeypatch.delenv("OCR_PROVIDER")
    doc = upload_aadhaar(client, job)
    assert doc["status"] == "uploaded"
    assert ocr_task(db_session, doc["id"]) is None
    assert (
        client.get(f"{API}/documents/{doc['id']}/ocr").json()["ocr_status"]
        == "not_started"
    )

    monkeypatch.setenv("OCR_PROVIDER", "fake")  # OCR set up later: start it
    started = client.post(f"{API}/documents/{doc['id']}/ocr/retry")
    assert started.status_code == 200
    assert started.json()["ocr_status"] == "pending"
    assert ocr_task(db_session, doc["id"]) is not None


def test_retry_refused_on_closed_job(
    client, employee_client, employee_user, job, db_session
):
    """Review L-7."""
    from app.models import Job

    client.post(
        f"{API}/jobs/{job['id']}/assignments", json={"user_id": employee_user.id}
    )
    doc = upload_aadhaar(client, job, scan(error="unsupported"))
    task = ocr_task(db_session, doc["id"])
    task.status = "failed"
    info = TaskInfo(
        id=task.id, task_type=task.task_type, payload=task.payload, attempt=1
    )
    ocr_handler.on_ocr_failed(db_session, info, "x")
    db_session.get(Job, job["id"]).status = "completed"
    db_session.flush()
    assert (
        employee_client.post(f"{API}/documents/{doc['id']}/ocr/retry").status_code
        == 422
    )


def test_sealed_values_cannot_be_swapped(client, job, db_session):
    """Review L-8: extraction and review use different binding contexts."""
    from cryptography.exceptions import InvalidTag

    from app.core.storage import unseal

    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    result = db_session.scalar(
        select(OcrResult).where(OcrResult.document_id == doc["id"])
    )
    with pytest.raises(InvalidTag):
        unseal(
            result.extraction_sealed, ocr_handler.sealed_context(doc["id"], 1, "review")
        )


def test_ocr_views_and_refused_reveals_are_audited(
    client, make_client, make_user, job, customer, db_session
):
    """Review L-9."""
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    client.get(f"{API}/documents/{doc['id']}/ocr")
    other = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": job["service"]["id"],
            "title": "2",
        },
    ).json()
    viewer_user = make_user()
    client.post(
        f"{API}/jobs/{other['id']}/assignments", json={"user_id": viewer_user.id}
    )
    make_client(viewer_user).get(
        f"{API}/documents/{doc['id']}/ocr", params={"reveal": True}
    )
    actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == doc["id"])
    ).all()
    assert "ocr_viewed" in actions
    assert "ocr_reveal_denied" in actions


def test_masking_follows_the_result_not_later_type_edits(
    client, job, catalogue, db_session
):
    """Review L-5: the field list is frozen with the result."""
    doc = upload_aadhaar(client, job)
    run_ocr(db_session, doc["id"])
    catalogue["aadhaar"].ocr_fields = [
        {"key": "aadhaar_number", "label": "Aadhaar", "sensitive": False}
    ]
    db_session.flush()
    fields = client.get(f"{API}/documents/{doc['id']}/ocr").json()["fields"]
    by_key = {f["key"]: f for f in fields}
    assert by_key["aadhaar_number"]["value"] == "**** **** 9012"  # still masked
    assert len(fields) == 4


# ---------- review H-1: documents never stay in 'processing' ----------


def _committed_upload(factory, ids, data):
    customer_id, job_id, admin_id = ids
    with factory() as session:
        admin = session.get(User, admin_id)
        item_id = document_service.build_checklist(session, job_id).items[0].id
        return document_service.upload(
            session,
            customer_id,
            check_upload(data, "a.png"),
            admin,
            job_id=job_id,
            checklist_item_id=item_id,
        ).id


def _doc_status(factory, doc_id):
    with factory() as session:
        return session.get(Document, doc_id).status


def test_permanent_failure_through_the_worker_goes_to_review(committed):
    from app.workers.worker import Worker

    factory, ids = committed
    doc_id = _committed_upload(factory, ids, scan(error="unsupported"))
    Worker(factory, worker_id="w1").tick()
    assert _doc_status(factory, doc_id) == "needs_review"
    with factory() as session:
        result = session.scalar(select(OcrResult))
        assert (result.status, result.failure_reason) == (
            "failed",
            ocr_handler.UNREADABLE,
        )


def test_lease_expiry_on_last_attempt_goes_to_review(committed):
    """The worker died on the last attempt: the expiry path runs the hook."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.repositories import task_repository
    from app.workers.worker import Worker

    factory, ids = committed
    doc_id = _committed_upload(factory, ids, GOOD)
    with factory() as session:
        task_repository.claim(session, "crashed-worker")
        session.execute(
            update(BackgroundTask).values(
                attempts=3, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        session.commit()
    Worker(factory, worker_id="w2").requeue_expired()
    assert _doc_status(factory, doc_id) == "needs_review"


def test_maintenance_recovers_documents_the_hook_missed(committed):
    """E.g. the hook could not run (lock clash, crash): the sweep fixes it."""
    from sqlalchemy import update

    from app.workers.worker import Worker

    factory, ids = committed
    doc_id = _committed_upload(factory, ids, GOOD)
    with factory() as session:  # failed task, document left in processing
        session.execute(
            update(BackgroundTask).values(status="failed", last_error="Timed out.")
        )
        session.commit()
    assert _doc_status(factory, doc_id) == "processing"

    assert Worker(factory, worker_id="w1").run_maintenance() == 1

    assert _doc_status(factory, doc_id) == "needs_review"
    with factory() as session:
        assert session.scalar(select(OcrResult)).failure_reason == "Timed out."
