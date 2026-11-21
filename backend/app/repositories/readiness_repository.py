"""Document progress of jobs, worked out in the database.

One definition serves the job read model, the job list filters and the work
queue, so they always agree. It mirrors ``document_service.build_checklist``
(non-archived documents linked to the job's checklist item):

- ``verified``: at least one document for the item is verified
- ``to_review``: otherwise, a document waits for a person (``needs_review``,
  or ``uploaded`` and never read by OCR)
- ``processing``: otherwise, OCR is still reading a document
- ``rejected``: every document for the item was rejected
- ``missing``: nothing uploaded yet

Only required items count. Nothing is stored, so progress never goes stale.
"""

from datetime import date

from sqlalchemy import (
    ColumnElement,
    Subquery,
    and_,
    case,
    func,
    literal,
    select,
)
from sqlalchemy.orm import Session

from app.models import Document, JobChecklistItem, JobDocument

ITEM_STATES = ("verified", "to_review", "processing", "rejected", "missing")
# Business dates follow the centre's calendar (same zone as job numbers).
BUSINESS_TIMEZONE = "Asia/Kolkata"


def business_today(db: Session) -> date:
    return db.scalar(select(func.date(func.timezone(BUSINESS_TIMEZONE, func.now()))))


def _item_states() -> Subquery:
    """One row per required checklist item: (job_id, state)."""
    docs = (
        select(
            JobDocument.job_id.label("job_id"),
            JobDocument.checklist_item_id.label("item_id"),
            func.bool_or(Document.status == "verified").label("any_verified"),
            func.bool_or(Document.status.in_(("uploaded", "needs_review"))).label(
                "any_to_review"
            ),
            func.bool_or(Document.status == "processing").label("any_processing"),
        )
        .join(Document, Document.id == JobDocument.document_id)
        .where(
            JobDocument.checklist_item_id.is_not(None),
            Document.archived_at.is_(None),
        )
        .group_by(JobDocument.job_id, JobDocument.checklist_item_id)
        .subquery()
    )
    state = case(
        (docs.c.any_verified, literal("verified")),
        (docs.c.any_to_review, literal("to_review")),
        (docs.c.any_processing, literal("processing")),
        (docs.c.item_id.is_not(None), literal("rejected")),
        else_=literal("missing"),
    )
    return (
        select(JobChecklistItem.job_id.label("job_id"), state.label("state"))
        .outerjoin(
            docs,
            and_(
                docs.c.item_id == JobChecklistItem.id,
                docs.c.job_id == JobChecklistItem.job_id,
            ),
        )
        .where(JobChecklistItem.is_required.is_(True))
        .subquery()
    )


def progress_subquery() -> Subquery:
    """Per job with required items: required_total and a count per state.
    Jobs without required items have no row (treat counts as 0)."""
    items = _item_states()
    counts = [func.count().filter(items.c.state == s).label(s) for s in ITEM_STATES]
    return (
        select(
            items.c.job_id,
            func.count().label("required_total"),
            *counts,
        )
        .group_by(items.c.job_id)
        .subquery()
    )


def column(progress: Subquery, name: str) -> ColumnElement[int]:
    """A count from the progress subquery, 0 for jobs without a row."""
    return func.coalesce(progress.c[name], 0)


def is_complete(progress: Subquery) -> ColumnElement[bool]:
    return column(progress, "verified") == column(progress, "required_total")


def progress_for_jobs(db: Session, job_ids: list[int]) -> dict[int, dict[str, int]]:
    """Counts for a page of jobs, in one query."""
    empty = {"required_total": 0, **{s: 0 for s in ITEM_STATES}}
    result = {job_id: dict(empty) for job_id in job_ids}
    if not job_ids:
        return result
    progress = progress_subquery()
    rows = db.execute(select(progress).where(progress.c.job_id.in_(job_ids)))
    for row in rows.mappings():
        result[row["job_id"]] = {k: int(row[k]) for k in empty}
    return result
