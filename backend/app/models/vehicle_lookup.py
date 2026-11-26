from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VehicleLookup(Base):
    """One lookup of a vehicle's validity dates and challans (Phase 4.4).

    queued -> (waiting_for_captcha -> queued)* -> completed | failed;
    a CAPTCHA not answered in time makes it ``expired``. At most one lookup
    per vehicle is in progress. Results are kept with their time and source;
    failed and partial lookups are recorded too.
    """

    __tablename__ = "vehicle_lookups"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'waiting_for_captcha', 'completed', 'failed', "
            "'expired')",
            name="ck_vehicle_lookups_status",
        ),
        Index(
            "uq_vehicle_lookups_active",
            "vehicle_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'waiting_for_captcha')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id"), nullable=False, index=True
    )
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(50))
    # Rounds of the task (a CAPTCHA answer starts a new one).
    round: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # What was found: {"expiries": {type: "YYYY-MM-DD"}, "challan_status": ...,
    # "challan_count": ..., "applied": [types written to the vehicle]}.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    captcha_png: Mapped[bytes | None] = mapped_column(LargeBinary)
    captcha_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The provider's opaque state while waiting for a CAPTCHA (encrypted).
    state_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    captcha_answer_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
