from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VehicleReminder(Base):
    """One expiry that needs a follow-up: a vehicle's validity date of one
    type, within 30 days (Phase 4.2).

    One reminder per vehicle + type + date, so the daily check never
    creates a second one. ``open`` reminders are shown as ``ready_to_send``
    (a WhatsApp number is available) or ``pending`` (none yet). A changed
    date ``supersedes`` the open reminder for the old date.
    """

    __tablename__ = "vehicle_reminders"
    __table_args__ = (
        UniqueConstraint(
            "vehicle_id",
            "expiry_type",
            "valid_until",
            name="uq_vehicle_reminders_event",
        ),
        CheckConstraint(
            "status IN ('open', 'done', 'dismissed', 'superseded')",
            name="ck_vehicle_reminders_status",
        ),
        CheckConstraint(
            "expiry_type IN ('insurance', 'pucc', 'fitness', 'tax', 'permit')",
            name="ck_vehicle_reminders_type",
        ),
        Index(
            "ix_vehicle_reminders_open",
            "vehicle_id",
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id"), nullable=False, index=True
    )
    expiry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_vehicle_reminders_closed_by")
    )
    close_reason: Mapped[str | None] = mapped_column(Text)


NOTIFICATION_KINDS = (
    "vehicle_expiry",
    # Phase 5.1: work that needs this person.
    "job_assigned",
    "ocr_failed",
    "lookup_captcha",
    "task_failed",
)


class Notification(Base):
    """A message for one user in the app (unread until they open it).

    Each event notifies a person once: vehicle expiries by reminder, the
    rest by ``event_key``. A repeating event (tasks failing in the same
    hour) updates its one notification and counts ``occurrences``."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "reminder_id", name="uq_notifications_reminder"),
        UniqueConstraint("user_id", "event_key", name="uq_notifications_event"),
        CheckConstraint(
            "kind IN ('vehicle_expiry', 'job_assigned', 'ocr_failed', "
            "'lookup_captcha', 'task_failed')",
            name="ck_notifications_kind",
        ),
        CheckConstraint("occurrences >= 1", name="ck_notifications_occurrences"),
        Index(
            "ix_notifications_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    reminder_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicle_reminders.id", name="fk_notifications_reminder")
    )
    event_key: Mapped[str | None] = mapped_column(String(200))
    # What it is about, for a link in the app (at most one is usually set).
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", name="fk_notifications_job"), index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", name="fk_notifications_document"), index=True
    )
    vehicle_lookup_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "vehicle_lookups.id",
            name="fk_notifications_vehicle_lookup",
            ondelete="SET NULL",
        ),
        index=True,
    )
    task_type: Mapped[str | None] = mapped_column(String(50))
    occurrences: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyRun(Base):
    """Marks that a once-a-day job ran for a business date, so only one
    worker does it however many are running."""

    __tablename__ = "daily_runs"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
