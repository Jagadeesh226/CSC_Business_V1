from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models import User
from app.schemas.error import ErrorResponse
from app.schemas.user import UserRole
from app.services import auth_service
from app.services.auth_service import AuthContext
from app.services.errors import PermissionDeniedError

DbSession = Annotated[Session, Depends(get_db)]


def get_auth_context(request: Request, db: DbSession) -> AuthContext:
    """Any signed-in active user, even one who must still change their password."""
    settings = get_settings()
    return auth_service.authenticate(
        db,
        session_token=request.cookies.get(settings.session_cookie_name),
        method=request.method,
        csrf_token=request.headers.get(settings.csrf_header_name),
    )


Auth = Annotated[AuthContext, Depends(get_auth_context)]


def get_current_user(ctx: Auth) -> User:
    """A signed-in active user who has set their own password."""
    if ctx.user.must_change_password:
        raise PermissionDeniedError("You must change your password first.")
    return ctx.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN.value:
        raise PermissionDeniedError("Admin access required.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]

# OpenAPI documentation for protected routes.
AUTH_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Not signed in or session expired"},
    403: {"model": ErrorResponse, "description": "Not allowed, or CSRF missing"},
}
