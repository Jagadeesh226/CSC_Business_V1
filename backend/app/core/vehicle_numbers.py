"""Vehicle registration, chassis and engine numbers: normalising and
masking."""

import re

_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize(value: str | None) -> str:
    """'ka-01 ab 1234' -> 'KA01AB1234' (letters and digits, upper case)."""
    return _NOT_ALNUM.sub("", (value or "").upper())


def looks_like_registration(query: str) -> bool:
    """A search term that could be (part of) a registration number: at
    least four letters/digits, containing both a letter and a digit."""
    key = normalize(query)
    return (
        len(key) >= 4
        and any(c.isdigit() for c in key)
        and any(c.isalpha() for c in key)
    )


def mask_identifier(last4: str | None) -> str | None:
    """Chassis/engine numbers in lists: only the last four characters."""
    return None if last4 is None else f"****{last4}"
