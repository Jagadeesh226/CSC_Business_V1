from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import JobFee, User
from app.repositories import fee_repository as repo
from app.repositories import job_repository
from app.schemas.fee import (
    FeeCreate,
    FeeListResponse,
    FeeRead,
    FeeTotals,
    FeeType,
    FeeUpdate,
)
from app.schemas.job import FINAL_STATUSES, AssigneeRef
from app.services import audit_service, job_service
from app.services.errors import BusinessRuleError, NotFoundError

# Rules (owner decisions, docs/contracts/decisions.md section 7):
# - Admins add, edit and remove fees on any job, including closed ones, to
#   correct mistakes.
# - Employees add fees only to open jobs they are assigned to, and only until
#   the job has a payment with status 'received'. They never edit or remove.
# - Removing a fee marks it; rows are never erased.
#
# Every write locks the job row first (the same lock order as the rest of
# job_service). Phase 3 payment recording must take the same job lock, so
# "no payment received yet" cannot change between this check and the insert.

FEE_NOT_FOUND = "Fee not found."


def _locked_job(db: Session, job_id: int, actor: User):
    job = job_repository.get_job_for_update(db, job_id)
    if job is None:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    job_service.ensure_job_access(db, job_id, actor)
    return job


def _fee_read(fee: JobFee, creator: User | None, remover: User | None) -> FeeRead:
    return FeeRead(
        id=fee.id,
        fee_type=fee.fee_type,
        description=fee.description,
        amount=fee.amount,
        created_by=AssigneeRef.model_validate(creator) if creator else None,
        created_at=fee.created_at,
        deleted_by=AssigneeRef.model_validate(remover) if remover else None,
        deleted_at=fee.deleted_at,
    )


def _read_one(db: Session, job_id: int, fee_id: int) -> FeeRead:
    for fee, creator, remover in repo.list_fees(db, job_id, include_removed=True):
        if fee.id == fee_id:
            return _fee_read(fee, creator, remover)
    raise NotFoundError(FEE_NOT_FOUND)


def list_fees(
    db: Session, job_id: int, viewer: User | None, include_removed: bool = False
) -> FeeListResponse:
    if job_repository.get_job(db, job_id) is None:
        raise NotFoundError(job_service.JOB_NOT_FOUND)
    job_service.ensure_job_access(db, job_id, viewer)
    totals = repo.active_totals(db, job_id)
    by_type = {t.value: totals.get(t.value, Decimal("0.00")) for t in FeeType}
    return FeeListResponse(
        items=[
            _fee_read(fee, creator, remover)
            for fee, creator, remover in repo.list_fees(db, job_id, include_removed)
        ],
        totals=FeeTotals(**by_type, total=sum(by_type.values(), Decimal("0.00"))),
    )


def add_fee(db: Session, job_id: int, data: FeeCreate, actor: User) -> FeeRead:
    job = _locked_job(db, job_id, actor)
    if not job_service.is_admin(actor):
        if job.status in FINAL_STATUSES:
            raise BusinessRuleError(f"Fees cannot be added to a {job.status} job.")
        if repo.has_received_payment(db, job_id):
            raise BusinessRuleError(
                "Fees can no longer be added: a payment has been received for "
                "this job. Ask an admin."
            )
    fee = _add(db, job_id, data.fee_type, data.description, data.amount, actor.id)
    db.commit()
    return _read_one(db, job_id, fee.id)


def _add(
    db: Session,
    job_id: int,
    fee_type: FeeType,
    description: str,
    amount: Decimal,
    actor_id: int | None,
    reason: str | None = None,
) -> JobFee:
    fee = repo.add_fee(
        db,
        JobFee(
            job_id=job_id,
            fee_type=fee_type.value,
            description=description,
            amount=amount,
            created_by=actor_id,
        ),
    )
    details: dict[str, object] = {
        "fee_id": fee.id,
        "fee_type": fee.fee_type,
        "amount": str(fee.amount),
    }
    if reason:
        details["reason"] = reason
    audit_service.record(
        db,
        action="fee_added",
        entity_type="job",
        entity_id=job_id,
        user_id=actor_id,
        details=details,
    )
    return fee


def add_default_service_fee(
    db: Session, job_id: int, service, actor_id: int | None
) -> None:
    """On job creation: a fixed-fee service starts the job with its default
    service fee (caller commits). Variable-fee services add none."""
    if service.fee_type == "fixed" and service.default_service_fee is not None:
        _add(
            db,
            job_id,
            FeeType.SERVICE,
            service.name,
            service.default_service_fee,
            actor_id,
            reason="service_default",
        )


def update_fee(
    db: Session, job_id: int, fee_id: int, data: FeeUpdate, actor: User
) -> FeeRead:
    """Admin only (enforced by the route)."""
    _locked_job(db, job_id, actor)
    fee = repo.get_active_fee(db, job_id, fee_id)
    if fee is None:
        raise NotFoundError(FEE_NOT_FOUND)
    changes = data.model_dump(exclude_unset=True)
    if "fee_type" in changes:
        changes["fee_type"] = changes["fee_type"].value
    changed = {k: v for k, v in changes.items() if getattr(fee, k) != v}
    if changed:
        audit_service.record(
            db,
            action="fee_updated",
            entity_type="job",
            entity_id=job_id,
            user_id=actor.id,
            details={
                "fee_id": fee.id,
                "changes": {
                    k: {"old": str(getattr(fee, k)), "new": str(v)}
                    for k, v in changed.items()
                },
            },
        )
        for field, value in changed.items():
            setattr(fee, field, value)
    db.commit()
    return _read_one(db, job_id, fee_id)


def remove_fee(db: Session, job_id: int, fee_id: int, actor: User) -> None:
    """Admin only (enforced by the route). Marks the fee as removed."""
    _locked_job(db, job_id, actor)
    fee = repo.get_active_fee(db, job_id, fee_id)
    if fee is None:
        raise NotFoundError(FEE_NOT_FOUND)
    fee.deleted_at = datetime.now(UTC)
    fee.deleted_by = actor.id
    audit_service.record(
        db,
        action="fee_removed",
        entity_type="job",
        entity_id=job_id,
        user_id=actor.id,
        details={
            "fee_id": fee.id,
            "fee_type": fee.fee_type,
            "amount": str(fee.amount),
        },
    )
    db.commit()
