"""The Booking Confirmation / E-Ticket, for email and for the browser.

Structure follows the visual hierarchy the design brief fixes: booking confirmed,
venue branding, booking number, visit date and validity, a large entrance QR,
ticket details, total, support. The QR is the strongest element on the card
because it is the only part a gate actually needs.

Two constraints shaped the markup:

* **Email safety.** Sections are laid out with tables and every rule that
  matters is also written inline, so a client that discards ``<style>`` still
  produces a readable ticket. The stylesheet then adds the polish (grid, radii,
  shadows) for clients and browsers that support it.
* **Nothing behind the QR.** The QR sits on a plain white panel with a real
  quiet zone and no gradient, pattern, logo or text behind it — the single most
  common cause of a scan failing at a gate.

Icons are inline SVG line art, never cartoons, and every icon is paired with a
text label, so the ticket still reads correctly where SVG is stripped and for a
screen reader (ticketDesign.md, R68.3).
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

from .qr import qr_png, qr_png_data_url
from .strings import translator

# --------------------------------------------------------------------------- #
# Palette — Peacock Blue, used for hierarchy and branding only
# --------------------------------------------------------------------------- #

PEACOCK_900 = "#003b4d"
PEACOCK_800 = "#004e64"
PEACOCK_700 = "#00677f"
PEACOCK_600 = "#007f96"
NAVY = "#102a43"
TEXT = "#243b53"
MUTED = "#627d98"
LINE = "#d9e2ec"
BACKGROUND = "#f5f9fb"
WHITE = "#ffffff"

_FONT_STACK = (
    "Inter,'Noto Sans','Noto Sans Thai','Noto Sans SC','Noto Sans JP',"
    "Arial,Helvetica,sans-serif"
)


# --------------------------------------------------------------------------- #
# Icons (stroked line art, 20x20 viewBox, currentColor)
# --------------------------------------------------------------------------- #

def _icon(paths: str, *, size: int = 21) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true" focusable="false">{paths}</svg>'
    )


_ICONS = {
    "ticket": _icon(
        '<path d="M3 9V7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a2 2 0 0 0 0 6v2a2 2 0 0 1-2 2H5'
        'a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-6z"/><path d="M9 5v14" stroke-dasharray="2 2.5"/>'
    ),
    "calendar": _icon(
        '<rect x="3.5" y="5" width="17" height="16" rx="2.5"/><path d="M3.5 10h17"/>'
        '<path d="M8 3v4M16 3v4"/>'
    ),
    "clock": _icon('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    "person": _icon(
        '<circle cx="12" cy="8" r="3.6"/><path d="M5 20a7 7 0 0 1 14 0"/>'
    ),
    "pin": _icon(
        '<path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11z"/>'
        '<circle cx="12" cy="10" r="2.6"/>'
    ),
    "info": _icon('<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><path d="M12 7.8h.01"/>'),
    "check": (
        '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        'focusable="false"><path d="M4.5 12.5l5 5 10-11"/></svg>'
    ),
    "wave": (
        '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true" focusable="false">'
        '<path d="M2 8.5c2.2-2.2 4.1-2.2 6.3 0s4.1 2.2 6.3 0 4.1-2.2 6.3 0"/>'
        '<path d="M2 14c2.2-2.2 4.1-2.2 6.3 0s4.1 2.2 6.3 0 4.1-2.2 6.3 0"/>'
        '<path d="M2 19.5c2.2-2.2 4.1-2.2 6.3 0s4.1 2.2 6.3 0 4.1-2.2 6.3 0"/></svg>'
    ),
}


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def _confirmation_header(t) -> str:
    return f"""
  <div class="confirmation-header">
    <div class="confirmation-icon" aria-hidden="true">{_ICONS["check"]}</div>
    <div>
      <h1 class="confirmation-title">{escape(t("confirmed_title"))}</h1>
      <p class="confirmation-subtitle">{escape(t("confirmed_subtitle"))}</p>
    </div>
  </div>"""


def _hero(venue: dict[str, Any]) -> str:
    """Venue branding. A configured logo wins; otherwise the wave brand mark."""
    if venue.get("logo_url"):
        mark = (
            f'<img src="{escape(str(venue["logo_url"]))}" alt="" width="46" height="46" '
            f'style="display:block;border-radius:50%">'
        )
    else:
        mark = _ICONS["wave"]
    tagline = venue.get("tagline") or ""
    tagline_html = (
        f'<p class="venue-subtitle">{escape(tagline)}</p>' if tagline else ""
    )
    return f"""
    <header class="ticket-hero">
      <div class="brand-row">
        <div class="brand-logo">{mark}</div>
        <div>
          <h2 class="venue-name">{escape(venue["name"])}</h2>
          {tagline_html}
        </div>
      </div>
    </header>"""


def _info_row(icon: str, label: str, value: str, helper: str = "") -> str:
    helper_html = f'<div class="info-helper">{escape(helper)}</div>' if helper else ""
    return f"""
          <div class="info-row">
            <div class="info-icon">{_ICONS[icon]}</div>
            <div>
              <div class="info-label">{escape(label)}</div>
              <div class="info-value">{escape(value)}</div>
              {helper_html}
            </div>
          </div>"""


def _booking_information(payload: dict[str, Any], t) -> str:
    booking = payload["booking"]
    ticket = payload["ticket"]
    venue = payload["venue"]

    valid_from = ticket.get("valid_from") or {}
    valid_until = ticket.get("valid_until") or {}
    validity = " – ".join(
        part for part in (valid_from.get("time"), valid_until.get("time_seconds")) if part
    )
    last_admission = venue.get("last_admission") or ""
    helper = f'{t("last_admission")}: {last_admission}' if last_admission else ""

    rows = [
        _info_row("ticket", t("booking_number"), booking["number"]),
        _info_row("calendar", t("visit_date"), _date_display(booking["visit_date"], booking.get("session_time"))),
    ]
    if validity:
        rows.append(_info_row("clock", t("valid_time"), validity, helper))
    if booking.get("customer_name"):
        rows.append(_info_row("person", t("customer_name"), booking["customer_name"]))
    if ticket.get("entry_location"):
        rows.append(_info_row("pin", t("entry_location"), ticket["entry_location"]))
    return "".join(rows)


def _date_display(visit_date: str, session_time: str | None) -> str:
    return f"{visit_date} · {session_time}" if session_time else visit_date


def _ticket_items(payload: dict[str, Any], t) -> str:
    lines = payload["booking"]["lines"]
    if not lines:
        return ""
    body = "".join(
        f"""
              <tr>
                <td>{escape(line["name"])}</td>
                <td>{line["quantity"]}</td>
              </tr>"""
        for line in lines
    )
    return f"""
          <h3 class="section-title">{escape(t("ticket_details"))}</h3>
          <table class="ticket-table" role="presentation">
            <thead>
              <tr>
                <th scope="col">{escape(t("ticket_type"))}</th>
                <th scope="col">{escape(t("quantity"))}</th>
              </tr>
            </thead>
            <tbody>{body}
            </tbody>
          </table>"""


def data_url_source(payload: dict[str, Any]) -> str:
    """Default QR source: the image inlined as a ``data:`` URL.

    Right for the browser and for a saved page. Note that Gmail strips ``data:``
    images, so the mail path uses :class:`CidSource` instead.
    """
    return qr_png_data_url(payload["ticket"]["qr_payload"], level="M", scale=8)


class CidSource:
    """Emit ``cid:`` references and collect the image bytes to attach.

    This is the delivery mode that actually works everywhere: the QR travels
    inside the message, so it needs no remote fetch and survives Gmail stripping
    ``data:`` URLs. Callers render first, then hand :attr:`images` to
    :func:`utp.services.mail_mime.build_message`, which refuses to send if the
    references and the attachments disagree.
    """

    def __init__(self) -> None:
        self.images: dict[str, bytes] = {}

    def __call__(self, payload: dict[str, Any]) -> str:
        cid = self._cid_for(payload)
        if cid not in self.images:
            self.images[cid] = qr_png(payload["ticket"]["qr_payload"], level="M", scale=8)
        return f"cid:{cid}"

    @staticmethod
    def _cid_for(payload: dict[str, Any]) -> str:
        # A Content-ID must be token-safe, and a ticket number is venue-configured
        # text, so it is reduced rather than trusted. The ticket id disambiguates
        # if two numbers ever normalise to the same thing.
        number = re.sub(r"[^A-Za-z0-9]+", "-", str(payload["ticket"]["number"])).strip("-")
        suffix = str(payload["ticket"]["id"])[-8:]
        return f"qr-{number or 'ticket'}-{suffix}".lower()


class LinkSource:
    """Emit a signed, expiring remote URL for each QR.

    For clients that prefer a fetched image, and for a "view in browser" copy. The
    token is a capability with its own expiry, so the URL needs no session and
    still cannot be used to enumerate tickets.
    """

    def __init__(self, *, base_url: str = "", ttl_days: int | None = None) -> None:
        self.base_url = base_url
        self.ttl_days = ttl_days

    def __call__(self, payload: dict[str, Any]) -> str:
        from .links import DEFAULT_TTL_DAYS, qr_image_url

        return qr_image_url(
            payload["ticket"]["id"],
            base_url=self.base_url,
            ttl_days=self.ttl_days or DEFAULT_TTL_DAYS,
        )


def qr_source_for(mode: str, *, base_url: str = "") -> Any:
    """Resolve the configured delivery mode to a source. Unknown modes fall back."""
    normalized = (mode or "").upper()
    if normalized == "CID":
        return CidSource()
    if normalized == "LINK":
        return LinkSource(base_url=base_url)
    return data_url_source


def _access_qr_panel(payload: dict[str, Any], t, qr_source) -> str:
    """The QR panel.

    Plain white behind the code, a real quiet zone, and no decoration inside the
    shell. The label below is a solid peacock band so the instruction is
    unmistakable without competing with the code itself.

    ``qr_source`` decides how the image arrives — inline data, a ``cid:``
    attachment reference, or a signed remote URL — because that is a delivery
    concern, not a design one, and the layout must not change with it.
    """
    ticket = payload["ticket"]
    src = qr_source(payload)
    return f"""
        <div class="access-panel">
          <div class="access-heading">{escape(t("entrance_access"))}</div>
          <div class="qr-shell">
            <img class="qr-code" src="{escape(src, quote=True)}" alt="{escape(t("qr_alt"))}"
                 width="248" height="248">
          </div>
          <div class="scan-label">{escape(t("scan_at_entrance"))}</div>
          <p class="scan-helper">{escape(t("scan_helper"))}</p>
          <p class="ticket-ref">{escape(t("ticket_number"))}: <strong>{escape(ticket["number"])}</strong></p>
        </div>"""


def _price_summary(payload: dict[str, Any], t) -> str:
    money = payload["money"]
    rows: list[str] = [_price_line(t("subtotal"), money["subtotal"]["text"])]
    if money["has_discount"]:
        rows.append(_price_line(t("discount"), "-" + money["discount"]["text"]))
    if money["has_service_charge"]:
        label = t("service_charge")
        if money["service_charge_rate_bp"]:
            label = f'{label} {money["service_charge_rate_text"]}%'
        if money["service_charge_included"]:
            label = f'{label} ({t("included")})'
        rows.append(_price_line(label, money["service_charge"]["text"]))
    if money["has_vat"]:
        label = f'{t("vat")} {money["vat_rate_text"]}%'
        if money["vat_included"]:
            label = f'{label} ({t("included")})'
        rows.append(_price_line(label, money["vat"]["text"]))
    return f"""
        <div class="price-box">
          {"".join(rows)}
          <div class="price-total">
            <span>{escape(t("total"))}</span>
            <span>{escape(money["total"]["text"])}</span>
          </div>
        </div>"""


def _price_line(label: str, value: str) -> str:
    return (
        f'<div class="price-line"><span>{escape(label)}</span>'
        f"<strong>{escape(value)}</strong></div>"
    )


def _entrance_note(t) -> str:
    return f"""
          <div class="entrance-note">
            <div class="note-icon">{_ICONS["info"]}</div>
            <div>{escape(t("entrance_note"))}</div>
          </div>"""


def _footer(payload: dict[str, Any], t) -> str:
    venue = payload["venue"]
    contact_bits = [venue.get("support_email") or "", venue.get("support_phone") or ""]
    contact = "<br>".join(escape(bit) for bit in contact_bits if bit)
    return f"""
      <footer class="ticket-footer">
        <div class="footer-row">
          <div>
            <div class="support-title">{escape(t("need_help"))}</div>
            <p class="support-text">{contact}</p>
          </div>
          <div class="footer-message">{escape(t("closing_message"))}</div>
        </div>
      </footer>"""


# --------------------------------------------------------------------------- #
# Stylesheet
# --------------------------------------------------------------------------- #

_STYLES = f"""
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: {BACKGROUND}; color: {TEXT};
      font-family: {_FONT_STACK};
      -webkit-text-size-adjust: 100%;
    }}
    .page {{ width: 100%; padding: 32px 16px 60px; }}
    .ticket-wrapper {{ width: 100%; max-width: 880px; margin: 0 auto; }}

    .confirmation-header {{
      display: flex; align-items: center; gap: 16px;
      margin-bottom: 22px; padding: 0 8px;
    }}
    .confirmation-icon {{
      width: 54px; height: 54px; flex: 0 0 54px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(145deg, {PEACOCK_600}, {PEACOCK_900});
      color: #fff; box-shadow: 0 8px 20px rgba(0,91,115,.18);
    }}
    .confirmation-title {{
      margin: 0 0 4px; color: {NAVY}; font-size: 25px; line-height: 1.2; font-weight: 750;
    }}
    .confirmation-subtitle {{ margin: 0; color: {MUTED}; font-size: 15px; line-height: 1.5; }}

    .ticket-card {{
      overflow: hidden; background: {WHITE};
      border: 1px solid rgba(0,79,100,.10); border-radius: 22px;
      box-shadow: 0 18px 50px rgba(20,50,70,.10);
    }}
    /* Several tickets in one booking stack as separate cards. */
    .ticket-card + .ticket-card {{ margin-top: 26px; }}
    .ticket-counter {{
      padding: 11px 38px; background: #eef7f9; border-bottom: 1px solid #d6e8ec;
      color: {PEACOCK_800}; font-size: 12px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .06em;
    }}

    .ticket-hero {{
      position: relative; overflow: hidden; padding: 34px 38px; color: #fff;
      background: linear-gradient(120deg, #00394d 0%, #005a72 55%, #007c8e 100%);
    }}
    .ticket-hero::after {{
      content: ""; position: absolute; right: -120px; bottom: -150px;
      width: 380px; height: 280px; border-radius: 50%;
      background: radial-gradient(circle, rgba(38,198,218,.25), transparent 68%);
    }}
    .brand-row {{ position: relative; z-index: 2; display: flex; gap: 18px; align-items: center; }}
    .brand-logo {{
      width: 76px; height: 76px; flex: 0 0 76px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      border: 2px solid rgba(255,255,255,.8); background: rgba(255,255,255,.10); color: #fff;
    }}
    .venue-name {{ margin: 0 0 5px; font-size: 30px; font-weight: 750; line-height: 1.15; }}
    .venue-subtitle {{
      margin: 0; color: rgba(255,255,255,.78); font-size: 15px;
      font-weight: 500; letter-spacing: .03em;
    }}

    .ticket-content {{
      display: grid; grid-template-columns: minmax(0,1.05fr) minmax(300px,.95fr);
      gap: 34px; padding: 34px 38px 38px;
    }}

    .info-row {{
      display: grid; grid-template-columns: 38px minmax(0,1fr); gap: 14px;
      padding: 17px 0; border-bottom: 1px solid {LINE};
    }}
    .info-row:first-child {{ padding-top: 0; }}
    .info-icon {{
      width: 36px; height: 36px; display: flex; align-items: center;
      justify-content: center; color: {PEACOCK_700};
    }}
    .info-label {{
      margin-bottom: 4px; color: {PEACOCK_700}; font-size: 12px; font-weight: 750;
      text-transform: uppercase; letter-spacing: .06em;
    }}
    .info-value {{ color: {NAVY}; font-size: 17px; font-weight: 650; line-height: 1.45; }}
    .info-helper {{ margin-top: 3px; color: {MUTED}; font-size: 13px; line-height: 1.4; }}

    .section-title {{
      margin: 27px 0 12px; color: {NAVY}; font-size: 15px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .04em;
    }}
    .ticket-table {{ width: 100%; border-collapse: collapse; }}
    .ticket-table th {{
      padding: 8px 0; color: {MUTED}; font-size: 11px; font-weight: 700;
      text-align: left; text-transform: uppercase; letter-spacing: .05em;
    }}
    .ticket-table th:last-child, .ticket-table td:last-child {{ text-align: right; }}
    .ticket-table td {{
      padding: 11px 0; border-bottom: 1px dashed {LINE};
      color: {TEXT}; font-size: 14px; font-weight: 550;
    }}

    .access-panel {{
      padding: 22px; background: linear-gradient(180deg, #ffffff, #f6fcfd);
      border: 1px solid #d1e7eb; border-radius: 18px;
    }}
    .access-heading {{
      margin-bottom: 14px; color: {NAVY}; font-size: 14px; font-weight: 800;
      text-align: center; text-transform: uppercase; letter-spacing: .05em;
    }}
    /* Nothing but white behind the code, and a real quiet zone. */
    .qr-shell {{
      width: 100%; max-width: 292px; margin: 0 auto; padding: 18px;
      background: #ffffff; border: 1px solid #d7e1e6; border-radius: 14px;
    }}
    .qr-code {{
      display: block; width: 100%; height: auto; background: #fff;
      image-rendering: pixelated;
    }}
    .scan-label {{
      margin-top: 15px; padding: 12px 16px; border-radius: 10px;
      background: linear-gradient(90deg, {PEACOCK_800}, {PEACOCK_600});
      color: #fff; font-size: 14px; font-weight: 750;
      text-align: center; letter-spacing: .03em;
    }}
    .scan-helper {{
      max-width: 300px; margin: 12px auto 0; color: {MUTED};
      font-size: 13px; line-height: 1.45; text-align: center;
    }}
    .ticket-ref {{
      margin: 10px 0 0; color: {MUTED}; font-size: 12px; text-align: center;
      letter-spacing: .02em;
    }}
    .ticket-ref strong {{ color: {NAVY}; }}

    .price-box {{
      margin-top: 20px; padding: 19px; border-radius: 15px;
      background: linear-gradient(135deg, #f1fbfc, #e3f5f8);
    }}
    .price-line {{
      display: flex; justify-content: space-between; gap: 20px;
      margin-bottom: 9px; color: {TEXT}; font-size: 14px;
    }}
    .price-total {{
      display: flex; justify-content: space-between; gap: 20px;
      margin-top: 13px; padding-top: 14px; border-top: 1px dashed #9fcbd3;
      color: {NAVY}; font-size: 22px; font-weight: 800;
    }}

    .entrance-note {{
      display: flex; gap: 12px; margin-top: 22px; padding: 14px 16px;
      border: 1px solid #d7e4ea; border-radius: 12px; background: #fafcfd;
      color: {TEXT}; font-size: 14px; line-height: 1.5;
    }}
    .note-icon {{ flex: 0 0 auto; color: {PEACOCK_700}; }}

    .ticket-footer {{ padding: 24px 38px; background: {PEACOCK_900}; color: rgba(255,255,255,.9); }}
    .footer-row {{
      display: flex; align-items: center; justify-content: space-between; gap: 28px;
    }}
    .support-title {{ margin-bottom: 4px; font-size: 14px; font-weight: 750; }}
    .support-text {{ margin: 0; color: rgba(255,255,255,.73); font-size: 13px; line-height: 1.5; }}
    .footer-message {{ color: #6ee7ea; font-size: 15px; font-weight: 650; text-align: right; }}

    @media (max-width: 720px) {{
      .page {{ padding: 20px 10px 40px; }}
      .ticket-content {{ grid-template-columns: 1fr; padding: 26px 22px 30px; }}
      .ticket-hero {{ padding: 27px 24px; }}
      .ticket-counter {{ padding: 10px 22px; }}
      .venue-name {{ font-size: 24px; }}
      .brand-logo {{ width: 62px; height: 62px; flex-basis: 62px; }}
      .confirmation-title {{ font-size: 21px; }}
      .footer-row {{ align-items: flex-start; flex-direction: column; }}
      .footer-message {{ text-align: left; }}
    }}

    /* Printing the digital ticket on an ordinary printer: drop the page
       furniture and let the card sit on white. The thermal ticket is a
       separate document, not this one scaled down. */
    @media print {{
      body {{ background: #fff; }}
      .page {{ padding: 0; }}
      .ticket-card {{ box-shadow: none; border-color: #bbb; }}
      /* One ticket per sheet, and never split a card or its QR across a page. */
      .ticket-card {{ break-inside: avoid; page-break-inside: avoid; }}
      .ticket-card + .ticket-card {{ break-before: page; page-break-before: always; margin-top: 0; }}
      .access-panel {{ break-inside: avoid; page-break-inside: avoid; }}
      .no-print {{ display: none !important; }}
    }}
"""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def render_ticket_card(
    payload: dict[str, Any], *, index: int = 1, total: int = 1, qr_source=None
) -> str:
    """One ticket as a standalone card, with no document wrapper.

    When a booking holds several tickets each card is labelled, because the guest
    needs to know which code belongs to which person and that more follow.
    """
    t = translator(payload.get("language"))
    source = qr_source or data_url_source
    counter = ""
    if total > 1:
        counter = (
            f'<div class="ticket-counter">{escape(t("ticket_of", index=index, total=total))}</div>'
        )
    return f"""
    <section class="ticket-card">{_hero(payload["venue"])}{counter}
      <main class="ticket-content">
        <div>{_booking_information(payload, t)}{_ticket_items(payload, t)}{_entrance_note(t)}
        </div>
        <aside>{_access_qr_panel(payload, t, source)}{_price_summary(payload, t)}
        </aside>
      </main>{_footer(payload, t)}
    </section>"""


def _document(inner: str, *, lang: str, title: str, auto_print: bool = False) -> str:
    # External same-origin script: the CSP mints a nonce per response, so an
    # inline snippet would have to be threaded through this template.
    script = '<script src="/print.js" defer></script>' if auto_print else ""
    return f"""<!DOCTYPE html>
<html lang="{escape(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{escape(title)}</title>
<style>{_STYLES}</style>{script}
</head>
<body>
{inner}
</body>
</html>
"""


def render_eticket_document(
    payloads: list[dict[str, Any]], *, auto_print: bool = False, qr_source=None
) -> str:
    """One document, one confirmation header, and a card per issued ticket.

    Every admitted person gets their own scannable code (R15.1); a single shared
    QR would turn the rest of the party away at the gate.
    """
    if not payloads:
        raise ValueError("at least one ticket payload is required")
    first = payloads[0]
    t = translator(first.get("language"))
    lang = first.get("language") or "en"
    total = len(payloads)
    cards = "".join(
        render_ticket_card(payload, index=i + 1, total=total, qr_source=qr_source)
        for i, payload in enumerate(payloads)
    )
    inner = f"""<div class="page">
  <div class="ticket-wrapper">{_confirmation_header(t)}{cards}
  </div>
</div>"""
    title = f'{t("confirmed_title")} — {first["booking"]["number"]}'
    return _document(inner, lang=lang, title=title, auto_print=auto_print)


def render_email_ticket(
    payload: dict[str, Any],
    *,
    standalone: bool = True,
    auto_print: bool = False,
    qr_source=None,
) -> str:
    """Render a single ticket. ``standalone=False`` returns just the page body."""
    t = translator(payload.get("language"))
    lang = payload.get("language") or "en"
    body = f"""<div class="page">
  <div class="ticket-wrapper">{_confirmation_header(t)}{
        render_ticket_card(payload, qr_source=qr_source)}
  </div>
</div>"""
    if not standalone:
        return body
    title = f'{t("confirmed_title")} — {payload["booking"]["number"]}'
    return _document(body, lang=lang, title=title, auto_print=auto_print)
