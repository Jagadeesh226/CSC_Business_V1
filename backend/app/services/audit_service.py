import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit entry to the current transaction (the caller commits).

    Never put passwords, tokens or document contents in ``details``.
    """
    db.add(
        AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            user_id=user_id,
            details=json.dumps(details, default=str, sort_keys=True)
            if details
            else None,
        )
    )
