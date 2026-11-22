from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Payment
from app.repositories.readiness_repository import BUSINESS_TIMEZONE


def _start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(BUSINESS_TIMEZONE))


def payment_totals(
    db: Session, date_from: date, date_to: date
) -> list[tuple[str, date, str, int | None, int, Decimal]]:
    """(status, business day, method, recorded_by, count, total) for
    payments received in the period. Whole days in the centre's calendar,
    compared as instants so the received_at index can be used."""
    day = func.date(func.timezone(BUSINESS_TIMEZONE, Payment.received_at))
    rows = db.execute(
        select(
            Payment.status,
            day,
            Payment.payment_method,
            Payment.recorded_by,
            func.count(),
            func.sum(Payment.amount),
        )
        .where(
            Payment.received_at >= _start(date_from),
            Payment.received_at < _start(date_to + timedelta(days=1)),
        )
        .group_by(Payment.status, day, Payment.payment_method, Payment.recorded_by)
    ).all()
    return [tuple(r) for r in rows]


def voids_in_period(db: Session, date_from: date, date_to: date) -> tuple[int, Decimal]:
    """Payments voided during the period (by voided_at)."""
    count, total = db.execute(
        select(func.count(), func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == "voided",
            Payment.voided_at >= _start(date_from),
            Payment.voided_at < _start(date_to + timedelta(days=1)),
        )
    ).one()
    return int(count), Decimal(total).quantize(Decimal("0.01"))
