"""Draw a receipt as an A5 PDF (half an A4 sheet).

Everything printed comes from the receipt's frozen snapshot, so a reprint
shows exactly what the customer was first given. Every print after the
first says DUPLICATE COPY; a cancelled receipt says CANCELLED with the
reason. No identity numbers are ever printed and the phone is masked.

Fonts: the built-in Helvetica covers English (Latin-1). For names in other
scripts set RECEIPT_FONT_PATH to a Unicode TTF; without it, characters the
font cannot print are shown as "?" rather than failing the print.
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fpdf import FPDF

from app.core.config import get_settings
from app.core.money_words import rupees_in_words
from app.repositories.readiness_repository import BUSINESS_TIMEZONE
from app.schemas.receipt import ReceiptSnapshot

FEE_LABELS = {
    "service": "Service charge",
    "third_party": "Third-party fee",
    "department": "Department fee",
}
METHOD_LABELS = {
    "cash": "Cash",
    "upi": "UPI",
    "card": "Card",
    "bank_transfer": "Bank transfer",
    "cheque": "Cheque",
    "other": "Other",
}


def _rs(value: Decimal) -> str:
    return f"Rs. {value:,.2f}"


def _local(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo(BUSINESS_TIMEZONE)).strftime("%d-%m-%Y %I:%M %p")


class _Receipt(FPDF):
    def __init__(self, font_path: str) -> None:
        super().__init__(format=(148, 210), unit="mm")  # A5
        self.set_margins(10, 10, 10)
        self.set_auto_page_break(auto=True, margin=12)
        self._unicode = False
        if font_path:
            self.add_font("Receipt", "", font_path)
            self.add_font("Receipt", "B", font_path)
            self._family = "Receipt"
            self._unicode = True
        else:
            self._family = "Helvetica"

    def safe(self, value: object) -> str:
        s = "" if value is None else str(value)
        return s if self._unicode else s.encode("latin-1", "replace").decode("latin-1")

    def use(self, size: float, bold: bool = False) -> None:
        self.set_font(self._family, "B" if bold else "", size)

    def line_item(self, label: str, value: str, bold: bool = False) -> None:
        self.use(9, bold)
        width = self.epw
        self.cell(width * 0.6, 6, self.safe(label))
        self.cell(
            width * 0.4, 6, self.safe(value), align="R", new_x="LMARGIN", new_y="NEXT"
        )


def render(
    *,
    receipt_number: str,
    issued_at: datetime,
    snapshot: ReceiptSnapshot,
    duplicate: bool,
    cancelled_reason: str | None,
) -> bytes:
    settings = get_settings()
    pdf = _Receipt(settings.receipt_font_path)
    pdf.set_title(f"Receipt {receipt_number}")
    pdf.set_creator("CSC Business CRM")
    pdf.add_page()
    width = pdf.epw

    # Centre
    pdf.use(13, bold=True)
    pdf.cell(
        width,
        7,
        pdf.safe(settings.centre_name),
        align="C",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.use(8)
    for line in (settings.centre_address, settings.centre_phone):
        if line:
            pdf.cell(
                width, 4.5, pdf.safe(line), align="C", new_x="LMARGIN", new_y="NEXT"
            )
    pdf.ln(2)

    # Title and copy marks
    pdf.use(11, bold=True)
    pdf.cell(width, 7, "PAYMENT RECEIPT", align="C", new_x="LMARGIN", new_y="NEXT")
    for mark, shown in (
        ("DUPLICATE COPY", duplicate),
        ("CANCELLED", cancelled_reason is not None),
    ):
        if shown:
            pdf.use(10, bold=True)
            pdf.cell(width, 6, mark, border=1, align="C", new_x="LMARGIN", new_y="NEXT")
    if cancelled_reason is not None:
        pdf.use(8)
        pdf.multi_cell(width, 4.5, pdf.safe(f"Reason: {cancelled_reason}"), align="C")
    pdf.ln(2)

    # Who and what
    job, customer = snapshot.job, snapshot.customer
    for label, value in (
        ("Receipt no.", receipt_number),
        ("Date", _local(issued_at)),
        ("Job no.", job.job_number),
        ("Service", job.service_name or job.title),
        ("Customer", customer.name),
        ("Phone", customer.phone or "-"),
    ):
        pdf.use(9, bold=True)
        pdf.cell(25, 5.5, label)
        pdf.use(9)
        pdf.cell(width - 25, 5.5, pdf.safe(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # Fees
    pdf.set_draw_color(150)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
    pdf.ln(1)
    for fee in snapshot.fees:
        label = f"{FEE_LABELS.get(fee.fee_type.value, fee.fee_type.value)}: {fee.description}"
        pdf.line_item(label, _rs(fee.amount))
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
    pdf.line_item("Total fees", _rs(snapshot.fees_total), bold=True)
    if snapshot.paid_before:
        pdf.line_item("Paid earlier", _rs(snapshot.paid_before))
    pdf.line_item("Amount received now", _rs(snapshot.amount), bold=True)
    pdf.line_item("Balance due", _rs(snapshot.balance_after))
    pdf.ln(1)
    pdf.use(8)
    pdf.multi_cell(width, 4.5, rupees_in_words(snapshot.amount))
    pdf.ln(1)

    method = METHOD_LABELS.get(snapshot.method.value, snapshot.method.value)
    if snapshot.reference_number:
        method += f" (ref. {snapshot.reference_number})"
    pdf.line_item("Paid by", method)
    pdf.line_item("Received by", snapshot.received_by)
    pdf.ln(4)
    pdf.use(7)
    pdf.multi_cell(
        width,
        4,
        "This is a computer-generated receipt for the payment above. "
        "It is not a tax invoice.",
        align="C",
    )
    return bytes(pdf.output())
