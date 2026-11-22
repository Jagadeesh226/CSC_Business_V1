"""Draw a receipt as an A5 PDF (half an A4 sheet).

Everything printed comes from the receipt's frozen snapshot, so a reprint
shows exactly what the customer was first given. Every print after the
first says DUPLICATE COPY; a cancelled receipt says CANCELLED with the
reason. No identity numbers are ever printed and the phone is masked.

Fonts: the built-in Helvetica covers English (Latin-1). For names in other
scripts set RECEIPT_FONT_PATH to a Unicode TTF; without it, characters the
font cannot print are shown as "?" rather than failing the print.
"""

import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fpdf import FPDF

from app.core.config import get_settings
from app.core.money_words import rupees_in_words
from app.repositories.readiness_repository import BUSINESS_TIMEZONE
from app.schemas.receipt import ReceiptSnapshot

log = logging.getLogger("app.receipts")

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
    def __init__(self, font_path: str, header_note: str) -> None:
        super().__init__(format=(148, 210), unit="mm")  # A5
        self.set_margins(10, 10, 10)
        self.set_auto_page_break(auto=True, margin=12)
        self._header_note = header_note
        self._unicode = False
        self._family = "Helvetica"
        if font_path:
            try:
                self.add_font("Receipt", "", font_path)
                self.add_font("Receipt", "B", font_path)
                self._family = "Receipt"
                self._unicode = True
            except (OSError, RuntimeError) as exc:
                # A wrong RECEIPT_FONT_PATH must not stop printing.
                log.warning(
                    "Receipt font could not be loaded (%s); using Helvetica.",
                    type(exc).__name__,
                )

    def header(self) -> None:
        # Pages after the first repeat the receipt number and any marks.
        if self.page_no() > 1 and self._header_note:
            self.use(8, bold=True)
            self.cell(
                self.epw,
                5,
                self.safe(self._header_note),
                align="C",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            self.ln(1)

    def safe(self, value: object) -> str:
        s = "" if value is None else str(value)
        return s if self._unicode else s.encode("latin-1", "replace").decode("latin-1")

    def use(self, size: float, bold: bool = False) -> None:
        self.set_font(self._family, "B" if bold else "", size)

    def _lines(self, width: float, text: str) -> int:
        return len(self.multi_cell(width, 5, text, dry_run=True, output="LINES"))

    def row(
        self,
        left: str,
        right: str,
        *,
        split: float = 0.62,
        bold: bool = False,
        left_bold: bool | None = None,
        right_align: str = "R",
    ) -> None:
        """Two columns that wrap instead of running into each other."""
        width = self.epw
        lw, rw = width * split, width * (1 - split)
        left, right = self.safe(left), self.safe(right)
        self.use(9, bold if left_bold is None else left_bold)
        lines = self._lines(lw - 1, left)
        self.use(9, bold)
        lines = max(lines, self._lines(rw, right))
        height = lines * 5
        if self.get_y() + height > self.page_break_trigger:
            self.add_page()
        x, y = self.l_margin, self.get_y()
        self.use(9, bold if left_bold is None else left_bold)
        self.set_xy(x, y)
        self.multi_cell(lw - 1, 5, left, align="L")
        self.use(9, bold)
        self.set_xy(x + lw, y)
        self.multi_cell(rw, 5, right, align=right_align)
        self.set_xy(x, y + height)


def render(
    *,
    receipt_number: str,
    issued_at: datetime,
    snapshot: ReceiptSnapshot,
    duplicate: bool,
    cancelled_reason: str | None,
) -> bytes:
    settings = get_settings()
    marks = [
        m
        for m, on in (
            ("DUPLICATE COPY", duplicate),
            ("CANCELLED", cancelled_reason is not None),
        )
        if on
    ]
    pdf = _Receipt(
        settings.receipt_font_path, " - ".join([f"Receipt {receipt_number}", *marks])
    )
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
        reason = (
            cancelled_reason
            if len(cancelled_reason) <= 300
            else cancelled_reason[:297] + "..."
        )
        pdf.use(8)
        pdf.multi_cell(width, 4.5, pdf.safe(f"Reason: {reason}"), align="C")
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
        pdf.row(label, value, split=0.22, left_bold=True, right_align="L")
    pdf.ln(2)

    # Fees
    pdf.set_draw_color(150)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
    pdf.ln(1)
    for fee in snapshot.fees:
        label = f"{FEE_LABELS.get(fee.fee_type, fee.fee_type)}: {fee.description}"
        pdf.row(label, _rs(fee.amount))
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
    pdf.row("Total fees", _rs(snapshot.fees_total), bold=True)
    if snapshot.paid_before:
        pdf.row("Paid earlier", _rs(snapshot.paid_before))
    pdf.row("Amount received now", _rs(snapshot.amount), bold=True)
    pdf.row("Balance due", _rs(snapshot.balance_after))
    pdf.ln(1)
    pdf.use(8)
    pdf.multi_cell(width, 4.5, rupees_in_words(snapshot.amount))
    pdf.ln(1)

    method = METHOD_LABELS.get(snapshot.method, snapshot.method)
    if snapshot.reference_number:
        method += f" (ref. {snapshot.reference_number})"
    pdf.row("Paid by", method)
    pdf.row("Received by", snapshot.received_by)
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
