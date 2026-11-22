"""How much of a job is paid. Kept apart from payment_service so the job
read model can use it without an import cycle."""

from decimal import Decimal

from app.schemas.money import PaymentState, PaymentSummary

ZERO = Decimal("0.00")


def summarize(fees_total: Decimal, paid: Decimal) -> PaymentSummary:
    if fees_total == ZERO and paid == ZERO:
        state = PaymentState.NO_FEES
    elif paid == ZERO:
        state = PaymentState.UNPAID
    elif paid < fees_total:
        state = PaymentState.PARTIAL
    elif paid == fees_total:
        state = PaymentState.PAID
    else:
        state = PaymentState.OVERPAID
    q = Decimal("0.01")
    return PaymentSummary(
        fees_total=fees_total.quantize(q),
        paid=paid.quantize(q),
        balance=(fees_total - paid).quantize(q),
        state=state,
    )
