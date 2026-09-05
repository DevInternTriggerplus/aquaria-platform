"""One render payload shared by the e-ticket and the thermal ticket.

Both templates read from this and nothing else. That is deliberate: the two
designs are intentionally different, but they must never disagree about what a
guest bought, when it is valid or what they paid. A single builder also means a
new field is added once, and the mail, the browser and the printer all show it.

Two properties are load-bearing:

* **Venue-local times.** ``valid_from`` / ``valid_until`` are stored as UTC
  instants with the venue's timezone snapshotted beside them. A ticket that says
  "valid until 23:59:59" must say so in the venue's local time, so every instant
  is converted through the ticket's own ``validity_timezone`` — not the server's
  clock and not the venue's *current* setting (R1.9, add_features §14).
* **Snapshot, not recomputation.** Money comes from the booking row and its
  ``charge_snapshot_json``, the same discipline ``documents._document_payload``
  follows. Reprinting a ticket a year later must reproduce the ticket that was
  issued, not reprice it against today's VAT (add_features §33).
"""

from __future__ import annotations

from typing import Any

from ..core.clock import local, parse_instant
from ..core.context import RequestContext
from ..core.db import decode
from ..core.i18n import text as i18n_text
from ..core.money import currency_decimals, format_currency


def _local_parts(instant_text: str | None, tz_name: str) -> dict[str, str] | None:
    """Split a stored UTC instant into venue-local display pieces."""
    if not instant_text:
        return None
    moment = local(parse_instant(instant_text), tz_name)
    return {
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M"),
        "time_seconds": moment.strftime("%H:%M:%S"),
        "iso": moment.isoformat(),
    }


def ticket_render_payload(
    platform: Any,
    ctx: RequestContext,
    *,
    ticket_id: str,
    language: str | None = None,
) -> dict[str, Any]:
    """Everything either ticket template needs, resolved and localized.

    ``platform`` is the composition root, so this function stays a pure
    assembler over the existing services rather than a second source of truth
    for tickets, bookings, venues or money.
    """
    lang = language or ctx.language or "en"

    presentation = platform.tickets.presentation(ctx, ticket_id, language=lang)
    ticket_row = platform.tickets.get(ctx, ticket_id, include_qr=True)
    booking = platform.booking.get_booking(ctx, ticket_row["booking_id"], mask=False)
    venue = platform.tenancy.get_venue(ctx, ticket_row["venue_id"])

    tz_name = ticket_row.get("validity_timezone") or venue["timezone"]
    currency = booking["currency"]

    hours = (venue.get("operating_hours") or {}).get("default") or {}
    contact = venue.get("contact") or {}
    address = venue.get("address") or {}

    # ---- money -------------------------------------------------------- #
    # Read the snapshot taken at confirmation; fall back to the venue's tax
    # configuration only for older rows that predate the settings module.
    snapshot = decode(booking.get("charge_snapshot_json"), {}) or {}
    gross = int(booking.get("gross_minor") or 0)
    discount = int(booking.get("discount_minor") or 0)
    service_charge = int(booking.get("service_charge_minor") or 0)
    tax = int(booking.get("tax_minor") or 0)
    net = int(booking.get("net_minor") or 0)
    settlement = int(booking.get("settlement_minor") or 0)

    vat_rate_bp = int(snapshot.get("vat_rate_bp") or venue.get("tax_rate_bp") or 0)
    vat_included = bool(snapshot.get("vat_included", (venue.get("tax_model") == "INCLUSIVE")))
    service_included = bool(snapshot.get("service_charge_included", False))
    subtotal = int(snapshot.get("taxable_base_minor") or (gross - discount))

    def amount(minor: int) -> dict[str, Any]:
        """Both the formatted string and the raw minor units.

        Templates must never divide by 100 themselves — JPY has no decimal
        places, and a ticket showing the wrong magnitude is worse than one
        showing no price at all.
        """
        return {"minor": minor, "text": format_currency(minor, currency, locale=lang)}

    # ---- ticket lines ------------------------------------------------- #
    # Joined the same way documents._invoice_lines does, so the ticket's
    # "Ticket Details" table and the tax invoice describe identical lines.
    rows = platform.db.query(
        """
        SELECT bi.quantity, bi.unit_price_minor, bi.gross_minor, bi.net_minor,
               tt.name_json AS tt_name, s.name_json AS seg_name
        FROM booking_items bi
        JOIN ticket_types tt ON tt.id = bi.ticket_type_id AND tt.tenant_id = bi.tenant_id
        JOIN customer_segments s ON s.id = bi.segment_id AND s.tenant_id = bi.tenant_id
        WHERE bi.tenant_id = ? AND bi.booking_id = ? AND bi.state = 'ACTIVE'
        ORDER BY bi.created_at
        """,
        (ctx.tenant_id, booking["id"]),
    )
    lines = [
        {
            "name": i18n_text(decode(row["tt_name"], {}), lang),
            "segment": i18n_text(decode(row["seg_name"], {}), lang),
            "quantity": int(row["quantity"]),
            "unit_price": amount(int(row["unit_price_minor"])),
            "line_total": amount(int(row["gross_minor"])),
        }
        for row in rows
    ]

    # ---- customer ----------------------------------------------------- #
    # Unmasked deliberately: this is the guest's own ticket. Nothing here is
    # ever written into the QR (R15.2) and staff-facing surfaces still require
    # VIEW_PII, which is enforced by the endpoints, not by this assembler.
    customer = booking.get("customer") or {}
    customer_name = (customer.get("full_name") or "").strip() or None

    valid_from = _local_parts(presentation.get("valid_from"), tz_name)
    valid_until = _local_parts(presentation.get("valid_until"), tz_name)

    tickets_in_booking = platform.tickets.list_for_booking(ctx, booking["id"])

    return {
        "language": lang,
        # ---- venue / brand ------------------------------------------- #
        "venue": {
            "name": presentation["venue"],
            "tagline": _tagline(venue, address, lang),
            "logo_url": venue.get("logo_url") or None,
            "timezone": tz_name,
            "address_line": address.get("line1") or "",
            "city": address.get("city") or "",
            "support_email": contact.get("email") or "",
            "support_phone": contact.get("phone") or "",
            "open_time": hours.get("open") or "",
            "close_time": hours.get("close") or "",
            "last_admission": hours.get("last_admission") or "",
        },
        # ---- booking -------------------------------------------------- #
        "booking": {
            "number": presentation["booking_number"],
            "status": booking.get("status"),
            "visit_date": presentation["visit_date"],
            "session_time": presentation.get("session_time"),
            "customer_name": customer_name,
            "ticket_count": len(tickets_in_booking),
            "lines": lines,
        },
        # ---- this ticket ---------------------------------------------- #
        "ticket": {
            "id": presentation["ticket_id"],
            "number": presentation["ticket_number"],
            "product": presentation["product"],
            "type": presentation["ticket_type"],
            "segment": presentation["segment"],
            "state": presentation["state"],
            "entry_location": presentation.get("entry_location") or "",
            "valid_from": valid_from,
            "valid_until": valid_until,
            "entries_used": presentation["entries_used"],
            "entries_remaining": presentation["entries_remaining"],
            "entry_allowance": int(ticket_row.get("entry_allowance") or 0),
            "unlimited_entries": bool(ticket_row.get("unlimited_entries")),
            "reentry_minutes": ticket_row.get("reentry_window_minutes") or 0,
            "proof_required": bool(ticket_row.get("proof_required")),
            "conditions": presentation.get("conditions") or [],
            "reentry_rules": presentation.get("reentry_rules"),
            # The opaque signed credential, encoded verbatim into the QR.
            "qr_payload": presentation["qr_payload"],
        },
        # ---- money ---------------------------------------------------- #
        "money": {
            "currency": currency,
            "decimals": currency_decimals(currency),
            "subtotal": amount(subtotal),
            "discount": amount(discount),
            "service_charge": amount(service_charge),
            "vat": amount(tax),
            "total": amount(net),
            "settlement": amount(settlement),
            "vat_rate_bp": vat_rate_bp,
            "vat_rate_text": _rate_text(vat_rate_bp),
            "vat_included": vat_included,
            "service_charge_rate_bp": int(snapshot.get("service_charge_rate_bp") or 0),
            "service_charge_rate_text": _rate_text(int(snapshot.get("service_charge_rate_bp") or 0)),
            "service_charge_included": service_included,
            "has_discount": discount > 0,
            "has_service_charge": service_charge > 0,
            "has_vat": tax > 0,
        },
    }


def _rate_text(rate_bp: int) -> str:
    """Basis points as a human percentage, without trailing noise ("7", "7.5")."""
    if not rate_bp:
        return "0"
    whole, remainder = divmod(rate_bp, 100)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:02d}".rstrip("0")


def _tagline(venue: dict[str, Any], address: dict[str, Any], lang: str) -> str:
    """A short line under the venue name.

    There is no dedicated tagline field, and inventing one in code would be
    hardcoding venue copy. So this prefers configuration if a venue supplies
    ``tagline`` in its name map, and otherwise derives a neutral locality line
    from the address the venue already has.
    """
    configured = venue.get("tagline")
    if isinstance(configured, dict) and configured:
        resolved = i18n_text(configured, lang)
        if resolved:
            return resolved
    if isinstance(configured, str) and configured:
        return configured
    parts = [part for part in (address.get("city"), address.get("country")) if part]
    return ", ".join(parts)
