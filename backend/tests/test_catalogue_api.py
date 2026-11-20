import json

import pytest
from sqlalchemy import select

from app.models import AuditLog

API = "/api/v1"
AADHAAR = {
    "code": "aadhaar",
    "name": "Aadhaar card",
    "sensitivity": "sensitive",
    "ocr_fields": [
        {"key": "name", "label": "Name"},
        {"key": "aadhaar_number", "label": "Aadhaar number", "sensitive": True},
        {"key": "date_of_birth", "label": "Date of birth"},
    ],
}


@pytest.fixture
def category(client):
    response = client.post(
        f"{API}/service-categories", json={"name": "Transport", "code": "transport"}
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def service(client, category):
    response = client.post(
        f"{API}/services",
        json={
            "category_id": category["id"],
            "name": "RC Renewal",
            "code": "rc-renewal",
            "fee_type": "fixed",
            "default_service_fee": "250",
            "requires_vehicle": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def aadhaar(client):
    response = client.post(f"{API}/document-types", json=AADHAAR)
    assert response.status_code == 201, response.text
    return response.json()


# ---------- categories ----------


def test_category_create_and_list(client, employee_client, category):
    assert category["code"] == "TRANSPORT"
    assert category["is_active"] is True
    listed = employee_client.get(f"{API}/service-categories").json()
    assert [c["id"] for c in listed] == [category["id"]]


def test_category_duplicates(client, category):
    dup_code = {"name": "Other", "code": "TRANSPORT"}
    dup_name = {"name": "Transport", "code": "OTHER"}
    assert client.post(f"{API}/service-categories", json=dup_code).status_code == 409
    assert client.post(f"{API}/service-categories", json=dup_name).status_code == 409


def test_category_deactivate_and_filter(client, category):
    url = f"{API}/service-categories/{category['id']}"
    assert client.patch(url, json={"is_active": False}).json()["is_active"] is False
    assert client.get(f"{API}/service-categories", params={"active": True}).json() == []


@pytest.mark.parametrize(
    "body",
    [{"name": "x", "code": "a"}, {"name": "x", "code": "has space"}, {"code": "OK"}],
)
def test_category_validation(client, body):
    assert client.post(f"{API}/service-categories", json=body).status_code == 422


# ---------- services ----------


def test_service_create_and_read(client, employee_client, service, category):
    assert service["code"] == "RC-RENEWAL"
    assert service["default_service_fee"] == "250.00"
    assert service["category"] == {
        "id": category["id"],
        "name": "Transport",
        "code": "TRANSPORT",
    }
    assert employee_client.get(f"{API}/services/{service['id']}").json() == service


def test_service_fee_rules(client, category):
    base = {"category_id": category["id"], "name": "S", "code": "S1"}
    assert client.post(f"{API}/services", json=base).status_code == 422  # fixed, no fee
    variable = client.post(f"{API}/services", json={**base, "fee_type": "variable"})
    assert variable.status_code == 201
    switch = client.patch(
        f"{API}/services/{variable.json()['id']}", json={"fee_type": "fixed"}
    )
    assert switch.status_code == 422
    assert "default_service_fee" in switch.json()["detail"]


def test_service_duplicate_code(client, service, category):
    body = {
        "category_id": category["id"],
        "name": "Again",
        "code": "rc-renewal",
        "fee_type": "variable",
    }
    assert client.post(f"{API}/services", json=body).status_code == 409


def test_service_in_inactive_or_missing_category(client, category):
    client.patch(
        f"{API}/service-categories/{category['id']}", json={"is_active": False}
    )
    body = {
        "category_id": category["id"],
        "name": "S",
        "code": "S2",
        "fee_type": "variable",
    }
    assert client.post(f"{API}/services", json=body).status_code == 422
    body["category_id"] = 999999
    assert client.post(f"{API}/services", json=body).status_code == 404


def test_active_service_list_for_dropdown(client, service, category):
    other = client.post(
        f"{API}/services",
        json={
            "category_id": category["id"],
            "name": "Old",
            "code": "OLD",
            "fee_type": "variable",
        },
    ).json()
    client.patch(f"{API}/services/{other['id']}", json={"is_active": False})

    active = client.get(f"{API}/services", params={"active": True}).json()
    assert [s["id"] for s in active] == [service["id"]]

    # A deactivated category hides its services from the dropdown.
    client.patch(
        f"{API}/service-categories/{category['id']}", json={"is_active": False}
    )
    assert client.get(f"{API}/services", params={"active": True}).json() == []
    assert len(client.get(f"{API}/services").json()) == 2


def test_service_update_audited(client, admin_user, service, db_session):
    client.patch(f"{API}/services/{service['id']}", json={"default_service_fee": "300"})
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "service_updated")
    )
    assert entry.user_id == admin_user.id
    assert entry.entity_id == service["id"]


# ---------- document types ----------


def test_document_type_create(client, employee_client, aadhaar):
    assert aadhaar["code"] == "AADHAAR"
    assert aadhaar["sensitivity"] == "sensitive"
    assert aadhaar["ocr_fields"][1] == {
        "key": "aadhaar_number",
        "label": "Aadhaar number",
        "sensitive": True,
    }
    assert aadhaar["ocr_fields"][0]["sensitive"] is False
    assert [t["id"] for t in employee_client.get(f"{API}/document-types").json()] == [
        aadhaar["id"]
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"code": "A A"},
        {"sensitivity": "secret"},
        {"ocr_fields": [{"key": "Bad Key", "label": "x"}]},
        {"ocr_fields": [{"key": "a", "label": "x"}, {"key": "a", "label": "y"}]},
        {"ocr_fields": [{"key": f"f{i}", "label": "x"} for i in range(31)]},
    ],
)
def test_document_type_validation(client, overrides):
    assert (
        client.post(f"{API}/document-types", json={**AADHAAR, **overrides}).status_code
        == 422
    )


def test_document_type_duplicate_and_update(client, aadhaar):
    assert client.post(f"{API}/document-types", json=AADHAAR).status_code == 409
    url = f"{API}/document-types/{aadhaar['id']}"
    updated = client.patch(
        url, json={"ocr_fields": [{"key": "name", "label": "Full name"}]}
    )
    assert updated.json()["ocr_fields"] == [
        {"key": "name", "label": "Full name", "sensitive": False}
    ]
    assert client.patch(url, json={"name": None}).status_code == 422


# ---------- service document requirements ----------


def test_service_requirements(client, employee_client, service, aadhaar):
    rc = client.post(
        f"{API}/document-types", json={"code": "RC", "name": "RC book"}
    ).json()
    url = f"{API}/services/{service['id']}/document-requirements"

    first = client.post(url, json={"document_type_id": aadhaar["id"]})
    assert first.status_code == 201
    client.post(
        url, json={"document_type_id": rc["id"], "is_required": False, "sort_order": 1}
    )

    listed = employee_client.get(url).json()
    assert [(r["document_type"]["code"], r["is_required"]) for r in listed] == [
        ("AADHAAR", True),
        ("RC", False),
    ]
    assert listed[0]["document_type"]["sensitivity"] == "sensitive"

    assert client.post(url, json={"document_type_id": aadhaar["id"]}).status_code == 409

    req_url = f"{url}/{first.json()['id']}"
    assert client.patch(req_url, json={"is_active": False}).json()["is_active"] is False
    active = client.get(url, params={"active": True}).json()
    assert [r["document_type"]["code"] for r in active] == ["RC"]


def test_requirement_rules(client, service, aadhaar, category):
    url = f"{API}/services/{service['id']}/document-requirements"
    client.patch(f"{API}/document-types/{aadhaar['id']}", json={"is_active": False})
    assert client.post(url, json={"document_type_id": aadhaar["id"]}).status_code == 422
    assert client.post(url, json={"document_type_id": 999999}).status_code == 404
    assert (
        client.post(
            f"{API}/services/999999/document-requirements",
            json={"document_type_id": aadhaar["id"]},
        ).status_code
        == 404
    )


def test_requirement_must_belong_to_service(client, service, aadhaar, category):
    other = client.post(
        f"{API}/services",
        json={
            "category_id": category["id"],
            "name": "O",
            "code": "O1",
            "fee_type": "variable",
        },
    ).json()
    req = client.post(
        f"{API}/services/{service['id']}/document-requirements",
        json={"document_type_id": aadhaar["id"]},
    ).json()
    wrong = f"{API}/services/{other['id']}/document-requirements/{req['id']}"
    assert client.patch(wrong, json={"is_required": False}).status_code == 404


# ---------- access ----------


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("post", "/service-categories", {"name": "X", "code": "XX"}),
        ("patch", "/service-categories/1", {"name": "X"}),
        (
            "post",
            "/services",
            {"category_id": 1, "name": "X", "code": "XX", "fee_type": "variable"},
        ),
        ("patch", "/services/1", {"name": "X"}),
        ("post", "/services/1/document-requirements", {"document_type_id": 1}),
        ("patch", "/services/1/document-requirements/1", {"is_required": False}),
        ("post", "/document-types", {"code": "XX", "name": "X"}),
        ("patch", "/document-types/1", {"name": "X"}),
    ],
)
def test_catalogue_changes_are_admin_only(
    anon_client, employee_client, method, path, body
):
    assert getattr(anon_client, method)(API + path, json=body).status_code == 401
    response = getattr(employee_client, method)(API + path, json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin access required."}


@pytest.mark.parametrize(
    "path", ["/service-categories", "/services", "/document-types", "/services/1"]
)
def test_catalogue_reads_require_sign_in(anon_client, path):
    assert anon_client.get(API + path).status_code == 401


# ---------- review fixes ----------


def test_inactive_category_blocks_new_jobs(client, service, category, make_customer):
    """Review M-1: hidden from the dropdown means unusable for jobs too."""
    customer = make_customer()
    client.patch(
        f"{API}/service-categories/{category['id']}", json={"is_active": False}
    )
    response = client.post(
        f"{API}/jobs",
        json={"customer_id": customer.id, "service_id": service["id"], "title": "J"},
    )
    assert response.status_code == 422
    assert "category is inactive" in response.json()["detail"]


def test_cannot_reactivate_service_in_inactive_category(client, service, category):
    """Review M-1."""
    client.patch(f"{API}/services/{service['id']}", json={"is_active": False})
    client.patch(
        f"{API}/service-categories/{category['id']}", json={"is_active": False}
    )
    response = client.patch(f"{API}/services/{service['id']}", json={"is_active": True})
    assert response.status_code == 422
    assert "Reactivate the category first" in response.json()["detail"]


def test_retired_document_type_leaves_active_requirements(client, service, aadhaar):
    """Review L-2."""
    url = f"{API}/services/{service['id']}/document-requirements"
    req = client.post(url, json={"document_type_id": aadhaar["id"]}).json()
    client.patch(f"{API}/document-types/{aadhaar['id']}", json={"is_active": False})

    assert client.get(url, params={"active": True}).json() == []
    assert [r["id"] for r in client.get(url, params={"active": False}).json()] == [
        req["id"]
    ]
    reactivate = client.patch(f"{url}/{req['id']}", json={"is_active": True})
    assert reactivate.status_code == 422
    # Re-adding a type the service already lists is a conflict, whatever its state.
    assert client.post(url, json={"document_type_id": aadhaar["id"]}).status_code == 409


def test_requirement_audit_names_the_requirement(client, service, aadhaar, db_session):
    """Review L-3."""
    url = f"{API}/services/{service['id']}/document-requirements"
    req = client.post(url, json={"document_type_id": aadhaar["id"]}).json()
    client.patch(f"{url}/{req['id']}", json={"is_required": False})
    added, updated = db_session.scalars(
        select(AuditLog.details)
        .where(AuditLog.action.like("service_requirement_%"))
        .order_by(AuditLog.id)
    ).all()
    assert json.loads(added) == {
        "requirement_id": req["id"],
        "document_type": "AADHAAR",
    }
    assert json.loads(updated) == {
        "requirement_id": req["id"],
        "document_type": "AADHAAR",
        "fields": ["is_required"],
    }


def test_category_names_unique_ignoring_case(client, category):
    """Review L-7."""
    body = {"name": "TRANSPORT", "code": "OTHER"}
    assert client.post(f"{API}/service-categories", json=body).status_code == 409


def test_unstorable_characters_are_rejected(client, category):
    """Review L-8: a NUL character is a 422, not a server error."""
    body = {"name": "Bad\u0000Name", "code": "BAD"}
    response = client.post(f"{API}/service-categories", json=body)
    assert response.status_code == 422
    assert "cannot be stored" in response.json()["detail"]


def test_concurrent_duplicate_create_is_conflict(test_engine):
    """Review L-1: the insert race returns 409, not 500."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.models import User
    from app.schemas.catalogue import DocumentTypeCreate
    from app.services import catalogue_service
    from app.services.errors import ConflictError

    with Session(test_engine) as setup:
        setup.execute(text("TRUNCATE document_types RESTART IDENTITY CASCADE"))
        setup.commit()
    first = Session(test_engine, autoflush=False)
    try:
        actor = User(id=0, role="admin")
        data = DocumentTypeCreate(code="RACE", name="Race")
        # First insert holds the unique key, uncommitted.
        from app.models import DocumentType

        first.add(DocumentType(code="RACE", name="Race"))
        first.flush()

        import threading

        result: dict[str, object] = {}

        def second():
            with Session(test_engine, autoflush=False) as session:
                # Bypass the pre-check, as a request that passed it just before
                # the first commit would.
                from app.repositories import catalogue_repository

                original = catalogue_repository.code_taken
                catalogue_repository.code_taken = lambda *a, **k: False
                try:
                    catalogue_service.create_document_type(session, data, actor)
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc
                finally:
                    catalogue_repository.code_taken = original

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=1)
        first.commit()
    finally:
        first.close()
    thread.join(timeout=10)
    assert isinstance(result.get("error"), ConflictError), result
    with Session(test_engine) as cleanup:
        cleanup.execute(text("TRUNCATE document_types RESTART IDENTITY CASCADE"))
        cleanup.commit()
