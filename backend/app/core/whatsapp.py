"""WhatsApp click-to-chat links (https://wa.me/<number>?text=<message>).

Opening a link only opens a chat with the message filled in; nothing is
sent automatically, and opening it is never taken as "sent".
"""

from datetime import date
from urllib.parse import quote

from app.core.masking import digits


def whatsapp_number(phone: str | None) -> str | None:
    """'+91 98765 43210', '9876543210' -> '919876543210'.

    Only Indian mobile numbers (10 digits starting 6-9, optionally with 91)
    get a link. Numbers written with a leading 0 are refused: "080 2345
    6789" (a landline with its STD code) looks exactly like a mobile, and a
    wrong guess would send a customer's details to a stranger."""
    d = digits(phone)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) == 10 and d[0] in "6789":
        return "91" + d
    return None


def describe(label: str, valid_until: date, days_left: int) -> str:
    when = valid_until.strftime("%d-%m-%Y")
    if days_left < 0:
        return f"{label} expired on {when}"
    if days_left == 0:
        return f"{label} expires today ({when})"
    unit = "day" if days_left == 1 else "days"
    return f"{label} expires on {when} ({days_left} {unit} left)"


def renewal_message(
    customer_name: str,
    registration_number: str,
    items: list[tuple[str, date, int]],
    centre_name: str,
) -> str:
    """One message for a vehicle, listing every expiry to follow up."""
    lines = "; ".join(describe(label, when, left) for label, when, left in items)
    return (
        f"Hello {customer_name}, for your vehicle {registration_number}: {lines}. "
        f"Please contact us for renewal. - {centre_name}"
    )


def link(number: str, message: str) -> str:
    return f"https://wa.me/{number}?text={quote(message, safe='')}"
