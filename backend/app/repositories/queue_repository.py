"""Queries for the review queue: documents waiting for a person.

A document is in the queue when it is not archived, its status is
``needs_review`` or ``uploaded`` (never read by OCR), and someone can act on
it:
- admins: it is used by at least one open job, or by no job at all (a
  customer document uploaded on its own);
- employees: it is used by an open job they are assigned to, and by no
  closed job (they cannot change documents of closed jobs).
"""

from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Document,
    DocumentType,
    DocumentVersion,
    Job,
    JobAssignment,
    JobDocument,
    OcrResult,
)
from app.schemas.job import FINAL_STATUSES

WAITING_STATUSES = ("needs_review", "uploaded")


def _assigned(user_id: int) -> ColumnElement[bool]:
    return exists().where(
        JobAssignment.job_id == Job.id,
        JobAssignment.user_id == user_id,
        JobAssignment.unassigned_at.is_(None),
    )


def _linked_job(*conditions: ColumnElement[bool]) -> ColumnElement[bool]:
    return exists().where(
        JobDocument.document_id == Document.id,
        JobDocument.job_id == Job.id,
        *conditions,
    )


def _conditions(
    employee_id: int | None, customer_id: int | None, job_id: int | None
) -> list[ColumnElement[bool]]:
    open_job = Job.status.not_in(FINAL_STATUSES)
    conditions: list[ColumnElement[bool]] = [
        Document.archived_at.is_(None),
        Document.status.in_(WAITING_STATUSES),
    ]
    if employee_id is None:
        conditions.append(
            or_(
                _linked_job(open_job),
                ~exists().where(JobDocument.document_id == Document.id),
            )
        )
    else:
        conditions.append(_linked_job(open_job, _assigned(employee_id)))
        conditions.append(~_linked_job(Job.status.in_(FINAL_STATUSES)))
    if customer_id is not None:
        conditions.append(Document.customer_id == customer_id)
    if job_id is not None:
        conditions.append(
            exists().where(
                JobDocument.document_id == Document.id, JobDocument.job_id == job_id
            )
        )
    return conditions


def _base(select_stmt):
    """Same joins for the count and the page, so totals always match."""
    return (
        select_stmt.select_from(Document)
        .join(
            DocumentVersion,
            and_(
                DocumentVersion.document_id == Document.id,
                DocumentVersion.version == Document.current_version,
            ),
        )
        .join(Customer, Customer.id == Document.customer_id)
        .outerjoin(DocumentType, DocumentType.id == Document.document_type_id)
    )


def count(
    db: Session,
    *,
    employee_id: int | None,
    customer_id: int | None = None,
    job_id: int | None = None,
) -> int:
    return int(
        db.scalar(
            _base(select(func.count())).where(
                *_conditions(employee_id, customer_id, job_id)
            )
        )
        or 0
    )


def page(
    db: Session,
    *,
    employee_id: int | None,
    customer_id: int | None,
    job_id: int | None,
    offset: int,
    limit: int,
) -> list[tuple[Document, datetime, Customer, DocumentType | None]]:
    """Oldest waiting first (by when the current version was uploaded)."""
    rows = db.execute(
        _base(select(Document, DocumentVersion.uploaded_at, Customer, DocumentType))
        .where(*_conditions(employee_id, customer_id, job_id))
        .order_by(DocumentVersion.uploaded_at, Document.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [tuple(r) for r in rows]


def open_jobs_for_documents(
    db: Session, document_ids: list[int], employee_id: int | None
) -> dict[int, list[Job]]:
    """Open jobs using each document; for employees only their assigned
    ones, so the queue never reveals other jobs."""
    result: dict[int, list[Job]] = {d: [] for d in document_ids}
    if not document_ids:
        return result
    stmt = (
        select(JobDocument.document_id, Job)
        .join(Job, Job.id == JobDocument.job_id)
        .where(
            JobDocument.document_id.in_(document_ids),
            Job.status.not_in(FINAL_STATUSES),
        )
        .order_by(Job.id)
    )
    if employee_id is not None:
        stmt = stmt.where(_assigned(employee_id))
    for document_id, job in db.execute(stmt):
        result[document_id].append(job)
    return result


def ocr_results(db: Session, documents: list[Document]) -> dict[int, OcrResult]:
    """The OCR result of each document's current version, if any."""
    if not documents:
        return {}
    pairs = [(d.id, d.current_version) for d in documents]
    rows = db.scalars(
        select(OcrResult).where(
            tuple_(OcrResult.document_id, OcrResult.document_version).in_(pairs)
        )
    )
    return {r.document_id: r for r in rows}
