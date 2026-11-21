from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.document import DocumentDetail, Reason
from app.schemas.job import AssigneeRef

FieldValue = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class OcrStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # the document type lists no OCR fields
    PENDING = "pending"  # queued or running
    COMPLETED = "completed"
    FAILED = "failed"  # review the document by hand


class FieldFlag(StrEnum):
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"  # check this one carefully
    MISSING = "missing"  # not found on the document


class OcrFieldRead(BaseModel):
    key: str
    label: str
    sensitive: bool
    # Sensitive values (e.g. Aadhaar, PAN numbers) are masked unless the
    # reviewer asks to reveal them, which is audited.
    value: str | None
    masked: bool
    confidence: float | None
    flag: FieldFlag
    # What the reviewer confirmed, if reviewed (masked the same way).
    reviewed_value: str | None


class OcrRead(BaseModel):
    document_id: int
    document_version: int
    document_status: str
    ocr_status: OcrStatus
    provider: str | None
    failure_reason: str | None
    fields: list[OcrFieldRead]
    # Raw text is only included when revealed.
    raw_text: str | None
    reviewed_by: AssigneeRef | None
    reviewed_at: datetime | None


class ReviewDecision(StrEnum):
    VERIFY = "verify"
    REJECT = "reject"


class CustomerField(StrEnum):
    NAME = "name"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"


class ApplyToCustomer(BaseModel):
    """Copy one confirmed OCR value onto the customer, explicitly."""

    model_config = ConfigDict(extra="forbid")

    customer_field: CustomerField
    ocr_field: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,49}$")]


class OcrReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The values the reviewer confirms, keyed by OCR field. Every field of
    # the document type must be given (null = not on the document).
    fields: dict[str, FieldValue | None] = Field(max_length=30)
    decision: ReviewDecision
    reason: Reason | None = None
    apply_to_customer: list[ApplyToCustomer] = Field(default=[], max_length=4)

    @model_validator(mode="after")
    def rules(self) -> "OcrReviewRequest":
        if self.decision == ReviewDecision.REJECT and not self.reason:
            raise ValueError("A rejection needs a reason.")
        if self.decision == ReviewDecision.REJECT and self.apply_to_customer:
            raise ValueError("Values from a rejected document cannot be applied.")
        targets = [a.customer_field for a in self.apply_to_customer]
        if len(targets) != len(set(targets)):
            raise ValueError("Each customer field can be applied only once.")
        return self


class OcrReviewResponse(BaseModel):
    document: DocumentDetail
    ocr: OcrRead
    customer_fields_updated: list[CustomerField]
