"""Vehicle expiry reminders and notifications (Phase 4.2).

Owner decisions:
- Insurance, PUCC, fitness, tax or permit expiring from today up to 30
  days ahead gets one reminder, and the responsible employee one
  notification. Already-expired dates are shown in the "expired" bucket
  but get no new reminder.
- One reminder per vehicle + type + date (database-enforced): the daily
  check never repeats it. When a date changes, the open reminder for the
  old date is superseded and the new date gets its own.
- WhatsApp: a click-to-chat link with the message filled in; nothing is
  sent automatically. The employee marks the reminder done after sending
  (audited); opening the link changes nothing.

The daily check runs from every worker's maintenance loop, but only once
per business day (``daily_runs``).
"""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.whatsapp import describe, link, renewal_message, whatsapp_number
from app.models import (
    Customer,
    DailyRun,
    Notification,
    User,
    Vehicle,
    VehicleExpiry,
    VehicleReminder,
)
from app.repositories import vehicle_repository
from app.repositories.document_repository import users_by_id
from app.repositories.readiness_repository import business_today
from app.schemas.job import AssigneeRef
from app.schemas.reminder import (
    EXPIRY_LABELS,
    ExpiryBucket,
    ExpiryListItem,
    ExpiryListResponse,
    NotificationListResponse,
    NotificationRead,
    ReminderListResponse,
    ReminderRead,
    ReminderStatus,
    ReminderVehicleRef,
)
from app.services import audit_service, job_service
from app.services.errors import BusinessRuleError, NotFoundError

WINDOW_DAYS = 30
DAILY_JOB = "vehicle_expiry_check"
REMINDER_NOT_FOUND = "Reminder not found."


def _now(db: Session) -> datetime:
    return db.scalar(select(func.clock_timestamp()))


def _label(expiry_type: str) -> str:
    return EXPIRY_LABELS.get(expiry_type, expiry_type)


# ---------- creating reminders ----------


def _notify(
    db: Session,
    reminder_id: int,
    vehicle: Vehicle,
    customer: Customer,
    expiry_type: str,
    valid_until: date,
    today: date,
) -> None:
    user = db.get(User, vehicle.assigned_to) if vehicle.assigned_to else None
    if user is None or not user.is_active:
        return
    days = (valid_until - today).days
    label = _label(expiry_type)
    db.execute(
        insert(Notification)
        .values(
            user_id=user.id,
            kind="vehicle_expiry",
            reminder_id=reminder_id,
            title=f"{vehicle.registration_number}: {describe(label, valid_until, days)}",
            body=(
                f"{label} for {vehicle.registration_number} ({customer.name}) "
                f"expires on {valid_until.strftime('%d-%m-%Y')}."
            ),
        )
        .on_conflict_do_nothing(constraint="uq_notifications_reminder")
    )


def _ensure(
    db: Session, vehicle: Vehicle, expiry_type: str, valid_until: date, today: date
) -> bool:
    """One reminder (and notification) for this expiry. True if new.

    Re-checks, under a share lock on the vehicle (date changes take an
    update lock on it), that the date is still current: a date changed
    meanwhile never gets a stale reminder back."""
    db.execute(
        select(Vehicle.id).where(Vehicle.id == vehicle.id).with_for_update(read=True)
    )
    still_current = db.scalar(
        select(
            exists().where(
                VehicleExpiry.vehicle_id == Vehicle.id,
                VehicleExpiry.expiry_type == expiry_type,
                VehicleExpiry.valid_until == valid_until,
                VehicleExpiry.is_current.is_(True),
                Vehicle.id == vehicle.id,
                Vehicle.is_active.is_(True),
            )
        )
    )
    if not still_current:
        return False
    created = db.scalar(
        insert(VehicleReminder)
        .values(
            vehicle_id=vehicle.id,
            expiry_type=expiry_type,
            valid_until=valid_until,
            status="open",
        )
        .on_conflict_do_nothing(constraint="uq_vehicle_reminders_event")
        .returning(VehicleReminder.id)
    )
    if created is None:
        # A date changed back to one seen before: reopen its reminder.
        reopened = db.scalar(
            update(VehicleReminder)
            .where(
                VehicleReminder.vehicle_id == vehicle.id,
                VehicleReminder.expiry_type == expiry_type,
                VehicleReminder.valid_until == valid_until,
                VehicleReminder.status == "superseded",
            )
            .values(status="open", closed_at=None, closed_by=None, close_reason=None)
            .returning(VehicleReminder.id)
        )
        if reopened is None:
            return False
        created = reopened
        # Needs attention again: its notification becomes unread.
        db.execute(
            update(Notification)
            .where(
                Notification.reminder_id == reopened,
                Notification.user_id == vehicle.assigned_to,
            )
            .values(read_at=None)
        )
    customer = db.get(Customer, vehicle.customer_id)
    _notify(db, created, vehicle, customer, expiry_type, valid_until, today)
    return True


def notify_responsible(
    db: Session, vehicle: Vehicle, previous: int | None = None
) -> None:
    """The vehicle's responsible employee gets a notification for each of
    its open reminders (once each). Called when the responsible person
    changes; the previous one's notifications for them are settled."""
    today = business_today(db)
    open_reminders = db.scalars(
        select(VehicleReminder).where(
            VehicleReminder.vehicle_id == vehicle.id, VehicleReminder.status == "open"
        )
    ).all()
    if previous is not None and previous != vehicle.assigned_to:
        db.execute(
            update(Notification)
            .where(
                Notification.user_id == previous,
                Notification.reminder_id.in_([r.id for r in open_reminders]),
                Notification.read_at.is_(None),
            )
            .values(read_at=func.now())
        )
    customer = db.get(Customer, vehicle.customer_id)
    for r in open_reminders:
        _notify(db, r.id, vehicle, customer, r.expiry_type, r.valid_until, today)


def _supersede(db: Session, condition, reason: str) -> int:
    rows = db.scalars(
        update(VehicleReminder)
        .where(VehicleReminder.status == "open", condition)
        .values(status="superseded", closed_at=func.now(), close_reason=reason)
        .returning(VehicleReminder.id)
    ).all()
    if rows:
        # A replaced reminder no longer needs anyone's attention.
        db.execute(
            update(Notification)
            .where(Notification.reminder_id.in_(rows), Notification.read_at.is_(None))
            .values(read_at=func.now())
        )
    return len(rows)


def _in_window(valid_until: date | None, today: date) -> bool:
    return valid_until is not None and today <= valid_until <= today + timedelta(
        days=WINDOW_DAYS
    )


def sync_vehicle(db: Session, vehicle: Vehicle, expiry_type: str | None = None) -> None:
    """Bring one vehicle's reminders in line with its current dates (after a
    date or the vehicle changed). Caller commits."""
    today = business_today(db)
    current = {
        e.expiry_type: e.valid_until
        for e in vehicle_repository.current_expiries(db, [vehicle.id])[vehicle.id]
    }
    types = [expiry_type] if expiry_type else list(EXPIRY_LABELS)
    for kind in types:
        valid_until = current.get(kind) if vehicle.is_active else None
        stale = and_(
            VehicleReminder.vehicle_id == vehicle.id,
            VehicleReminder.expiry_type == kind,
        )
        if valid_until is not None:
            stale = and_(stale, VehicleReminder.valid_until != valid_until)
        _supersede(
            db,
            stale,
            "Vehicle inactive" if not vehicle.is_active else "Date changed",
        )
        if _in_window(valid_until, today):
            _ensure(db, vehicle, kind, valid_until, today)


def run_daily(db: Session) -> int:
    """Maintenance job: once per business day, create the reminders now in
    the window and retire ones whose date or vehicle changed. Returns how
    many reminders were created (0 if today's run already happened)."""
    today = business_today(db)
    # Another worker is doing it right now: don't wait on its lock.
    if not db.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(DAILY_JOB)))):
        return 0
    claimed = db.scalar(
        insert(DailyRun)
        .values(name=DAILY_JOB, business_date=today)
        .on_conflict_do_nothing()
        .returning(DailyRun.name)
    )
    if claimed is None:
        return 0
    # Open reminders with no matching current date on an active vehicle.
    _supersede(
        db,
        ~exists().where(
            VehicleExpiry.vehicle_id == VehicleReminder.vehicle_id,
            VehicleExpiry.expiry_type == VehicleReminder.expiry_type,
            VehicleExpiry.valid_until == VehicleReminder.valid_until,
            VehicleExpiry.is_current.is_(True),
            Vehicle.id == VehicleExpiry.vehicle_id,
            Vehicle.is_active.is_(True),
        ),
        "Date changed",
    )
    due = db.execute(
        select(Vehicle, VehicleExpiry.expiry_type, VehicleExpiry.valid_until)
        .join(VehicleExpiry, VehicleExpiry.vehicle_id == Vehicle.id)
        .where(
            Vehicle.is_active.is_(True),
            VehicleExpiry.is_current.is_(True),
            VehicleExpiry.valid_until.between(
                today, today + timedelta(days=WINDOW_DAYS)
            ),
        )
        .order_by(Vehicle.id, VehicleExpiry.expiry_type)
    ).all()
    created = sum(_ensure(db, v, kind, until, today) for v, kind, until in due)
    # Open reminders whose responsible employee has no notification yet
    # (e.g. the vehicle was assigned after the reminder was made).
    missing = db.execute(
        select(VehicleReminder, Vehicle)
        .join(Vehicle, Vehicle.id == VehicleReminder.vehicle_id)
        .where(
            VehicleReminder.status == "open",
            Vehicle.assigned_to.is_not(None),
            ~exists().where(
                Notification.reminder_id == VehicleReminder.id,
                Notification.user_id == Vehicle.assigned_to,
            ),
        )
    ).all()
    for reminder, vehicle in missing:
        customer = db.get(Customer, vehicle.customer_id)
        _notify(
            db,
            reminder.id,
            vehicle,
            customer,
            reminder.expiry_type,
            reminder.valid_until,
            today,
        )
    return created


# ---------- reading ----------


def _visible(employee_id: int):
    return vehicle_repository.visible_to(employee_id)


def _employee_id(viewer: User) -> int | None:
    return None if job_service.is_admin(viewer) else viewer.id


def _phone(vehicle: Vehicle, customer: Customer) -> str | None:
    return whatsapp_number(vehicle.contact_phone) or whatsapp_number(customer.phone)


def _links(
    db: Session, pairs: list[tuple[Vehicle, Customer]], today: date
) -> dict[int, str | None]:
    """One WhatsApp link per vehicle, listing every open reminder of it."""
    ids = [v.id for v, _ in pairs]
    open_items: dict[int, list] = defaultdict(list)
    if ids:
        for r in db.scalars(
            select(VehicleReminder)
            .where(
                VehicleReminder.vehicle_id.in_(ids), VehicleReminder.status == "open"
            )
            .order_by(VehicleReminder.valid_until, VehicleReminder.expiry_type)
        ):
            open_items[r.vehicle_id].append(
                (_label(r.expiry_type), r.valid_until, (r.valid_until - today).days)
            )
    centre = get_settings().centre_name
    result: dict[int, str | None] = {}
    for vehicle, customer in pairs:
        number = _phone(vehicle, customer)
        items = open_items.get(vehicle.id)
        result[vehicle.id] = (
            link(
                number,
                renewal_message(
                    customer.name, vehicle.registration_number, items, centre
                ),
            )
            if number and items
            else None
        )
    return result


def _status(reminder: VehicleReminder, link_: str | None) -> ReminderStatus:
    if reminder.status == "open":
        return ReminderStatus.READY_TO_SEND if link_ else ReminderStatus.PENDING
    return ReminderStatus(reminder.status)


def _reads(db: Session, rows, today: date) -> list[ReminderRead]:
    pairs = {v.id: (v, c) for _, v, c in rows}
    links = _links(db, list(pairs.values()), today)
    users = users_by_id(
        db, {r.closed_by for r, _, _ in rows} | {v.assigned_to for _, v, _ in rows}
    )
    out = []
    for reminder, vehicle, customer in rows:
        link_ = links.get(vehicle.id) if reminder.status == "open" else None
        out.append(
            ReminderRead(
                id=reminder.id,
                vehicle=ReminderVehicleRef(
                    id=vehicle.id,
                    registration_number=vehicle.registration_number,
                    customer_id=customer.id,
                    customer_name=customer.name,
                    assigned_to=AssigneeRef.model_validate(users[vehicle.assigned_to])
                    if vehicle.assigned_to in users
                    else None,
                ),
                expiry_type=reminder.expiry_type,
                valid_until=reminder.valid_until,
                days_left=(reminder.valid_until - today).days,
                status=_status(reminder, link_),
                whatsapp_link=link_,
                created_at=reminder.created_at,
                closed_at=reminder.closed_at,
                closed_by=AssigneeRef.model_validate(users[reminder.closed_by])
                if reminder.closed_by in users
                else None,
                close_reason=reminder.close_reason,
            )
        )
    return out


def _base(employee_id: int | None):
    stmt = (
        select(VehicleReminder, Vehicle, Customer)
        .join(Vehicle, Vehicle.id == VehicleReminder.vehicle_id)
        .join(Customer, Customer.id == Vehicle.customer_id)
    )
    if employee_id is not None:
        stmt = stmt.where(_visible(employee_id))
    return stmt


def list_reminders(
    db: Session,
    viewer: User,
    *,
    page: int,
    page_size: int,
    status: ReminderStatus | None = None,
    vehicle_id: int | None = None,
    expiry_type: str | None = None,
) -> ReminderListResponse:
    today = business_today(db)
    stmt = _base(_employee_id(viewer))
    if status in (
        ReminderStatus.PENDING,
        ReminderStatus.READY_TO_SEND,
        ReminderStatus.OPEN,
    ):
        stmt = stmt.where(VehicleReminder.status == "open")
    elif status is not None:
        stmt = stmt.where(VehicleReminder.status == status.value)
    if vehicle_id is not None:
        stmt = stmt.where(VehicleReminder.vehicle_id == vehicle_id)
    if expiry_type is not None:
        stmt = stmt.where(VehicleReminder.expiry_type == expiry_type)
    stmt = stmt.order_by(VehicleReminder.valid_until, VehicleReminder.id)
    start = (page - 1) * page_size
    if status in (ReminderStatus.PENDING, ReminderStatus.READY_TO_SEND):
        # Depends on the phone number: decided per row.
        rows = [tuple(r) for r in db.execute(stmt).all()]
        matching = [i for i in _reads(db, rows, today) if i.status == status]
        items, total = matching[start : start + page_size], len(matching)
    else:
        total = (
            db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
            or 0
        )
        rows = [tuple(r) for r in db.execute(stmt.offset(start).limit(page_size)).all()]
        items = _reads(db, rows, today)
    return ReminderListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def _locked_visible(db: Session, reminder_id: int, actor: User) -> VehicleReminder:
    reminder = db.scalar(
        select(VehicleReminder)
        .where(VehicleReminder.id == reminder_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reminder is None or (
        not job_service.is_admin(actor)
        and not vehicle_repository.is_visible(db, reminder.vehicle_id, actor.id)
    ):
        raise NotFoundError(REMINDER_NOT_FOUND)
    return reminder


def _close(
    db: Session, reminder_id: int, actor: User, status: str, reason: str | None
) -> ReminderRead:
    reminder = _locked_visible(db, reminder_id, actor)
    if reminder.status != "open":
        raise BusinessRuleError(f"This reminder is already {reminder.status}.")
    reminder.status = status
    reminder.closed_at = _now(db)
    reminder.closed_by = actor.id
    reminder.close_reason = reason
    audit_service.record(
        db,
        action="reminder_completed" if status == "done" else "reminder_dismissed",
        entity_type="vehicle",
        entity_id=reminder.vehicle_id,
        user_id=actor.id,
        details={
            "reminder_id": reminder.id,
            "expiry_type": reminder.expiry_type,
            "valid_until": reminder.valid_until.isoformat(),
            **({"reason": reason} if reason else {}),
        },
    )
    # Its notification has done its job.
    db.execute(
        update(Notification)
        .where(Notification.reminder_id == reminder.id, Notification.read_at.is_(None))
        .values(read_at=func.now())
    )
    db.commit()
    vehicle = db.get(Vehicle, reminder.vehicle_id)
    customer = db.get(Customer, vehicle.customer_id)
    return _reads(db, [(reminder, vehicle, customer)], business_today(db))[0]


def mark_done(
    db: Session, reminder_id: int, actor: User, note: str | None
) -> ReminderRead:
    """The employee says they contacted the customer (e.g. sent the
    WhatsApp message). Opening the link is never taken as this."""
    return _close(db, reminder_id, actor, "done", note)


def dismiss(db: Session, reminder_id: int, actor: User, reason: str) -> ReminderRead:
    return _close(db, reminder_id, actor, "dismissed", reason)


# ---------- expiry buckets ----------


def _bucket(days_left: int) -> ExpiryBucket:
    if days_left < 0:
        return ExpiryBucket.EXPIRED
    if days_left <= 7:
        return ExpiryBucket.DAYS_0_7
    if days_left <= 15:
        return ExpiryBucket.DAYS_8_15
    if days_left <= 30:
        return ExpiryBucket.DAYS_16_30
    return ExpiryBucket.OVER_30


def _bucket_condition(bucket: ExpiryBucket, today: date):
    d = lambda n: today + timedelta(days=n)
    col = VehicleExpiry.valid_until
    return {
        ExpiryBucket.EXPIRED: col < today,
        ExpiryBucket.DAYS_0_7: col.between(today, d(7)),
        ExpiryBucket.DAYS_8_15: col.between(d(8), d(15)),
        ExpiryBucket.DAYS_16_30: col.between(d(16), d(30)),
        ExpiryBucket.OVER_30: col > d(30),
    }[bucket]


def _row_link(expiry, days: int, vehicle_link, own_link, open_keys) -> str | None:
    """Due rows (an open reminder) share the vehicle's message; expired rows
    get a message about that expiry alone; rows over 30 days get none."""
    if (expiry.vehicle_id, expiry.expiry_type, expiry.valid_until) in open_keys:
        return vehicle_link
    if days < 0:
        return own_link
    return None


def list_expiries(
    db: Session,
    viewer: User,
    *,
    page: int,
    page_size: int,
    bucket: ExpiryBucket | None = None,
    expiry_type: str | None = None,
) -> ExpiryListResponse:
    """Current validity dates of active vehicles, soonest first, with the
    count in each bucket (Expired, 0-7, 8-15, 16-30, more than 30 days)."""
    today = business_today(db)
    conditions = [
        VehicleExpiry.is_current.is_(True),
        VehicleExpiry.valid_until.is_not(None),
        Vehicle.is_active.is_(True),
    ]
    employee_id = _employee_id(viewer)
    if employee_id is not None:
        conditions.append(_visible(employee_id))
    if expiry_type is not None:
        conditions.append(VehicleExpiry.expiry_type == expiry_type)
    base = (
        select(VehicleExpiry, Vehicle, Customer)
        .join(Vehicle, Vehicle.id == VehicleExpiry.vehicle_id)
        .join(Customer, Customer.id == Vehicle.customer_id)
        .where(*conditions)
    )
    counts = {
        b.value: db.scalar(
            select(func.count())
            .select_from(VehicleExpiry)
            .join(Vehicle, Vehicle.id == VehicleExpiry.vehicle_id)
            .where(*conditions, _bucket_condition(b, today))
        )
        or 0
        for b in ExpiryBucket
    }
    if bucket is not None:
        base = base.where(_bucket_condition(bucket, today))
    total = counts[bucket.value] if bucket else sum(counts.values())
    rows = db.execute(
        base.order_by(VehicleExpiry.valid_until, Vehicle.id, VehicleExpiry.expiry_type)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    pairs = {v.id: (v, c) for _, v, c in rows}
    links = _links(db, list(pairs.values()), today)
    open_keys = {
        (r.vehicle_id, r.expiry_type, r.valid_until)
        for r in db.scalars(
            select(VehicleReminder).where(
                VehicleReminder.vehicle_id.in_(list(pairs)),
                VehicleReminder.status == "open",
            )
        )
    }
    centre = get_settings().centre_name
    items = []
    for expiry, vehicle, customer in rows:
        days = (expiry.valid_until - today).days
        number = _phone(vehicle, customer)
        own_link = (
            link(
                number,
                renewal_message(
                    customer.name,
                    vehicle.registration_number,
                    [(_label(expiry.expiry_type), expiry.valid_until, days)],
                    centre,
                ),
            )
            if number
            else None
        )
        items.append(
            ExpiryListItem(
                vehicle_id=vehicle.id,
                registration_number=vehicle.registration_number,
                customer_id=customer.id,
                customer_name=customer.name,
                expiry_type=expiry.expiry_type,
                valid_until=expiry.valid_until,
                days_left=days,
                bucket=_bucket(days),
                whatsapp_link=_row_link(
                    expiry, days, links.get(vehicle.id), own_link, open_keys
                ),
            )
        )
    return ExpiryListResponse(
        as_of=today,
        counts=counts,
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


# ---------- notifications ----------


def list_notifications(
    db: Session, viewer: User, *, unread_only: bool, page: int, page_size: int
) -> NotificationListResponse:
    conditions = [Notification.user_id == viewer.id]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))
    total = (
        db.scalar(select(func.count()).select_from(Notification).where(*conditions))
        or 0
    )
    unread = (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == viewer.id, Notification.read_at.is_(None))
        )
        or 0
    )
    rows = db.scalars(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return NotificationListResponse(
        items=[NotificationRead.model_validate(n) for n in rows],
        unread=unread,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def mark_read(db: Session, viewer: User, notification_id: int | None) -> int:
    """Mark one (or, with None, all) of the viewer's notifications read."""
    stmt = update(Notification).where(
        Notification.user_id == viewer.id, Notification.read_at.is_(None)
    )
    if notification_id is not None:
        exists_ = db.scalar(
            select(Notification.id).where(
                Notification.id == notification_id, Notification.user_id == viewer.id
            )
        )
        if exists_ is None:
            raise NotFoundError("Notification not found.")
        stmt = stmt.where(Notification.id == notification_id)
    count = len(
        db.scalars(stmt.values(read_at=func.now()).returning(Notification.id)).all()
    )
    db.commit()
    return count


def counts_for(db: Session, viewer: User) -> tuple[int, int]:
    """(open reminders the viewer can see, the viewer's unread notifications)."""
    employee_id = _employee_id(viewer)
    stmt = (
        select(func.count())
        .select_from(VehicleReminder)
        .join(Vehicle, Vehicle.id == VehicleReminder.vehicle_id)
        .where(VehicleReminder.status == "open")
    )
    if employee_id is not None:
        stmt = stmt.where(_visible(employee_id))
    unread = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == viewer.id, Notification.read_at.is_(None))
    )
    return int(db.scalar(stmt) or 0), int(unread or 0)
