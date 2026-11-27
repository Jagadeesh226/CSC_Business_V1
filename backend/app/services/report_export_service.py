"""Background Excel exports (Phase 5.2).

A person asks for an export; the worker builds it with what that person
may see at that moment (``report_export`` task); the file is kept in the
private encrypted storage for a day and only that person can download it.
Asking, building and every download are audited. When the day is over the
file is deleted (maintenance) and the record stays as history.

Limits: three exports waiting per person, 100,000 rows per export.
"""

import hashlib
import math
from datetime import timedelta

from pydantic import TypeAdapter
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.storage import get_storage
from app.models import ReportExport, User
from app.repositories import user_repository
from app.schemas.report_export import (
    ExportListResponse,
    ExportRead,
    ExportRequest,
    ExportStatus,
    JobsExportRequest,
    VehiclesExportRequest,
)
from app.services import audit_service, export_service, notification_service, task_queue
from app.services.errors import ConflictError, NotFoundError, ServiceError

TASK_TYPE = "report_export"
MAX_WAITING_PER_PERSON = 3
MAX_ROWS = 100_000
KEEP_FILE_FOR = timedelta(hours=24)
EXPORT_NOT_FOUND = "Export not found."
_request = TypeAdapter(ExportRequest)


class ExportGone(ServiceError):
    status_code = 410


def idempotency_key(export_id: int) -> str:
    return f"report_export:{export_id}"


def _filters(request: VehiclesExportRequest | JobsExportRequest) -> dict:
    return request.model_dump(mode="json", exclude={"kind"})


def parse(kind: str, filters: dict) -> VehiclesExportRequest | JobsExportRequest:
    return _request.validate_python({"kind": kind, **filters})


def request_export(
    db: Session, request: VehiclesExportRequest | JobsExportRequest, actor: User
) -> ExportRead:
    # One request at a time per person, so the limit cannot be raced past.
    user_repository.get_user_for_update(db, actor.id)
    waiting = db.scalar(
        select(func.count())
        .select_from(ReportExport)
        .where(
            ReportExport.requested_by == actor.id,
            ReportExport.status == ExportStatus.QUEUED.value,
        )
    )
    if waiting >= MAX_WAITING_PER_PERSON:
        raise ConflictError(
            f"You already have {waiting} exports being made; wait for one to finish."
        )
    export = ReportExport(
        kind=request.kind,
        requested_by=actor.id,
        status=ExportStatus.QUEUED.value,
        filters=_filters(request),
    )
    db.add(export)
    db.flush()
    task_queue.enqueue(
        db,
        TASK_TYPE,
        idempotency_key(export.id),
        {"export_id": export.id},
        created_by=actor.id,
    )
    audit_service.record(
        db,
        action="export_requested",
        entity_type="export",
        entity_id=export.id,
        user_id=actor.id,
        details={"kind": export.kind, "filters": export.filters},
    )
    db.commit()
    return ExportRead.model_validate(export)


def _own(db: Session, export_id: int, viewer: User, lock: bool = False):
    """Only the person who asked; others get 404 (not 403), as for jobs."""
    stmt = select(ReportExport).where(
        ReportExport.id == export_id, ReportExport.requested_by == viewer.id
    )
    if lock:
        stmt = stmt.with_for_update()
    export = db.scalar(stmt)
    if export is None:
        raise NotFoundError(EXPORT_NOT_FOUND)
    return export


def get_export(db: Session, export_id: int, viewer: User) -> ExportRead:
    return ExportRead.model_validate(_own(db, export_id, viewer))


def list_exports(
    db: Session, viewer: User, *, page: int, page_size: int
) -> ExportListResponse:
    mine = ReportExport.requested_by == viewer.id
    total = db.scalar(select(func.count()).select_from(ReportExport).where(mine)) or 0
    rows = db.scalars(
        select(ReportExport)
        .where(mine)
        .order_by(ReportExport.created_at.desc(), ReportExport.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ExportListResponse(
        items=[ExportRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def download(db: Session, export_id: int, viewer: User) -> tuple[str, bytes]:
    export = _own(db, export_id, viewer, lock=True)
    if export.status == ExportStatus.QUEUED.value:
        raise ConflictError("The export is still being made.")
    if export.status == ExportStatus.FAILED.value:
        raise ConflictError("The export could not be made.")
    now = db.scalar(select(func.now()))
    if export.status == ExportStatus.EXPIRED.value or export.expires_at <= now:
        raise ExportGone("The export has expired; ask for a new one.")
    try:
        data = get_storage().load(export.storage_key)
    except FileNotFoundError:
        raise ExportGone("The export has expired; ask for a new one.") from None
    if hashlib.sha256(data).hexdigest() != export.sha256:
        raise ExportGone("The export file is damaged; ask for a new one.")
    export.download_count += 1
    export.last_downloaded_at = now
    notification_service.export_downloaded(db, export.id)
    audit_service.record(
        db,
        action="export_downloaded",
        entity_type="export",
        entity_id=export.id,
        user_id=viewer.id,
        details={"kind": export.kind, "rows": export.row_count},
    )
    db.commit()
    return export.filename, data


# ---------- the worker's side (no commits) ----------


def build(db: Session, export: ReportExport, user: User) -> tuple[str, bytes, int]:
    request = parse(export.kind, export.filters)
    if isinstance(request, VehiclesExportRequest):
        return export_service.build_vehicles(
            db,
            user,
            due_within_days=request.due_within_days,
            include_inactive=request.include_inactive,
            customer_id=request.customer_id,
            include_whatsapp=request.include_whatsapp,
            max_rows=MAX_ROWS,
        )
    return export_service.build_jobs(
        db,
        user,
        max_rows=MAX_ROWS,
        status=request.status,
        service_id=request.service_id,
        customer_id=request.customer_id,
        assigned_to=request.assigned_to,
        readiness=request.readiness,
        due=request.due,
        payment=request.payment,
        unassigned=request.unassigned,
    )


def finish(
    db: Session, export: ReportExport, filename: str, data: bytes, rows: int, key: str
) -> None:
    now = db.scalar(select(func.now()))
    export.status = ExportStatus.COMPLETED.value
    export.filename = filename
    export.storage_key = key
    export.file_size = len(data)
    export.sha256 = hashlib.sha256(data).hexdigest()
    export.row_count = rows
    export.finished_at = now
    export.expires_at = now + KEEP_FILE_FOR
    audit_service.record(
        db,
        action=f"{export.kind}_exported",
        entity_type="export",
        entity_id=export.id,
        user_id=export.requested_by,
        details={"rows": rows, "filters": export.filters, "background": True},
    )
    notification_service.export_ready(db, export)


def fail(db: Session, export: ReportExport, reason: str) -> None:
    export.status = ExportStatus.FAILED.value
    export.failure_reason = reason[:300]
    export.finished_at = db.scalar(select(func.now()))
    audit_service.record(
        db,
        action="export_failed",
        entity_type="export",
        entity_id=export.id,
        user_id=export.requested_by,
        details={"kind": export.kind, "reason": reason[:200]},
    )
    notification_service.export_failed(db, export)


def expire_files(db: Session) -> int:
    """Maintenance: files past their day are deleted; the record stays."""
    rows = db.scalars(
        select(ReportExport)
        .where(
            ReportExport.status == ExportStatus.COMPLETED.value,
            ReportExport.expires_at <= func.now(),
        )
        .with_for_update(skip_locked=True)
        .limit(50)
    ).all()
    storage = get_storage() if rows else None
    for export in rows:
        storage.delete(export.storage_key)
        export.status = ExportStatus.EXPIRED.value
        export.storage_key = None
        notification_service.export_downloaded(db, export.id)
    return len(rows)


def recover_stuck(db: Session) -> int:
    """Maintenance: an export still queued whose task finished or vanished
    would wait for ever; it fails."""
    from app.models import BackgroundTask

    key = func.concat("report_export:", ReportExport.id)
    rows = db.scalars(
        select(ReportExport)
        .where(
            ReportExport.status == ExportStatus.QUEUED.value,
            ~select(BackgroundTask.id)
            .where(
                BackgroundTask.idempotency_key == key,
                BackgroundTask.status.in_(("queued", "running")),
            )
            .exists(),
            ReportExport.created_at < func.now() - text("interval '10 minutes'"),
        )
        .with_for_update(skip_locked=True)
        .limit(50)
    ).all()
    for export in rows:
        fail(db, export, "The export stopped unexpectedly; ask for a new one.")
    return len(rows)
