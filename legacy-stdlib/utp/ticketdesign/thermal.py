"""80mm thermal admission ticket for the Star Micronics MCP31LB.

A separate design, not the e-ticket in greyscale. The brief is explicit about
that and it is the right call: gradients turn to mud, light grey text does not
survive a thermal head, and hairlines disappear.

Printer facts that drove the numbers
------------------------------------
The MCP31LB (mC-Print3 family, models 143IVUEWB etc.) takes 80mm roll and has a
**72mm printable width at 203 dpi — 8 dots per millimetre**. So:

* the page is ``80mm auto`` with 4mm side padding, giving exactly 72mm of
  content and no clipped right edge;
* the QR is rendered at **46mm**, inside the 45-50mm the brief asks for. At 8
  dots/mm that is ~368 dots across; a version-9 symbol plus its quiet zone is 61
  modules, so roughly 6 dots per module — comfortably above the ~3 dots where
  thermal scanning starts to fail;
* the QR is inline SVG, so the browser rasterises it at printer resolution
  instead of scaling a bitmap;
* rules are solid 1px black, never dotted grey;
* every value is pure black. Hierarchy comes from size and weight only.

Height is deliberately unset (``auto``): a booking with eight ticket lines must
simply print a longer ticket, and nothing may be pushed off the end.

Paper is not free, so the vertical rhythm is tight — small margins, no empty
spacer blocks — while keeping the four things gate staff and guests actually
read large: booking number, visit date, last admission and total.
"""

from __future__ import annotations

from html import escape
from typing import Any

from .qr import qr_svg
from .strings import translator

#: Printable width of the MCP31LB on 80mm stock.
PRINT_WIDTH_MM = 72.0
PAGE_WIDTH_MM = 80.0
SIDE_PADDING_MM = (PAGE_WIDTH_MM - PRINT_WIDTH_MM) / 2      # 4mm each side

#: Within the 45-50mm the design brief specifies.
QR_SIZE_MM = 46.0

_FONT_STACK = (
    "Arial,'Helvetica Neue',Helvetica,'Noto Sans Thai','Noto Sans SC',"
    "'Noto Sans JP','Noto Sans',sans-serif"
)


def _divider() -> str:
    return '<div class="tdiv"></div>'


def _field(label: str, value: str, *, big: bool = False, note: str = "") -> str:
    cls = "tvalue tbig" if big else "tvalue"
    note_html = f'<div class="tnote">{escape(note)}</div>' if note else ""
    return (
        f'<div class="tfield"><div class="tlabel">{escape(label)}</div>'
        f'<div class="{cls}">{escape(value)}</div>{note_html}</div>'
    )


def _row(left: str, right: str, *, strong: bool = False) -> str:
    cls = "trow tstrong" if strong else "trow"
    return (
        f'<div class="{cls}"><span>{escape(left)}</span>'
        f"<span>{escape(right)}</span></div>"
    )


def _brand_mark() -> str:
    """A monochrome mark only — the brief forbids anything tonal here."""
    return (
        '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#000" '
        'stroke-width="2" stroke-linecap="round" aria-hidden="true">'
        '<path d="M2 8c2.2-2.4 4.1-2.4 6.3 0s4.1 2.4 6.3 0 4.1-2.4 6.3 0"/>'
        '<path d="M2 14c2.2-2.4 4.1-2.4 6.3 0s4.1 2.4 6.3 0 4.1-2.4 6.3 0"/>'
        '<path d="M2 20c2.2-2.4 4.1-2.4 6.3 0s4.1 2.4 6.3 0 4.1-2.4 6.3 0"/></svg>'
    )


_STYLES = f"""
  @page {{ size: {PAGE_WIDTH_MM:g}mm auto; margin: 0; }}

  * {{ box-sizing: border-box; }}

  html, body {{
    margin: 0; padding: 0; background: #fff; color: #000;
    font-family: {_FONT_STACK};
    -webkit-font-smoothing: none;
  }}

  .thermal {{
    width: {PAGE_WIDTH_MM:g}mm;
    padding: 4mm {SIDE_PADDING_MM:g}mm 7mm;
    margin: 0 auto;
    background: #fff; color: #000;
  }}

  /* Every rule solid black: a dotted or grey line can vanish on thermal paper. */
  .tdiv {{ margin: 2.6mm 0; border-top: 1px solid #000; }}

  .tmark {{ text-align: center; line-height: 1; }}
  .tbrand {{
    margin-top: 1.5mm; text-align: center;
    font-size: 15pt; font-weight: 800; line-height: 1.15;
  }}
  .tsub {{
    margin-top: 1mm; text-align: center;
    font-size: 7.5pt; font-weight: 700; letter-spacing: .1em;
  }}

  .tfield {{ margin: 2.2mm 0; }}
  .tlabel {{
    font-size: 7pt; font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
  }}
  .tvalue {{ margin-top: .6mm; font-size: 10.5pt; font-weight: 700; line-height: 1.25; }}
  /* Booking number, visit date and last admission must read at arm's length. */
  .tbig {{ font-size: 14pt; font-weight: 800; letter-spacing: .04em; }}
  .tnote {{ margin-top: .6mm; font-size: 8.5pt; font-weight: 700; }}

  .tsection {{
    margin: 2.6mm 0 1.2mm; font-size: 8.5pt; font-weight: 800;
    text-transform: uppercase; letter-spacing: .05em;
  }}

  .trow {{
    display: flex; justify-content: space-between; gap: 3mm;
    padding: 1.1mm 0; font-size: 8.5pt; font-weight: 600;
  }}
  .trow + .trow {{ border-top: 1px solid #000; }}
  .tstrong {{ font-size: 13pt; font-weight: 800; padding-top: 1.8mm; }}

  /* QR block. Nothing may sit beside or behind the code, and the shell keeps a
     white quiet zone of its own on top of the four modules the encoder adds. */
  .tqr {{
    width: {QR_SIZE_MM:g}mm; height: {QR_SIZE_MM:g}mm;
    margin: 3.2mm auto 1.6mm;
    background: #fff;
  }}
  .tqr svg {{ display: block; width: 100%; height: 100%; }}

  .tscan {{
    margin: 1.6mm 0; padding: 1.9mm 1mm;
    background: #000; color: #fff;
    text-align: center; font-size: 10pt; font-weight: 800; letter-spacing: .06em;
    /* Keep the inverse band solid when a browser strips backgrounds. */
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }}
  .thelp {{ margin-top: 1.6mm; font-size: 7.5pt; font-weight: 600; line-height: 1.4; text-align: center; }}
  .tref {{ margin-top: 1.4mm; font-size: 8pt; font-weight: 700; text-align: center; }}

  /* On screen the ticket is previewed on a neutral backdrop so the operator can
     see the paper edges; printing drops all of that. */
  @media screen {{
    body {{ background: #e9eef2; padding: 18px 0 40px; }}
    .thermal {{ box-shadow: 0 6px 20px rgba(0,0,0,.18); }}
    .toolbar {{
      width: {PAGE_WIDTH_MM:g}mm; margin: 0 auto 12px; text-align: center;
      font-family: {_FONT_STACK}; font-size: 12px; color: #33475b;
    }}
  }}
  @media print {{
    .toolbar {{ display: none !important; }}
    body {{ background: #fff; padding: 0; }}
    .thermal {{ box-shadow: none; }}
  }}
"""


def render_thermal_ticket(
    payload: dict[str, Any], *, standalone: bool = True, auto_print: bool = False
) -> str:
    """Render the 80mm gate ticket."""
    t = translator(payload.get("language"))
    lang = payload.get("language") or "en"

    venue = payload["venue"]
    booking = payload["booking"]
    ticket = payload["ticket"]
    money = payload["money"]

    valid_from = ticket.get("valid_from") or {}
    valid_until = ticket.get("valid_until") or {}
    validity = " - ".join(
        part for part in (valid_from.get("time"), valid_until.get("time_seconds")) if part
    )

    parts: list[str] = [
        f'<div class="tmark">{_brand_mark()}</div>',
        f'<div class="tbrand">{escape(venue["name"])}</div>',
        f'<div class="tsub">{escape(t("admission_ticket"))}</div>',
        _divider(),
        _field(t("booking_number"), booking["number"], big=True),
        _field(t("visit_date"), _visit_line(booking), big=True),
    ]

    if validity:
        note = ""
        if venue.get("last_admission"):
            note = f'{t("last_admission")}: {venue["last_admission"]}'
        parts.append(_field(t("valid_time"), validity, note=note))
    elif venue.get("last_admission"):
        parts.append(_field(t("last_admission"), venue["last_admission"], big=True))

    if ticket.get("entry_location"):
        parts.append(_field(t("entry_location"), ticket["entry_location"]))
    if booking.get("customer_name"):
        parts.append(_field(t("customer"), booking["customer_name"]))

    # ---- ticket lines --------------------------------------------------- #
    lines = booking.get("lines") or []
    if lines:
        parts.append(_divider())
        parts.append(f'<div class="tsection">{escape(t("ticket_details"))}</div>')
        for line in lines:
            parts.append(_row(line["name"], f'x {line["quantity"]}'))

    # ---- QR ------------------------------------------------------------- #
    # Inline SVG so the printer's own resolution is used, not a scaled bitmap.
    qr_markup = qr_svg(
        ticket["qr_payload"],
        level="M",
        dark="#000000",
        light="#ffffff",
        title=t("qr_alt"),
    )
    parts.append(f'<div class="tqr">{qr_markup}</div>')
    parts.append(f'<div class="tscan">{escape(t("scan_at_entrance"))}</div>')
    parts.append(f'<div class="thelp">{escape(t("entrance_note"))}</div>')
    parts.append(
        f'<div class="tref">{escape(t("ticket_number"))}: {escape(ticket["number"])}</div>'
    )

    # ---- money ---------------------------------------------------------- #
    parts.append(_divider())
    parts.append(_row(t("subtotal"), money["subtotal"]["text"]))
    if money["has_discount"]:
        parts.append(_row(t("discount"), "-" + money["discount"]["text"]))
    if money["has_service_charge"]:
        label = t("service_charge")
        if money["service_charge_rate_bp"]:
            label = f'{label} {money["service_charge_rate_text"]}%'
        parts.append(_row(label, money["service_charge"]["text"]))
    if money["has_vat"]:
        label = f'{t("vat")} {money["vat_rate_text"]}%'
        if money["vat_included"]:
            label = f'{label} ({t("included")})'
        parts.append(_row(label, money["vat"]["text"]))
    parts.append(_row(t("total"), money["total"]["text"], strong=True))

    # ---- support -------------------------------------------------------- #
    parts.append(_divider())
    support = [venue.get("support_phone") or "", venue.get("support_email") or ""]
    support_html = "<br>".join(escape(bit) for bit in support if bit)
    parts.append(
        f'<div class="thelp">{escape(t("need_help"))}<br>{support_html}'
        f'<br><br>{escape(t("thank_you"))}</div>'
    )

    ticket_html = f'<section class="thermal">{"".join(parts)}</section>'
    if not standalone:
        return ticket_html

    # External same-origin script; see web/print.js for why it is not inline.
    script = '<script src="/print.js" defer></script>' if auto_print else ""
    return f"""<!DOCTYPE html>
<html lang="{escape(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{escape(t("admission_ticket"))} — {escape(booking["number"])}</title>
<style>{_STYLES}</style>{script}
</head>
<body>
<div class="toolbar">{escape(t("print_thermal"))} · {PAGE_WIDTH_MM:g}mm · Star MCP31LB</div>
{ticket_html}
</body>
</html>
"""


def _visit_line(booking: dict[str, Any]) -> str:
    session = booking.get("session_time")
    return f'{booking["visit_date"]} {session}' if session else booking["visit_date"]
