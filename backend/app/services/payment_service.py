"""Payments and receipts.

Rules (owner decisions, Phase 3):
- The employee assigned to the job, or an admin, records a payment on any
  job that is not cancelled (completed jobs included: customers often pay on
  pick-up). Each payment gets its receipt in the same transaction.
- Part-payments are fine; an amount above the outstanding balance is
  refused, so there are no advances.
- Recording is idempotent: the client sends an Idempotency-Key. A repeat
  with the same key and the same payment returns the first one (nothing new
  is recorded); the same key with a different payment is refused (409).
  The database enforces the key as unique, so even racing requests cannot
  record twice.
- Only an admin voids a payment, with a reason; its receipt is marked
  cancelled and keeps its number. Nothing is ever deleted.

Lock order: the job row first (as fees and status changes do, so "is
anything received yet?" and the balance cannot change underneath), then the
payment.
"""

import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.masking import mask_phone
from app.models import Customer, Job, Payment, Receipt, User
from app.repositories import fee_repository, job_repository
from app.repositories import payment_repository as repo
from app.repositories.document_repository import users_by_id
from app.schemas.job import AssigneeRef, JobStatus
from app.schemas.payment import (
    PaymentCreate,
    PaymentListResponse,
    PaymentRead,
    PaymentRecorded,
    PaymentStatus,
    PaymentWithSummary,
    ReceiptRead,
)
from app.services import audit_service, job_service
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError
from app.services.payment_summary import summarize

log = logging.getLogger("app.payments")

PAYMENT_NOT_FOUND = "Payment not found."
RECEIPT_SEQUENCE = "receipt_number"
RECEIPT_PREFIX = "RCP"
KEY_REUSED = (
    "This Idempotency-Key was already used for a different payment request. "
    "Use a new key for each new payment."
)


def format_receipt_number(year: int, value: int) -> str:
    return f"{RECEIPT_PREFIX}-{year}-{value:06d}"


def request_hash(job_id: int, data: PaymentCreate, actor_id: int) -> str:
    """Fingerprint of what was asked, to tell a retry from a reused key."""
    canonical = json.dumps(
        {
            "job_id": job_id,
            "actor_id": actor_id,
            "amount": f"{data.amount:.2f}",
            "method": data.method.value,
            "reference_number": data.reference_number,
            "notes": data.notes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


# ---------- reading ----------


def _ref(users: dict[int, User], user_id: int | None) -> AssigneeRef | None:
    user = users.get(user_id) if user_id else None
    return AssigneeRef.model_validate(user) if user else None


def _reads(db: Session, payments: list[Payment]) -> list[PaymentRead]:
    receipts = repo.receipts_for(db, [p.id for p in payments])
    user_ids = {p.recorded_by for p in payments} | {p.voided_by for p in payments}
    user_ids |= {r.issued_by for r in receipts.values()}
    users = users_by_id(db, user_ids)
    out = []
    for p in payments:
        receipt = receipts.get(p.id)
        out.append(
            PaymentRead(
                id=p.id,
                job_id=p.job_id,
                amount=p.amount,
                method=p.payment_method,
                reference_number=p.reference_number,
                notes=p.notes,
                status=p.status,
                received_at=p.received_at,
                recorded_by=_ref(users, p.recorded_by),
                voided_at=p.voided_at,
                voided_by=_ref(users, p.voided_by),
                void_reason=p.void_reason,
                receipt=ReceiptRead(
                    id=receipt.id,
                    receipt_number=receipt.receipt_number,
                    issued_at=receipt.issued_at,
                    issued_by=_ref(users, receipt.issued_by),
                    cancelled=receipt.cancelled_at is not None,
                    cancelled_at=receipt.cancelled_at,
                    cancel_reason=receipt.cancel_reason,
                )
                if receipt
                else None,
            )
        )
    return out


def _summary(db: Session, job_id: int):
    fees, paid = repo.totals_for_jobs(db, [job_id])[job_id]
    return summarize(fees, paid)


def list_payments(db: Session, job_id: int, viewer: User) -> PaymentListResponse:
    if job_repository.get_job(db, job_id) is None:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    job_service.ensure_job_access(db, job_id, viewer)
    return PaymentListResponse(
        items=_reads(db, repo.list_for_job(db, job_id)),
        summary=_summary(db, job_id),
    )


def _recorded(db: Session, payment: Payment, replayed: bool) -> PaymentRecorded:
    read = _reads(db, [payment])[0]
    return PaymentRecorded(
        **read.model_dump(), replayed=replayed, summary=_summary(db, payment.job_id)
    )


# ---------- recording ----------


def _replay(
    db: Session, existing: Payment, job_id: int, fingerprint: str, actor: User
) -> PaymentRecorded:
    if existing.job_id != job_id or existing.request_hash != fingerprint:
        raise ConflictError(KEY_REUSED)
    # Access is checked again: it may have been withdrawn since.
    job_service.ensure_job_access(db, job_id, actor)
    return _recorded(db, existing, replayed=True)


def _snapshot(
    db: Session, job: Job, payment: Payment, paid_before: Decimal, actor: User
) -> dict:
    """What the receipt shows, frozen now. No identity numbers; the phone
    is masked."""
    customer = db.get(Customer, job.customer_id)
    fees = fee_repository.list_fees(db, job.id, include_removed=False)
    fees_total = sum((fee.amount for fee, _, _ in fees), Decimal("0.00"))
    return {
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "phone": mask_phone(customer.phone),
        },
        "job": {
            "id": job.id,
            "job_number": job.job_number,
            "title": job.title,
            "service_name": job.service_name,
        },
        "fees": [
            {
                "fee_type": fee.fee_type,
                "description": fee.description,
                "amount": _money(fee.amount),
            }
            for fee, _, _ in fees
        ],
        "fees_total": _money(fees_total),
        "paid_before": _money(paid_before),
        "amount": _money(payment.amount),
        "balance_after": _money(fees_total - paid_before - payment.amount),
        "method": payment.payment_method,
        "reference_number": payment.reference_number,
        "received_by": actor.name,
    }


def _constraint(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def record_payment(
    db: Session, job_id: int, data: PaymentCreate, key: str, actor: User
) -> PaymentRecorded:
    # The job and access come first, so a key never reveals anything about
    # a job the person cannot see.
    if job_repository.get_job(db, job_id) is None:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    job_service.ensure_job_access(db, job_id, actor)
    fingerprint = request_hash(job_id, data, actor.id)
    existing = repo.get_by_key(db, key)
    if existing is not None:
        return _replay(db, existing, job_id, fingerprint, actor)

    for attempt in range(2):
        try:
            return _record(db, job_id, data, key, fingerprint, actor)
        except IntegrityError as exc:
            db.rollback()
            constraint = _constraint(exc)
            if constraint == "uq_payments_idempotency_key":
                # The same key was used at the same moment on another job.
                existing = repo.get_by_key(db, key)
                if existing is not None:
                    return _replay(db, existing, job_id, fingerprint, actor)
            elif constraint == "uq_receipts_receipt_number" and attempt == 0:
                # The counter is behind existing receipts (restored data, a
                # lost counter row): move it past them and try once more.
                log.warning("Receipt counter was behind; moving it forward.")
                year = job_repository.current_year(db, job_service.BUSINESS_TIMEZONE)
                job_repository.sync_sequence_to_existing_numbers(
                    db,
                    RECEIPT_SEQUENCE,
                    year,
                    f"{RECEIPT_PREFIX}-{year}-",
                    Receipt.receipt_number,
                )
                db.commit()
                continue
            log.error("Payment could not be recorded (constraint %s).", constraint)
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def _record(
    db: Session,
    job_id: int,
    data: PaymentCreate,
    key: str,
    fingerprint: str,
    actor: User,
) -> PaymentRecorded:
    job = job_repository.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    # Again under the lock: assignment changes take the same lock.
    job_service.ensure_job_access(db, job_id, actor)
    # A racing repeat on this job committed while we waited for the lock.
    existing = repo.get_by_key(db, key)
    if existing is not None:
        return _replay(db, existing, job_id, fingerprint, actor)
    if job.status == JobStatus.CANCELLED.value:
        raise BusinessRuleError("Payments cannot be recorded for a cancelled job.")

    fees, paid = repo.totals_for_jobs(db, [job_id])[job_id]
    balance = fees - paid
    if balance <= 0:
        raise BusinessRuleError("Nothing is due on this job.")
    if data.amount > balance:
        raise BusinessRuleError(
            f"The amount is more than the balance due (Rs {_money(balance)})."
        )

    payment = repo.add(
        db,
        Payment(
            job_id=job_id,
            amount=data.amount,
            payment_method=data.method.value,
            reference_number=data.reference_number,
            notes=data.notes,
            status=PaymentStatus.RECEIVED.value,
            recorded_by=actor.id,
            idempotency_key=key,
            request_hash=fingerprint,
        ),
    )
    year = job_repository.current_year(db, job_service.BUSINESS_TIMEZONE)
    number = format_receipt_number(
        year, job_repository.next_sequence_value(db, RECEIPT_SEQUENCE, year)
    )
    receipt = repo.add(
        db,
        Receipt(
            receipt_number=number,
            payment_id=payment.id,
            job_id=job_id,
            customer_id=job.customer_id,
            issued_by=actor.id,
            snapshot=_snapshot(db, job, payment, paid, actor),
        ),
    )
    audit_service.record(
        db,
        action="payment_recorded",
        entity_type="job",
        entity_id=job_id,
        user_id=actor.id,
        details={
            "payment_id": payment.id,
            "receipt_number": receipt.receipt_number,
            "amount": _money(payment.amount),
            "method": payment.payment_method,
        },
    )
    db.commit()
    return _recorded(db, payment, replayed=False)


# ---------- voiding ----------


def _now(db: Session) -> datetime:
    return db.scalar(select(func.clock_timestamp()))


def void_payment(
    db: Session, payment_id: int, reason: str, actor: User
) -> PaymentWithSummary:
    """Admin only (enforced by the route)."""
    payment = repo.get(db, payment_id)
    if payment is None:
        raise NotFoundError(PAYMENT_NOT_FOUND)
    job_repository.get_job_for_update(db, payment.job_id)
    payment = repo.lock(db, payment_id)
    if payment.status == PaymentStatus.VOIDED.value:
        raise ConflictError("The payment is already voided.")
    now = _now(db)
    payment.status = PaymentStatus.VOIDED.value
    payment.voided_at = now
    payment.voided_by = actor.id
    payment.void_reason = reason
    receipt = repo.receipt_for(db, payment.id)
    if receipt is not None:
        receipt.cancelled_at = now
        receipt.cancelled_by = actor.id
        receipt.cancel_reason = reason
    audit_service.record(
        db,
        action="payment_voided",
        entity_type="job",
        entity_id=payment.job_id,
        user_id=actor.id,
        details={
            "payment_id": payment.id,
            "receipt_number": receipt.receipt_number if receipt else None,
            "amount": _money(payment.amount),
            "reason": reason,
        },
    )
    db.commit()
    read = _reads(db, [payment])[0]
    return PaymentWithSummary(**read.model_dump(), summary=_summary(db, payment.job_id))
