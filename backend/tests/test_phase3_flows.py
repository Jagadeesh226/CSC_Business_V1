"""Phase 3 whole-flow tests: the counter story through to money.

Each test is one realistic story driven through the HTTP API from the
employee's side, continuing the Phase 2 document flow: customer, job,
documents read by the real worker, review, then payment, receipt, printed
copy, and the admin's collections report at the end of the day.
"""

import json

import pytest
from flow_helpers import (
    API,
    Counter,
    aadhaar_scan,
    audit,
    ok,
    pdf_text,
    photo,
    run_worker,
    setup_catalogue,
)
from sqlalchemy import select

from app.models import AuditLog, Payment, Receipt


@pytest.fixture
def catalogue(client):
    return setup_catalogue(client)  # PAN Card, fixed fee 107.00


@pytest.fixture
def priya(make_user):
    return make_user("employee", name="Priya Counter")


@pytest.fixture
def desk(priya, make_client, catalogue):
    return Counter(make_client(priya), catalogue["service"])


def documents_ready(desk, db_session, job, name):
    """Aadhaar read by OCR and confirmed, photo checked by hand."""
    aadhaar = desk.upload(job, "AADHAAR", aadhaar_scan(name=name))
    picture = desk.upload(job, "PHOTO", photo())
    run_worker(db_session, expect=1)
    ok(desk.review(aadhaar["id"], name))
    ok(desk.c.post(f"{API}/documents/{picture['id']}/verify"))
    return aadhaar


# ---------- the main story ----------


def test_from_walk_in_to_paid_receipt(client, desk, priya, db_session):
    # Morning: a new customer for a PAN card; the RTO-style department fee
    # is added on top of the 107.00 service charge.
    customer = desk.new_customer("Ravi Kumar", "+91 98765 43210")["customer"]
    job = desk.open_job(customer["id"])
    ok(
        desk.c.post(
            f"{API}/jobs/{job['id']}/fees",
            json={"fee_type": "department", "description": "NSDL fee", "amount": "50"},
        ),
        201,
    )
    ok(desk.move(job["id"], "in_progress"))
    documents_ready(desk, db_session, job, "Ravi Kumar")
    assert desk.job(job["id"])["payment_summary"] == {
        "fees_total": "157.00",
        "paid": "0.00",
        "balance": "157.00",
        "state": "unpaid",
    }

    # He pays an advance by UPI; the receipt is printed and handed over.
    advance = ok(
        desk.take_payment(job, "100.00", "upi", reference_number="UTR4455"), 201
    )
    first_receipt = advance["receipt"]
    original = pdf_text(desk.print_receipt(first_receipt["id"]))
    for expected in (
        first_receipt["receipt_number"],
        job["job_number"],
        "Ravi Kumar",
        "******3210",
        "Rs. 157.00",
        "Rs. 100.00",
        "Rs. 57.00",  # still to pay
        "UPI (ref. UTR4455)",
        "Priya Counter",
    ):
        assert expected in original, expected
    assert "DUPLICATE" not in original
    assert "1234" not in original  # no Aadhaar number on a receipt

    # Money has been received: the employee can no longer add fees.
    late_fee = desk.c.post(
        f"{API}/jobs/{job['id']}/fees",
        json={"fee_type": "third_party", "description": "Courier", "amount": "20"},
    )
    assert late_fee.status_code == 422
    assert "payment has been received" in late_fee.json()["detail"]

    # Once documents are complete the money due shows in the queue.
    summary = desk.queue()
    assert (summary["jobs_ready_to_submit"], summary["jobs_with_balance_due"]) == (1, 1)
    ok(desk.move(job["id"], "submitted"))
    ok(desk.move(job["id"], "completed"))  # allowed with a balance due
    assert desk.queue()["jobs_with_balance_due"] == 1  # still to collect

    # Pick-up: he offers 60 for the 57 due; only the balance is taken.
    too_much = desk.take_payment(job, "60.00")
    assert too_much.status_code == 422
    assert "Rs 57.00" in too_much.json()["detail"]
    rest = ok(desk.take_payment(job, "57.00"), 201)
    assert rest["summary"]["state"] == "paid"
    final = pdf_text(desk.print_receipt(rest["receipt"]["id"]))
    assert "Rs. 0.00" in final and "Rupees Fifty Seven Only" in final
    assert desk.queue()["jobs_with_balance_due"] == 0

    # He lost the first slip: a reprint says DUPLICATE COPY.
    assert "DUPLICATE COPY" in pdf_text(desk.print_receipt(first_receipt["id"]))

    # Both receipts are found by his name, newest first.
    found = ok(desk.c.get(f"{API}/receipts", params={"search": "Ravi"}))
    assert [r["receipt_number"] for r in found["items"]] == [
        rest["receipt"]["receipt_number"],
        first_receipt["receipt_number"],
    ]

    # Evening: the admin's collections report.
    report = ok(client.get(f"{API}/reports/collections"))
    assert (report["label"], report["total"], report["count"]) == (
        "Collections",
        "157.00",
        2,
    )
    assert report["by_method"] == [
        {"method": "cash", "count": 1, "total": "57.00"},
        {"method": "upi", "count": 1, "total": "100.00"},
    ]
    assert report["by_receiver"] == [
        {
            "received_by": {"id": priya.id, "name": "Priya Counter"},
            "count": 2,
            "total": "157.00",
        }
    ]

    # The trail: money and prints recorded, nothing sensitive in it.
    actions = audit(db_session, "job", job["id"])
    assert actions.count("payment_recorded") == 2
    prints = db_session.scalars(
        select(AuditLog.details).where(AuditLog.action == "receipt_printed")
    ).all()
    assert [json.loads(d)["copy"] for d in prints] == [1, 1, 2]
    everything = json.dumps(db_session.scalars(select(AuditLog.details)).all())
    for secret in ("9876543210", "1234 5678 9012", "UTR4455"):
        assert secret not in everything
    snapshots = json.dumps(db_session.scalars(select(Receipt.snapshot)).all())
    assert "9876543210" not in snapshots and "1234" not in snapshots


# ---------- a shaky connection ----------


def test_retry_at_the_counter_never_charges_twice(desk, db_session):
    customer = desk.new_customer("Meena", "9000011111")["customer"]
    job = desk.open_job(customer["id"])

    # The app sends the payment, the network drops, the app resends it.
    first = desk.take_payment(job, "107.00", key="counter-tablet-7f3a9c")
    again = desk.take_payment(job, "107.00", key="counter-tablet-7f3a9c")

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["replayed"] is True
    assert again.json()["receipt"] == first.json()["receipt"]
    assert db_session.query(Payment).filter_by(job_id=job["id"]).count() == 1
    assert desk.job(job["id"])["payment_summary"]["state"] == "paid"
    # A genuinely new payment with the old key is refused, not merged.
    assert (
        desk.take_payment(job, "5.00", key="counter-tablet-7f3a9c").status_code == 409
    )


# ---------- a mistake corrected ----------


def test_wrong_amount_is_voided_and_reentered(client, desk, db_session):
    customer = desk.new_customer("Kiran", "9000022222")["customer"]
    job = desk.open_job(customer["id"])

    # 100 typed instead of 10; the employee cannot void it, an admin can.
    wrong = ok(desk.take_payment(job, "100.00"), 201)
    assert (
        desk.c.post(
            f"{API}/payments/{wrong['id']}/void", json={"reason": "Typo"}
        ).status_code
        == 403
    )
    ok(
        client.post(
            f"{API}/payments/{wrong['id']}/void",
            json={"reason": "Typed 100 instead of 10"},
        )
    )
    right = ok(desk.take_payment(job, "10.00"), 201)

    # The cancelled receipt keeps its number and prints CANCELLED; the
    # correct one gets the next number.
    wrong_no = wrong["receipt"]["receipt_number"]
    right_no = right["receipt"]["receipt_number"]
    assert int(right_no[-6:]) == int(wrong_no[-6:]) + 1
    cancelled = pdf_text(desk.print_receipt(wrong["receipt"]["id"]))
    assert "CANCELLED" in cancelled and "Typed 100 instead of 10" in cancelled
    assert desk.job(job["id"])["payment_summary"]["paid"] == "10.00"

    report = ok(client.get(f"{API}/reports/collections"))
    assert report["total"] == "10.00"
    assert report["voided"] == {"count": 1, "total": "100.00"}
    assert report["voids_in_period"] == {"count": 1, "total": "100.00"}
    cancelled_list = ok(client.get(f"{API}/receipts", params={"status": "cancelled"}))
    assert [r["receipt_number"] for r in cancelled_list["items"]] == [wrong_no]


# ---------- fees corrected after payment ----------


def test_fee_lowered_after_payment_shows_as_overpaid(client, desk):
    customer = desk.new_customer("Asha", "9000033333")["customer"]
    job = desk.open_job(customer["id"])
    ok(desk.take_payment(job, "107.00"), 201)

    # The admin finds the service charge should have been 100.
    fee = ok(client.get(f"{API}/jobs/{job['id']}/fees"))["items"][0]
    ok(client.patch(f"{API}/jobs/{job['id']}/fees/{fee['id']}", json={"amount": "100"}))

    summary = desk.job(job["id"])["payment_summary"]
    assert (summary["balance"], summary["state"]) == ("-7.00", "overpaid")
    assert ok(client.get(f"{API}/work-queue"))["jobs_overpaid"] == 1
    assert desk.queue()["jobs_overpaid"] is None  # admins settle it
    assert desk.take_payment(job, "1.00").status_code == 422  # nothing due
    listed = ok(client.get(f"{API}/jobs", params={"payment": "overpaid"}))
    assert [j["id"] for j in listed["items"]] == [job["id"]]


# ---------- handover ----------


def test_receipts_follow_the_job_when_it_is_handed_over(
    client, desk, priya, make_user, make_client, catalogue
):
    customer = desk.new_customer("Sameer", "9000044444")["customer"]
    job = desk.open_job(customer["id"])
    paid = ok(desk.take_payment(job, "50.00"), 201)
    receipt_id = paid["receipt"]["id"]
    pdf_text(desk.print_receipt(receipt_id))

    colleague_user = make_user("employee", name="Sameer Desk")
    colleague = Counter(make_client(colleague_user), catalogue["service"])
    assert colleague.print_receipt(receipt_id).status_code == 404
    ok(
        client.post(
            f"{API}/jobs/{job['id']}/assignments", json={"user_id": colleague_user.id}
        ),
        201,
    )
    ok(client.delete(f"{API}/jobs/{job['id']}/assignments/{priya.id}"))

    assert desk.print_receipt(receipt_id).status_code == 404
    assert ok(desk.c.get(f"{API}/receipts"))["total"] == 0
    # The colleague collects the rest and can reprint the first slip.
    assert "DUPLICATE COPY" in pdf_text(colleague.print_receipt(receipt_id))
    rest = ok(colleague.take_payment(job, "57.00"), 201)
    assert rest["receipt"]["issued_by"]["name"] == "Sameer Desk"
    assert rest["summary"]["state"] == "paid"
