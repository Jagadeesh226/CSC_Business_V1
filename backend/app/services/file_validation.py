"""Upload checks: allow-listed types by content signature, size and pages."""

import hashlib
import io
import re
import threading
from dataclasses import dataclass
from pathlib import PurePath

from pypdf import PdfReader

from app.services.errors import BusinessRuleError, ServiceError

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PDF_PAGES = 20
# Parsing a hostile PDF can take a lot of memory; parse a few at a time.
_PDF_PARSE_SLOTS = threading.BoundedSemaphore(2)

# (content type, allowed extensions, signature check)
_TYPES = (
    ("application/pdf", {".pdf"}, lambda b: b.startswith(b"%PDF-")),
    ("image/png", {".png"}, lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ("image/jpeg", {".jpg", ".jpeg"}, lambda b: b.startswith(b"\xff\xd8\xff")),
)


class FileTooLargeError(ServiceError):
    status_code = 413


@dataclass(frozen=True)
class CheckedFile:
    data: bytes
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    page_count: int | None


def safe_filename(name: str | None) -> str:
    """Base name only, with unusual characters replaced; never empty."""
    base = PurePath((name or "").replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._ ()-]", "_", base).strip(" .")
    return (cleaned or "document")[-255:]


def check_upload(data: bytes, filename: str | None) -> CheckedFile:
    if len(data) > MAX_BYTES:
        raise FileTooLargeError("Files can be at most 10 MB.")
    if not data:
        raise BusinessRuleError("The file is empty.")
    name = safe_filename(filename)
    extension = PurePath(name).suffix.lower()
    for content_type, extensions, matches in _TYPES:
        if matches(data):
            if extension not in extensions:
                raise BusinessRuleError(
                    "The file name extension does not match its contents."
                )
            break
    else:
        raise BusinessRuleError("Only PDF, PNG and JPEG files are accepted.")

    page_count = None
    if content_type == "application/pdf":
        try:
            with _PDF_PARSE_SLOTS:
                reader = PdfReader(io.BytesIO(data))
                if reader.is_encrypted:
                    raise BusinessRuleError("Password-protected PDFs are not accepted.")
                # The declared page count is checked before walking the page
                # tree, so an oversized PDF is refused cheaply.
                declared = reader.trailer["/Root"]["/Pages"].get("/Count", 0)
                if not isinstance(declared, int) or declared > MAX_PDF_PAGES:
                    raise BusinessRuleError(
                        f"PDFs can have at most {MAX_PDF_PAGES} pages."
                    )
                page_count = len(reader.pages)
        except BusinessRuleError:
            raise
        except Exception as exc:  # any parse failure of an untrusted file
            raise BusinessRuleError("The PDF could not be read.") from exc
        if page_count > MAX_PDF_PAGES:
            raise BusinessRuleError(f"PDFs can have at most {MAX_PDF_PAGES} pages.")

    return CheckedFile(
        data=data,
        filename=name,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        page_count=page_count,
    )
