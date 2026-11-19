from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import User, UserSession


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email.lower()))


def email_taken(db: Session, email: str, exclude_user_id: int | None = None) -> bool:
    stmt = select(User.id).where(func.lower(User.email) == email.lower())
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return db.scalar(stmt) is not None


def lock_active_admin_ids(db: Session) -> list[int]:
    """Active admin ids, row-locked until the transaction ends.

    Serialises concurrent changes that could remove the last active admin.
    """
    return list(
        db.scalars(
            select(User.id)
            .where(User.role == "admin", User.is_active.is_(True))
            .order_by(User.id)
            .with_for_update()
        )
    )


def add_user(db: Session, user: User) -> User:
    db.add(user)
    db.flush()
    return user


def list_users(
    db: Session,
    *,
    offset: int,
    limit: int,
    search: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
) -> tuple[list[User], int]:
    conditions: list[ColumnElement[bool]] = []
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions.append(
            or_(
                User.name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
            )
        )
    if role is not None:
        conditions.append(User.role == role)
    if is_active is not None:
        conditions.append(User.is_active.is_(is_active))
    total = db.scalar(select(func.count()).select_from(User).where(*conditions))
    users = db.scalars(
        select(User).where(*conditions).order_by(User.id).offset(offset).limit(limit)
    )
    return list(users), int(total or 0)


def add_session(db: Session, session: UserSession) -> UserSession:
    db.add(session)
    db.flush()
    return session


def get_active_session(
    db: Session, token_hash: str, now: datetime
) -> tuple[UserSession, User] | None:
    row = db.execute(
        select(UserSession, User)
        .join(User, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
    ).first()
    return (row[0], row[1]) if row else None


def revoke_sessions(
    db: Session, user_id: int, now: datetime, except_session_id: int | None = None
) -> None:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    db.execute(stmt.values(revoked_at=now))
