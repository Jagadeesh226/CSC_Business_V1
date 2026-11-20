import json

import pytest
from sqlalchemy import select

from app.core.masking import mask_phone
from app.models import AuditLog

CUSTOMERS = "/api/v1/customers"
RAVI = {
    "name": "Ravi Kumar",
    "phone": "+91 98765 43210",
    "email": "Ravi@Example.com",
    "address": "12 MG Road",
}


def test_mask_phone():
    assert mask_phone("+91 98765 43210") == "********3210"
    assert mask_phone("98765") == "*8765"
    assert mask_phone("123") == "****"
    assert mask_phone(None) is None


# ---------- create ----------


def test_create_customer(client, admin_user, db_session):
    response = client.post(CUSTOMERS, json=RAVI)

    assert response.status_code == 201
    body = response.json()
    customer = body["customer"]
    assert customer["name"] == "Ravi Kumar"
    assert customer["phone"] == "+91 98765 43210"
    assert customer["email"] == "ravi@example.com"
    assert body["possible_duplicates"] == []
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "customer_created")
    )
    assert entry.entity_id == customer["id"]
    assert entry.user_id == admin_user.id
    assert entry.details is None  # no personal data in the audit log


def test_employees_can_create_customers(employee_client):
    assert employee_client.post(CUSTOMERS, json={"name": "Asha"}).status_code == 201


def test_possible_duplicates_by_phone_and_name(client):
    first = client.post(CUSTOMERS, json=RAVI).json()["customer"]
    # Same number written differently: still flagged.
    same_phone = client.post(
        CUSTOMERS, json={"name": "Sita Kumar", "phone": "9876543210"}
    ).json()
    assert [d["id"] for d in same_phone["possible_duplicates"]] == [first["id"]]
    assert same_phone["possible_duplicates"][0]["phone_masked"] == "********3210"

    same_name = client.post(CUSTOMERS, json={"name": "ravi kumar"}).json()
    assert [d["id"] for d in same_name["possible_duplicates"]] == [first["id"]]

    unrelated = client.post(
        CUSTOMERS, json={"name": "Meena", "phone": "9000000000"}
    ).json()
    assert unrelated["possible_duplicates"] == []


def test_duplicates_are_audited_by_id_only(client, db_session):
    first = client.post(CUSTOMERS, json=RAVI).json()["customer"]
    client.post(CUSTOMERS, json={"name": "Other", "phone": RAVI["phone"]})
    entry = db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "customer_created")
        .order_by(AuditLog.id.desc())
    )
    assert json.loads(entry.details) == {"possible_duplicates": [first["id"]]}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "A", "phone": "12345"},
        {"name": "A", "phone": "98765abc10"},
        {"name": "A", "phone": "1" * 14},
        {"name": "A", "email": "not-an-email"},
        {"name": "A", "id": 5},
    ],
)
def test_create_customer_validation(client, body):
    assert client.post(CUSTOMERS, json=body).status_code == 422


# ---------- read and search ----------


def test_get_customer_shows_full_details(client):
    created = client.post(CUSTOMERS, json=RAVI).json()["customer"]
    response = client.get(f"{CUSTOMERS}/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_customer_not_found(client):
    assert client.get(f"{CUSTOMERS}/999999").status_code == 404
    assert client.get(f"{CUSTOMERS}/{2**31}").status_code == 422


def test_list_masks_phone_numbers(client):
    client.post(CUSTOMERS, json=RAVI)
    item = client.get(CUSTOMERS).json()["items"][0]
    assert item["phone_masked"] == "********3210"
    assert "phone" not in item
    assert "43210" not in json.dumps(item)


def test_search_by_name_and_phone_digits(client):
    ravi = client.post(CUSTOMERS, json=RAVI).json()["customer"]
    client.post(CUSTOMERS, json={"name": "Meena", "phone": "9000011111"})

    def ids(search):
        return [
            c["id"]
            for c in client.get(CUSTOMERS, params={"search": search}).json()["items"]
        ]

    assert ids("ravi") == [ravi["id"]]
    assert ids("KUMAR") == [ravi["id"]]
    assert ids("43210") == [ravi["id"]]  # part of the number
    assert ids("98765 43210") == [ravi["id"]]  # spacing ignored
    assert ids("123") == []  # too few digits for a phone search
    assert ids("%") == []  # wildcards are literal


def test_list_pagination(client):
    for i in range(5):
        client.post(CUSTOMERS, json={"name": f"Customer {i}"})
    page = client.get(CUSTOMERS, params={"page": 2, "page_size": 2}).json()
    assert page["total"] == 5
    assert page["total_pages"] == 3
    assert [c["name"] for c in page["items"]] == ["Customer 2", "Customer 3"]


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 101}])
def test_list_validation(client, params):
    assert client.get(CUSTOMERS, params=params).status_code == 422


# ---------- update ----------


def test_update_customer(client, employee_client, db_session):
    created = client.post(CUSTOMERS, json=RAVI).json()["customer"]

    response = employee_client.patch(
        f"{CUSTOMERS}/{created['id']}",
        json={"address": "45 Brigade Road", "email": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["address"] == "45 Brigade Road"
    assert body["email"] is None
    assert body["name"] == "Ravi Kumar"
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "customer_updated")
    )
    assert json.loads(entry.details) == {"fields": ["address", "email"]}
    assert "Brigade" not in entry.details


@pytest.mark.parametrize(
    "body", [{"name": None}, {"phone": "12"}, {"created_at": "2020-01-01"}]
)
def test_update_customer_validation(client, body):
    created = client.post(CUSTOMERS, json=RAVI).json()["customer"]
    assert client.patch(f"{CUSTOMERS}/{created['id']}", json=body).status_code == 422


def test_update_customer_not_found(client):
    assert client.patch(f"{CUSTOMERS}/999999", json={"name": "X"}).status_code == 404


# ---------- access ----------


def test_customers_require_sign_in(anon_client):
    assert anon_client.get(CUSTOMERS).status_code == 401
    assert anon_client.post(CUSTOMERS, json=RAVI).status_code == 401
    assert anon_client.get(f"{CUSTOMERS}/1").status_code == 401
    assert anon_client.patch(f"{CUSTOMERS}/1", json={"name": "x"}).status_code == 401


def test_customer_write_requires_csrf(client):
    del client.headers["X-CSRF-Token"]
    assert client.post(CUSTOMERS, json=RAVI).status_code == 403


# ---------- review fixes ----------


@pytest.mark.parametrize(
    "query", ["+91 98765 43210", "098765 43210", "9876543210", "919876543210", "43210"]
)
def test_phone_search_handles_country_code_and_formatting(client, query):
    """Review L-5."""
    stored = client.post(
        CUSTOMERS, json={"name": "Dashes", "phone": "98765-43210"}
    ).json()["customer"]
    found = client.get(CUSTOMERS, params={"search": query}).json()["items"]
    assert [c["id"] for c in found] == [stored["id"]]


def test_name_search_does_not_match_phone_digits(client):
    """Review L-5: 'Ravi 98765' is a name search, not 'any phone with 98765'."""
    client.post(CUSTOMERS, json={"name": "Someone Else", "phone": "9876500000"})
    assert client.get(CUSTOMERS, params={"search": "Ravi 98765"}).json()["total"] == 0


def test_nul_in_search_is_rejected(client):
    """Review L-8."""
    assert client.get(CUSTOMERS, params={"search": "a\u0000b"}).status_code == 422
