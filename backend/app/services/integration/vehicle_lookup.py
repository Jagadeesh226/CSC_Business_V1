"""Vehicle lookup adapter (Phase 4.4): validity dates and pending challans
for a registration number, from an authorised source.

A provider either answers, or asks for a CAPTCHA that a person must solve
(the system never solves one), or fails. The worker calls it; nothing
here commits or talks to the database.

``VEHICLE_LOOKUP_PROVIDER`` must be set explicitly; without it lookups are
refused. Only ``fake`` exists: it is for development and tests. A real
provider (e.g. browser automation of the VAHAN citizen portal) is added only
after the owner has confirmed the portal's terms allow it; it must keep any
browser session it needs for a CAPTCHA itself, identified by the opaque
``state`` it returns, and must never bypass CAPTCHA, OTP or rate limits.

Errors:
- ``LookupTemporaryError``: try again later (portal busy, network).
- ``LookupFailedError``: retrying cannot help (not found, blocked). Its
  message is shown to staff, so it must be a fixed, safe sentence.
"""

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

EXPIRY_TYPES = ("insurance", "pucc", "fitness", "tax", "permit")


@dataclass(frozen=True)
class LookupRequest:
    registration_number: str
    chassis_number: str | None = None
    engine_number: str | None = None


@dataclass(frozen=True)
class LookupResult:
    provider: str
    # Validity dates found, by expiry type (only the ones the source gave).
    expiries: dict[str, date] = field(default_factory=dict)
    # "none" or "pending"; None when the source did not say.
    challan_status: str | None = None
    challan_count: int | None = None


@dataclass(frozen=True)
class CaptchaNeeded:
    provider: str
    image_png: bytes  # shown to the employee as-is
    # Provider-specific, opaque; kept encrypted and handed back on resume.
    state: dict


class LookupTemporaryError(Exception):
    pass


class LookupFailedError(Exception):
    pass


class LookupProvider(Protocol):
    name: str

    def start(
        self, request: LookupRequest, today: date
    ) -> LookupResult | CaptchaNeeded:
        """Begin a lookup."""

    def resume(
        self, request: LookupRequest, state: dict, answer: str, today: date
    ) -> LookupResult | CaptchaNeeded:
        """Continue after a person typed the CAPTCHA answer."""


# ---------- fake provider (development and tests) ----------

# A 1x1 PNG: the "CAPTCHA image" of the fake provider.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
FAKE_CAPTCHA_ANSWER = "ABCD"


class FakeLookupProvider:
    """Deterministic answers from the registration key (letters and
    digits only), for development and tests:

    - ending in ``0000``: not found (fails, no retry);
    - containing ``TMP``: the portal is busy (temporary error);
    - containing ``CAP``: asks for a CAPTCHA first; the answer is ``ABCD``,
      a wrong answer asks again;
    - otherwise: insurance and PUCC dates worked out from the number,
      challans pending when the last digit is odd.
    """

    name = "fake"

    def _key(self, request: LookupRequest) -> str:
        return "".join(c for c in request.registration_number.upper() if c.isalnum())

    def _answer(self, key: str, today: date) -> LookupResult:
        if key.endswith("0000"):
            raise LookupFailedError("No vehicle with this registration was found.")
        seed = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        digits = [int(c) for c in key if c.isdigit()] or [0]
        return LookupResult(
            provider=self.name,
            expiries={
                "insurance": today + timedelta(days=seed % 90 - 15),
                "pucc": today + timedelta(days=seed // 90 % 180),
            },
            challan_status="pending" if digits[-1] % 2 else "none",
            challan_count=digits[-1] % 2,
        )

    def start(self, request: LookupRequest, today: date):
        key = self._key(request)
        if "TMP" in key:
            raise LookupTemporaryError("The vehicle portal is busy; try again later.")
        if "CAP" in key:
            return CaptchaNeeded(self.name, _PNG, {"key": key, "tries": 1})
        return self._answer(key, today)

    def resume(self, request: LookupRequest, state: dict, answer: str, today: date):
        if answer.strip().upper() != FAKE_CAPTCHA_ANSWER:
            return CaptchaNeeded(
                self.name, _PNG, {"key": state["key"], "tries": state["tries"] + 1}
            )
        return self._answer(state["key"], today)


_PROVIDERS: dict[str, type] = {"fake": FakeLookupProvider}


def configured() -> bool:
    return os.getenv("VEHICLE_LOOKUP_PROVIDER", "") in _PROVIDERS


def get_provider() -> LookupProvider:
    name = os.getenv("VEHICLE_LOOKUP_PROVIDER", "")
    if name not in _PROVIDERS:
        raise LookupFailedError("Vehicle lookups are not set up.")
    return _PROVIDERS[name]()
