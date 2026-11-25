from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.job import AssigneeRef


class IntakeStatus(StrEnum):
    APPLIED = "applied"  # vehicle created or filled; nothing to check
    NO_CHANGES = "no_changes"  # the vehicle already had these values
    NEEDS_CHECK = "needs_check"  # some fields wait for a person
    CONFLICT = "conflict"  # registration used by another customer's vehicle
    RESOLVED = "resolved"  # a person finished it


class IntakeField(StrEnum):
    REGISTRATION_NUMBER = "registration_number"
    OWNER_NAME = "owner_name"
    CHASSIS_NUMBER = "chassis_number"
    ENGINE_NUMBER = "engine_number"


class IntakeCheckField(BaseModel):
    key: str
    # missing | low_confidence | invalid | unusual_length (chassis not 17)
    reason: str
    # What OCR read (chassis/engine masked unless revealed).
    read_value: str | None


class IntakePendingChange(BaseModel):
    key: str
    current: str | None
    proposed: str | None


class IntakeVehicleRef(BaseModel):
    id: int
    registration_number: str
    customer_id: int


class IntakeRead(BaseModel):
    document_id: int
    document_version: int
    status: IntakeStatus
    vehicle: IntakeVehicleRef | None
    check_fields: list[IntakeCheckField]
    pending_changes: list[IntakePendingChange]
    values_masked: bool
    created_at: datetime
    resolved_by: AssigneeRef | None
    resolved_at: datetime | None


Value = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class IntakeResolve(BaseModel):
    """The person's answer: values for doubtful fields (null leaves a field
    empty), and whether readings that differ from the vehicle replace the
    current values."""

    model_config = ConfigDict(extra="forbid")

    values: dict[IntakeField, Value | None] = Field(default_factory=dict)
    accept_changes: bool = False
