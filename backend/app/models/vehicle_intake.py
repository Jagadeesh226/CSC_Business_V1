from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VehicleIntake(Base):
    """What happened when OCR read a registration certificate (one row per
    document version).

    - ``applied``: the vehicle was created or filled; nothing needs a person.
    - ``no_changes``: the vehicle already had exactly these values.
    - ``needs_check``: some fields were doubtful (``check_fields``) or differ
      from values already on the vehicle (``pending_changes``); those wait
      for a person, everything else was applied.
    - ``conflict``: the registration belongs to another customer's vehicle.
    - ``resolved``: a person finished it.

    OCR's reading is kept encrypted (``proposal_sealed``) so the person can
    see what was read; identifier values never go into plain columns.
    """

    __tablename__ = "vehicle_intakes"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "document_version", name="uq_vehicle_intakes_version"
        ),
        CheckConstraint(
            "status IN ('applied', 'no_changes', 'needs_check', 'conflict', "
            "'resolved')",
            name="ck_vehicle_intakes_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), nullable=False, index=True
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicles.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # [{"key": "chassis_number", "reason": "low_confidence"}, ...]
    check_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # Field names whose reading differs from the vehicle's current value.
    pending_changes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    proposal_sealed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_vehicle_intakes_resolved_by")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
