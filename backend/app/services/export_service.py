"""Excel exports (Phase 4.3: vehicles).

Rules (AGENTS.md, decisions.md section 13, owner decisions):
- Built from PostgreSQL; the spreadsheet is never a source of data.
- Respects access: employees export only vehicles they may see.
- Chassis and engine numbers are masked; the mobile column is masked too.
  The WhatsApp link necessarily carries the number (that is how it opens
  the chat), so it is only present when there is something to say.
- Every text cell that a spreadsheet could read as a formula (starting
  with = + - @, or a tab/carriage return) is written as plain text with a
  leading apostrophe (formula-injection protection).
- Every export is audited with its filters and row count.
"""

import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.masking import mask_phone
from app.core.vehicle_numbers import mask_identifier
from app.core.whatsapp import link, renewal_message, whatsapp_number
from app.models import Customer, User, Vehicle, VehicleExpiry
from app.repositories import vehicle_repository
from app.repositories.document_repository import users_by_id
from app.repositories.readiness_repository import BUSINESS_TIMEZONE, business_today
from app.schemas.reminder import EXPIRY_LABELS
from app.services import audit_service, job_service
from app.services.errors import BusinessRuleError

MAX_ROWS = 20_000
DUE_WINDOW_DAYS = 30
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CATEGORY_LABELS = {
    "two_wheeler": "Two-wheeler",
    "private_four_wheeler": "Private four-wheeler",
    "commercial": "Commercial",
    "other": "Other",
}
CHALLAN_LABELS = {
    "unknown": "Not checked",
    "none": "None pending",
    "pending": "Pending",
}


def safe_text(value: object) -> object:
    """Text a spreadsheet would treat as a formula gets a leading
    apostrophe; anything else is returned unchanged."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS):
        return "'" + value
    return value


def _put(ws, row: int, col: int, value) -> None:
    cell = ws.cell(row=row, column=col, value=safe_text(value))
    if isinstance(value, str):
        cell.data_type = "s"  # never a formula, whatever it starts with
    elif isinstance(value, date):
        cell.number_format = "DD-MM-YYYY"


def _vehicle_message(vehicle, customer, expiries, today) -> str | None:
    """What the WhatsApp link says: the expiries due (expired or within 30
    days), or else the next one; None when the vehicle has no dates."""
    dated = sorted(
        (e for e in expiries if e.valid_until is not None),
        key=lambda e: (e.valid_until, e.expiry_type),
    )
    if not dated:
        return None
    due = [e for e in dated if (e.valid_until - today).days <= DUE_WINDOW_DAYS]
    chosen = due or dated[:1]
    items = [
        (EXPIRY_LABELS[e.expiry_type], e.valid_until, (e.valid_until - today).days)
        for e in chosen
    ]
    return renewal_message(
        customer.name, vehicle.registration_number, items, get_settings().centre_name
    )


def vehicles_xlsx(
    db: Session,
    viewer: User,
    *,
    due_within_days: int | None = None,
    include_inactive: bool = False,
    customer_id: int | None = None,
) -> tuple[str, bytes, int]:
    """(filename, file bytes, rows) for the vehicles the viewer may see."""
    today = business_today(db)
    conditions = []
    if not job_service.is_admin(viewer):
        conditions.append(vehicle_repository.visible_to(viewer.id))
    if not include_inactive:
        conditions.append(Vehicle.is_active.is_(True))
    if customer_id is not None:
        conditions.append(Vehicle.customer_id == customer_id)
    if due_within_days is not None:
        conditions.append(
            exists().where(
                VehicleExpiry.vehicle_id == Vehicle.id,
                VehicleExpiry.is_current.is_(True),
                VehicleExpiry.valid_until <= today + timedelta(days=due_within_days),
            )
        )
    rows = db.execute(
        select(Vehicle, Customer)
        .join(Customer, Customer.id == Vehicle.customer_id)
        .where(*conditions)
        .order_by(Vehicle.registration_key, Vehicle.id)
        .limit(MAX_ROWS + 1)
    ).all()
    if len(rows) > MAX_ROWS:
        raise BusinessRuleError(
            f"More than {MAX_ROWS} vehicles match; narrow the export with filters."
        )
    vehicles = [v for v, _ in rows]
    expiries = vehicle_repository.current_expiries(db, [v.id for v in vehicles])
    users = users_by_id(db, {v.assigned_to for v in vehicles})

    wb = Workbook()
    ws = wb.active
    ws.title = "Vehicles"
    exported_at = datetime.now(ZoneInfo(BUSINESS_TIMEZONE))
    headers = [
        "Registration no.",
        "Category",
        "Customer ID",
        "Customer name",
        "Mobile",
        "Owner name (RC)",
        "Chassis no.",
        "Engine no.",
        "Responsible",
    ]
    for kind, label in EXPIRY_LABELS.items():
        headers += [f"{label} valid until", f"{label} days left"]
    headers += ["Challan status", "Active", "WhatsApp"]
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"

    for r, (vehicle, customer) in enumerate(rows, start=2):
        current = {e.expiry_type: e for e in expiries[vehicle.id]}
        responsible = users.get(vehicle.assigned_to)
        values = [
            vehicle.registration_number,
            CATEGORY_LABELS.get(vehicle.category, vehicle.category),
            customer.id,
            customer.name,
            mask_phone(vehicle.contact_phone or customer.phone),
            vehicle.owner_name,
            mask_identifier(vehicle.chassis_last4),
            mask_identifier(vehicle.engine_last4),
            responsible.name if responsible else None,
        ]
        for kind in EXPIRY_LABELS:
            e = current.get(kind)
            until = e.valid_until if e is not None else None
            values += [until, (until - today).days if until else None]
        values += [
            CHALLAN_LABELS.get(vehicle.challan_status, vehicle.challan_status),
            "Yes" if vehicle.is_active else "No",
        ]
        for col, value in enumerate(values, start=1):
            _put(ws, r, col, value)
        number = whatsapp_number(vehicle.contact_phone) or whatsapp_number(
            customer.phone
        )
        message = _vehicle_message(vehicle, customer, expiries[vehicle.id], today)
        wa = ws.cell(row=r, column=len(values) + 1)
        if number and message:
            wa.value = "Open WhatsApp"
            wa.hyperlink = link(number, message)
            wa.style = "Hyperlink"

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    ws.cell(
        row=len(rows) + 3,
        column=1,
        value=safe_text(
            f"Exported {exported_at.strftime('%d-%m-%Y %H:%M')} IST by {viewer.name}"
        ),
    ).data_type = "s"

    out = io.BytesIO()
    wb.save(out)
    audit_service.record(
        db,
        action="vehicles_exported",
        entity_type="export",
        entity_id=None,
        user_id=viewer.id,
        details={
            "rows": len(rows),
            "due_within_days": due_within_days,
            "include_inactive": include_inactive,
            "customer_id": customer_id,
        },
    )
    db.commit()
    return f"vehicles-{today.strftime('%Y%m%d')}.xlsx", out.getvalue(), len(rows)
