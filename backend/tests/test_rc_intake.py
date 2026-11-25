"""Phase 4.1: the registration certificate creates the vehicle by itself."""

import itertools
import json

import pytest
from flow_helpers import run_worker
from sqlalchemy import select

from app.models import AuditLog, Document, Vehicle, VehicleIntake
from app.services import vehicle_intake

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n"
CHASSIS = "MA3FJEB1S00123456"
ENGINE = "K12MN1234567"
_unique = itertools.count(1)


def rc_scan(**fields) -> bytes:
    """A fake RC image; each field is (value, confidence)."""
    defaults = {
        "registration_number": ("GA 07 AB 1234", 0.98),
        "owner_name": ("RAVI KUMAR", 0.95),
        "chassis_number": (CHASSIS, 0.96),
        "engine_number": (ENGINE, 0.94),
        "vehicle_class": ("MOTOR CAR (LMV)", 0.9),
    }
    defaults.update(fields)
    script = {
        "fields": {k: list(v) for k, v in defaults.items() if v is not None},
        "raw_text": "CERTIFICATE OF REGISTRATION",
        "n": next(_unique),
    }
    return PNG + b"FAKEOCR:" + json.dumps(script).encode()


@pytest.fixture
def rc_type(client):
    response = client.post(
        f"{API}/document-types",
        json={
            "code": "RC",
            "name": "Registration certificate",
            "sensitivity": "sensitive",
            "vehicle_role": "registration_certificate",
            "ocr_fields": [
                {"key": "registration_number", "label": "Registration no."},
                {"key": "owner_name", "label": "Owner"},
                {"key": "chassis_number", "label": "Chassis no.", "sensitive": True},
                {"key": "engine_number", "label": "Engine no.", "sensitive": True},
                {"key": "vehicle_class", "label": "Vehicle class"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(make_client, priya, customer, make_service):
    """Priya at the counter, with a job open for the customer (employees
    add a customer's documents while working on one of their jobs)."""
    client = make_client(priya)
    response = client.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service().id,
            "title": "RC renewal",
        },
    )
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="9876543210")


def upload_rc(client, customer_id, rc_type, data):
    response = client.post(
        f"{API}/customers/{customer_id}/documents",
        files={"file": ("rc.png", data, "image/png")},
        data={"document_type_id": str(rc_type["id"])},
    )
    assert response.status_code == 201, response.text
    return response.json()


def intake(client, document_id, **params):
    return client.get(f"{API}/documents/{document_id}/vehicle-intake", params=params)


def vehicles_of(db_session, customer_id):
    return db_session.scalars(
        select(Vehicle).where(Vehicle.customer_id == customer_id)
    ).all()


# ---------- reading rules ----------


@pytest.mark.parametrize(
    "text, category",
    [
        ("M-CYCLE/SCOOTER", "two_wheeler"),
        ("MOTOR CAR (LMV)", "private_four_wheeler"),
        ("GOODS CARRIER (LGV)", "commercial"),
        ("TRACTOR", "other"),
        (None, None),
    ],
)
def test_category_from_class(text, category):
    result = vehicle_intake.category_from_class(text)
    assert (result.value if result else None) == category


def test_assess_keeps_only_safe_readings():
    clean, checks = vehicle_intake.assess(
        {
            "registration_number": "ga 07 ab 1234",
            "owner_name": "RAVI",
            "chassis_number": "MA3FJ123",  # not 17 characters
            "engine_number": "K12MN1234567",
            "vehicle_class": "M-CYCLE",
        },
        {
            "registration_number": 0.99,
            "owner_name": 0.5,  # unsure
            "chassis_number": 0.99,
            "engine_number": 0.99,
            "vehicle_class": 0.95,
        },
    )
    assert clean == {
        "registration_number": "GA 07 AB 1234",
        "engine_number": "K12MN1234567",
        "category": "two_wheeler",
    }
    assert checks == [
        {"key": "owner_name", "reason": "low_confidence"},
        {"key": "chassis_number", "reason": "unusual_length"},
    ]


# ---------- catalogue ----------


def test_rc_type_needs_a_registration_field(client, rc_type):
    bad = client.post(
        f"{API}/document-types",
        json={
            "code": "RC2",
            "name": "RC",
            "vehicle_role": "registration_certificate",
            "ocr_fields": [{"key": "owner_name", "label": "Owner"}],
        },
    )
    assert bad.status_code == 422
    assert rc_type["vehicle_role"] == "registration_certificate"
    dropped = client.patch(
        f"{API}/document-types/{rc_type['id']}",
        json={"ocr_fields": [{"key": "owner_name", "label": "Owner"}]},
    )
    assert dropped.status_code == 422
    cleared = client.patch(
        f"{API}/document-types/{rc_type['id']}", json={"vehicle_role": None}
    )
    assert cleared.json()["vehicle_role"] is None


# ---------- the automatic path ----------


def test_clean_rc_creates_the_vehicle_with_no_one_typing(
    client, desk, priya, customer, rc_type, db_session
):
    document = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)

    [vehicle] = vehicles_of(db_session, customer.id)
    body = client.get(f"{API}/vehicles/{vehicle.id}").json()
    assert body["registration_number"] == "GA 07 AB 1234"
    assert body["owner_name"] == "RAVI KUMAR"
    assert body["category"] == "private_four_wheeler"
    assert (body["chassis_number"], body["engine_number"]) == ("****3456", "****4567")
    assert body["assigned_to"]["id"] == priya.id  # the uploader follows up
    assert CHASSIS.encode() not in vehicle.chassis_sealed

    reading = intake(desk, document["id"]).json()
    assert reading["status"] == "applied"
    assert (reading["check_fields"], reading["pending_changes"]) == ([], [])
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 0
    created = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "vehicle_created")
    )
    details = json.loads(created.details)
    assert (details["source"], details["document_id"]) == ("document", document["id"])
    trail = json.dumps(db_session.scalars(select(AuditLog.details)).all())
    assert CHASSIS not in trail and ENGINE not in trail


def test_only_doubtful_fields_wait_for_a_person(
    client, desk, customer, rc_type, db_session
):
    document = upload_rc(
        desk,
        customer.id,
        rc_type,
        rc_scan(chassis_number=("MA3FJEB1S0012345", 0.97), engine_number=(ENGINE, 0.4)),
    )
    run_worker(db_session, expect=1)

    [vehicle] = vehicles_of(db_session, customer.id)
    assert vehicle.chassis_sealed is None and vehicle.engine_sealed is None
    assert vehicle.owner_name == "RAVI KUMAR"  # the rest went straight in
    reading = intake(desk, document["id"]).json()
    assert reading["status"] == "needs_check"
    assert reading["check_fields"] == [
        {"key": "chassis_number", "reason": "unusual_length", "read_value": "****2345"},
        {"key": "engine_number", "reason": "low_confidence", "read_value": "****4567"},
    ]
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 1
    full = intake(desk, document["id"], reveal="true").json()
    assert full["check_fields"][1]["read_value"] == ENGINE

    resolved = desk.post(
        f"{API}/documents/{document['id']}/vehicle-intake/resolve",
        json={"values": {"chassis_number": CHASSIS, "engine_number": ENGINE}},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    shown = desk.get(f"{API}/vehicles/{vehicle.id}", params={"reveal": "true"}).json()
    assert (shown["chassis_number"], shown["engine_number"]) == (CHASSIS, ENGINE)
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 0
    again = desk.post(
        f"{API}/documents/{document['id']}/vehicle-intake/resolve", json={}
    )
    assert again.status_code == 422  # nothing left to do


def test_unreadable_registration_waits_then_creates(
    client, desk, customer, rc_type, db_session
):
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(registration_number=("GA07AB12?4", 0.3))
    )
    run_worker(db_session, expect=1)
    assert vehicles_of(db_session, customer.id) == []
    reading = intake(desk, document["id"]).json()
    assert (reading["status"], reading["vehicle"]) == ("needs_check", None)

    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    assert desk.post(url, json={}).status_code == 422
    assert (
        desk.post(url, json={"values": {"registration_number": "TEMP"}}).status_code
        == 422
    )
    done = desk.post(url, json={"values": {"registration_number": "GA 07 AB 1234"}})
    assert done.status_code == 200, done.text
    [vehicle] = vehicles_of(db_session, customer.id)
    assert done.json()["vehicle"]["id"] == vehicle.id
    assert vehicle.category == "private_four_wheeler"  # from the RC's class


def test_existing_vehicle_is_filled_but_never_overwritten(
    client,
    desk,
    priya,
    customer,
    rc_type,
    make_user,
    make_client,
    make_service,
    db_session,
):
    created = desk.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer.id,
            "registration_number": "GA07AB1234",
            "category": "other",
            "chassis_number": "MA3FJEB1S00999999",  # differs from the RC
        },
    ).json()
    document = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)

    vehicle = db_session.get(Vehicle, created["id"])
    db_session.refresh(vehicle)
    shown = desk.get(f"{API}/vehicles/{vehicle.id}", params={"reveal": "true"}).json()
    assert shown["chassis_number"] == "MA3FJEB1S00999999"  # kept
    assert shown["engine_number"] == ENGINE  # was empty: filled
    assert shown["category"] == "private_four_wheeler"  # "other" counts as empty
    reading = intake(desk, document["id"]).json()
    assert reading["status"] == "needs_check"
    assert reading["pending_changes"] == [
        {"key": "chassis_number", "current": "****9999", "proposed": "****3456"}
    ]

    # A colleague who can see the vehicle but not its numbers can't accept.
    colleague = make_client(make_user("employee"))
    colleague.post(
        f"{API}/jobs",
        json={
            "customer_id": customer.id,
            "service_id": make_service().id,
            "title": "T",
        },
    )
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    assert colleague.post(url, json={"accept_changes": True}).status_code == 403

    accepted = desk.post(url, json={"accept_changes": True})
    assert accepted.status_code == 200, accepted.text
    shown = desk.get(f"{API}/vehicles/{vehicle.id}", params={"reveal": "true"}).json()
    assert shown["chassis_number"] == CHASSIS


def test_keeping_current_values_is_a_choice_too(desk, customer, rc_type, db_session):
    created = desk.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer.id,
            "registration_number": "GA07AB1234",
            "category": "private_four_wheeler",
            "owner_name": "R KUMAR",
        },
    ).json()
    document = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    assert desk.post(url, json={"accept_changes": False}).json()["status"] == "resolved"
    assert desk.get(f"{API}/vehicles/{created['id']}").json()["owner_name"] == "R KUMAR"


def test_registration_of_another_customer_is_a_conflict(
    client, desk, customer, make_customer, rc_type, db_session
):
    seller = make_customer(name="Old Owner")
    sold = client.post(
        f"{API}/vehicles",
        json={
            "customer_id": seller.id,
            "registration_number": "GA07AB1234",
            "category": "private_four_wheeler",
        },
    ).json()
    document = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)

    assert db_session.get(Vehicle, sold["id"]).customer_id == seller.id  # untouched
    reading = intake(client, document["id"]).json()
    assert reading["status"] == "conflict"
    assert reading["vehicle"]["customer_id"] == seller.id
    assert client.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 1
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    assert client.post(url, json={}).status_code == 409

    # The admin moves the vehicle to the buyer, then finishes the reading.
    client.patch(f"{API}/vehicles/{sold['id']}", json={"customer_id": customer.id})
    assert client.post(url, json={}).json()["status"] == "resolved"


def test_reading_runs_once_per_version(desk, customer, rc_type, db_session):
    document = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)
    doc = db_session.get(Document, document["id"])
    first = db_session.scalar(select(VehicleIntake))
    again = vehicle_intake.process(db_session, doc, 1, {}, {}, None)
    assert again.id == first.id
    assert len(db_session.scalars(select(VehicleIntake)).all()) == 1


def test_other_documents_do_not_touch_vehicles(client, desk, customer, db_session):
    aadhaar = client.post(
        f"{API}/document-types",
        json={
            "code": "AADHAAR",
            "name": "Aadhaar",
            "ocr_fields": [{"key": "registration_number", "label": "Odd field"}],
        },
    ).json()
    document = upload_rc(desk, customer.id, aadhaar, rc_scan())
    run_worker(db_session, expect=1)
    assert vehicles_of(db_session, customer.id) == []
    assert intake(desk, document["id"]).status_code == 404


def test_reading_access_follows_the_document(
    client, desk, customer, rc_type, make_user, make_client, db_session
):
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(engine_number=(ENGINE, 0.4))
    )
    run_worker(db_session, expect=1)
    stranger = make_client(make_user("employee"))
    assert intake(stranger, document["id"]).status_code == 404
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    assert stranger.post(url, json={}).status_code == 404
    assert stranger.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 0


# ---------- review fixes ----------


def test_conflict_reveals_nothing_about_the_other_vehicle(
    client, desk, customer, make_customer, rc_type, db_session
):
    seller = make_customer(name="Old Owner")
    theirs = client.post(
        f"{API}/vehicles",
        json={
            "customer_id": seller.id,
            "registration_number": "GA07AB1234",
            "category": "private_four_wheeler",
            "owner_name": "OLD OWNER",
        },
    ).json()
    document = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)

    seen = intake(desk, document["id"]).json()
    assert (seen["status"], seen["vehicle"]) == ("conflict", None)
    assert str(theirs["id"]) not in json.dumps(seen)
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    refused = desk.post(url, json={})
    assert refused.status_code == 409
    assert str(seller.id) not in refused.text
    assert db_session.get(Vehicle, theirs["id"]).owner_name == "OLD OWNER"


def test_a_typed_registration_clash_saves_nothing(
    client, desk, customer, make_customer, rc_type, db_session
):
    client.post(
        f"{API}/vehicles",
        json={
            "customer_id": make_customer().id,
            "registration_number": "KA01CD5678",
            "category": "other",
        },
    )
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(registration_number=("GA07AB1234", 0.2))
    )
    run_worker(db_session, expect=1)
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"
    typo = desk.post(url, json={"values": {"registration_number": "KA01CD5678"}})
    assert typo.status_code == 409
    reading = db_session.scalar(select(VehicleIntake))
    db_session.refresh(reading)
    assert (reading.vehicle_id, reading.status) == (None, "needs_check")
    fixed = desk.post(url, json={"values": {"registration_number": "GA07AB1234"}})
    assert fixed.status_code == 200, fixed.text


def test_confident_readings_survive_a_doubtful_registration(
    desk, customer, rc_type, db_session
):
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(registration_number=("GA07AB1234", 0.3))
    )
    run_worker(db_session, expect=1)
    desk.post(
        f"{API}/documents/{document['id']}/vehicle-intake/resolve",
        json={"values": {"registration_number": "GA 07 AB 1234"}},
    )
    [vehicle] = vehicles_of(db_session, customer.id)
    shown = desk.get(f"{API}/vehicles/{vehicle.id}", params={"reveal": "true"}).json()
    assert (shown["owner_name"], shown["chassis_number"], shown["engine_number"]) == (
        "RAVI KUMAR",
        CHASSIS,
        ENGINE,
    )


def test_resolve_cannot_touch_settled_fields(desk, customer, rc_type, db_session):
    desk.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer.id,
            "registration_number": "GA07AB1234",
            "category": "private_four_wheeler",
            "owner_name": "VERIFIED NAME",
            "engine_number": "OLDENGINE99",
        },
    )
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(engine_number=(ENGINE, 0.3))
    )
    run_worker(db_session, expect=1)
    url = f"{API}/documents/{document['id']}/vehicle-intake/resolve"

    # owner_name did not wait for a person: it can't be changed or cleared here.
    for body in ({"values": {"owner_name": None}}, {"values": {"owner_name": "X"}}):
        assert desk.post(url, json=body).status_code == 422
    # engine waits, but the vehicle already has one: replacing it needs accept.
    assert desk.post(url, json={"values": {"engine_number": ENGINE}}).status_code == 422
    # A null leaves it as it is.
    assert desk.post(url, json={"values": {"engine_number": None}}).status_code == 200
    [vehicle] = vehicles_of(db_session, customer.id)
    shown = desk.get(f"{API}/vehicles/{vehicle.id}", params={"reveal": "true"}).json()
    assert (shown["owner_name"], shown["engine_number"]) == (
        "VERIFIED NAME",
        "OLDENGINE99",
    )


def test_accepted_change_is_stored_cleaned(desk, customer, rc_type, db_session):
    created = desk.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer.id,
            "registration_number": "GA07AB1234",
            "category": "private_four_wheeler",
            "chassis_number": "MA3FJEB1S00999999",
        },
    ).json()
    document = upload_rc(
        desk,
        customer.id,
        rc_type,
        rc_scan(chassis_number=("ma3-fjeb1s 00123456", 0.97)),
    )
    run_worker(db_session, expect=1)
    desk.post(
        f"{API}/documents/{document['id']}/vehicle-intake/resolve",
        json={"accept_changes": True},
    )
    shown = desk.get(
        f"{API}/vehicles/{created['id']}", params={"reveal": "true"}
    ).json()
    assert shown["chassis_number"] == CHASSIS


def test_old_versions_and_archived_documents_leave_the_queue(
    client, desk, customer, rc_type, db_session
):
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(engine_number=(ENGINE, 0.3))
    )
    run_worker(db_session, expect=1)
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 1

    # A clearer copy replaces it: the old reading no longer counts.
    replaced = client.post(  # new versions need review rights (admin here)
        f"{API}/documents/{document['id']}/versions",
        files={"file": ("rc.png", rc_scan(), "image/png")},
        data={"change_reason": "Clearer copy"},
    )
    assert replaced.status_code == 201, replaced.text
    run_worker(db_session, expect=1)
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 0

    other = upload_rc(
        desk,
        customer.id,
        rc_type,
        rc_scan(registration_number=("KA01CD5678", 0.99), engine_number=(ENGINE, 0.3)),
    )
    run_worker(db_session, expect=1)
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 1
    client.post(f"{API}/documents/{other['id']}/archive", json={"reason": "Duplicate"})
    assert desk.get(f"{API}/work-queue").json()["vehicle_readings_to_check"] == 0
    archived = desk.post(
        f"{API}/documents/{other['id']}/vehicle-intake/resolve", json={}
    )
    assert archived.status_code == 422


def test_vehicle_trouble_never_loses_the_ocr_result(
    desk, customer, rc_type, db_session, monkeypatch
):
    from sqlalchemy.exc import IntegrityError

    calls = {"n": 0}
    real_apply = vehicle_intake._apply

    def clashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # someone created the same vehicle a moment ago
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(vehicle_intake, "_apply", clashing)
    first = upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)
    assert intake(desk, first["id"]).json()["status"] == "applied"
    assert calls["n"] == 2

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(vehicle_intake, "_apply", broken)
    second = upload_rc(
        desk, customer.id, rc_type, rc_scan(registration_number=("KA01CD5678", 0.99))
    )
    run_worker(db_session, expect=1)
    reading = intake(desk, second["id"]).json()
    assert (reading["status"], reading["vehicle"]) == ("needs_check", None)
    ocr = desk.get(f"{API}/documents/{second['id']}/ocr").json()
    assert ocr["ocr_status"] == "completed"  # the reading itself was kept


def test_short_readings_are_fully_masked(desk, customer, rc_type, db_session):
    document = upload_rc(
        desk, customer.id, rc_type, rc_scan(chassis_number=("AB12C", 0.99))
    )
    run_worker(db_session, expect=1)
    field = intake(desk, document["id"]).json()["check_fields"][0]
    assert (field["key"], field["read_value"]) == ("chassis_number", "****")


def test_taxis_are_commercial():
    assert vehicle_intake.category_from_class("MOTOR CAB (LMV)").value == "commercial"
    assert vehicle_intake.category_from_class("MAXI CAB").value == "commercial"


def test_worker_actions_are_not_put_on_a_person(desk, customer, rc_type, db_session):
    upload_rc(desk, customer.id, rc_type, rc_scan())
    run_worker(db_session, expect=1)
    created = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "vehicle_created")
    )
    assert created.user_id is None
    assert json.loads(created.details)["source"] == "document"
