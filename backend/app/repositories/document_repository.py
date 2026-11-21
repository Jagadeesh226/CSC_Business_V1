from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentVersion,
    Job,
    JobAssignment,
    JobChecklistItem,
    JobDocument,
    User,
)


def add(db: Session, obj):
    db.add(obj)
    db.flush()
    return obj


def get_document(db: Session, document_id: int) -> Document | None:
    return db.get(Document, document_id)


def lock_document(db: Session, document_id: int) -> Document | None:
    return db.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def linked_job_ids(db: Session, document_id: int) -> list[int]:
    return list(
        db.scalars(
            select(JobDocument.job_id)
            .where(JobDocument.document_id == document_id)
            .order_by(JobDocument.job_id)
        )
    )


def links_for_documents(db: Session, document_ids: list[int]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {d: [] for d in document_ids}
    if document_ids:
        rows = db.execute(
            select(JobDocument.document_id, JobDocument.job_id)
            .where(JobDocument.document_id.in_(document_ids))
            .order_by(JobDocument.job_id)
        )
        for document_id, job_id in rows:
            result[document_id].append(job_id)
    return result


def lock_jobs(db: Session, job_ids: list[int]) -> list[Job]:
    """Row-lock jobs in id order (lock order: jobs before documents)."""
    if not job_ids:
        return []
    return list(
        db.scalars(
            select(Job)
            .where(Job.id.in_(job_ids))
            .order_by(Job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _active_assignment(user_id: int):
    return exists().where(
        JobAssignment.job_id == Job.id,
        JobAssignment.user_id == user_id,
        JobAssignment.unassigned_at.is_(None),
    )


def assigned_to_open_job_of_customer(
    db: Session, user_id: int, customer_id: int, final_statuses: tuple[str, ...]
) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    Job.customer_id == customer_id,
                    Job.status.not_in(final_statuses),
                    _active_assignment(user_id),
                )
            )
        )
    )


def assigned_to_linked_job(db: Session, user_id: int, document_id: int) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    JobDocument.document_id == document_id,
                    JobDocument.job_id == Job.id,
                    _active_assignment(user_id),
                )
            )
        )
    )


def list_customer_documents(
    db: Session,
    customer_id: int,
    *,
    include_archived: bool,
    status: str | None,
    only_for_user_jobs: int | None,
) -> list[Document]:
    stmt = select(Document).where(Document.customer_id == customer_id)
    if not include_archived:
        stmt = stmt.where(Document.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Document.status == status)
    if only_for_user_jobs is not None:
        stmt = stmt.where(
            exists().where(
                JobDocument.document_id == Document.id,
                JobDocument.job_id == Job.id,
                _active_assignment(only_for_user_jobs),
            )
        )
    return list(
        db.scalars(stmt.order_by(Document.created_at.desc(), Document.id.desc()))
    )


def list_versions(db: Session, document_id: int) -> list[DocumentVersion]:
    return list(
        db.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version)
        )
    )


def get_version(db: Session, document_id: int, version: int) -> DocumentVersion | None:
    return db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version == version,
        )
    )


def users_by_id(db: Session, user_ids: set[int]) -> dict[int, User]:
    ids = {i for i in user_ids if i is not None}
    if not ids:
        return {}
    return {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids)))}


# ---------- checklists ----------


def checklist_items(db: Session, job_id: int) -> list[JobChecklistItem]:
    return list(
        db.scalars(
            select(JobChecklistItem)
            .where(JobChecklistItem.job_id == job_id)
            .order_by(JobChecklistItem.sort_order, JobChecklistItem.id)
        )
    )


def get_checklist_item(db: Session, item_id: int) -> JobChecklistItem | None:
    return db.get(JobChecklistItem, item_id)


def documents_for_job_items(db: Session, job_id: int) -> list[tuple[int, Document]]:
    """(checklist_item_id, document) for a job's non-archived linked documents."""
    rows = db.execute(
        select(JobDocument.checklist_item_id, Document)
        .join(Document, Document.id == JobDocument.document_id)
        .where(
            JobDocument.job_id == job_id,
            JobDocument.checklist_item_id.is_not(None),
            Document.archived_at.is_(None),
        )
        .order_by(Document.id)
    )
    return [(item_id, doc) for item_id, doc in rows]


def link_exists(db: Session, job_id: int, document_id: int) -> bool:
    return (
        db.scalar(
            select(JobDocument.id).where(
                JobDocument.job_id == job_id, JobDocument.document_id == document_id
            )
        )
        is not None
    )
