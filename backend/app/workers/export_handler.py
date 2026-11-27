"""The ``report_export`` task (Phase 5.2): build an Excel export in the
background with what the person who asked may see, store it privately,
and tell them it is ready.

The file is built and stored without holding locks; then, holding the
export row and only if it is still queued, it is recorded (a stale run
deletes its file). A task that fails for good marks the export failed and
tells the person, with a fixed, safe reason.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.storage import StorageNotConfigured, get_storage
from app.models import ReportExport, User
from app.services import report_export_service as service
from app.services.errors import BusinessRuleError
from app.workers import registry
from app.workers.registry import PermanentTaskError, TaskError, TaskInfo

TASK_TYPE = service.TASK_TYPE


def _lock(db: Session, export_id: int) -> ReportExport | None:
    return db.scalar(
        select(ReportExport)
        .where(ReportExport.id == export_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def run_report_export(db: Session, info: TaskInfo) -> dict:
    export_id = int(info.payload["export_id"])
    export = db.get(ReportExport, export_id)
    if export is None:
        raise PermanentTaskError("The export no longer exists.")
    if export.status != "queued":
        return {"skipped": "stale"}
    user = db.get(User, export.requested_by)
    if user is None or not user.is_active:
        raise PermanentTaskError(
            "The person who asked for this export can no longer sign in."
        )
    try:
        filename, data, rows = service.build(db, export, user)
    except BusinessRuleError as exc:
        # Our own fixed sentences, e.g. "More than 100000 jobs match; ...".
        raise PermanentTaskError(exc.detail[:300]) from None
    try:
        key = get_storage().save(data)
    except StorageNotConfigured:
        raise PermanentTaskError("File storage is not set up.") from None
    except OSError:
        raise TaskError("The file could not be saved; trying again.") from None
    export = _lock(db, export_id)
    if export is None or export.status != "queued":
        get_storage().delete(key)
        return {"skipped": "stale"}
    service.finish(db, export, filename, data, rows, key)
    return {"export_id": export_id, "rows": rows}


def on_export_failed(db: Session, info: TaskInfo, error: str) -> None:
    export = _lock(db, int(info.payload["export_id"]))
    if export is None or export.status != "queued":
        return
    service.fail(db, export, error)


registry.register(TASK_TYPE, run_report_export, on_final_failure=on_export_failed)
registry.register_maintenance(service.expire_files)
registry.register_maintenance(service.recover_stuck)
