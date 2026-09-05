"""The e-ticket and the 80mm thermal ticket, against a real seeded booking.

Two artefacts are being defended here, and they fail in different ways:

* the **e-ticket** is what the guest keeps, so its job is to be correct,
  complete, legible on a phone and free of anything that leaks;
* the **thermal ticket** is what a counter hands over, so its job is to survive
  a Star MCP31LB — 80mm stock, 72mm printable, 203 dpi, no gradients, no grey.

The rules asserted below are the ones with a real consequence:

* every admitted person gets their **own** QR (a shared code turns the rest of
  the party away at the gate);
* the QR contains only the opaque signed credential, never personal data;
* validity is shown in the **venue's** timezone (raw UTC would tell a Bangkok
  guest their ticket expires seven hours early);
* money is never recomputed for display, and a settings change afterwards must
  not move an issued ticket;
* the thermal QR stays inside the 45-50mm band, which is what keeps the module
  size above the ~3 printer dots where thermal scanning starts to fail.
"""

from __future__ import annotations

import datetime as _dt
import re
import unittest

import seed
from utp.app import Platform
from utp.services.booking import QuoteLineRequest
from utp.ticketdesign import qr
from utp.ticketdesign.email_ticket import render_eticket_document, render_email_ticket
from utp.ticketdesign.payload import ticket_render_payload
from utp.ticketdesign.strings import TICKET_STRINGS, ticket_text
from utp.ticketdesign.thermal import PRINT_WIDTH_MM, QR_SIZE_MM, render_thermal_ticket


class TicketDesignTests(unittest.TestCase):
    """One seeded booking, shared across the assertions to keep the suite quick."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = Platform(db_path=":memory:")
        info = seed.provision(cls.platform)
        cls.tenant_id = info["tenant_id"]
        cls.venue_id = info["venue_id"]
        cls.ticket_types = info["ticket_types"]
        cls.staff_ctx = cls.platform.system_context(cls.tenant_id).for_venue(cls.venue_id)
        cls.visit_date = (_dt.date.today() + _dt.timedelta(days=6)).isoformat()

        guest = cls.platform.guest_context(
            cls.tenant_id, venue_id=cls.venue_id, channel="ONLINE", language="en"
        )
        quote = cls.platform.booking.quote(
            guest,
            venue_id=cls.venue_id,
            visit_date=cls.visit_date,
            lines=[
                QuoteLineRequest(ticket_type_id=cls.ticket_types["GA-INTL-ADULT"], quantity=2),
                QuoteLineRequest(ticket_type_id=cls.ticket_types["GA-INTL-CHILD"], quantity=1),
            ],
        )
        quote = cls.platform.booking.start_checkout(guest, quote)
        cls.result = cls.platform.booking.confirm(
            guest,
            quote,
            customer={
                "email": "somchai@example.test",
                "full_name": "Somchai Jaidee",
                "phone": "+66811234567",
            },
            consent_items={"BOOKING_SERVICE": True, "MARKETING": False},
            payment_method="CARD",
            idempotency_key="design-tests",
        )
        cls.ticket_id = cls.result["tickets"][0]["id"]

    def payload(self, language: str = "en") -> dict:
        return ticket_render_payload(
            self.platform, self.staff_ctx, ticket_id=self.ticket_id, language=language
        )

    # ------------------------------------------------------------------ #
    # Payload
    # ------------------------------------------------------------------ #

    def test_payload_carries_every_field_the_templates_need(self) -> None:
        p = self.payload()
        self.assertEqual(p["venue"]["name"], "Aquaria Phuket")
        self.assertEqual(p["venue"]["timezone"], "Asia/Bangkok")
        self.assertEqual(p["venue"]["last_admission"], "18:00")
        self.assertTrue(p["venue"]["support_email"])
        self.assertTrue(p["venue"]["support_phone"])
        self.assertEqual(p["booking"]["number"], self.result["booking_number"])
        self.assertEqual(p["booking"]["customer_name"], "Somchai Jaidee")
        self.assertEqual(p["booking"]["ticket_count"], 3)
        self.assertTrue(p["ticket"]["number"])
        self.assertTrue(p["ticket"]["qr_payload"].startswith("UTP1."))

    def test_validity_is_expressed_in_the_venue_timezone(self) -> None:
        """23:59:59 local, not the UTC instant it is stored as (R1.9, §14)."""
        p = self.payload()
        self.assertEqual(p["ticket"]["valid_until"]["time_seconds"], "23:59:59")
        self.assertEqual(p["ticket"]["valid_until"]["date"], self.visit_date)
        self.assertIn("+07:00", p["ticket"]["valid_until"]["iso"])
        self.assertEqual(p["ticket"]["valid_from"]["time_seconds"], "00:00:00")

    def test_ticket_lines_come_from_the_booking_with_quantities(self) -> None:
        lines = self.payload()["booking"]["lines"]
        self.assertEqual([(line["name"], line["quantity"]) for line in lines],
                         [("Adult(s)", 2), ("Child(ren)", 1)])

    def test_money_is_formatted_not_divided_by_a_hundred(self) -> None:
        money = self.payload()["money"]
        self.assertEqual(money["currency"], "THB")
        self.assertEqual(money["decimals"], 2)
        # Aquaria is 7% VAT inclusive.
        self.assertEqual(money["vat_rate_text"], "7")
        self.assertTrue(money["vat_included"])
        self.assertTrue(money["has_vat"])
        # 2 adults at 1,251 + 1 child at 675 = 3,177.
        self.assertEqual(money["total"]["minor"], 317_700)
        self.assertIn("3,177", money["total"]["text"])

    def test_line_totals_sum_to_the_charged_total(self) -> None:
        p = self.payload()
        lines = sum(line["line_total"]["minor"] for line in p["booking"]["lines"])
        self.assertEqual(lines, p["money"]["total"]["minor"])

    def test_payload_is_translated(self) -> None:
        thai = self.payload("th")
        self.assertEqual(thai["venue"]["name"], "อควาเรีย ภูเก็ต")
        self.assertEqual(
            [line["name"] for line in thai["booking"]["lines"]], ["ผู้ใหญ่", "เด็ก"]
        )
        # The credential must not change with display language (ticketDesign.md).
        self.assertEqual(thai["ticket"]["qr_payload"], self.payload("en")["ticket"]["qr_payload"])

    # ------------------------------------------------------------------ #
    # E-ticket
    # ------------------------------------------------------------------ #

    def test_eticket_states_the_hierarchy_the_brief_requires(self) -> None:
        html = render_email_ticket(self.payload())
        for needle in (
            "Your booking is confirmed",       # 1. confirmed
            "Aquaria Phuket",                  # 2. venue branding
            "Booking Number",                  # 3. booking number
            self.result["booking_number"],
            "Visit Date",                      # 4. date and validity
            "Valid Time",
            "Last admission: 18:00",
            "Entrance Access",                 # 5. the QR
            "SCAN AT ENTRANCE",
            "Ticket Details",                  # 6. what was bought
            "TOTAL",                           # 7. total
        ):
            self.assertIn(needle, html, f"e-ticket is missing {needle!r}")

    def test_eticket_qr_is_a_real_embedded_image_with_a_white_panel(self) -> None:
        html = render_email_ticket(self.payload())
        self.assertIn('src="data:image/png;base64,', html)
        self.assertIn('alt="Entrance access QR code"', html)
        # Nothing tonal may sit behind the code.
        shell = re.search(r"\.qr-shell \{\{?(.*?)\}\}?", html, re.S)
        self.assertIsNotNone(shell)
        self.assertIn("background: #ffffff", shell.group(1))
        self.assertNotIn("gradient", shell.group(1))

    def test_eticket_never_contains_the_qr_payload_as_text(self) -> None:
        """The credential belongs in the image, not in copyable page text."""
        p = self.payload()
        html = render_email_ticket(p)
        self.assertNotIn(p["ticket"]["qr_payload"], html)

    def test_eticket_is_responsive_and_printable(self) -> None:
        html = render_email_ticket(self.payload())
        self.assertIn("@media (max-width: 720px)", html)
        self.assertIn("grid-template-columns: 1fr", html)
        self.assertIn("@media print", html)

    def test_eticket_escapes_customer_supplied_text(self) -> None:
        p = self.payload()
        p["booking"]["customer_name"] = '<script>alert("x")</script>'
        html = render_email_ticket(p)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_eticket_has_no_external_references(self) -> None:
        html = render_email_ticket(self.payload())
        stripped = html.replace("http://www.w3.org", "")
        self.assertNotIn("http://", stripped)
        self.assertNotIn("https://", stripped)

    def test_every_language_renders_without_a_missing_label(self) -> None:
        for language in TICKET_STRINGS:
            html = render_email_ticket(self.payload(language))
            self.assertNotIn("{{", html)
            self.assertIn("SCAN", html.upper())

    # ------------------------------------------------------------------ #
    # One document, one QR per admitted person
    # ------------------------------------------------------------------ #

    def test_document_renders_one_card_and_one_distinct_qr_per_ticket(self) -> None:
        tickets = self.platform.tickets.list_for_booking(self.staff_ctx, self.result["booking_id"])
        payloads = [
            ticket_render_payload(self.platform, self.staff_ctx, ticket_id=t["id"], language="en")
            for t in tickets
        ]
        html = render_eticket_document(payloads)
        self.assertEqual(html.count('class="ticket-card"'), 3)
        self.assertEqual(html.count('class="ticket-counter"'), 3)
        # One confirmation header for the whole message, not one per ticket.
        self.assertEqual(html.count('class="confirmation-header"'), 1)
        images = re.findall(r'src="(data:image/png;base64,[^"]+)"', html)
        self.assertEqual(len(images), 3)
        self.assertEqual(len(set(images)), 3, "tickets share a QR image")
        # Each ticket prints on its own sheet.
        self.assertIn("break-before: page", html)

    def test_document_requires_at_least_one_ticket(self) -> None:
        with self.assertRaises(ValueError):
            render_eticket_document([])

    # ------------------------------------------------------------------ #
    # Thermal ticket
    # ------------------------------------------------------------------ #

    def test_thermal_is_sized_for_the_mcp31lb(self) -> None:
        html = render_thermal_ticket(self.payload())
        self.assertEqual(PRINT_WIDTH_MM, 72.0)
        self.assertIn("size: 80mm auto", html)
        # 4mm each side of an 80mm page leaves exactly the 72mm printable width.
        self.assertIn("padding: 4mm 4mm 7mm", html)
        self.assertIn("width: 80mm", html)

    def test_thermal_qr_is_in_the_specified_size_band(self) -> None:
        self.assertGreaterEqual(QR_SIZE_MM, 45.0)
        self.assertLessEqual(QR_SIZE_MM, 50.0)
        html = render_thermal_ticket(self.payload())
        self.assertIn("width: 46mm; height: 46mm", html)

    def test_thermal_qr_prints_above_the_thermal_scanning_floor(self) -> None:
        """At 203 dpi the module must stay well above ~3 dots to scan reliably."""
        payload = self.payload()
        code = qr.encode(payload["ticket"]["qr_payload"], level="M")
        span = code.size + qr.QUIET_ZONE * 2
        dots_per_module = (QR_SIZE_MM * 8) / span      # MCP31LB: 8 dots/mm
        self.assertGreater(dots_per_module, 4.0, f"only {dots_per_module:.1f} dots per module")

    def test_thermal_is_pure_black_and_white(self) -> None:
        html = render_thermal_ticket(self.payload())
        self.assertNotIn("gradient", html)
        # No light grey anywhere: it does not survive a thermal head.
        for grey in ("#777", "#888", "#999", "#aaa", "#ccc"):
            self.assertNotIn(grey, html, f"thermal ticket uses grey {grey}")
        self.assertIn("border-top: 1px solid #000", html)

    def test_thermal_shows_the_operationally_critical_values(self) -> None:
        html = render_thermal_ticket(self.payload())
        self.assertIn(self.result["booking_number"], html)
        self.assertIn("ADMISSION TICKET", html)
        self.assertIn("Last admission: 18:00", html)
        self.assertIn("SCAN AT ENTRANCE", html)
        self.assertIn("TOTAL", html)
        self.assertIn("x 2", html)          # ticket quantities
        self.assertIn("tbig", html)         # booking number and date set large

    def test_thermal_qr_is_inline_svg_so_the_printer_rasterises_it(self) -> None:
        html = render_thermal_ticket(self.payload())
        self.assertIn('shape-rendering="crispEdges"', html)
        self.assertNotIn("data:image/png", html)

    def test_thermal_qr_decodes_back_to_the_access_credential(self) -> None:
        """The printed code must be the real credential, and carry no personal data."""
        payload = self.payload()
        html = render_thermal_ticket(payload)
        element = re.search(r'<svg[^>]*shape-rendering="crispEdges".*?</svg>', html, re.S)
        self.assertIsNotNone(element, "no QR SVG in the thermal ticket")
        markup = element.group(0)
        span = int(re.search(r'viewBox="0 0 (\d+)', markup).group(1))
        size = span - qr.QUIET_ZONE * 2
        cells = {
            (int(x) - qr.QUIET_ZONE, int(y) - qr.QUIET_ZONE)
            for x, y in re.findall(r"M(\d+) (\d+)h1v1h-1z", markup)
        }
        self.assertEqual(size, qr.encode(payload["ticket"]["qr_payload"]).size)
        expected = qr.encode(payload["ticket"]["qr_payload"])
        rendered = [[(x, y) in cells for x in range(size)] for y in range(size)]
        self.assertEqual(rendered, expected.modules, "rendered QR differs from the encoding")
        for secret in ("Somchai", "somchai@example.test", "+66811234567"):
            self.assertNotIn(secret, payload["ticket"]["qr_payload"])

    def test_thermal_grows_with_the_number_of_lines(self) -> None:
        """Height is auto: a longer order must print a longer ticket, not clip."""
        one_line = self.payload()
        one_line["booking"]["lines"] = one_line["booking"]["lines"][:1]
        many = self.payload()
        many["booking"]["lines"] = many["booking"]["lines"] * 6
        self.assertGreater(
            len(render_thermal_ticket(many)), len(render_thermal_ticket(one_line))
        )

    def test_thermal_renders_in_every_language(self) -> None:
        for language in TICKET_STRINGS:
            html = render_thermal_ticket(self.payload(language))
            self.assertIn("size: 80mm auto", html)
            self.assertIn("Noto Sans Thai", html)      # Thai must not clip (R69.8)

    # ------------------------------------------------------------------ #
    # Auto-print
    # ------------------------------------------------------------------ #

    def test_print_script_is_opt_in(self) -> None:
        self.assertNotIn("/print.js", render_email_ticket(self.payload()))
        self.assertNotIn("/print.js", render_thermal_ticket(self.payload()))
        self.assertIn("/print.js", render_email_ticket(self.payload(), auto_print=True))
        self.assertIn("/print.js", render_thermal_ticket(self.payload(), auto_print=True))

    # ------------------------------------------------------------------ #
    # Snapshot immutability
    # ------------------------------------------------------------------ #

    def test_a_later_vat_change_does_not_move_an_issued_ticket(self) -> None:
        """A reprint must reproduce the ticket that was issued (§33)."""
        before = self.payload()["money"]
        self.platform.settings.set_vat(
            self.staff_ctx,
            venue_id=self.venue_id,
            enabled=True,
            rate_bp=2000,
            mode="EXCLUSIVE",
            effective_from=_dt.date.today().isoformat(),
            reason="ticket design test: prove the snapshot holds",
        )
        after = self.payload()["money"]
        self.assertEqual(before["total"]["minor"], after["total"]["minor"])
        self.assertEqual(before["vat_rate_text"], after["vat_rate_text"])
        self.assertEqual(after["vat_rate_text"], "7")

    def test_a_later_timezone_change_does_not_move_the_expiry(self) -> None:
        before = self.payload()["ticket"]["valid_until"]
        self.platform.settings.set_timezone(
            self.staff_ctx,
            venue_id=self.venue_id,
            timezone="Asia/Tokyo",
            reason="ticket design test: prove the validity snapshot holds",
        )
        after = self.payload()["ticket"]["valid_until"]
        self.assertEqual(before, after)
        self.assertIn("+07:00", after["iso"])


class TicketStringTests(unittest.TestCase):
    def test_every_language_defines_every_key(self) -> None:
        english = set(TICKET_STRINGS["en"])
        for language, table in TICKET_STRINGS.items():
            self.assertEqual(
                english - set(table), set(), f"{language} is missing ticket labels"
            )

    def test_lookup_falls_back_to_english_then_to_the_key(self) -> None:
        self.assertEqual(ticket_text("total", "en"), "TOTAL")
        self.assertEqual(ticket_text("total", "xx"), "TOTAL")
        self.assertEqual(ticket_text("no_such_label", "en"), "no_such_label")

    def test_parameters_are_substituted(self) -> None:
        self.assertEqual(ticket_text("ticket_of", "en", index=2, total=4), "Ticket 2 of 4")

    def test_five_languages_are_supported(self) -> None:
        self.assertEqual(set(TICKET_STRINGS), {"en", "th", "zh", "ja", "ru"})


class ConfirmationEmailTests(unittest.TestCase):
    """The confirmation message must carry the designed e-ticket."""

    def setUp(self) -> None:
        self.platform = Platform(db_path=":memory:")
        info = seed.provision(self.platform)
        self.tenant_id = info["tenant_id"]
        self.venue_id = info["venue_id"]
        self.staff_ctx = self.platform.system_context(self.tenant_id).for_venue(self.venue_id)
        guest = self.platform.guest_context(
            self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en"
        )
        quote = self.platform.booking.quote(
            guest,
            venue_id=self.venue_id,
            visit_date=(_dt.date.today() + _dt.timedelta(days=8)).isoformat(),
            lines=[QuoteLineRequest(ticket_type_id=info["ticket_types"]["GA-INTL-ADULT"], quantity=2)],
        )
        quote = self.platform.booking.start_checkout(guest, quote)
        self.result = self.platform.booking.confirm(
            guest,
            quote,
            customer={"email": "guest@example.test", "full_name": "Anong Suksawat"},
            consent_items={"BOOKING_SERVICE": True, "MARKETING": False},
            payment_method="CARD",
            idempotency_key="email-design",
        )
        self.platform.notifications.dispatch_due(self.staff_ctx)
        self.sent = self.platform.notifications.provider.sent

    def test_confirmation_carries_the_html_eticket(self) -> None:
        html = next((m.get("html") for m in self.sent if m.get("html")), None)
        self.assertIsNotNone(html, "no HTML e-ticket was attached")
        self.assertIn("SCAN AT ENTRANCE", html)
        self.assertIn(self.result["booking_number"], html)
        self.assertEqual(html.count('class="ticket-card"'), 2)

    def test_plain_text_body_survives_alongside_the_html(self) -> None:
        """A client that cannot render HTML must still get a complete message."""
        message = self.sent[0]
        self.assertTrue(message["body"])
        self.assertIn(self.result["booking_number"], message["body"])
        self.assertNotIn("{{", message["body"])

    def test_html_is_stored_on_the_message_row_for_the_log(self) -> None:
        row = self.platform.db.query_one(
            "SELECT rendered_html FROM notification_messages "
            "WHERE tenant_id = ? AND event_type = 'BOOKING_CONFIRMATION'",
            (self.tenant_id,),
        )
        self.assertIsNotNone(row["rendered_html"])
        self.assertIn("ticket-card", row["rendered_html"])

    def test_a_renderer_failure_does_not_block_the_email(self) -> None:
        """The ticket is still retrievable from Manage Booking (R37.13)."""

        def broken(ctx, booking_id, language=None):
            raise RuntimeError("renderer exploded")

        self.platform.notifications.eticket_renderer = broken
        guest = self.platform.guest_context(
            self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en"
        )
        quote = self.platform.booking.quote(
            guest,
            venue_id=self.venue_id,
            visit_date=(_dt.date.today() + _dt.timedelta(days=9)).isoformat(),
            lines=[
                QuoteLineRequest(
                    ticket_type_id=self.platform.db.query_one(
                        "SELECT id FROM ticket_types WHERE tenant_id = ? AND code = 'GA-INTL-ADULT'",
                        (self.tenant_id,),
                    )["id"],
                    quantity=1,
                )
            ],
        )
        quote = self.platform.booking.start_checkout(guest, quote)
        result = self.platform.booking.confirm(
            guest,
            quote,
            customer={"email": "resilient@example.test", "full_name": "Test Guest"},
            consent_items={"BOOKING_SERVICE": True, "MARKETING": False},
            payment_method="CARD",
            idempotency_key="renderer-failure",
        )
        self.assertTrue(result["confirmed"])
        self.platform.notifications.dispatch_due(self.staff_ctx)
        delivered = [m for m in self.platform.notifications.provider.sent
                     if m["to"] == "resilient@example.test"]
        self.assertTrue(delivered, "email was not sent when the renderer failed")
        self.assertIsNone(delivered[0].get("html"))
        self.assertTrue(delivered[0]["body"])


if __name__ == "__main__":
    unittest.main()
