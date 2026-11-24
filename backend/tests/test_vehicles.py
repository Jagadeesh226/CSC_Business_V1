"""Phase 4.0: vehicles, validity dates and their history."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.vehicle_numbers import looks_like_registration, normalize
from app.models import AuditLog, Job, Vehicle
from app.repositories.readiness_repository import business_today

API = "/api/v1"
CHASSIS = "MA3FJEB1S00123456"
ENGINE = "K12MN1234567"


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="9876543210")


def create(client, customer_id, registration="GA 07 AB 1234", **extra):
    return client.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer_id,
            "registration_number": registration,
            "category": "private_four_wheeler",
            **extra,
        },
    )


def vehicle(client, customer_id, **extra):
    response = create(client, customer_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def actions(db_session, vehicle_id):
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_type == "vehicle", AuditLog.entity_id == vehicle_id)
            .order_by(AuditLog.id)
        )
    )


# ---------- numbers ----------


@pytest.mark.parametrize(
    "raw, key",
    [
        ("ga-07 ab 1234", "GA07AB1234"),
        ("22 BH 1234 AA", "22BH1234AA"),
        (" 6a06u4313 ", "6A06U4313"),
    ],
)
def test_normalize(raw, key):
    assert normalize(raw) == key


@pytest.mark.parametrize(
    "query, expected",
    [
        ("GA07", True),
        ("ga 07 ab", True),
        ("Ravi", False),
        ("1234", False),
        ("G1", False),
    ],
)
def test_looks_like_registration(query, expected):
    assert looks_like_registration(query) is expected


# ---------- creating and reading ----------


def test_create_vehicle(client, admin_user, customer, db_session):
    body = vehicle(
        client,
        customer.id,
        owner_name="RAVI KUMAR",
        chassis_number="ma3fjeb1s00123456",
        engine_number="K12MN-1234567",
    )

    assert body["registration_number"] == "GA 07 AB 1234"
    assert body["customer"] == {"id": customer.id, "name": "Ravi Kumar"}
    assert body["category"] == "private_four_wheeler"
    # Identifiers are masked; stored encrypted with the last four in clear.
    assert (body["chassis_number"], body["engine_number"]) == ("****3456", "****4567")
    assert body["identifiers_masked"] is True
    assert body["assigned_to"] is None  # an admin left it unassigned
    assert (body["challan_status"], body["is_active"], body["expiries"]) == (
        "unknown",
        True,
        [],
    )
    stored = db_session.get(Vehicle, body["id"])
    assert stored.registration_key == "GA07AB1234"
    assert CHASSIS.encode() not in stored.chassis_sealed
    assert actions(db_session, body["id"]) == ["vehicle_created"]


def test_registration_is_unique_whatever_the_spacing(client, customer, make_customer):
    vehicle(client, customer.id, registration="GA07AB1234")
    for same in ("ga 07 ab 1234", "GA-07-AB-1234"):
        response = create(client, make_customer().id, registration=same)
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]


@pytest.mark.parametrize(
    "extra",
    [
        {"registration_number": "GA1"},  # too short
        {"registration_number": "X" * 16},
        {"category": "tractor"},
        {"chassis_number": "AB12C"},  # under 6: the mask would show it whole
        {"registration_number": "TEMP"},  # needs letters and digits
        {"registration_number": "1234"},
        {"contact_phone": "not a phone"},
        {"colour": "red"},
    ],
)
def test_create_validation(client, customer, extra):
    body = {
        "customer_id": customer.id,
        "registration_number": "GA07AB1234",
        "category": "two_wheeler",
        **extra,
    }
    assert client.post(f"{API}/vehicles", json=body).status_code == 422


def test_unknown_customer(client):
    assert create(client, 999_999).status_code == 404


# ---------- access ----------


def test_employee_vehicles_are_theirs(
    client, customer, make_user, make_client, make_customer, make_service
):
    priya, sameer = make_user("employee"), make_user("employee")
    desk, other = make_client(priya), make_client(sameer)

    mine = vehicle(desk, customer.id)
    assert mine["assigned_to"]["id"] == priya.id  # employees own what they add
    assert desk.get(f"{API}/vehicles/{mine['id']}").status_code == 200
    assert other.get(f"{API}/vehicles/{mine['id']}").status_code == 404
    assert other.get(f"{API}/vehicles").json()["total"] == 0

    # An employee may not choose who is responsible.
    response = create(
        desk, customer.id, registration="GA07ZZ0001", assigned_to=sameer.id
    )
    assert response.status_code == 403

    # An open job for the customer lets a colleague see the customer's vehicles.
    job = other.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service().id,
            "title": "T",
        },
    ).json()
    assert other.get(f"{API}/vehicles/{mine['id']}").status_code == 200
    client.post(f"{API}/jobs/{job['id']}/cancel", json={"reason": "Left"})
    assert other.get(f"{API}/vehicles/{mine['id']}").status_code == 404

    # Admins see everything.
    assert client.get(f"{API}/vehicles").json()["total"] == 1


def test_reveal_identifiers(
    client, customer, make_user, make_client, make_service, db_session
):
    priya, sameer = make_user("employee"), make_user("employee")
    desk, other = make_client(priya), make_client(sameer)
    v = vehicle(desk, customer.id, chassis_number=CHASSIS, engine_number=ENGINE)
    url = f"{API}/vehicles/{v['id']}"

    shown = desk.get(url, params={"reveal": "true"}).json()
    assert (shown["chassis_number"], shown["engine_number"]) == (CHASSIS, ENGINE)
    assert shown["identifiers_masked"] is False
    assert (
        client.get(url, params={"reveal": "true"}).json()["chassis_number"] == CHASSIS
    )

    # A colleague who can see the vehicle (open job) may not reveal it.
    other.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service().id,
            "title": "T",
        },
    )
    assert other.get(url).json()["chassis_number"] == "****3456"
    assert other.get(url, params={"reveal": "true"}).status_code == 403
    assert actions(db_session, v["id"]) == [
        "vehicle_created",
        "vehicle_identifiers_viewed",
        "vehicle_identifiers_viewed",
        "vehicle_identifiers_reveal_denied",
    ]
    # Lists never reveal.
    listed = client.get(f"{API}/vehicles").json()["items"][0]
    assert listed["chassis_number"] == "****3456"


# ---------- updating ----------


def test_update_vehicle(client, customer, make_customer, make_user, db_session):
    v = vehicle(client, customer.id, chassis_number=CHASSIS)
    url = f"{API}/vehicles/{v['id']}"
    priya = make_user("employee")
    new_owner = make_customer(name="Meena Devi")

    updated = client.patch(
        url,
        json={
            "registration_number": "GA 07 AB 9999",
            "category": "commercial",
            "chassis_number": None,
            "engine_number": ENGINE,
            "contact_phone": "9000012345",
            "customer_id": new_owner.id,  # sold
            "assigned_to": priya.id,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["registration_number"] == "GA 07 AB 9999"
    assert (body["chassis_number"], body["engine_number"]) == (None, "****4567")
    assert (body["contact_phone"], body["contact_phone_source"]) == (
        "9000012345",
        "manual",
    )
    assert body["customer"]["id"] == new_owner.id
    assert body["assigned_to"]["id"] == priya.id
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "vehicle_updated")
    )
    assert json.loads(entry.details)["fields"] == [
        "assigned_to",
        "category",
        "chassis_number",
        "contact_phone",
        "customer_id",
        "engine_number",
        "registration_number",
    ]
    assert ENGINE not in entry.details  # names only, never values

    # Re-sending the same values changes nothing and records nothing new.
    client.patch(url, json={"engine_number": ENGINE, "category": "commercial"})
    assert actions(db_session, v["id"]).count("vehicle_updated") == 1


def test_update_rules(client, customer, make_user, make_client, make_customer):
    priya = make_user("employee")
    desk = make_client(priya)
    v = vehicle(desk, customer.id)
    other = vehicle(client, customer.id, registration="GA07CD0001")
    url = f"{API}/vehicles/{v['id']}"

    for field, value in (
        ("assigned_to", None),
        ("customer_id", make_customer().id),
        ("is_active", False),
    ):
        response = desk.patch(url, json={field: value})
        assert response.status_code == 403, field
    assert desk.patch(url, json={"owner_name": "R KUMAR"}).status_code == 200
    taken = client.patch(url, json={"registration_number": "ga07cd0001"})
    assert taken.status_code == 409
    assert client.patch(url, json={"registration_number": None}).status_code == 422
    assert client.patch(url, json={"assigned_to": 999_999}).status_code == 422
    assert (
        desk.patch(
            f"{API}/vehicles/{other['id']}", json={"owner_name": "X"}
        ).status_code
        == 404
    )


def test_inactive_vehicles_are_hidden_by_default(client, customer):
    v = vehicle(client, customer.id)
    client.patch(f"{API}/vehicles/{v['id']}", json={"is_active": False})
    assert client.get(f"{API}/vehicles").json()["total"] == 0
    listed = client.get(f"{API}/vehicles", params={"is_active": "false"}).json()
    assert [i["id"] for i in listed["items"]] == [v["id"]]


# ---------- validity dates ----------


def test_expiries_keep_their_history(client, admin_user, customer, db_session):
    v = vehicle(client, customer.id)
    today = business_today(db_session)
    url = f"{API}/vehicles/{v['id']}/expiries"

    first = client.put(
        f"{url}/insurance",
        json={"valid_until": (today + timedelta(days=20)).isoformat()},
    )
    assert first.status_code == 200, first.text
    insurance = first.json()["expiries"][0]
    assert insurance["expiry_type"] == "insurance"
    assert insurance["days_left"] == 20
    assert insurance["source"] == "manual"
    assert insurance["recorded_by"]["id"] == admin_user.id

    renewed = (today + timedelta(days=385)).isoformat()
    client.put(f"{url}/insurance", json={"valid_until": renewed})
    client.put(f"{url}/insurance", json={"valid_until": renewed})  # no change
    client.put(
        f"{url}/pucc", json={"valid_until": (today - timedelta(days=3)).isoformat()}
    )
    client.put(f"{url}/permit", json={"valid_until": None})  # never set: no-op

    body = client.get(f"{API}/vehicles/{v['id']}").json()
    current = {
        e["expiry_type"]: (e["valid_until"], e["days_left"]) for e in body["expiries"]
    }
    assert current == {
        "insurance": (renewed, 385),
        "pucc": ((today - timedelta(days=3)).isoformat(), -3),
    }
    history = client.get(f"{url}/insurance/history").json()["items"]
    assert [h["days_left"] for h in history] == [385, 20]  # newest first
    assert actions(db_session, v["id"]).count("vehicle_expiry_set") == 3
    assert client.put(f"{url}/road_tax", json={"valid_until": None}).status_code == 422


def test_expiry_from_a_document(client, customer, make_customer, db_session):
    v = vehicle(client, customer.id)
    policy = client.post(
        f"{API}/customers/{customer.id}/documents",
        files={"file": ("policy.png", b"\x89PNG\r\n\x1a\npolicy", "image/png")},
    ).json()
    response = client.put(
        f"{API}/vehicles/{v['id']}/expiries/insurance",
        json={"valid_until": "2027-03-31", "document_id": policy["id"]},
    )
    entry = response.json()["expiries"][0]
    assert (entry["source"], entry["source_document_id"]) == ("document", policy["id"])

    stranger = make_customer()
    other_doc = client.post(
        f"{API}/customers/{stranger.id}/documents",
        files={"file": ("p.png", b"\x89PNG\r\n\x1a\nother", "image/png")},
    ).json()
    wrong = client.put(
        f"{API}/vehicles/{v['id']}/expiries/insurance",
        json={"valid_until": "2027-03-31", "document_id": other_doc["id"]},
    )
    assert wrong.status_code == 422


# ---------- search ----------


def test_search_vehicles(client, customer, make_customer):
    a = vehicle(client, customer.id, registration="GA 07 AB 1234", owner_name="RAVI")
    b = vehicle(client, make_customer(name="Meena Devi").id, registration="KA01CD5678")

    def ids(**params):
        return [
            i["id"]
            for i in client.get(f"{API}/vehicles", params=params).json()["items"]
        ]

    assert ids(search="ga07ab") == [a["id"]]
    assert ids(search="GA-07") == [a["id"]]
    assert ids(search="meena") == [b["id"]]
    assert ids(search="ravi") == [a["id"]]
    assert ids(customer_id=customer.id) == [a["id"]]
    assert ids(category="private_four_wheeler") == [a["id"], b["id"]]
    assert ids(search="ZZ99") == []


def test_customer_search_finds_vehicle_owners(client, customer):
    vehicle(client, customer.id, registration="GA 07 AB 1234")
    found = client.get(f"{API}/customers", params={"search": "ga07ab1234"}).json()
    assert [c["id"] for c in found["items"]] == [customer.id]
    # Plain names still search names only.
    assert (
        client.get(f"{API}/customers", params={"search": "Ravi"}).json()["total"] == 1
    )


# ---------- jobs ----------


def test_job_for_a_vehicle(client, customer, make_customer, make_service, db_session):
    v = vehicle(client, customer.id)
    service = make_service()
    job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": service.id,
            "title": "Insurance renewal",
            "vehicle_id": v["id"],
        },
    )
    assert job.status_code == 201, job.text
    assert job.json()["vehicle"] == {
        "id": v["id"],
        "registration_number": "GA 07 AB 1234",
    }
    listed = client.get(f"{API}/jobs").json()["items"][0]
    assert listed["vehicle"]["id"] == v["id"]

    # Someone else's vehicle cannot be linked.
    other = vehicle(client, make_customer().id, registration="KA01CD5678")
    bad = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": service.id,
            "title": "X",
            "vehicle_id": other["id"],
        },
    )
    assert bad.status_code == 422
    url = f"{API}/jobs/{job.json()['id']}"
    assert client.patch(url, json={"vehicle_id": other["id"]}).status_code == 422
    assert client.patch(url, json={"vehicle_id": None}).json()["vehicle"] is None
    assert db_session.get(Job, job.json()["id"]).vehicle_id is None


# ---------- review fixes ----------


def test_customer_search_by_registration_respects_vehicle_access(
    client, customer, make_user, make_client
):
    vehicle(client, customer.id, registration="KA 01 AB 1234")
    desk = make_client(make_user("employee"))
    # The employee cannot see the vehicle, so the search finds nobody.
    assert desk.get(f"{API}/customers", params={"search": "KA01"}).json()["total"] == 0
    own = vehicle(desk, customer.id, registration="KA 01 ZZ 9999")
    found = desk.get(f"{API}/customers", params={"search": "KA01"}).json()
    assert [c["id"] for c in found["items"]] == [customer.id]
    assert own["assigned_to"] is not None
    # Admins find owners of any vehicle.
    assert (
        client.get(f"{API}/customers", params={"search": "KA01AB"}).json()["total"] == 1
    )


def test_only_those_who_may_see_identifiers_may_change_them(
    client, customer, make_user, make_client, make_service, db_session
):
    priya, sameer = make_user("employee"), make_user("employee")
    desk, other = make_client(priya), make_client(sameer)
    v = vehicle(desk, customer.id, chassis_number=CHASSIS)
    # Sameer can see the vehicle through an open job, but not its numbers.
    other.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service().id,
            "title": "T",
        },
    )
    url = f"{API}/vehicles/{v['id']}"
    for body in (
        {"chassis_number": None},
        {"chassis_number": CHASSIS},
        {"engine_number": ENGINE},
    ):
        assert other.patch(url, json=body).status_code == 403
    assert other.patch(url, json={"owner_name": "RAVI K"}).status_code == 200
    assert desk.get(url, params={"reveal": "true"}).json()["chassis_number"] == CHASSIS
    assert desk.patch(url, json={"engine_number": ENGINE}).status_code == 200


def test_transfer_waits_for_open_jobs_and_records_the_move(
    client, customer, make_customer, make_service, make_user, db_session
):
    v = vehicle(client, customer.id)
    job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service().id,
            "title": "Insurance",
            "vehicle_id": v["id"],
        },
    ).json()
    buyer = make_customer(name="New Owner")
    url = f"{API}/vehicles/{v['id']}"
    blocked = client.patch(url, json={"customer_id": buyer.id})
    assert blocked.status_code == 422
    assert "open jobs" in blocked.json()["detail"]

    client.post(f"{API}/jobs/{job['id']}/cancel", json={"reason": "Sold"})
    priya = make_user("employee")
    moved = client.patch(url, json={"customer_id": buyer.id, "assigned_to": priya.id})
    assert moved.status_code == 200, moved.text
    entry = db_session.scalars(
        select(AuditLog.details)
        .where(AuditLog.action == "vehicle_updated")
        .order_by(AuditLog.id.desc())
    ).first()
    details = json.loads(entry)
    assert details["customer_id"] == {"from": customer.id, "to": buyer.id}
    assert details["assigned_to"] == {"from": None, "to": priya.id}


def test_clearing_an_unset_expiry_records_nothing(client, customer, db_session):
    v = vehicle(client, customer.id)
    url = f"{API}/vehicles/{v['id']}/expiries/permit"
    assert client.put(url, json={"valid_until": None}).json()["expiries"] == []
    assert client.get(f"{url}/history").json()["items"] == []
    assert "vehicle_expiry_set" not in actions(db_session, v["id"])


def test_hidden_vehicle_dates_are_out_of_reach(
    client, customer, make_user, make_client
):
    v = vehicle(client, customer.id)
    desk = make_client(make_user("employee"))
    url = f"{API}/vehicles/{v['id']}/expiries/insurance"
    assert desk.put(url, json={"valid_until": "2027-01-01"}).status_code == 404
    assert desk.get(f"{url}/history").status_code == 404


def test_long_registration_spacing_is_tidied(client, customer):
    body = vehicle(client, customer.id, registration="KA  -  01  -  AB  -  1234")
    assert body["registration_number"] == "KA - 01 - AB - 1234"
    longer = vehicle(client, customer.id, registration="KA - 01 - AB - 1234 - 56")
    assert longer["registration_number"] == "KA01AB123456"
