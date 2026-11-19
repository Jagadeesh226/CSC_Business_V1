import math
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import User
from app.repositories import user_repository as repo
from app.schemas.user import (
    UserCreate,
    UserListResponse,
    UserRead,
    UserRole,
    UserUpdate,
)
from app.services import audit_service
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError

EMAIL_TAKEN = "A user with this email already exists."


def _get(db: Session, user_id: int) -> User:
    user = repo.get_user(db, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    return user


def _ensure_not_last_admin(db: Session, user: User) -> None:
    """Refuse a change that would leave no active admin."""
    admin_ids = repo.lock_active_admin_ids(db)
    if admin_ids == [user.id]:
        raise BusinessRuleError("At least one active admin is required.")


def create_admin(db: Session, *, name: str, email: str, password: str) -> User:
    """Bootstrap an admin from the command line (no actor exists yet)."""
    if repo.email_taken(db, email):
        raise ConflictError(EMAIL_TAKEN)
    user = repo.add_user(
        db,
        User(
            name=name,
            email=email.lower(),
            password_hash=hash_password(password),
            role=UserRole.ADMIN.value,
        ),
    )
    audit_service.record(
        db,
        action="user_created",
        entity_type="user",
        entity_id=user.id,
        details={"role": user.role, "via": "cli"},
    )
    db.commit()
    return user


def create_user(db: Session, actor: User, data: UserCreate) -> UserRead:
    if repo.email_taken(db, data.email):
        raise ConflictError(EMAIL_TAKEN)
    try:
        user = repo.add_user(
            db,
            User(
                name=data.name,
                email=str(data.email).lower(),
                password_hash=hash_password(data.temporary_password),
                role=data.role.value,
                must_change_password=True,
            ),
        )
        audit_service.record(
            db,
            action="user_created",
            entity_type="user",
            entity_id=user.id,
            user_id=actor.id,
            details={"role": user.role},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(EMAIL_TAKEN) from None
    return UserRead.model_validate(user)


def get_user(db: Session, user_id: int) -> UserRead:
    return UserRead.model_validate(_get(db, user_id))


def list_users(
    db: Session,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> UserListResponse:
    search = search.strip() if search else None
    users, total = repo.list_users(
        db,
        offset=(page - 1) * page_size,
        limit=page_size,
        search=search or None,
        role=role.value if role else None,
        is_active=is_active,
    )
    return UserListResponse(
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def update_user(db: Session, actor: User, user_id: int, data: UserUpdate) -> UserRead:
    user = _get(db, user_id)
    changes = data.model_dump(exclude_unset=True)
    if "email" in changes:
        changes["email"] = str(changes["email"]).lower()
        if repo.email_taken(db, changes["email"], exclude_user_id=user.id):
            raise ConflictError(EMAIL_TAKEN)
    if "role" in changes:
        changes["role"] = changes["role"].value
        if changes["role"] != user.role and user.role == UserRole.ADMIN.value:
            if user.id == actor.id:
                raise BusinessRuleError("You cannot remove your own admin role.")
            if user.is_active:
                _ensure_not_last_admin(db, user)

    changed = {k: v for k, v in changes.items() if getattr(user, k) != v}
    previous = {k: getattr(user, k) for k in changed}
    try:
        for field, value in changed.items():
            setattr(user, field, value)
        if changed:
            audit_service.record(
                db,
                action="user_updated",
                entity_type="user",
                entity_id=user.id,
                user_id=actor.id,
                details=_change_details(user, changed, previous),
            )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(EMAIL_TAKEN) from None
    return UserRead.model_validate(user)


# Fields whose old and new values are recorded in the audit log: role changes
# (promotion/demotion) and email (the login identifier).
_AUDITED_VALUES = ("role", "email")


def _change_details(
    user: User, changed: dict[str, object], previous: dict[str, object]
) -> dict[str, object]:
    details: dict[str, object] = {"fields": sorted(changed)}
    for field in _AUDITED_VALUES:
        if field in changed:
            details[field] = {"old": previous[field], "new": getattr(user, field)}
    return details


def deactivate_user(db: Session, actor: User, user_id: int) -> UserRead:
    user = _get(db, user_id)
    if user.id == actor.id:
        raise BusinessRuleError("You cannot deactivate your own account.")
    if user.is_active:
        if user.role == UserRole.ADMIN.value:
            _ensure_not_last_admin(db, user)
        user.is_active = False
        repo.revoke_sessions(db, user.id, datetime.now(UTC))
        audit_service.record(
            db,
            action="user_deactivated",
            entity_type="user",
            entity_id=user.id,
            user_id=actor.id,
        )
        db.commit()
    return UserRead.model_validate(user)


def reactivate_user(db: Session, actor: User, user_id: int) -> UserRead:
    user = _get(db, user_id)
    if not user.is_active:
        user.is_active = True
        audit_service.record(
            db,
            action="user_reactivated",
            entity_type="user",
            entity_id=user.id,
            user_id=actor.id,
        )
        db.commit()
    return UserRead.model_validate(user)


def reset_password(
    db: Session, actor: User, user_id: int, temporary_password: str
) -> UserRead:
    user = _get(db, user_id)
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    repo.revoke_sessions(db, user.id, datetime.now(UTC))
    audit_service.record(
        db,
        action="password_reset",
        entity_type="user",
        entity_id=user.id,
        user_id=actor.id,
    )
    db.commit()
    return UserRead.model_validate(user)
