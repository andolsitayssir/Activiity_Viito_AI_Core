"""Render a one-page invoice PDF from Odoo data (reportlab).

Odoo serves accounting over XML-RPC only (no web-auth report endpoint), so the
email executor turns the posted invoice's structured data — move + partner +
lines — into a real PDF attachment locally.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any


def _safe(value: Any) -> str:
    """Coerce to a latin-1-safe string (reportlab Helvetica = WinAnsi)."""
    return str(value if value is not None else "").encode("latin-1", "replace").decode("latin-1")


def _money(value: Any, currency: str = "EUR") -> str:
    try:
        return f"{float(value or 0):,.2f}".replace(",", " ").replace(".", ",") + " " + _safe(currency)
    except (TypeError, ValueError):
        return _safe(value)


def render_invoice_pdf(packet: dict[str, Any]) -> bytes:
    """Build the PDF bytes for an invoice packet from ``read_invoice_packet``."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    move = packet.get("move") or {}
    partner = packet.get("partner") or {}
    lines = packet.get("lines") or []

    width, height = A4
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    margin = 48
    top = height - 48

    currency_id = move.get("currency_id")
    currency = currency_id[1] if isinstance(currency_id, (list, tuple)) and len(currency_id) > 1 else "EUR"

    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, top, "FACTURE")
    c.setFont("Helvetica", 9)
    c.drawRightString(width - margin, top, _safe(move.get("state") or ""))

    top -= 22
    c.setFont("Helvetica", 9)
    c.drawString(margin, top, f"Numéro : {_safe(move.get('name') or move.get('ref') or '')}")
    c.drawRightString(width - margin, top, f"Date : {_safe(move.get('invoice_date'))}")
    top -= 14
    if move.get("ref"):
        c.drawString(margin, top, f"Référence : {_safe(move.get('ref'))}")
    top -= 18

    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, top, "Client")
    top -= 12
    c.setFont("Helvetica", 9)
    if partner.get("name"):
        c.drawString(margin, top, _safe(partner.get("name")))
        top -= 12
    if partner.get("email"):
        c.drawString(margin, top, _safe(partner.get("email")))
        top -= 12
    address_bits = [
        _safe(partner.get("street")),
        _safe(partner.get("street2")),
    ]
    address_bits = [b for b in address_bits if b]
    city_bits = [b for b in (_safe(partner.get("zip")), _safe(partner.get("city"))) if b]
    if city_bits:
        address_bits.append(" ".join(city_bits))
    for bit in address_bits:
        c.drawString(margin, top, bit)
        top -= 12

    top -= 14
    table_top = top
    c.setFont("Helvetica-Bold", 9)
    c.line(margin, table_top + 6, width - margin, table_top + 6)
    c.drawString(margin, table_top - 4, "Description")
    c.drawRightString(width - margin - 170, table_top - 4, "Qté")
    c.drawRightString(width - margin - 85, table_top - 4, "Prix unitaire")
    c.drawRightString(width - margin, table_top - 4, "Montant")
    top = table_top - 18
    c.setFont("Helvetica", 9)
    for line in lines:
        c.drawString(margin, top, _safe(line.get("name") or "")[:80])
        c.drawRightString(width - margin - 170, top, _safe(line.get("quantity") or ""))
        c.drawRightString(width - margin - 85, top, _money(line.get("price_unit"), currency))
        c.drawRightString(width - margin, top, _money(line.get("price_subtotal"), currency))
        top -= 14
    c.line(margin, top + 6, width - margin, top + 6)

    top -= 16
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(width - margin, top, "Total")
    c.drawRightString(width - margin, top - 14, _money(move.get("amount_total"), currency))

    c.showPage()
    c.save()
    return buf.getvalue()