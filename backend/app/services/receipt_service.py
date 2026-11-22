"""Reading, searching and printing receipts.

Access: admins see every receipt; employees see receipts of jobs they are
assigned to (open or closed); anything else is 404, like jobs. Printing
counts copies under the receipt's row lock: the first print is the
original, every later one says DUPLICATE COPY. Each print is audited.
"""

import math

from sqlalchemy.orm import Session

from app.core.money_words import rupees_in_words
from app.models import Receipt, User
from app.repositories import job_repository
from app.repositories import receipt_repository as repo
from app.repositories.document_repository import users_by_id
from app.schemas.job import AssigneeRef
from app.schemas.receipt import (
    ReceiptDetail,
    ReceiptFilters,
    ReceiptListItem,
    ReceiptListResponse,
    ReceiptSnapshot,
    ReceiptStatus,
)
from app.services import audit_service, job_service, receipt_pdf
from app.services.errors import NotFoundError

RECEIPT_NOT_FOUND = "Receipt not found."


def _status(receipt: Receipt) -> ReceiptStatus:
    return ReceiptStatus.CANCELLED if receipt.cancelled_at else ReceiptStatus.ISSUED


def _printed_name(receipt: Receipt) -> str:
    customer = (receipt.snapshot or {}).get("customer") or {}
    return str(customer.get("name") or "")


def _employee_id(viewer: User) -> int | None:
    return None if job_service.is_admin(viewer) else viewer.id


def _visible(db: Session, receipt: Receipt | None, viewer: User) -> Receipt:
    if receipt is None:
        raise NotFoundError(RECEIPT_NOT_FOUND)
    if not job_service.is_admin(viewer) and not job_repository.is_actively_assigned(
        db, receipt.job_id, viewer.id
    ):
        raise NotFoundError(RECEIPT_NOT_FOUND)
    return receipt


def _detail(db: Session, receipt: Receipt) -> ReceiptDetail:
    issuer = users_by_id(db, {receipt.issued_by}).get(receipt.issued_by)
    snapshot = ReceiptSnapshot.model_validate(receipt.snapshot)
    return ReceiptDetail(
        id=receipt.id,
        receipt_number=receipt.receipt_number,
        status=_status(receipt),
        payment_id=receipt.payment_id,
        job_id=receipt.job_id,
        customer_id=receipt.customer_id,
        issued_at=receipt.issued_at,
        issued_by=AssigneeRef.model_validate(issuer) if issuer else None,
        cancelled_at=receipt.cancelled_at,
        cancel_reason=receipt.cancel_reason,
        print_count=receipt.print_count,
        snapshot=snapshot,
        amount_in_words=rupees_in_words(snapshot.amount),
    )


def get_receipt(db: Session, receipt_id: int, viewer: User) -> ReceiptDetail:
    return _detail(db, _visible(db, repo.get(db, receipt_id), viewer))


def search(
    db: Session, filters: ReceiptFilters, viewer: User, *, page: int, page_size: int
) -> ReceiptListResponse:
    if filters.search is not None:
        filters.search = filters.search.strip() or None
    rows, total = repo.search(
        db,
        filters,
        employee_id=_employee_id(viewer),
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return ReceiptListResponse(
        items=[
            ReceiptListItem(
                id=receipt.id,
                receipt_number=receipt.receipt_number,
                status=_status(receipt),
                issued_at=receipt.issued_at,
                job_id=receipt.job_id,
                job_number=job.job_number,
                customer_id=receipt.customer_id,
                customer_name=_printed_name(receipt),
                amount=payment.amount,
                method=payment.payment_method,
            )
            for receipt, payment, job in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def print_receipt(db: Session, receipt_id: int, viewer: User) -> tuple[str, bytes]:
    """Count the copy, audit it, and return (filename, PDF bytes)."""
    _visible(db, repo.get(db, receipt_id), viewer)
    receipt = repo.lock(db, receipt_id)
    receipt.print_count += 1
    copy = receipt.print_count
    audit_service.record(
        db,
        action="receipt_printed",
        entity_type="receipt",
        entity_id=receipt.id,
        user_id=viewer.id,
        details={
            "receipt_number": receipt.receipt_number,
            "copy": copy,
            "duplicate": copy > 1,
        },
    )
    snapshot = ReceiptSnapshot.model_validate(receipt.snapshot)
    pdf = receipt_pdf.render(
        receipt_number=receipt.receipt_number,
        issued_at=receipt.issued_at,
        snapshot=snapshot,
        duplicate=copy > 1,
        cancelled_reason=receipt.cancel_reason if receipt.cancelled_at else None,
    )
    # Committed only once the PDF was drawn, so a failed print is not counted.
    db.commit()
    return f"{receipt.receipt_number}.pdf", pdf
