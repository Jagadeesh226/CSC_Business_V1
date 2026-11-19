from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, model_validator

# Matches users.name VARCHAR(150).
UserName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)
]
# Long enough to be meaningful, short enough to bound hashing cost.
Password = Annotated[str, StringConstraints(min_length=10, max_length=128)]


class UserRole(StrEnum):
    ADMIN = "admin"
    EMPLOYEE = "employee"


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: UserRole
    is_active: bool
    must_change_password: bool
    created_at: datetime


class UserCreate(BaseModel):
    """Admin creates an account with a temporary password the user must change."""

    model_config = ConfigDict(extra="forbid")

    name: UserName
    email: EmailStr
    role: UserRole = UserRole.EMPLOYEE
    temporary_password: Password


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: UserName | None = None
    email: EmailStr | None = None
    role: UserRole | None = None

    @model_validator(mode="after")
    def no_nulls(self) -> "UserUpdate":
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temporary_password: Password


class UserListResponse(BaseModel):
    items: list[UserRead]
    total: int
    page: int
    page_size: int
    total_pages: int
