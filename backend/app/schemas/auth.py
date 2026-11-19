from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from app.schemas.user import Password, UserRead


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # No minimum here: a wrong password should fail as a login, not a 422.
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class LoginResponse(BaseModel):
    user: UserRead
    # Also set as a readable cookie. Send it back in the X-CSRF-Token header
    # on every POST, PATCH, PUT and DELETE request.
    csrf_token: str


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    new_password: Password
