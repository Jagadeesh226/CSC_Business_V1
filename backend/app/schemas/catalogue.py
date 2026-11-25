from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# Upper-case codes such as AADHAAR, PAN_CARD, RC-RENEWAL. Input is trimmed and
# upper-cased before the pattern is checked, so "aadhaar" becomes "AADHAAR".
Code = Annotated[
    str,
    BeforeValidator(lambda v: v.strip().upper() if isinstance(v, str) else v),
    StringConstraints(pattern=r"^[A-Z0-9_-]{2,50}$"),
]
Name150 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)
]
Name200 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Text5000 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
SortOrder = Annotated[int, Field(ge=0, le=10_000)]
Money = Annotated[
    Decimal,
    Field(ge=0, max_digits=12, decimal_places=2),
    AfterValidator(lambda v: v.quantize(Decimal("0.01"))),
]
ResourceId = Annotated[int, Field(gt=0, le=2**31 - 1)]


class _NoNulls(BaseModel):
    """Partial updates: a field may be omitted but not set to null."""

    _nullable: tuple[str, ...] = ()

    @model_validator(mode="after")
    def no_nulls(self):
        for field in self.model_fields_set:
            if field not in self._nullable and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class CategoryRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name150
    code: Code
    description: Text5000 | None = None
    sort_order: SortOrder = 0


class CategoryUpdate(_NoNulls):
    model_config = ConfigDict(extra="forbid")
    _nullable = ("description",)

    name: Name150 | None = None
    description: Text5000 | None = None
    is_active: bool | None = None
    sort_order: SortOrder | None = None


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    description: str | None
    is_active: bool
    sort_order: int


class ServiceFeeType(StrEnum):
    FIXED = "fixed"
    VARIABLE = "variable"


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: ResourceId
    name: Name200
    code: Code
    description: Text5000 | None = None
    fee_type: ServiceFeeType = ServiceFeeType.FIXED
    default_service_fee: Money | None = None
    requires_vehicle: bool = False
    sort_order: SortOrder = 0

    @model_validator(mode="after")
    def fixed_needs_fee(self) -> "ServiceCreate":
        if self.fee_type == ServiceFeeType.FIXED and self.default_service_fee is None:
            raise ValueError("A fixed-fee service needs a default_service_fee.")
        return self


class ServiceUpdate(_NoNulls):
    model_config = ConfigDict(extra="forbid")
    _nullable = ("description", "default_service_fee")

    category_id: ResourceId | None = None
    name: Name200 | None = None
    description: Text5000 | None = None
    fee_type: ServiceFeeType | None = None
    default_service_fee: Money | None = None
    requires_vehicle: bool | None = None
    is_active: bool | None = None
    sort_order: SortOrder | None = None


class ServiceRead(BaseModel):
    id: int
    category: CategoryRef
    name: str
    code: str
    description: str | None
    fee_type: ServiceFeeType
    default_service_fee: Decimal | None
    requires_vehicle: bool
    is_active: bool
    sort_order: int


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"  # e.g. Aadhaar, PAN: stricter handling


class OcrField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,49}$")]
    label: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    # Sensitive values (Aadhaar/PAN numbers) are masked outside the review screen.
    sensitive: bool = False


def _unique_keys(fields: list[OcrField]) -> list[OcrField]:
    keys = [f.key for f in fields]
    if len(keys) != len(set(keys)):
        raise ValueError("OCR field keys must be unique.")
    return fields


OcrFields = Annotated[
    list[OcrField], Field(max_length=30), AfterValidator(_unique_keys)
]


class VehicleRole(StrEnum):
    # OCR of this document creates or fills the customer's vehicle. Fields
    # it uses (by OCR key): registration_number (required), chassis_number,
    # engine_number, owner_name, vehicle_class.
    REGISTRATION_CERTIFICATE = "registration_certificate"


RC_REQUIRED_KEY = "registration_number"


def check_vehicle_role(role: "VehicleRole | None", fields: list) -> None:
    keys = {f["key"] if isinstance(f, dict) else f.key for f in fields or []}
    if role is not None and RC_REQUIRED_KEY not in keys:
        raise ValueError(
            "A registration certificate type needs an OCR field with key "
            f"'{RC_REQUIRED_KEY}'."
        )


class DocumentTypeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Code
    name: Name150
    description: Text5000 | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    ocr_fields: OcrFields = []
    vehicle_role: VehicleRole | None = None
    sort_order: SortOrder = 0

    @model_validator(mode="after")
    def role_needs_registration(self) -> "DocumentTypeCreate":
        check_vehicle_role(self.vehicle_role, self.ocr_fields)
        return self


class DocumentTypeUpdate(_NoNulls):
    model_config = ConfigDict(extra="forbid")
    _nullable = ("description", "vehicle_role")

    name: Name150 | None = None
    description: Text5000 | None = None
    sensitivity: Sensitivity | None = None
    ocr_fields: OcrFields | None = None
    vehicle_role: VehicleRole | None = None
    is_active: bool | None = None
    sort_order: SortOrder | None = None


class DocumentTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    sensitivity: Sensitivity
    ocr_fields: list[OcrField]
    vehicle_role: VehicleRole | None = None
    is_active: bool
    sort_order: int


class DocumentTypeRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    sensitivity: Sensitivity


class RequirementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type_id: ResourceId
    is_required: bool = True
    allow_multiple: bool = False
    description: Text5000 | None = None
    sort_order: SortOrder = 0


class RequirementUpdate(_NoNulls):
    model_config = ConfigDict(extra="forbid")
    _nullable = ("description",)

    is_required: bool | None = None
    allow_multiple: bool | None = None
    is_active: bool | None = None
    description: Text5000 | None = None
    sort_order: SortOrder | None = None


class RequirementRead(BaseModel):
    id: int
    document_type: DocumentTypeRef
    is_required: bool
    allow_multiple: bool
    is_active: bool
    description: str | None
    sort_order: int
    created_at: datetime
