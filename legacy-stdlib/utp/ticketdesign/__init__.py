"""Customer-facing ticket artefacts: the digital e-ticket and the thermal ticket.

A ticket is the one thing a guest actually holds, so it is treated here as a
product rather than as a receipt. Two deliberately *separate* designs live in
this package, because a premium colour e-ticket converted to greyscale makes a
poor 80mm thermal ticket and a thermal layout makes a poor email:

* ``email_ticket`` — the Booking Confirmation / E-Ticket. Peacock Blue
  branding, generous whitespace, responsive down to one column, email-safe
  markup (tables for structure, inline styles duplicated alongside the
  stylesheet so clients that strip ``<style>`` still render sensibly).
* ``thermal`` — an 80mm admission ticket for the Star Micronics MCP31LB.
  Pure black on white, no gradients, no light-grey text, a 45-50mm QR and a
  compact vertical rhythm so paper is not wasted.

Both render from one payload (``payload.ticket_render_payload``) and one set of
translated strings (``strings``), so there is a single source of truth for what
a ticket says and only the presentation differs. The QR itself is identical in
both and never varies with display language (ticketDesign.md).

``qr`` is a dependency-free QR encoder: the platform is standard-library only,
and a ticket that cannot be scanned is not a ticket.
"""

from .qr import QrCode, qr_png_data_url, qr_svg

__all__ = [
    "QrCode",
    "qr_svg",
    "qr_png_data_url",
    "ticket_render_payload",
    "render_email_ticket",
    "render_thermal_ticket",
]


def __getattr__(name: str):
    """Import the renderers on first use.

    Keeps ``qr`` importable on its own (it has no platform dependencies, which is
    what lets the encoder be tested in isolation) while still exposing the whole
    package surface from one place.
    """
    if name == "ticket_render_payload":
        from .payload import ticket_render_payload

        return ticket_render_payload
    if name == "render_email_ticket":
        from .email_ticket import render_email_ticket

        return render_email_ticket
    if name == "render_thermal_ticket":
        from .thermal import render_thermal_ticket

        return render_thermal_ticket
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
