"""Work queues: what each person should work on next.

Nothing here changes data or job statuses (owner decision: documents never
move a job automatically); the queues only point at the next step. Access
follows the usual rules: employees see only open jobs they are assigned to,
admins see everything.
"""

import math

from sqlalchemy.orm import Session

from app.models import User
from app.repositories import job_repository, queue_repository
from app.repositories.readiness_repository import business_today
from app.schemas.catalogue import DocumentTypeRef
from app.schemas.job import CustomerRef
from app.schemas.queue import (
    ReviewJobRef,
    ReviewKind,
    ReviewQueueItem,
    ReviewQueueResponse,
    WorkQueueSummary,
)
from app.services import job_service
from app.services.errors import NotFoundError
from app.workers.ocr_handler import LOW_CONFIDENCE


def _employee_id(viewer: User) -> int | None:
    return None if job_service.is_admin(viewer) else viewer.id


def work_queue(db: Session, viewer: User) -> WorkQueueSummary:
    employee_id = _employee_id(viewer)
    today = business_today(db)

    def jobs(**filters) -> int:
        return job_repository.count_jobs(db, visible_to=employee_id, **filters)

    return WorkQueueSummary(
        as_of=today,
        documents_to_review=queue_repository.count(db, employee_id=employee_id),
        jobs_missing_documents=jobs(readiness="missing_documents"),
        jobs_processing=jobs(readiness="processing"),
        jobs_ready_to_submit=jobs(readiness="ready_to_submit"),
        jobs_ready_to_resume=jobs(readiness="ready_to_resume"),
        jobs_overdue=jobs(due="overdue", today=today),
        jobs_due_today=jobs(due="today", today=today),
        jobs_with_balance_due=jobs(payment="due"),
        unassigned_jobs=jobs(unassigned=True) if employee_id is None else None,
        jobs_overpaid=jobs(payment="overpaid") if employee_id is None else None,
    )


def _kind_and_flags(result) -> tuple[ReviewKind, int | None]:
    if result is None:
        return ReviewKind.MANUAL, None
    if result.status != "completed":
        return ReviewKind.OCR_FAILED, None
    flagged = sum(
        1
        for c in (result.confidences or {}).values()
        if c is None or c < LOW_CONFIDENCE
    )
    return ReviewKind.OCR_CHECK, flagged


def review_queue(
    db: Session,
    viewer: User,
    *,
    page: int,
    page_size: int,
    customer_id: int | None = None,
    job_id: int | None = None,
) -> ReviewQueueResponse:
    employee_id = _employee_id(viewer)
    if job_id is not None:
        # 404 for jobs the viewer cannot see, as everywhere else; otherwise
        # the filter would reveal which hidden jobs share a document.
        if job_repository.get_job(db, job_id) is None:
            raise NotFoundError(job_service.JOB_NOT_FOUND)
        job_service.ensure_job_access(db, job_id, viewer)
    filters = {
        "employee_id": employee_id,
        "customer_id": customer_id,
        "job_id": job_id,
    }
    total = queue_repository.count(db, **filters)
    rows = queue_repository.page(
        db, offset=(page - 1) * page_size, limit=page_size, **filters
    )
    documents = [r[0] for r in rows]
    jobs = queue_repository.open_jobs_for_documents(
        db, [d.id for d in documents], employee_id
    )
    results = queue_repository.ocr_results(db, documents)
    items = []
    for document, waiting_since, customer, doc_type in rows:
        kind, flagged = _kind_and_flags(results.get(document.id))
        items.append(
            ReviewQueueItem(
                document_id=document.id,
                filename=document.filename,
                status=document.status,
                current_version=document.current_version,
                document_type=DocumentTypeRef.model_validate(doc_type)
                if doc_type
                else None,
                customer=CustomerRef.model_validate(customer),
                jobs=[ReviewJobRef.model_validate(j) for j in jobs[document.id]],
                review_kind=kind,
                flagged_fields=flagged,
                waiting_since=waiting_since,
            )
        )
    return ReviewQueueResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )
