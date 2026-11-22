"""Phase 3.1: receipt details, search and printable PDF."""

import io
import itertools
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.core.config import get_settings
from app.core.money_words import number_in_words, rupees_in_words
from app.models import AuditLog, Receipt
from app.repositories.readiness_repository import business_today

API = "/api/v1"
_keys = itertools.count(1)


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Ravi Kumar", phone="9876543210")


@pytest.fixture
def make_job(client, customer, make_service):
    service = make_service(name="RC Renewal")  # fixed 100.00

    def _make(title="RC renewal", who=None):
        job = (
            (who or client)
            .post(
                f"{API}/jobs",
                json={
                    "customer_id": customer.id,
                    "service_id": service.id,
                    "title": title,
                },
            )
            .json()
        )
        client.post(
            f"{API}/jobs/{job['id']}/fees",
            json={"fee_type": "department", "description": "RTO fee", "amount": "57"},
        )
        return job

    return _make


def pay(client, job_id, amount="100.00", method="cash", **extra):
    response = client.post(
        f"{API}/jobs/{job_id}/payments",
        json={"amount": amount, "method": method, **extra},
        headers={"Idempotency-Key": f"receipt-test-{next(_keys):06d}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def pdf_text(response) -> str:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() for page in reader.pages)


# ---------- amount in words ----------


@pytest.mark.parametrize(
    "amount, words",
    [
        ("0", "Rupees Zero Only"),
        ("7", "Rupees Seven Only"),
        ("157.00", "Rupees One Hundred Fifty Seven Only"),
        ("1250.50", "Rupees One Thousand Two Hundred Fifty and Paise Fifty Only"),
        ("100000", "Rupees One Lakh Only"),
        (
            "2345678.05",
            (
                "Rupees Twenty Three Lakh Forty Five Thousand Six Hundred "
                "Seventy Eight and Paise Five Only"
            ),
        ),
        ("10000000", "Rupees One Crore Only"),
        (
            "999999999.99",
            (
                "Rupees Ninety Nine Crore Ninety Nine Lakh Ninety Nine "
                "Thousand Nine Hundred Ninety Nine and Paise Ninety Nine Only"
            ),
        ),
    ],
)
def test_rupees_in_words(amount, words):
    assert rupees_in_words(Decimal(amount)) == words


def test_number_in_words_edges():
    assert number_in_words(19) == "Nineteen"
    assert number_in_words(20) == "Twenty"
    assert number_in_words(101) == "One Hundred One"
    assert number_in_words(100_001) == "One Lakh One"


# ---------- detail ----------


def test_receipt_detail(client, admin_user, make_job):
    job = make_job()
    payment = pay(client, job["id"], "100.00", method="upi", reference_number="UTR42")
    receipt_id = payment["receipt"]["id"]

    body = client.get(f"{API}/receipts/{receipt_id}").json()

    assert body["receipt_number"] == payment["receipt"]["receipt_number"]
    assert body["status"] == "issued"
    assert (body["payment_id"], body["job_id"]) == (payment["id"], job["id"])
    assert body["issued_by"]["id"] == admin_user.id
    assert body["print_count"] == 0
    assert body["amount_in_words"] == "Rupees One Hundred Only"
    snapshot = body["snapshot"]
    assert snapshot["customer"] == {
        "id": job["customer"]["id"],
        "name": "Ravi Kumar",
        "phone": "******3210",
    }
    assert (snapshot["fees_total"], snapshot["amount"], snapshot["balance_after"]) == (
        "157.00",
        "100.00",
        "57.00",
    )
    assert (snapshot["method"], snapshot["reference_number"]) == ("upi", "UTR42")
    assert client.get(f"{API}/receipts/999999").status_code == 404


# ---------- PDF ----------


def test_first_print_is_the_original_then_duplicates(client, make_job, db_session):
    job = make_job()
    first_payment = pay(client, job["id"], "100.00")
    second_payment = pay(
        client, job["id"], "57.00", method="card", reference_number="AUTH7"
    )
    receipt_id = second_payment["receipt"]["id"]
    url = f"{API}/receipts/{receipt_id}/pdf"

    original = client.get(url)
    text = pdf_text(original)
    assert original.headers["cache-control"] == "no-store"
    assert (
        original.headers["content-disposition"]
        == f'inline; filename="{second_payment["receipt"]["receipt_number"]}.pdf"'
    )
    for expected in (
        "PAYMENT RECEIPT",
        get_settings().centre_name,
        second_payment["receipt"]["receipt_number"],
        job["job_number"],
        "RC Renewal",
        "Ravi Kumar",
        "******3210",
        "Service charge: RC Renewal",
        "Department fee: RTO fee",
        "Rs. 157.00",  # total fees
        "Rs. 100.00",  # paid earlier
        "Rs. 57.00",  # received now
        "Rs. 0.00",  # balance
        "Rupees Fifty Seven Only",
        "Card (ref. AUTH7)",
        "Admin Test",
        "not a tax invoice",
    ):
        assert expected in text, expected
    assert "DUPLICATE" not in text
    assert "9876543210" not in text

    again = pdf_text(client.get(url))
    assert "DUPLICATE COPY" in again
    assert client.get(f"{API}/receipts/{receipt_id}").json()["print_count"] == 2
    entries = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "receipt_printed")
        .order_by(AuditLog.id)
    ).all()
    assert [json.loads(e.details) for e in entries] == [
        {
            "receipt_number": second_payment["receipt"]["receipt_number"],
            "copy": 1,
            "duplicate": False,
        },
        {
            "receipt_number": second_payment["receipt"]["receipt_number"],
            "copy": 2,
            "duplicate": True,
        },
    ]
    assert {e.entity_type for e in entries} == {"receipt"}
    assert first_payment["receipt"]["id"] != receipt_id


def test_reprint_shows_the_receipt_as_issued(client, make_job, customer):
    job = make_job()
    payment = pay(client, job["id"], "100.00")
    client.patch(f"{API}/customers/{customer.id}", json={"name": "Ravi K Kumar"})
    fees = client.get(f"{API}/jobs/{job['id']}/fees").json()["items"]
    client.patch(
        f"{API}/jobs/{job['id']}/fees/{fees[1]['id']}", json={"amount": "60.00"}
    )
    text = pdf_text(client.get(f"{API}/receipts/{payment['receipt']['id']}/pdf"))
    assert "Ravi Kumar" in text and "Ravi K Kumar" not in text
    assert "Rs. 157.00" in text


def test_cancelled_receipt_prints_cancelled(client, make_job):
    job = make_job()
    payment = pay(client, job["id"])
    client.post(
        f"{API}/payments/{payment['id']}/void", json={"reason": "Entered twice"}
    )
    text = pdf_text(client.get(f"{API}/receipts/{payment['receipt']['id']}/pdf"))
    assert "CANCELLED" in text
    assert "Reason: Entered twice" in text
    detail = client.get(f"{API}/receipts/{payment['receipt']['id']}").json()
    assert (detail["status"], detail["cancel_reason"]) == ("cancelled", "Entered twice")


def test_names_outside_the_builtin_font_still_print(
    client, make_customer, make_service
):
    hindi = make_customer(name="रवि Kumar", phone="9000000001")
    job = client.post(
        f"{API}/jobs",
        json={"customer_id": hindi.id, "service_id": make_service().id, "title": "T"},
    ).json()
    payment = pay(client, job["id"], "100.00")
    text = pdf_text(client.get(f"{API}/receipts/{payment['receipt']['id']}/pdf"))
    assert "Kumar" in text  # unprintable letters become "?", the print works


def test_failed_render_is_not_counted(client, make_job, db_session, monkeypatch):
    from app.services import receipt_pdf

    job = make_job()
    payment = pay(client, job["id"])

    def broken(**_):
        raise RuntimeError("printer on fire")

    monkeypatch.setattr(receipt_pdf, "render", broken)
    with pytest.raises(RuntimeError):
        client.get(f"{API}/receipts/{payment['receipt']['id']}/pdf")
    db_session.rollback()
    receipt = db_session.get(Receipt, payment["receipt"]["id"])
    db_session.refresh(receipt)
    assert receipt.print_count == 0


# ---------- access ----------


def test_employees_see_only_receipts_of_their_jobs(
    client, make_job, make_user, make_client
):
    mine_user, other_user = make_user("employee"), make_user("employee")
    mine, other = make_client(mine_user), make_client(other_user)
    my_job = make_job("mine", who=mine)
    their_job = make_job("theirs", who=other)
    my_receipt = pay(mine, my_job["id"])["receipt"]
    their_receipt = pay(other, their_job["id"])["receipt"]

    listed = mine.get(f"{API}/receipts").json()
    assert [r["id"] for r in listed["items"]] == [my_receipt["id"]]
    for path in ("", "/pdf"):
        assert (
            mine.get(f"{API}/receipts/{their_receipt['id']}{path}").status_code == 404
        )
    assert mine.get(f"{API}/receipts/{my_receipt['id']}/pdf").status_code == 200
    # Admins see both.
    assert client.get(f"{API}/receipts").json()["total"] == 2

    # Taken off the job: the receipt is no longer theirs to see.
    client.delete(f"{API}/jobs/{my_job['id']}/assignments/{mine_user.id}")
    assert mine.get(f"{API}/receipts/{my_receipt['id']}").status_code == 404
    assert mine.get(f"{API}/receipts").json()["total"] == 0


def test_receipts_need_sign_in(anon_client):
    assert anon_client.get(f"{API}/receipts").status_code == 401
    assert anon_client.get(f"{API}/receipts/1/pdf").status_code == 401


# ---------- search ----------


def test_receipt_search(client, make_job, make_customer, make_service, db_session):
    job = make_job("first")
    cash = pay(client, job["id"], "100.00")
    upi = pay(client, job["id"], "57.00", method="upi", reference_number="U1")
    other_customer = make_customer(name="Meena Devi", phone="9000000002")
    other_job = client.post(
        f"{API}/jobs",
        json={
            "customer_id": other_customer.id,
            "service_id": make_service().id,
            "title": "Other",
        },
    ).json()
    other = pay(client, other_job["id"], "10.00")
    client.post(f"{API}/payments/{other['id']}/void", json={"reason": "Mistake"})

    def ids(**params):
        response = client.get(f"{API}/receipts", params=params)
        assert response.status_code == 200, response.text
        return [r["id"] for r in response.json()["items"]]

    newest_first = [other["receipt"]["id"], upi["receipt"]["id"], cash["receipt"]["id"]]
    assert ids() == newest_first
    assert ids(search=cash["receipt"]["receipt_number"]) == [cash["receipt"]["id"]]
    assert ids(search=other_job["job_number"]) == [other["receipt"]["id"]]
    assert ids(search="meena") == [other["receipt"]["id"]]
    # "_" and "%" are literal: "RCP_" must not match "RCP-".
    assert ids(search="RCP_") == []
    assert ids(search="RCP%2026") == []
    assert ids(customer_id=other_customer.id) == [other["receipt"]["id"]]
    assert ids(job_id=job["id"]) == newest_first[1:]
    assert ids(method="upi") == [upi["receipt"]["id"]]
    assert ids(status="cancelled") == [other["receipt"]["id"]]
    assert ids(status="issued") == newest_first[1:]
    today = business_today(db_session)
    assert ids(date_from=today.isoformat(), date_to=today.isoformat()) == newest_first
    assert ids(date_from=(today + timedelta(days=1)).isoformat()) == []
    assert ids(date_to=(today - timedelta(days=1)).isoformat()) == []

    item = client.get(f"{API}/receipts", params={"method": "upi"}).json()["items"][0]
    assert item == {
        "id": upi["receipt"]["id"],
        "receipt_number": upi["receipt"]["receipt_number"],
        "status": "issued",
        "issued_at": item["issued_at"],
        "job_id": job["id"],
        "job_number": job["job_number"],
        "customer_id": job["customer"]["id"],
        "customer_name": "Ravi Kumar",
        "amount": "57.00",
        "method": "upi",
    }
    page = client.get(f"{API}/receipts", params={"page_size": 2, "page": 2}).json()
    assert (page["total"], page["total_pages"], len(page["items"])) == (3, 2, 1)
    assert client.get(f"{API}/receipts", params={"method": "gold"}).status_code == 422
