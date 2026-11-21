"""OCR provider adapter.

Every provider turns a file plus the list of fields a document type wants
into an ``OcrOutput``: raw text and, per field, a value and a confidence
(0..1). The real provider is chosen by the owner after testing candidates on
their own sample documents (sending identity documents to a cloud service
needs a legal/privacy review first).

``OCR_PROVIDER`` must be set explicitly; when it is not, documents are not
sent to OCR at all (they wait for manual review). ``fake`` is for
development and tests only: it trusts instructions embedded in the file.

Provider errors:
- ``OcrTemporaryError``: try again later (service busy, network) -> retried.
- ``OcrUnsupportedError``: retrying cannot help -> fails at once.
Their messages must not contain document content.
"""

import json
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    sensitive: bool = False


@dataclass(frozen=True)
class ExtractedField:
    value: str | None  # None: not found
    confidence: float | None  # 0..1; None when not found


@dataclass(frozen=True)
class OcrOutput:
    provider: str
    provider_version: str
    raw_text: str
    fields: dict[str, ExtractedField]


class OcrTemporaryError(Exception):
    pass


class OcrUnsupportedError(Exception):
    pass


class OcrProvider(Protocol):
    name: str

    def extract(
        self,
        data: bytes,
        content_type: str,
        fields: list[FieldSpec],
        timeout_seconds: float,
    ) -> OcrOutput:
        """Must give up (OcrTemporaryError) after ``timeout_seconds``."""
        ...


class FakeOcrProvider:
    """For development and tests only; never reads real documents.

    A test file may carry ``FAKEOCR:{json}`` anywhere in its bytes, e.g.
    ``{"fields": {"name": ["Ravi Kumar", 0.97]}, "raw_text": "..."}``; those
    values are returned. Anything else yields every field "not found".
    ``{"error": "temporary"|"unsupported"}`` simulates provider failures.
    """

    name = "fake"
    version = "1"
    MARKER = b"FAKEOCR:"

    def extract(
        self,
        data: bytes,
        content_type: str,
        fields: list[FieldSpec],
        timeout_seconds: float = 60,
    ) -> OcrOutput:
        script: dict = {}
        start = data.find(self.MARKER)
        if start != -1:
            try:
                script = json.loads(data[start + len(self.MARKER) :].decode())
            except (ValueError, UnicodeDecodeError):
                script = {}
        if script.get("error") == "temporary":
            raise OcrTemporaryError("OCR service is busy.")
        if script.get("error") == "unsupported":
            raise OcrUnsupportedError("The OCR service cannot read this file.")
        given = script.get("fields", {})
        extracted = {}
        for spec in fields:
            value, confidence = (given.get(spec.key) or [None, None])[:2]
            extracted[spec.key] = ExtractedField(
                value=None if value is None else str(value),
                confidence=None if value is None else float(confidence or 0),
            )
        return OcrOutput(
            provider=self.name,
            provider_version=self.version,
            raw_text=str(script.get("raw_text", "")),
            fields=extracted,
        )


_PROVIDERS = {"fake": FakeOcrProvider}


def configured() -> bool:
    """True when OCR_PROVIDER names a known provider."""
    return os.getenv("OCR_PROVIDER", "") in _PROVIDERS


def get_provider() -> OcrProvider:
    name = os.getenv("OCR_PROVIDER", "")
    if name not in _PROVIDERS:
        raise OcrUnsupportedError("No OCR provider is configured.")
    return _PROVIDERS[name]()
