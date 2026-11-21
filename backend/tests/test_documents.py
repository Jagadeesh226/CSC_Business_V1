import io
import json
import threading

import pytest
from cryptography.exceptions import InvalidTag
from pypdf import PdfWriter
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.core.storage import LocalEncryptedStorage, generate_key, get_storage
from app.models import (
    AuditLog,
    Customer,
    Document,
    DocumentType,
    Job,
    JobChecklistItem,
    Service,
    ServiceCategory,
    ServiceDocumentRequirement,
    User,
)
from app.schemas.job import JobCreate
from app.services import document_service, job_service
from app.services.errors import BusinessRuleError, ConflictError
from app.services.file_validation import check_upload, safe_filename

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes"


def pdf_bytes(pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------- storage and file checks (unit) ----------


def test_storage_round_trip_is_encrypted(tmp_path):
    import base64

    storage = LocalEncryptedStorage(tmp_path, base64.urlsafe_b64decode(generate_key()))
    key = storage.save(b"secret aadhaar scan")
    raw = (tmp_path / key).read_bytes()
    assert b"secret aadhaar scan" not in raw
    assert storage.load(key) == b"secret aadhaar scan"
    assert oct((tmp_path / key).stat().st_mode & 0o777) == "0o600"


def test_storage_detects_tampering_and_wrong_key(tmp_path):
    import base64

    storage = LocalEncryptedStorage(tmp_path, base64.urlsafe_b64decode(generate_key()))
    key = storage.save(b"data")
    path = tmp_path / key
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 1
    path.write_bytes(bytes(blob))
    with pytest.raises(InvalidTag):
        storage.load(key)
    other = LocalEncryptedStorage(tmp_path, base64.urlsafe_b64decode(generate_key()))
    with pytest.raises(InvalidTag):
        other.load(storage.save(b"x"))


@pytest.mark.parametrize("key", ["../etc/passwd", "ab/../../x", "AB/" + "0" * 32])
def test_storage_rejects_foreign_keys(tmp_path, key):
    import base64

    storage = LocalEncryptedStorage(tmp_path, base64.urlsafe_b64decode(generate_key()))
    with pytest.raises(ValueError):
        storage.load(key)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\aadhaar card.pdf", "aadhaar card.pdf"),
        ("<script>.png", "_script_.png"),
        ("", "document"),
        ("...", "document"),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_check_upload_accepts_allowed_types():
    pdf = check_upload(pdf_bytes(3), "rc.PDF")
    assert (pdf.content_type, pdf.page_count) == ("application/pdf", 3)
    assert check_upload(PNG, "a.png").content_type == "image/png"
    assert check_upload(JPEG, "a.jpeg").content_type == "image/jpeg"
    assert len(check_upload(PNG, "a.png").sha256) == 64


@pytest.mark.parametrize(
    "data, name, message",
    [
        (b"", "a.pdf", "empty"),
        (b"MZ\x90\x00 windows exe", "a.pdf", "Only PDF, PNG and JPEG"),
        (b"<html><script>", "a.png", "Only PDF, PNG and JPEG"),
        (PNG, "a.pdf", "does not match"),
        (pdf_bytes(), "a.exe", "does not match"),
        (b"%PDF-1.4 garbage", "a.pdf", "could not be read"),
        (pdf_bytes(21), "a.pdf", "at most 20 pages"),
        (pdf_bytes(1, password="pw"), "a.pdf", "Password-protected"),
    ],
)
def test_check_upload_rejects(data, name, message):
    with pytest.raises(BusinessRuleError, match=message):
        check_upload(data, name)


# ---------- fixtures ----------


@pytest.fixture
def catalogue(db_session):
    category = ServiceCategory(name="Transport", code="TRANSPORT")
    db_session.add(category)
    db_session.flush()
    service = Service(
        category_id=category.id,
        name="RC Renewal",
        code="RC-RENEWAL",
        fee_type="variable",
    )
    aadhaar = DocumentType(
        code="AADHAAR",
        name="Aadhaar card",
        sensitivity="sensitive",
        ocr_fields=[{"key": "name", "label": "Name", "sensitive": False}],
    )
    rc = DocumentType(code="RC", name="RC book")
    photo = DocumentType(code="PHOTO", name="Photo")
    retired = DocumentType(code="OLD", name="Old form", is_active=False)
    db_session.add_all([service, aadhaar, rc, photo, retired])
    db_session.flush()
    for doc_type, required, multiple in (
        (aadhaar, True, False),
        (rc, False, False),
        (photo, True, True),
        (retired, True, False),
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
    return {"service": service, "aadhaar": aadhaar, "rc": rc, "photo": photo}


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar")


@pytest.fixture
def job(client, customer, catalogue):
    response = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": catalogue["service"].id,
            "title": "RC renewal",
        },
    )
    assert response.status_code == 201
    return response.json()


def checklist(client, job_id):
    return client.get(f"{API}/jobs/{job_id}/checklist").json()


def item_for(client, job_id, code):
    return next(
        i for i in checklist(client, job_id)["items"] if i["document_type_code"] == code
    )


def upload(client, customer_id, data=PNG, name="scan.png", **form):
    return client.post(
        f"{API}/customers/{customer_id}/documents",
        files={"file": (name, data, "application/octet-stream")},
        data={k: str(v) for k, v in form.items()},
    )


def upload_for(client, job, code, data=PNG, name="scan.png"):
    item = item_for(client, job["id"], code)
    return upload(
        client,
        job["customer"]["id"],
        data,
        name,
        job_id=job["id"],
        checklist_item_id=item["id"],
    )


def audit_actions(db_session, document_id):
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(
                AuditLog.entity_type == "document", AuditLog.entity_id == document_id
            )
            .order_by(AuditLog.id)
        )
    )


# ---------- checklists ----------


def test_new_job_gets_frozen_checklist(client, job, catalogue, db_session):
    body = checklist(client, job["id"])
    assert [
        (i["document_type_code"], i["is_required"], i["state"]) for i in body["items"]
    ] == [
        ("AADHAAR", True, "missing"),
        ("RC", False, "missing"),
        ("PHOTO", True, "missing"),
    ]  # the retired document type is left out
    assert body["required_total"] == 2
    assert body["complete"] is False
    assert body["items"][0]["sensitivity"] == "sensitive"

    # Catalogue edits later do not touch existing jobs.
    catalogue["aadhaar"].name = "Renamed"
    catalogue["service"].name = "RC Renewal (new)"
    db_session.flush()
    assert (
        item_for(client, job["id"], "AADHAAR")["document_type_name"] == "Aadhaar card"
    )
    assert (
        client.get(f"{API}/jobs/{job['id']}").json()["service"]["name"] == "RC Renewal"
    )


def test_checklist_progress_to_complete(client, job):
    doc = upload_for(client, job, "AADHAAR").json()
    assert item_for(client, job["id"], "AADHAAR")["state"] == "pending"

    client.post(f"{API}/documents/{doc['id']}/verify")
    photo = upload_for(client, job, "PHOTO").json()
    client.post(f"{API}/documents/{photo['id']}/verify")

    body = checklist(client, job["id"])
    assert body["required_verified"] == 2
    assert body["complete"] is True  # the optional RC is not needed


# ---------- upload ----------


def test_upload_against_checklist_item(client, admin_user, job, catalogue, db_session):
    response = upload_for(client, job, "AADHAAR", pdf_bytes(2), "aadhaar.pdf")

    assert response.status_code == 201
    doc = response.json()
    assert doc["status"] == "uploaded"
    assert doc["document_type"]["code"] == "AADHAAR"
    assert doc["content_type"] == "application/pdf"
    assert doc["page_count"] == 2
    assert doc["current_version"] == 1
    assert doc["job_ids"] == [job["id"]]
    assert doc["uploaded_by"]["id"] == admin_user.id
    assert [v["version"] for v in doc["versions"]] == [1]
    assert "storage_key" not in json.dumps(doc)
    stored = db_session.get(Document, doc["id"])
    assert get_storage().load(stored.storage_key).startswith(b"%PDF")
    assert audit_actions(db_session, doc["id"]) == ["document_uploaded"]


def test_single_document_item_conflict(client, job):
    first = upload_for(client, job, "AADHAAR").json()
    second = upload_for(client, job, "AADHAAR")
    assert second.status_code == 409
    assert "new version" in second.json()["detail"]

    client.post(f"{API}/documents/{first['id']}/reject", json={"reason": "Blurry"})
    assert upload_for(client, job, "AADHAAR").status_code == 201
    assert item_for(client, job["id"], "AADHAAR")["state"] == "pending"


def test_multiple_document_item(client, job):
    assert upload_for(client, job, "PHOTO").status_code == 201
    assert upload_for(client, job, "PHOTO").status_code == 201
    assert len(item_for(client, job["id"], "PHOTO")["documents"]) == 2


def test_upload_rules(client, job, customer, make_customer, catalogue):
    other = make_customer()
    item = item_for(client, job["id"], "AADHAAR")
    cases = [
        (upload(client, 999999, job_id=job["id"]), 404),
        (upload(client, customer.id, checklist_item_id=item["id"]), 422),
        (upload(client, other.id, job_id=job["id"]), 422),  # other customer's job
        (upload(client, customer.id, job_id=job["id"], checklist_item_id=999999), 404),
        (
            upload(
                client,
                customer.id,
                job_id=job["id"],
                checklist_item_id=item["id"],
                document_type_id=catalogue["rc"].id,
            ),
            422,
        ),
        (upload(client, customer.id, document_type_id=999999), 404),
        (upload(client, customer.id, b"not a real file", "x.png"), 422),
    ]
    assert [r.status_code for r, _ in cases] == [code for _, code in cases]


def test_upload_too_large(client, customer):
    big = PNG + b"0" * (10 * 1024 * 1024)
    response = upload(client, customer.id, big, "big.png")
    assert response.status_code == 413


def test_customer_level_upload(client, customer):
    response = upload(client, customer.id, JPEG, "photo.jpg")
    assert response.status_code == 201
    assert response.json()["job_ids"] == []


# ---------- versions and files ----------


def test_new_version_resets_verification(client, job, db_session):
    doc = upload_for(client, job, "AADHAAR", PNG, "v1.png").json()
    client.post(f"{API}/documents/{doc['id']}/verify")

    response = client.post(
        f"{API}/documents/{doc['id']}/versions",
        files={"file": ("v2.jpg", JPEG, "image/jpeg")},
        data={"change_reason": "Clearer scan"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["current_version"] == 2
    assert body["status"] == "uploaded"
    assert body["verified_by"] is None
    assert [(v["version"], v["change_reason"]) for v in body["versions"]] == [
        (1, None),
        (2, "Clearer scan"),
    ]
    old = client.get(f"{API}/documents/{doc['id']}/download", params={"version": 1})
    new = client.get(f"{API}/documents/{doc['id']}/download")
    assert old.content == PNG
    assert new.content == JPEG
    assert item_for(client, job["id"], "AADHAAR")["state"] == "pending"


def test_new_version_needs_reason(client, job):
    doc = upload_for(client, job, "AADHAAR").json()
    response = client.post(
        f"{API}/documents/{doc['id']}/versions",
        files={"file": ("v2.png", PNG, "image/png")},
    )
    assert response.status_code == 422


def test_download_and_preview_headers_and_audit(client, job, db_session):
    doc = upload_for(client, job, "AADHAAR", pdf_bytes(), "my aadhaar.pdf").json()

    download = client.get(f"{API}/documents/{doc['id']}/download")
    preview = client.get(f"{API}/documents/{doc['id']}/preview")

    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"] == (
        'attachment; filename="my aadhaar.pdf"'
    )
    assert preview.headers["content-disposition"].startswith("inline;")
    for headers in (download.headers, preview.headers):
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"
    assert audit_actions(db_session, doc["id"])[-2:] == [
        "document_downloaded",
        "document_previewed",
    ]
    assert (
        client.get(
            f"{API}/documents/{doc['id']}/download", params={"version": 9}
        ).status_code
        == 404
    )


# ---------- verify, reject, archive ----------


def test_verify_and_reject_lifecycle(client, admin_user, job, db_session):
    doc = upload_for(client, job, "AADHAAR").json()
    url = f"{API}/documents/{doc['id']}"

    verified = client.post(f"{url}/verify").json()
    assert verified["status"] == "verified"
    assert verified["verified_by"]["id"] == admin_user.id
    assert client.post(f"{url}/verify").status_code == 422  # already verified

    rejected = client.post(f"{url}/reject", json={"reason": "Wrong person"}).json()
    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "Wrong person"
    assert rejected["verified_by"] is None
    again = client.post(f"{url}/verify")
    assert again.status_code == 422
    assert "new version" in again.json()["detail"]
    assert client.post(f"{url}/reject", json={"reason": ""}).status_code == 422
    assert audit_actions(db_session, doc["id"])[1:] == [
        "document_verified",
        "document_rejected",
    ]


def test_archive_is_admin_only_and_removes_from_checklist(
    client, employee_client, employee_user, job
):
    client.post(
        f"{API}/jobs/{job['id']}/assignments", json={"user_id": employee_user.id}
    )
    doc = upload_for(client, job, "AADHAAR").json()
    url = f"{API}/documents/{doc['id']}/archive"

    assert employee_client.post(url, json={"reason": "x"}).status_code == 403
    archived = client.post(url, json={"reason": "Uploaded by mistake"}).json()
    assert archived["archived_at"] is not None
    assert item_for(client, job["id"], "AADHAAR")["state"] == "missing"
    assert client.post(url, json={"reason": "again"}).status_code == 422
    listed = client.get(f"{API}/customers/{job['customer']['id']}/documents").json()
    assert listed == []
    everything = client.get(
        f"{API}/customers/{job['customer']['id']}/documents",
        params={"include_archived": True},
    ).json()
    assert [d["id"] for d in everything] == [doc["id"]]


# ---------- reuse ----------


def test_verified_document_is_reused_on_another_job(client, job, customer, catalogue):
    doc = upload_for(client, job, "AADHAAR").json()
    client.post(f"{API}/documents/{doc['id']}/verify")
    second = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": catalogue["service"].id,
            "title": "Second job",
        },
    ).json()
    item = item_for(client, second["id"], "AADHAAR")
    url = f"{API}/jobs/{second['id']}/checklist/{item['id']}/documents"

    response = client.post(url, json={"document_id": doc["id"]})

    assert response.status_code == 201
    assert item_for(client, second["id"], "AADHAAR")["state"] == "verified"
    assert sorted(
        client.get(f"{API}/documents/{doc['id']}").json()["job_ids"]
    ) == sorted([job["id"], second["id"]])
    assert client.post(url, json={"document_id": doc["id"]}).status_code == 409


def test_reuse_rules(client, job, customer, make_customer, catalogue):
    second = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": catalogue["service"].id,
            "title": "2",
        },
    ).json()
    aadhaar_item = item_for(client, second["id"], "AADHAAR")
    url = f"{API}/jobs/{second['id']}/checklist/{aadhaar_item['id']}/documents"

    unverified = upload_for(client, job, "AADHAAR").json()
    assert client.post(url, json={"document_id": unverified["id"]}).status_code == 422

    rc = upload_for(client, job, "RC").json()
    client.post(f"{API}/documents/{rc['id']}/verify")
    mismatch = client.post(url, json={"document_id": rc["id"]})
    assert mismatch.status_code == 422  # wrong document type

    stranger = make_customer()
    other_doc = upload(client, stranger.id).json()
    assert client.post(url, json={"document_id": other_doc["id"]}).status_code == 404


# ---------- submitting needs verified documents ----------


def move(client, job_id, status, **extra):
    return client.post(f"{API}/jobs/{job_id}/status", json={"status": status, **extra})


def test_submit_blocked_until_required_documents_verified(client, job):
    move(client, job["id"], "in_progress")
    blocked = move(client, job["id"], "submitted")
    assert blocked.status_code == 422
    assert blocked.json()["detail"] == (
        "Required documents are not verified yet: Aadhaar card, Photo."
    )

    for code in ("AADHAAR", "PHOTO"):
        doc = upload_for(client, job, code).json()
        client.post(f"{API}/documents/{doc['id']}/verify")
    assert move(client, job["id"], "submitted").status_code == 200


def test_admin_override_needs_reason_and_is_audited(
    client, employee_client, employee_user, job, db_session
):
    client.post(
        f"{API}/jobs/{job['id']}/assignments", json={"user_id": employee_user.id}
    )
    move(client, job["id"], "in_progress")

    no_reason = move(client, job["id"], "submitted", override_document_check=True)
    assert no_reason.status_code == 422
    employee = move(
        employee_client,
        job["id"],
        "submitted",
        override_document_check=True,
        reason="Rush",
    )
    assert employee.status_code == 403

    ok = move(
        client,
        job["id"],
        "submitted",
        override_document_check=True,
        reason="Customer will bring Aadhaar later",
    )
    assert ok.status_code == 200
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "document_check_overridden")
    )
    assert json.loads(entry.details) == {
        "missing": ["Aadhaar card", "Photo"],
        "reason": "Customer will bring Aadhaar later",
    }


def test_job_without_required_documents_submits_freely(client, customer, make_service):
    plain = make_service()  # no document requirements
    job = client.post(
        f"{API}/jobs",
        json={"customer_id": customer.id, "service_id": plain.id, "title": "J"},
    ).json()
    assert checklist(client, job["id"])["items"] == []
    move(client, job["id"], "in_progress")
    assert move(client, job["id"], "submitted").status_code == 200


# ---------- access ----------


def test_employee_document_access(
    client, make_client, make_user, job, customer, catalogue
):
    doc = upload_for(client, job, "AADHAAR").json()
    outsider_user = make_user()
    outsider = make_client(outsider_user)
    url = f"{API}/documents/{doc['id']}"

    # Not assigned to anything of this customer: nothing to see.
    for path in ("", "/download", "/preview"):
        assert outsider.get(url + path).status_code == 404
    assert outsider.get(f"{API}/customers/{customer.id}/documents").json() == []
    assert outsider.get(f"{API}/jobs/{job['id']}/checklist").status_code == 404
    assert outsider.post(f"{url}/verify").status_code == 404
    assert upload(outsider, customer.id).status_code == 403

    # Assigned to the job: sees, verifies and uploads.
    client.post(
        f"{API}/jobs/{job['id']}/assignments", json={"user_id": outsider_user.id}
    )
    assert outsider.get(url).status_code == 200
    assert outsider.get(f"{url}/download").content == PNG
    assert outsider.post(f"{url}/verify").status_code == 200
    assert upload(outsider, customer.id).status_code == 201


def test_employee_cannot_review_unlinked_customer_document(
    client, employee_client, employee_user, job, customer
):
    client.post(
        f"{API}/jobs/{job['id']}/assignments", json={"user_id": employee_user.id}
    )
    loose = upload(client, customer.id).json()  # not used by any job
    url = f"{API}/documents/{loose['id']}"
    assert employee_client.get(url).status_code == 200  # can see (open job)
    assert employee_client.post(f"{url}/verify").status_code == 403


def test_closed_job_documents_are_a_record(
    client, employee_client, employee_user, job, db_session
):
    client.post(
        f"{API}/jobs/{job['id']}/assignments", json={"user_id": employee_user.id}
    )
    doc = upload_for(client, job, "AADHAAR").json()
    db_session.get(Job, job["id"]).status = "completed"
    db_session.flush()

    assert (
        employee_client.post(f"{API}/documents/{doc['id']}/verify").status_code == 422
    )
    replace = employee_client.post(
        f"{API}/documents/{doc['id']}/versions",
        files={"file": ("v2.png", PNG, "image/png")},
        data={"change_reason": "x"},
    )
    assert replace.status_code == 422
    assert upload_for(employee_client, job, "RC").status_code == 422
    assert (
        client.post(f"{API}/documents/{doc['id']}/verify").status_code == 200
    )  # admin


def test_document_routes_require_sign_in(anon_client, customer):
    assert (
        anon_client.get(f"{API}/customers/{customer.id}/documents").status_code == 401
    )
    assert upload(anon_client, customer.id).status_code == 401
    assert anon_client.get(f"{API}/documents/1/download").status_code == 401


def test_storage_not_configured_is_503(client, customer, monkeypatch):
    monkeypatch.delenv("DOCUMENT_ENCRYPTION_KEY")
    response = upload(client, customer.id)
    assert response.status_code == 503
    assert response.json() == {"detail": "Document storage is not configured."}


# ---------- race: two uploads for one single-document item ----------


@pytest.fixture
def committed(test_engine):
    def truncate():
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE audit_logs, job_documents, document_versions, documents, "
                    "job_checklist_items, job_fees, job_status_history, "
                    "job_assignments, jobs, sequence_counters, customers, "
                    "service_document_requirements, document_types, services, "
                    "service_categories, user_sessions, users RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    with Session(test_engine) as session:
        category = ServiceCategory(name="C", code="C")
        session.add(category)
        session.flush()
        service = Service(
            category_id=category.id, name="S", code="S", fee_type="variable"
        )
        doc_type = DocumentType(code="AADHAAR", name="Aadhaar")
        customer = Customer(name="Race Customer")
        admin = User(
            name="Admin",
            email="race.admin@example.com",
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
        item = session.scalar(
            select(JobChecklistItem).where(JobChecklistItem.job_id == job.id)
        )
        ids = customer.id, job.id, item.id, admin.id
    yield ids
    truncate()


def test_concurrent_uploads_to_single_item(test_engine, committed):
    customer_id, job_id, item_id, admin_id = committed
    file = check_upload(PNG, "a.png")

    def do_upload(session):
        return document_service.upload(
            session,
            customer_id,
            file,
            session.get(User, admin_id),
            job_id=job_id,
            checklist_item_id=item_id,
        )

    first = Session(test_engine, autoflush=False)
    result: dict[str, object] = {}
    try:
        real_commit = first.commit
        first.commit = first.flush  # pause before commit, keeping the job lock
        do_upload(first)
        first.commit = real_commit

        def second():
            with Session(test_engine, autoflush=False) as session:
                try:
                    result["value"] = do_upload(session)
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=1)
        blocked = thread.is_alive()
        first.commit()
    finally:
        first.close()
    thread.join(timeout=10)

    assert blocked, f"expected to wait on the job lock, got {result}"
    assert isinstance(result.get("error"), ConflictError), result
    with Session(test_engine) as session:
        assert len(session.scalars(select(Document)).all()) == 1


# ---------- review fixes ----------


def test_employee_cannot_replace_document_of_someone_elses_job(
    client, make_client, make_user, job, customer, catalogue
):
    """Review M-1: replacing needs the same right as verifying."""
    doc = upload_for(client, job, "AADHAAR").json()
    client.post(f"{API}/documents/{doc['id']}/verify")
    other_job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": catalogue["service"].id,
            "title": "B",
        },
    ).json()
    e2_user = make_user()
    client.post(
        f"{API}/jobs/{other_job['id']}/assignments", json={"user_id": e2_user.id}
    )
    e2 = make_client(e2_user)

    assert e2.get(f"{API}/documents/{doc['id']}").status_code == 200  # may see it
    replace = e2.post(
        f"{API}/documents/{doc['id']}/versions",
        files={"file": ("v2.png", PNG, "image/png")},
        data={"change_reason": "mine now"},
    )
    assert replace.status_code == 403
    assert item_for(client, job["id"], "AADHAAR")["state"] == "verified"


def test_oversized_upload_refused_before_sign_in(anon_client, customer, tmp_path):
    """Review M-2: a big anonymous body is refused without being stored."""
    big = b"0" * (11 * 1024 * 1024)
    response = anon_client.post(
        f"{API}/customers/{customer.id}/documents",
        files={"file": ("big.png", big, "image/png")},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "Files can be at most 10 MB."}


def test_body_limit_counts_streamed_bytes():
    """Review M-2: no Content-Length (chunked) is counted as it arrives."""
    import asyncio

    from app.core.body_limit import UploadBodyLimit

    reached_app = []

    async def app(scope, receive, send):
        reached_app.append(True)

    sent = []

    async def send(message):
        sent.append(message)

    chunks = iter([b"a" * 600, b"b" * 600])

    async def receive():
        chunk = next(chunks, b"")
        return {"type": "http.request", "body": chunk, "more_body": chunk != b""}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/customers/1/documents",
        "headers": [],
    }
    asyncio.run(UploadBodyLimit(app, max_body=1000)(scope, receive, send))
    assert reached_app == []
    assert sent[0]["status"] == 413


def test_reviving_rejected_document_cannot_double_fill_item(client, job):
    """Review L-1."""
    first = upload_for(client, job, "AADHAAR").json()
    client.post(f"{API}/documents/{first['id']}/reject", json={"reason": "Blurry"})
    upload_for(client, job, "AADHAAR")  # the replacement document
    revive = client.post(
        f"{API}/documents/{first['id']}/versions",
        files={"file": ("v2.png", PNG, "image/png")},
        data={"change_reason": "retry"},
    )
    assert revive.status_code == 409
    assert (
        len(
            [
                d
                for d in item_for(client, job["id"], "AADHAAR")["documents"]
                if d["status"] != "rejected"
            ]
        )
        == 1
    )


def test_failed_commit_removes_stored_file(db_session, document_storage, customer):
    """Review L-3: no orphan encrypted file when the database write fails."""
    before = {p for p in document_storage.rglob("*") if p.is_file()}
    admin = User(name="A", email="a.l3@example.com", password_hash="x", role="admin")
    db_session.add(admin)
    db_session.flush()

    def fail():
        raise RuntimeError("database went away")

    original = db_session.commit
    db_session.commit = fail
    try:
        with pytest.raises(RuntimeError):
            document_service.upload(
                db_session, customer.id, check_upload(PNG, "a.png"), admin
            )
    finally:
        db_session.commit = original
    after = {p for p in document_storage.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("bad_key", ["not base64!!", "c2hvcnQ="])
def test_malformed_storage_key_is_503(client, customer, monkeypatch, bad_key):
    """Review L-4."""
    monkeypatch.setenv("DOCUMENT_ENCRYPTION_KEY", bad_key)
    assert upload(client, customer.id).status_code == 503


def test_views_and_denied_attempts_are_audited(
    client, make_client, make_user, job, db_session
):
    """Review L-5."""
    doc = upload_for(client, job, "AADHAAR").json()
    client.get(f"{API}/documents/{doc['id']}")
    outsider_user = make_user()
    make_client(outsider_user).get(f"{API}/documents/{doc['id']}/download")

    rows = db_session.execute(
        select(AuditLog.action, AuditLog.user_id)
        .where(AuditLog.entity_type == "document", AuditLog.entity_id == doc["id"])
        .order_by(AuditLog.id)
    ).all()
    assert ("document_viewed", job["created_by"]) in rows
    assert ("document_access_denied", outsider_user.id) in rows


def test_pdf_declaring_too_many_pages_is_refused_cheaply():
    """Review L-7: the declared /Count is checked before the page tree."""
    with pytest.raises(BusinessRuleError, match="at most 20 pages"):
        check_upload(pdf_bytes(25), "big.pdf")


def test_reject_and_submit_race(test_engine, committed):
    """Review L-6: a reject in flight is seen by a concurrent submit."""
    customer_id, job_id, item_id, admin_id = committed
    with Session(test_engine, autoflush=False) as session:
        admin = session.get(User, admin_id)
        doc = document_service.upload(
            session,
            customer_id,
            check_upload(PNG, "a.png"),
            admin,
            job_id=job_id,
            checklist_item_id=item_id,
        )
        document_service.verify(session, doc.id, admin)
        for status in ("in_progress",):
            job_service.change_status(
                session, job_id, job_service.JobStatus(status), actor=admin
            )

    holder = Session(test_engine, autoflush=False)
    result: dict[str, object] = {}
    try:
        real_commit = holder.commit
        holder.commit = holder.flush  # pause: keep the job and document locks
        document_service.reject(
            holder, doc.id, "Wrong person", holder.get(User, admin_id)
        )
        holder.commit = real_commit

        def submit():
            with Session(test_engine, autoflush=False) as session:
                try:
                    result["value"] = job_service.change_status(
                        session,
                        job_id,
                        job_service.JobStatus("submitted"),
                        actor=session.get(User, admin_id),
                    )
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc

        thread = threading.Thread(target=submit)
        thread.start()
        thread.join(timeout=1)
        blocked = thread.is_alive()
        holder.commit()
    finally:
        holder.close()
    thread.join(timeout=10)

    assert blocked, f"expected the submit to wait for the reject, got {result}"
    assert isinstance(result.get("error"), BusinessRuleError), result
    with Session(test_engine) as session:
        assert session.get(Job, job_id).status == "in_progress"
