from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    tokens_match,
    verify_password,
)
from app.models import User, UserSession
from app.repositories import user_repository as repo
from app.services import audit_service
from app.services.errors import (
    AuthenticationError,
    BusinessRuleError,
    PermissionDeniedError,
)

INVALID_LOGIN = "Invalid email or password."
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class LoginResult:
    user: User
    session_token: str
    csrf_token: str


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession


def _now() -> datetime:
    return datetime.now(UTC)


def login(
    db: Session, email: str, password: str, previous_token: str | None = None
) -> LoginResult:
    """Sign in. Any session the browser already had is revoked first, so
    signing in again (e.g. another person on a shared PC) never leaves the
    earlier session usable."""
    if previous_token:
        repo.revoke_session_by_token_hash(db, hash_token(previous_token), _now())
    user = repo.get_user_by_email(db, email)
    # Always verify a hash so timing does not reveal whether the email exists.
    valid = verify_password(user.password_hash if user else None, password)

    if user is None or not valid or not user.is_active:
        audit_service.record(
            db,
            action="login_failed",
            entity_type="user",
            entity_id=user.id if user else None,
            user_id=user.id if user else None,
            details={"reason": "inactive" if valid and user else "invalid_credentials"},
        )
        db.commit()
        raise AuthenticationError(INVALID_LOGIN)

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    session_token, csrf_token = generate_token(), generate_token()
    repo.add_session(
        db,
        UserSession(
            user_id=user.id,
            token_hash=hash_token(session_token),
            csrf_token_hash=hash_token(csrf_token),
            expires_at=_now() + timedelta(hours=get_settings().session_ttl_hours),
        ),
    )
    audit_service.record(
        db,
        action="login_succeeded",
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
    )
    db.commit()
    return LoginResult(user=user, session_token=session_token, csrf_token=csrf_token)


def authenticate(
    db: Session, session_token: str | None, method: str, csrf_token: str | None
) -> AuthContext:
    """Resolve the session cookie to an active user and enforce CSRF."""
    if not session_token:
        raise AuthenticationError("Not authenticated.")
    found = repo.get_active_session(db, hash_token(session_token), _now())
    if found is None or not found[1].is_active:
        raise AuthenticationError("Not authenticated.")
    session, user = found
    if method.upper() not in SAFE_METHODS and not (
        csrf_token and tokens_match(csrf_token, session.csrf_token_hash)
    ):
        raise PermissionDeniedError("Missing or invalid CSRF token.")
    return AuthContext(user=user, session=session)


def logout(db: Session, ctx: AuthContext) -> None:
    ctx.session.revoked_at = _now()
    audit_service.record(
        db,
        action="logout",
        entity_type="user",
        entity_id=ctx.user.id,
        user_id=ctx.user.id,
    )
    db.commit()


def change_password(
    db: Session, ctx: AuthContext, current_password: str, new_password: str
) -> User:
    user = ctx.user
    if not verify_password(user.password_hash, current_password):
        # Committed before raising so repeated guessing from a stolen or
        # unattended session leaves a trace.
        audit_service.record(
            db,
            action="password_change_failed",
            entity_type="user",
            entity_id=user.id,
            user_id=user.id,
        )
        db.commit()
        raise BusinessRuleError("Current password is incorrect.")
    if current_password == new_password:
        raise BusinessRuleError("New password must be different.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    # Sign out every other session; keep the one making this request.
    repo.revoke_sessions(db, user.id, _now(), except_session_id=ctx.session.id)
    audit_service.record(
        db,
        action="password_changed",
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
    )
    db.commit()
    return user
