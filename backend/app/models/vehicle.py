from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

VEHICLE_CATEGORIES = ("two_wheeler", "private_four_wheeler", "commercial", "other")
EXPIRY_TYPES = ("insurance", "pucc", "fitness", "tax", "permit")
VALUE_SOURCES = ("manual", "document", "lookup")


class Vehicle(Base):
    """A customer's vehicle.

    The registration number is kept as shown (``registration_number``) and
    normalised for search and uniqueness (``registration_key``: upper case,
    letters and digits only). Chassis and engine numbers are sensitive:
    stored encrypted, with only their last four characters in clear for
    masked display.

    Contact phone, challan status and verification each record where the
    value came from, so a later lookup can fill them without a redesign.
    """

    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("registration_key", name="uq_vehicles_registration_key"),
        CheckConstraint(
            "category IN ('two_wheeler', 'private_four_wheeler', 'commercial', "
            "'other')",
            name="ck_vehicles_category",
        ),
        CheckConstraint(
            "challan_status IN ('unknown', 'none', 'pending')",
            name="ck_vehicles_challan_status",
        ),
        CheckConstraint(
            "contact_phone_source IS NULL OR contact_phone_source IN "
            "('manual', 'document', 'lookup')",
            name="ck_vehicles_phone_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    registration_number: Mapped[str] = mapped_column(String(20), nullable=False)
    registration_key: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(200))
    chassis_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    chassis_last4: Mapped[str | None] = mapped_column(String(4))
    engine_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    engine_last4: Mapped[str | None] = mapped_column(String(4))
    # Person responsible for follow-up (reminders go to them).
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    # Number for reminders when it is not the customer's own phone.
    contact_phone: Mapped[str | None] = mapped_column(String(30))
    contact_phone_source: Mapped[str | None] = mapped_column(String(20))
    challan_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'unknown'")
    )
    challan_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VehicleExpiry(Base):
    """One validity date of a vehicle (insurance, PUCC, fitness, tax,
    permit). Changing a date adds a new row and marks the old one as no
    longer current, so the history stays; at most one current row per type.
    A current row with no date means "not applicable / cleared"."""

    __tablename__ = "vehicle_expiries"
    __table_args__ = (
        CheckConstraint(
            "expiry_type IN ('insurance', 'pucc', 'fitness', 'tax', 'permit')",
            name="ck_vehicle_expiries_type",
        ),
        CheckConstraint(
            "source IN ('manual', 'document', 'lookup')",
            name="ck_vehicle_expiries_source",
        ),
        Index(
            "uq_vehicle_expiries_current",
            "vehicle_id",
            "expiry_type",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_vehicle_expiries_valid_until", "valid_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id"), nullable=False, index=True
    )
    expiry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", name="fk_vehicle_expiries_document")
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
