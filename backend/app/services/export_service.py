"""Excel exports (Phase 4.3: vehicles).

Rules (AGENTS.md, decisions.md section 13, owner decisions):
- Built from PostgreSQL; the spreadsheet is never a source of data.
- Respects access: employees export only vehicles they may see.
- Chassis, engine and mobile numbers are masked. The WhatsApp link the
  owner asked for necessarily carries the full number (that is how it
  opens the chat); ``include_whatsapp=false`` leaves the links out, and the
  choice is audited.
- Formula injection: every text cell is written as text, never a formula;
  text a spreadsheet could read as one (starting with = + - @, a tab or a
  carriage return) also gets a leading apostrophe, which keeps it inert if
  the file is later saved as CSV. Characters a spreadsheet cannot hold
  (control characters) are removed.
- Every export is audited with its filters and row count.
- Built in openpyxl's streaming (write-only) mode to keep memory low.
"""

import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
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
    """Text safe for a spreadsheet cell: control characters removed, and a
    leading apostrophe when it could be read as a formula. Non-text values
    are returned unchanged."""
    if not isinstance(value, str):
        return value
    value = ILLEGAL_CHARACTERS_RE.sub("", value)
    if value.startswith(FORMULA_STARTS):
        return "'" + value
    return value


def _cell(ws, value) -> WriteOnlyCell:
    cell = WriteOnlyCell(ws, value=safe_text(value))
    if isinstance(value, str):
        cell.data_type = "s"  # never a formula, whatever it starts with
    elif isinstance(value, date):
        cell.number_format = "DD-MM-YYYY"
    return cell


def _message(vehicle, customer, expiries, today, window: int) -> str | None:
    """What the WhatsApp link says: the expiries due (expired or within the
    window), or else the next one; None when the vehicle has no dates."""
    dated = sorted(
        (e for e in expiries if e.valid_until is not None),
        key=lambda e: (e.valid_until, e.expiry_type),
    )
    if not dated:
        return None
    due = [e for e in dated if (e.valid_until - today).days <= window]
    chosen = due or dated[:1]
    items = [
        (EXPIRY_LABELS[e.expiry_type], e.valid_until, (e.valid_until - today).days)
        for e in chosen
    ]
    return renewal_message(
        customer.name, vehicle.registration_number, items, get_settings().centre_name
    )


def _contact(vehicle: Vehicle, customer: Customer) -> tuple[str | None, str | None]:
    """(number shown masked, WhatsApp number): the same number the link
    uses, so the masked column never points at a different one."""
    for phone in (vehicle.contact_phone, customer.phone):
        number = whatsapp_number(phone)
        if number:
            return phone, number
    return vehicle.contact_phone or customer.phone, None


def vehicles_xlsx(
    db: Session,
    viewer: User,
    *,
    due_within_days: int | None = None,
    include_inactive: bool = False,
    customer_id: int | None = None,
    include_whatsapp: bool = True,
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
    window = max(DUE_WINDOW_DAYS, due_within_days or 0)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Vehicles")
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
    for label in EXPIRY_LABELS.values():
        headers += [f"{label} valid until", f"{label} days left"]
    headers += ["Challan status", "Active"]
    if include_whatsapp:
        headers.append("WhatsApp")
    ws.freeze_panes = "A2"
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    bold = Font(bold=True)
    header_cells = []
    for title in headers:
        cell = WriteOnlyCell(ws, value=title)
        cell.font = bold
        header_cells.append(cell)
    ws.append(header_cells)

    for vehicle, customer in rows:
        current = {e.expiry_type: e for e in expiries[vehicle.id]}
        responsible = users.get(vehicle.assigned_to)
        shown_phone, number = _contact(vehicle, customer)
        values = [
            vehicle.registration_number,
            CATEGORY_LABELS.get(vehicle.category, vehicle.category),
            customer.id,
            customer.name,
            mask_phone(shown_phone),
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
        cells = [_cell(ws, value) for value in values]
        if include_whatsapp:
            message = _message(vehicle, customer, expiries[vehicle.id], today, window)
            wa = WriteOnlyCell(
                ws, value="Open WhatsApp" if number and message else None
            )
            if number and message:
                wa.hyperlink = link(number, message)
                wa.style = "Hyperlink"
            cells.append(wa)
        ws.append(cells)

    # Export details on their own sheet, out of the way of sorting/filters.
    about = wb.create_sheet("About")
    exported_at = datetime.now(ZoneInfo(BUSINESS_TIMEZONE))
    for label, value in (
        ("Exported at (IST)", exported_at.strftime("%d-%m-%Y %H:%M")),
        ("Exported by", viewer.name),
        ("Vehicles", len(rows)),
        ("Due within days", due_within_days if due_within_days is not None else "All"),
        ("Inactive included", "Yes" if include_inactive else "No"),
    ):
        about.append([_cell(about, label), _cell(about, value)])

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
            "include_whatsapp": include_whatsapp,
        },
    )
    db.commit()
    return f"vehicles-{today.strftime('%Y%m%d')}.xlsx", out.getvalue(), len(rows)
