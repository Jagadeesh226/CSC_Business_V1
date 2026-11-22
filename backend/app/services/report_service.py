"""Reports for admins. Collections are money received, never "profit"."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.repositories import report_repository
from app.repositories.document_repository import users_by_id
from app.repositories.readiness_repository import business_today
from app.schemas.job import AssigneeRef
from app.schemas.payment import PaymentMethod
from app.schemas.report import (
    CollectionsDay,
    CollectionsMethodTotal,
    CollectionsReceiverTotal,
    CollectionsReport,
    CollectionsVoided,
)
from app.services.errors import BusinessRuleError

MAX_DAYS = 366
ZERO = Decimal("0.00")


def _methods(totals: dict[str, list]) -> list[CollectionsMethodTotal]:
    # Fixed order (as in PaymentMethod), only methods that were used.
    return [
        CollectionsMethodTotal(
            method=m, count=totals[m.value][0], total=totals[m.value][1]
        )
        for m in PaymentMethod
        if m.value in totals
    ]


def collections(
    db: Session, date_from: date | None, date_to: date | None
) -> CollectionsReport:
    date_to = date_to or business_today(db)
    date_from = date_from or date_to
    if date_from > date_to:
        raise BusinessRuleError("date_from must be on or before date_to.")
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise BusinessRuleError(f"Choose a period of at most {MAX_DAYS} days.")

    count, total = 0, ZERO
    methods: dict[str, list] = defaultdict(lambda: [0, ZERO])
    days: dict[date, list] = defaultdict(
        lambda: [0, ZERO, defaultdict(lambda: [0, ZERO])]
    )
    receivers: dict[int | None, list] = defaultdict(lambda: [0, ZERO])
    voided = [0, ZERO]
    for status, day, method, user_id, n, amount in report_repository.payment_totals(
        db, date_from, date_to
    ):
        amount = Decimal(amount)
        if status != "received":
            voided[0] += n
            voided[1] += amount
            continue
        count += n
        total += amount
        for bucket in (methods[method], receivers[user_id], days[day]):
            bucket[0] += n
            bucket[1] += amount
        days[day][2][method][0] += n
        days[day][2][method][1] += amount

    users = users_by_id(db, {u for u in receivers if u is not None})
    by_receiver = sorted(
        (
            CollectionsReceiverTotal(
                received_by=AssigneeRef.model_validate(users[u])
                if u in users
                else None,
                count=n,
                total=t,
            )
            for u, (n, t) in receivers.items()
        ),
        key=lambda r: (-r.total, r.received_by.name if r.received_by else ""),
    )
    return CollectionsReport(
        date_from=date_from,
        date_to=date_to,
        count=count,
        total=total,
        by_method=_methods(methods),
        by_day=[
            CollectionsDay(date=d, count=n, total=t, by_method=_methods(m))
            for d, (n, t, m) in sorted(days.items())
        ],
        by_receiver=by_receiver,
        voided=CollectionsVoided(count=voided[0], total=voided[1]),
    )
