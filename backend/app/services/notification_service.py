"""Work notifications (Phase 5.1): tell the right person, once, when work
needs them.

- ``job_assigned``: someone else assigned you to a job.
- ``ocr_failed``: OCR could not read the document you uploaded.
- ``lookup_captcha``: the vehicle lookup you started waits for a CAPTCHA.
- ``task_failed`` (admins): background tasks of a type failed for good;
  one notification per type per hour, counting them.

Each is created in the caller's transaction (never committed here), at
most once per person and event (``event_key``). When the work no longer
needs the person (unassigned, job closed, document reviewed, lookup over)
the notification is settled: marked read, never deleted.

Messages hold what staff already see in the app (names, job numbers,
registration numbers), never document contents, identifiers or errors.
"""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Customer, Document, DocumentType, Job, Notification, User

TASK_LABELS = {
    "document_ocr": "document reading (OCR)",
    "vehicle_expiry_lookup": "vehicle lookup",
    "vehicle_mobile_lookup": "mobile number lookup",
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


def job_closed(db: Session, job_id: int) -> None:
    settle(db, kinds=("job_assigned",), job_id=job_id)


# ---------- documents ----------


def ocr_failed(db: Session, document: Document, version: int, uploaded_by: int | None):
    """The person who uploaded this version is asked to check it by hand."""
    user = _active(db, uploaded_by)
    if user is None:
        return
    doc_type = (
        db.get(DocumentType, document.document_type_id)
        if document.document_type_id
        else None
    )
    customer = db.get(Customer, document.customer_id)
    name = doc_type.name if doc_type else "A document"
    _insert(
        db,
        user.id,
        "ocr_failed",
        f"ocr_failed:{document.id}:v{version}",
        document_id=document.id,
        title=f"Could not read: {name} for {customer.name}",
        body=(
            "OCR could not read this document. Check the scan and enter the "
            "details by hand, or upload a clearer copy."
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


def task_failed(db: Session, task_type: str) -> None:
    """Every active admin: one notification per task type per hour (in
    India), counting the failures; a new failure makes it unread again."""
    hour = db.scalar(
        select(
            func.to_char(
                func.timezone("Asia/Kolkata", func.now()), "YYYY-MM-DD HH24:00"
            )
        )
    )
    label = TASK_LABELS.get(task_type, task_type.replace("_", " "))
    admins = db.scalars(
        select(User.id).where(User.role == "admin", User.is_active.is_(True))
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
                    f"Since {hour[-5:]}, {count} {label} {tasks} failed after all "
                    "retries. Open Background tasks to see why and run them again."
                ),
            )
        )
