import re


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def mask_phone(phone: str | None) -> str | None:
    """Show only the last four digits: '******3210'."""
    if not phone:
        return None
    d = digits(phone)
    return "*" * max(len(d) - 4, 0) + d[-4:] if len(d) > 4 else "****"
