"""Invoice and offer-letter PDFs for the seeded world.

Text PDFs have a real text layer (reportlab). The scanned-style invoice is a
picture of the page with no text layer at all, so text extraction finds nothing
and the worker has to say so instead of guessing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from sandbox.common.money import format_inr

PAGE_W, PAGE_H = A4
MARGIN = 50


def human_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d %b %Y")


def invoice_lines(
    invoice: dict[str, Any], vendor: dict[str, Any], company: dict[str, str], notes: list[str]
) -> list[tuple[str, str]]:
    """The invoice as (style, text) lines, shared by the text and image versions."""
    bank = vendor["bank"]
    out: list[tuple[str, str]] = [
        ("title", vendor["name"]),
        ("small", vendor["address"]),
        ("small", f"GSTIN: {vendor['gstin']}    Email: {vendor['contact_email']}"),
        ("gap", ""),
        ("heading", "TAX INVOICE"),
        ("body", f"Invoice No: {invoice['invoice_no']}"),
        ("body", f"Invoice Date: {human_date(invoice['invoice_date'])}"),
        ("body", f"Due Date: {human_date(invoice['due_date'])}"),
    ]
    if invoice["po_number"]:
        out.append(("body", f"PO Number: {invoice['po_number']}"))
    out += [
        ("gap", ""),
        ("body", f"Bill To: {company['name']}"),
        ("small", f"{company['address']}    GSTIN: {company['gstin']}"),
        ("gap", ""),
        ("row", "Description|Qty|Rate (INR)|Amount (INR)"),
    ]
    for line in invoice["lines"]:
        out.append(
            (
                "row",
                f"{line['description']}|{line['qty']}|{format_inr(line['rate_paise'])}"
                f"|{format_inr(line['amount_paise'])}",
            )
        )
    out += [
        ("gap", ""),
        ("total", f"Subtotal: INR {format_inr(invoice['subtotal_paise'])}"),
        ("total", f"GST @ 18%: INR {format_inr(invoice['gst_paise'])}"),
        ("grand", f"Total Due: INR {format_inr(invoice['total_paise'])}"),
        ("gap", ""),
        ("heading", "Payment details"),
        ("body", f"Bank: {bank['bank_name']}"),
        ("body", f"Account No: {bank['account']}    IFSC: {bank['ifsc']}"),
        ("gap", ""),
    ]
    out += [("small", n) for n in notes]
    return out


_FONTS = {
    "title": ("Helvetica-Bold", 16, 22),
    "heading": ("Helvetica-Bold", 12, 18),
    "body": ("Helvetica", 10, 15),
    "small": ("Helvetica", 8.5, 12),
    "row": ("Helvetica", 9.5, 15),
    "total": ("Helvetica", 10, 15),
    "grand": ("Helvetica-Bold", 11, 17),
    "gap": ("Helvetica", 10, 10),
}
_COLUMNS = (MARGIN, 330, 390, 470)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return [*lines, current] if current else lines


def write_text_pdf(path: Path, lines: list[tuple[str, str]], title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    y = PAGE_H - MARGIN
    for style, text in lines:
        font, size, step = _FONTS[style]
        pdf.setFont(font, size)
        if style == "row":
            for x, cell in zip(_COLUMNS, text.split("|"), strict=True):
                pdf.drawString(x, y, cell)
            y -= step
        elif style in ("total", "grand"):
            pdf.drawRightString(PAGE_W - MARGIN, y, text)
            y -= step
        else:
            for chunk in _wrap(text, 100) or [""]:
                pdf.drawString(MARGIN, y, chunk)
                y -= step
    pdf.showPage()
    pdf.save()


def write_scanned_pdf(path: Path, lines: list[tuple[str, str]], title: str) -> None:
    """Draw the page as an image (slightly tilted and blurred), embed only the image."""
    scale = 2
    img = Image.new("L", (int(PAGE_W * scale), int(PAGE_H * scale)), color=246)
    draw = ImageDraw.Draw(img)
    y = MARGIN * scale
    for style, text in lines:
        _, size, step = _FONTS[style]
        font = ImageFont.load_default(size=size * scale)
        if style == "row":
            for x, cell in zip(_COLUMNS, text.split("|"), strict=True):
                draw.text((x * scale, y), cell, fill=40, font=font)
        else:
            x = MARGIN if style not in ("total", "grand") else 330
            for chunk in _wrap(text, 100) or [""]:
                draw.text((x * scale, y), chunk, fill=40, font=font)
        y += step * scale
    img = img.rotate(0.7, fillcolor=246).filter(ImageFilter.GaussianBlur(0.8))
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    pdf.drawImage(ImageReader(img), 0, 0, width=PAGE_W, height=PAGE_H)
    pdf.showPage()
    pdf.save()


def offer_letter_lines(
    hire: dict[str, Any], hr: dict[str, Any], company: dict[str, str]
) -> list[tuple[str, str]]:
    return [
        ("title", company["name"]),
        ("small", company["address"]),
        ("gap", ""),
        ("heading", "Offer of Employment"),
        ("body", f"Date: {human_date(hire['offer_date'])}"),
        ("gap", ""),
        ("body", f"Dear {hire['full_name']},"),
        (
            "body",
            f"We are pleased to offer you the position of {hire['title']} in our "
            f"{hire['department']} team. The details of your appointment are below.",
        ),
        ("gap", ""),
        ("body", f"Position: {hire['title']}"),
        ("body", f"Department: {hire['department']}"),
        ("body", f"Start date: {human_date(hire['start_date'])}"),
        ("body", f"Reporting manager: {hire['manager_name']}"),
        ("body", f"Work email (active from your start date): {hire['work_email']}"),
        ("body", f"Personal email for correspondence before joining: {hire['personal_email']}"),
        ("gap", ""),
        (
            "body",
            "Please sign and return a copy of this letter to confirm your acceptance. "
            "We look forward to welcoming you.",
        ),
        ("gap", ""),
        ("body", f"{hr['full_name']}, People Team, {company['name']}"),
        ("gap", ""),
        ("body", f"Accepted and signed: {hire['full_name']}"),
    ]
