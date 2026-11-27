"""Work notifications (Phase 5.1): tell the right person, once, when work
needs them.

- ``job_assigned``: someone else assigned you to a job.
- ``ocr_failed``: OCR could not read a document: told to the uploader if
  they can still check it, otherwise to the people on its open jobs.
- ``lookup_captcha``: the vehicle lookup you started waits for a CAPTCHA.
- ``task_failed`` (admins): background tasks of a type failed for good for
  a system reason (retries used up, timeouts, no handler; not expected
  outcomes such as an unreadable scan); one notification per type per
  hour, counting them.

Each is created in the caller's transaction (never committed here), at
most once per person and event (``event_key``). When the work no longer
needs the person (unassigned, job closed, document reviewed, lookup over)
the notification is settled: marked read, never deleted.

Messages hold what staff already see in the app (names, job numbers,
registration numbers), never document contents, identifiers or errors.
"""

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Document,
    DocumentType,
    Job,
    JobAssignment,
    JobDocument,
    Notification,
    User,
)
from app.schemas.job import FINAL_STATUSES

# Everything that asks a person to do something (not vehicle expiries,
# which follow the vehicle's responsible employee).
WORK_KINDS = (
    "job_assigned",
    "ocr_failed",
    "lookup_captcha",
    "task_failed",
    "export_ready",
    "export_failed",
)

TASK_LABELS = {
    "document_ocr": "document reading (OCR)",
    "vehicle_expiry_lookup": "vehicle lookup",
    "vehicle_mobile_lookup": "mobile number lookup",
    "report_export": "Excel export",
}


def _active(db: Session, user_id: int | None) -> User | None:
    user = db.get(User, user_id) if user_id else None
    return user if user is not None and user.is_active else None


def _insert(db: Session, user_id: int, kind: str, key: str, **values) -> None:
    db.execute(
        insert(Notification)
        .values(user_id=user_id, kind=kind, event_key=key, **values)
        .on_conflict_do_nothing(constraint="uq_notifications_event")
    )


def settle(db: Session, *, kinds: tuple[str, ...], user_id: int | None = None, **about):
    """Mark matching unread notifications read: the work no longer needs
    them. ``about``: job_id, document_id or vehicle_lookup_id."""
    conditions = [Notification.kind.in_(kinds), Notification.read_at.is_(None)]
    if user_id is not None:
        conditions.append(Notification.user_id == user_id)
    for column, value in about.items():
        conditions.append(getattr(Notification, column) == value)
    db.execute(update(Notification).where(*conditions).values(read_at=func.now()))


# ---------- jobs ----------


def job_assigned(
    db: Session, job: Job, assignment_id: int, user_id: int, actor_id: int
):
    """Nobody is told about assigning themselves."""
    if user_id == actor_id or _active(db, user_id) is None:
        return
    customer = db.get(Customer, job.customer_id)
    _insert(
        db,
        user_id,
        "job_assigned",
        f"job_assigned:{assignment_id}",
        job_id=job.id,
        title=f"New job for you: {job.job_number}",
        body=f"{job.title} for {customer.name}.",
    )


def job_unassigned(db: Session, job_id: int, user_id: int) -> None:
    settle(db, kinds=("job_assigned",), user_id=user_id, job_id=job_id)
    _settle_ocr_nobody_can_act_on(db, _documents_of(db, job_id), user_id)


def job_closed(db: Session, job_id: int) -> None:
    """Call after the job's status became final."""
    db.flush()  # the checks below must see the new status
    settle(db, kinds=("job_assigned",), job_id=job_id)
    _settle_ocr_nobody_can_act_on(db, _documents_of(db, job_id))


def user_deactivated(db: Session, user_id: int) -> None:
    """They cannot act on anything now; vehicle expiries move with the
    vehicle's responsible employee instead."""
    settle(db, kinds=WORK_KINDS, user_id=user_id)


# ---------- documents ----------


def _documents_of(db: Session, job_id: int) -> list[int]:
    return list(
        db.scalars(select(JobDocument.document_id).where(JobDocument.job_id == job_id))
    )


def _open_job_of_document_for(user_id: int, document_id: int):
    return exists().where(
        JobDocument.document_id == document_id,
        Job.id == JobDocument.job_id,
        Job.status.not_in(FINAL_STATUSES),
        JobAssignment.job_id == Job.id,
        JobAssignment.user_id == user_id,
        JobAssignment.unassigned_at.is_(None),
    )


def _can_act_on(db: Session, user: User, document_id: int) -> bool:
    """Who may check a document by hand: an admin, or someone assigned to
    an open job that uses it (the same rule as reviewing it)."""
    if not user.is_active:
        return False
    if user.role == "admin":
        return True
    return bool(db.scalar(select(_open_job_of_document_for(user.id, document_id))))


def _who_can_act(db: Session, document_id: int, uploaded_by: int | None) -> list[int]:
    """The uploader, if they can still act on it; otherwise the people
    on the document's open jobs; otherwise nobody (it waits in the review
    queue, which admins see)."""
    uploader = _active(db, uploaded_by)
    if uploader is not None and _can_act_on(db, uploader, document_id):
        return [uploader.id]
    return list(
        db.scalars(
            select(User.id)
            .where(
                User.is_active.is_(True),
                _open_job_of_document_for(User.id, document_id),
            )
            .order_by(User.id)
        )
    )


def _settle_ocr_nobody_can_act_on(
    db: Session, document_ids: list[int], user_id: int | None = None
) -> None:
    """Failed readings of these documents, for people who can no longer
    act on them (job closed, taken off it)."""
    if not document_ids:
        return
    conditions = [
        Notification.kind == "ocr_failed",
        Notification.read_at.is_(None),
        Notification.document_id.in_(document_ids),
    ]
    if user_id is not None:
        conditions.append(Notification.user_id == user_id)
    for note in db.scalars(select(Notification).where(*conditions)).all():
        user = db.get(User, note.user_id)
        if not _can_act_on(db, user, note.document_id):
            note.read_at = func.now()
    db.flush()


def ocr_failed(
    db: Session,
    document: Document,
    version: int,
    uploaded_by: int | None,
    attempt: str = "",
):
    """Ask someone who can check the document by hand. ``attempt`` tells
    a later failure (after a retry) apart, so it is announced again."""
    recipients = _who_can_act(db, document.id, uploaded_by)
    if not recipients:
        return
    doc_type = (
        db.get(DocumentType, document.document_type_id)
        if document.document_type_id
        else None
    )
    customer = db.get(Customer, document.customer_id)
    name = doc_type.name if doc_type else "A document"
    for user_id in recipients:
        _insert(
            db,
            user_id,
            "ocr_failed",
            f"ocr_failed:{document.id}:v{version}{attempt}",
            document_id=document.id,
            title=f"Could not read: {name} for {customer.name}",
            body=(
                "OCR could not read this document. Check the scan and enter "
                "the details by hand, or upload a clearer copy."
            ),
        )


def document_handled(db: Session, document_id: int) -> None:
    """Reviewed, replaced or archived: the OCR failure needs nobody now."""
    settle(db, kinds=("ocr_failed",), document_id=document_id)


# ---------- vehicle lookups ----------


def lookup_captcha(db: Session, lookup, registration_number: str) -> None:
    """Once per lookup: later rounds follow a wrong answer the person has
    just typed, so they are already looking."""
    user = _active(db, lookup.requested_by)
    if user is None:
        return
    _insert(
        db,
        user.id,
        "lookup_captcha",
        f"lookup_captcha:{lookup.id}",
        vehicle_lookup_id=lookup.id,
        title=f"Type the CAPTCHA for {registration_number}",
        body=(
            "The vehicle lookup is waiting for you. The CAPTCHA expires in 5 minutes."
        ),
    )


def lookup_over(db: Session, lookup_id: int) -> None:
    settle(db, kinds=("lookup_captcha",), vehicle_lookup_id=lookup_id)


# ---------- background tasks (admins) ----------


def _hour_bucket(db: Session) -> str:
    """The current hour in India, e.g. "2026-11-26 14:00"."""
    return db.scalar(
        select(
            func.to_char(
                func.timezone("Asia/Kolkata", func.now()), "YYYY-MM-DD HH24:00"
            )
        )
    )


def task_failed(db: Session, task_type: str) -> None:
    """Every active admin: one notification per task type per hour (in
    India), counting the failures; a new failure makes it unread again."""
    hour = _hour_bucket(db)
    label = TASK_LABELS.get(task_type, task_type.replace("_", " "))
    admins = db.scalars(
        # Same order in every transaction: concurrent failures never deadlock.
        select(User.id)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.id)
    ).all()
    for admin_id in admins:
        count = db.scalar(
            insert(Notification)
            .values(
                user_id=admin_id,
                kind="task_failed",
                event_key=f"task_failed:{task_type}:{hour}",
                task_type=task_type,
                title="",
                body="",
            )
            .on_conflict_do_update(
                constraint="uq_notifications_event",
                set_={
                    "occurrences": Notification.occurrences + 1,
                    "read_at": None,
                    "created_at": func.now(),
                },
            )
            .returning(Notification.occurrences)
        )
        tasks = "task" if count == 1 else "tasks"
        db.execute(
            update(Notification)
            .where(
                Notification.user_id == admin_id,
                Notification.event_key == f"task_failed:{task_type}:{hour}",
            )
            .values(
                title=f"{count} {label} {tasks} failed",
                body=(
                    f"Since {hour[-5:]} IST, {count} {label} {tasks} failed after all "
                    "retries. Open Background tasks to see why and run them again."
                ),
            )
        )


# ---------- exports (Phase 5.2) ----------

_EXPORT_NAMES = {"vehicles": "vehicles", "jobs": "jobs"}


def export_ready(db: Session, export) -> None:
    user = _active(db, export.requested_by)
    if user is None:
        return
    until = db.scalar(
        select(
            func.to_char(func.timezone("Asia/Kolkata", export.expires_at), "HH24:MI")
        )
    )
    rows = "row" if export.row_count == 1 else "rows"
    _insert(
        db,
        user.id,
        "export_ready",
        f"export_ready:{export.id}",
        report_export_id=export.id,
        title=f"Your {_EXPORT_NAMES[export.kind]} export is ready",
        body=(
            f"{export.row_count} {rows}. Download it before {until} IST tomorrow; "
            "after that it is deleted."
        ),
    )


def export_failed(db: Session, export) -> None:
    user = _active(db, export.requested_by)
    if user is None:
        return
    _insert(
        db,
        user.id,
        "export_failed",
        f"export_failed:{export.id}",
        report_export_id=export.id,
        title=f"Your {_EXPORT_NAMES[export.kind]} export could not be made",
        body=export.failure_reason or "Ask for a new export.",
    )


def export_downloaded(db: Session, export_id: int) -> None:
    """Downloaded or expired: nothing left to do about it."""
    settle(db, kinds=("export_ready",), report_export_id=export_id)
