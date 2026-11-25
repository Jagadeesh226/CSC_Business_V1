"""Phase 4.3: vehicles Excel export."""

import io
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import AuditLog
from app.repositories.readiness_repository import business_today
from app.services import export_service

API = "/api/v1"
URL = f"{API}/reports/vehicles.xlsx"
CHASSIS = "MA3FJEB1S00123456"


@pytest.fixture
def today(db_session):
    return business_today(db_session)


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="+91 98765 43210")


def add_vehicle(client, customer_id, registration="GA 07 AB 1234", **extra):
    response = client.post(
        f"{API}/vehicles",
        json={
            "customer_id": customer_id,
            "registration_number": registration,
            "category": "private_four_wheeler",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def set_date(client, vehicle_id, kind, when):
    response = client.put(
        f"{API}/vehicles/{vehicle_id}/expiries/{kind}",
        json={"valid_until": when.isoformat()},
    )
    assert response.status_code == 200, response.text


def sheet(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == export_service.XLSX
    ws = load_workbook(io.BytesIO(response.content)).active
    headers = [c.value for c in ws[1]]
    rows = [
        dict(zip(headers, (c for c in row), strict=False))
        for row in ws.iter_rows(min_row=2)
        if row[0].value and not str(row[0].value).startswith("Exported")
    ]
    return headers, rows, ws


def test_formula_cells_are_neutralised():
    assert export_service.safe_text('=HYPERLINK("x")') == '\'=HYPERLINK("x")'
    for start in ("+", "-", "@", "\t", "\r"):
        assert export_service.safe_text(start + "1") == "'" + start + "1"
    assert export_service.safe_text("Ravi") == "Ravi"
    assert export_service.safe_text(42) == 42


def test_export_rows(client, customer, today):
    v = add_vehicle(
        client, customer.id, chassis_number=CHASSIS, owner_name="RAVI KUMAR"
    )
    set_date(client, v["id"], "insurance", today + timedelta(days=20))
    set_date(client, v["id"], "pucc", today - timedelta(days=3))
    set_date(client, v["id"], "permit", today + timedelta(days=200))

    response = client.get(URL)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="vehicles-{today.strftime("%Y%m%d")}.xlsx"'
    )
    headers, [row], _ = sheet(response)
    assert headers[:9] == [
        "Registration no.",
        "Category",
        "Customer ID",
        "Customer name",
        "Mobile",
        "Owner name (RC)",
        "Chassis no.",
        "Engine no.",
        "Responsible",
    ]
    assert headers[-3:] == ["Challan status", "Active", "WhatsApp"]
    assert row["Registration no."].value == "GA 07 AB 1234"
    assert row["Category"].value == "Private four-wheeler"
    assert row["Customer name"].value == "Ravi Kumar"
    assert row["Mobile"].value == "********3210"  # masked (12 digits with +91)
    assert row["Chassis no."].value == "****3456"  # masked
    assert row["Insurance valid until"].value.date() == today + timedelta(days=20)
    assert row["Insurance days left"].value == 20
    assert row["PUCC days left"].value == -3
    assert row["Fitness valid until"].value is None
    assert row["Challan status"].value == "Not checked"

    wa = row["WhatsApp"]
    assert wa.value == "Open WhatsApp"
    target = urlparse(wa.hyperlink.target)
    assert (target.netloc, target.path) == ("wa.me", "/919876543210")
    text = parse_qs(target.query)["text"][0]
    # Due items only (expired or within 30 days), not the permit in 200 days.
    assert "GA 07 AB 1234" in text and "PUCC expired on" in text
    assert "Insurance expires on" in text and "Permit" not in text


def test_nothing_due_links_the_next_expiry(client, customer, today):
    v = add_vehicle(client, customer.id)
    set_date(client, v["id"], "insurance", today + timedelta(days=90))
    _, [row], _ = sheet(client.get(URL))
    text = parse_qs(urlparse(row["WhatsApp"].hyperlink.target).query)["text"][0]
    assert "Insurance expires on" in text and "(90 days left)" in text


def test_no_link_without_a_mobile_or_dates(client, make_customer, today):
    no_phone = make_customer(name="No Phone", phone=None)
    v1 = add_vehicle(client, no_phone.id, registration="GA01A0001")
    set_date(client, v1["id"], "insurance", today + timedelta(days=5))
    add_vehicle(
        client,
        make_customer(name="No Dates", phone="9876500000").id,
        registration="GA01A0002",
    )
    _, rows, _ = sheet(client.get(URL))
    assert [r["WhatsApp"].value for r in rows] == [None, None]
    assert all(r["WhatsApp"].hyperlink is None for r in rows)


def test_formula_injection_is_blocked(client, make_customer):
    evil = make_customer(name='=HYPERLINK("http://evil.example","Click")', phone=None)
    add_vehicle(client, evil.id, owner_name="+cmd|' /C calc'!A0")
    _, [row], ws = sheet(client.get(URL))
    name = row["Customer name"]
    assert name.value == '\'=HYPERLINK("http://evil.example","Click")'
    assert name.data_type == "s"
    assert row["Owner name (RC)"].value.startswith("'+")
    # No cell anywhere is a formula.
    assert all(c.data_type != "f" for r in ws.iter_rows() for c in r)


def test_employees_export_only_what_they_see(
    client, customer, make_customer, make_user, make_client
):
    desk = make_client(make_user("employee"))
    mine = add_vehicle(desk, customer.id, registration="GA01A0001")
    add_vehicle(client, make_customer().id, registration="GA01A0002")
    _, rows, _ = sheet(desk.get(URL))
    assert [r["Registration no."].value for r in rows] == [mine["registration_number"]]
    _, rows, _ = sheet(client.get(URL))
    assert len(rows) == 2


def test_filters(client, customer, make_customer, today):
    soon = add_vehicle(client, customer.id, registration="GA01A0001")
    set_date(client, soon["id"], "tax", today + timedelta(days=10))
    later = add_vehicle(client, customer.id, registration="GA01A0002")
    set_date(client, later["id"], "tax", today + timedelta(days=100))
    gone = add_vehicle(client, make_customer().id, registration="GA01A0003")
    client.patch(f"{API}/vehicles/{gone['id']}", json={"is_active": False})

    def regs(**params):
        _, rows, _ = sheet(client.get(URL, params=params))
        return [r["Registration no."].value for r in rows]

    assert regs() == ["GA01A0001", "GA01A0002"]
    assert regs(due_within_days=30) == ["GA01A0001"]
    assert regs(include_inactive="true") == ["GA01A0001", "GA01A0002", "GA01A0003"]
    assert regs(customer_id=customer.id) == ["GA01A0001", "GA01A0002"]
    assert client.get(URL, params={"due_within_days": 400}).status_code == 422


def test_export_is_audited(client, admin_user, customer, db_session):
    add_vehicle(client, customer.id)
    client.get(URL, params={"due_within_days": 30})
    entry = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "vehicles_exported")
    )
    assert entry.user_id == admin_user.id
    assert json.loads(entry.details) == {
        "rows": 0,
        "due_within_days": 30,
        "include_inactive": False,
        "customer_id": None,
    }


def test_too_many_rows_asks_for_filters(client, customer, monkeypatch):
    monkeypatch.setattr(export_service, "MAX_ROWS", 1)
    add_vehicle(client, customer.id, registration="GA01A0001")
    add_vehicle(client, customer.id, registration="GA01A0002")
    response = client.get(URL)
    assert response.status_code == 422
    assert "narrow the export" in response.json()["detail"]


def test_export_needs_sign_in(anon_client):
    assert anon_client.get(URL).status_code == 401
