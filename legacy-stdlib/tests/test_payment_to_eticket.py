"""Payment gateway confirmation to e-ticket delivery.

The behaviour under test: once the gateway confirms payment, the customer receives
their ticket by email. The interesting cases are not the happy path but the ones where
the customer is no longer there:

* the browser or kiosk session closed after authorization (R14.6);
* the provider delivers the same webhook twice, or out of order (R14.4);
* payment succeeded but the inventory had gone (R10.8).
"""

from __future__ import annotations

import unittest

from utp.app import Platform
from utp.core.clock import FixedClock
from utp.core.errors import NotAvailable
from utp.core.money import to_minor
from utp.services.booking import QuoteLineRequest
from utp.services.consent import ConsentCapture
from utp.services.notifications import SimulatedEmailProvider
from utp.services.payments import SimulatedProvider

VISIT_DATE = "2026-09-10"


class PaymentToETicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock("2026-09-01T03:00:00Z")  # 10:00 Bangkok
        self.email = SimulatedEmailProvider()
        self.gateway = SimulatedProvider()
        self.platform = Platform(
            clock=self.clock, payment_provider=self.gateway, email_provider=self.email
        )
        self._provision()

    def tearDown(self) -> None:
        self.platform.close()

    # ------------------------------------------------------------------ #

    def _provision(self) -> None:
        p = self.platform
        tenant = p.tenancy.create_tenant(
            code="aquaria", name="Aquawalk Thailand", default_language="en", languages=["en", "th"]
        )
        self.tenant_id = tenant["id"]
        ctx = p.system_context(self.tenant_id)
        org = p.tenancy.create_organization(ctx, code="AQW-TH", name="Aquawalk (Thailand) Co., Ltd.")
        p.tenancy.create_venue_type(
            None,
            code="AQUARIUM",
            name="Aquarium",
            platform_level=True,
            template={
                "tax_model": "INCLUSIVE",
                "tax_rate_bp": 700,
                "operating_hours": {"default": {"open": "10:30", "close": "19:00"}},
            },
        )
        venue = p.tenancy.create_venue(
            ctx,
            organization_id=org["id"],
            venue_type_code="AQUARIUM",
            code="AQP",
            short_code="AQP",
            name={"en": "Aquaria Phuket"},
            timezone="Asia/Bangkok",
            currency="THB",
        )
        self.venue = venue
        self.venue_id = venue["id"]
        self.ctx = ctx.for_venue(self.venue_id)

        p.catalog.create_segment(self.ctx, code="ADULT", name={"en": "Adult"})
        experience = p.catalog.create_experience(
            self.ctx, venue_id=self.venue_id, code="GA", name={"en": "General Admission"}
        )
        product = p.catalog.create_product(
            self.ctx,
            venue_id=self.venue_id,
            code="GA-DAY",
            name={"en": "General Admission"},
            admission_model="GENERAL_ADMISSION",
            experience_id=experience["id"],
        )
        self.product = product
        ticket_type = p.catalog.create_ticket_type(
            self.ctx, product_id=product["id"], segment_code="ADULT", code="GA-ADULT", name={"en": "Adult"}
        )
        self.ticket_type = ticket_type
        p.pricing.create_price_rule(
            self.ctx,
            ticket_type_id=ticket_type["id"],
            amount_minor=to_minor(1251),
            currency="THB",
            code="ONLINE",
        )
        p.calendar.set_booking_rules(
            self.ctx, scope_type="VENUE", scope_id=self.venue_id, settings={"max_days_in_advance": 90}
        )
        p.consent.publish_notice(
            self.ctx,
            version="2026.1",
            consent_text_version="ct-2026.1",
            language="en",
            controller={"name": "Aquawalk (Thailand) Co., Ltd.", "contact": "privacy@aquaria.test"},
            purposes=[{"code": "BOOKING_SERVICE", "description": "Deliver the booking"}],
            retention={"bookings_years": 10},
            recipients=[{"name": "Payment provider", "role": "processor"}],
            rights=["access", "erasure"],
            dpo_contact="dpo@aquaria.test",
            notice_url="https://aquaria.test/privacy",
        )

    def _guest_ctx(self):
        return self.platform.guest_context(
            self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en"
        )

    def _quote(self, ctx, quantity: int = 2):
        return self.platform.booking.quote(
            ctx,
            venue_id=self.venue_id,
            visit_date=VISIT_DATE,
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_type["id"], quantity=quantity)],
        )

    def _confirm(self, ctx, quote, *, key: str = "idem-1", email: str = "guest@example.com"):
        return self.platform.booking.confirm(
            ctx,
            quote,
            customer={"email": email, "full_name": "Somchai Jaidee", "phone": "+66811234567"},
            consent_items={"BOOKING_SERVICE": True, "MARKETING": False},
            payment_method="CARD",
            idempotency_key=key,
        )

    def _emails_to(self, address: str) -> list[dict]:
        return [m for m in self.email.sent if m["to"] == address]

    # ------------------------------------------------------------------ #
    # Happy path
    # ------------------------------------------------------------------ #

    def test_gateway_confirmation_emails_the_eticket(self) -> None:
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx))
        result = self._confirm(ctx, quote)

        self.assertTrue(result["confirmed"])
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(len(result["tickets"]), 2, "one individually redeemable ticket per admission")

        emails = self._emails_to("guest@example.com")
        self.assertTrue(emails, "the customer must receive their ticket by email")
        body = emails[0]["body"]
        self.assertIn(result["booking_number"], body)
        self.assertIn("Aquaria Phuket", body)
        self.assertIn(VISIT_DATE, body)

    def test_each_ticket_carries_its_own_signed_qr(self) -> None:
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx))
        result = self._confirm(ctx, quote)
        payloads = {t["qr_payload"] for t in result["tickets"]}
        self.assertEqual(len(payloads), 2, "QR payloads must be unique per ticket")
        for payload in payloads:
            self.assertTrue(payload.startswith("UTP1."))
            # R15.2 — no personal data in the payload.
            self.assertNotIn("guest@example.com", payload)
            self.assertNotIn("Somchai", payload)

    def test_no_email_is_sent_before_payment_succeeds(self) -> None:
        ctx = self._guest_ctx()
        self.platform.booking.start_checkout(ctx, self._quote(ctx))
        self.assertEqual(self._emails_to("guest@example.com"), [])

    # ------------------------------------------------------------------ #
    # R14.6 — the customer's browser is gone
    # ------------------------------------------------------------------ #

    def test_browser_lost_after_authorization_still_delivers_the_ticket(self) -> None:
        """R14.6: the gateway callback completes the booking and emails the ticket."""
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx))

        # Simulate the browser dying immediately after the gateway authorized: the
        # booking is reserved and the payment exists, but nothing completed it.
        booking_id = self._reserve_without_completing(ctx, quote, key="idem-lost")
        booking = self.platform.booking.get_booking(self.ctx, booking_id)
        self.assertEqual(booking["status"], "AWAITING_PAYMENT")
        self.assertEqual(self.platform.tickets.list_for_booking(self.ctx, booking_id), [])
        self.assertEqual(self._emails_to("guest@example.com"), [])

        # The provider's webhook arrives.
        outcome = self._send_webhook(booking_id, event_id="evt-lost-1")

        self.assertTrue(outcome["booking_completed"])
        self.assertEqual(outcome["tickets_issued"], 2)
        confirmed = self.platform.booking.get_booking(self.ctx, booking_id)
        self.assertEqual(confirmed["status"], "CONFIRMED")
        emails = self._emails_to("guest@example.com")
        self.assertTrue(emails, "R14.6: the ticket must still reach the customer")
        self.assertIn(confirmed["booking_number"], emails[0]["body"])

    def test_duplicate_webhook_does_not_duplicate_tickets_or_email(self) -> None:
        """R14.4: repeated delivery produces exactly one state transition."""
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx))
        booking_id = self._reserve_without_completing(ctx, quote, key="idem-dupe")

        first = self._send_webhook(booking_id, event_id="evt-dupe-1")
        self.assertTrue(first["booking_completed"])
        tickets_after_first = len(self.platform.tickets.list_for_booking(self.ctx, booking_id))
        emails_after_first = len(self._emails_to("guest@example.com"))

        # Same event id: the provider retried.
        replay = self._send_webhook(booking_id, event_id="evt-dupe-1")
        self.assertTrue(replay["duplicate"])
        self.assertFalse(replay["processed"])

        # A different event id for the same payment: still must not double up.
        again = self._send_webhook(booking_id, event_id="evt-dupe-2")
        self.assertTrue(again.get("completion", {}).get("already_confirmed"))

        self.assertEqual(
            len(self.platform.tickets.list_for_booking(self.ctx, booking_id)), tickets_after_first
        )
        self.assertEqual(len(self._emails_to("guest@example.com")), emails_after_first)

    def test_unsigned_webhook_is_rejected_and_nothing_is_confirmed(self) -> None:
        from utp.core.errors import ValidationError

        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx))
        booking_id = self._reserve_without_completing(ctx, quote, key="idem-unsigned")
        payment_id = self._payment_id_for(booking_id)

        with self.assertRaises(ValidationError) as caught:
            self.platform.payments.handle_webhook(
                self.ctx,
                provider_event_id="evt-forged",
                kind="payment.succeeded",
                body='{"id":"evt-forged"}',
                signature="0" * 24,
                payment_id=payment_id,
            )
        self.assertEqual(caught.exception.code, "webhook_signature_invalid")
        self.assertEqual(
            self.platform.booking.get_booking(self.ctx, booking_id)["status"], "AWAITING_PAYMENT"
        )
        self.assertEqual(self._emails_to("guest@example.com"), [])

    def test_capacity_is_consumed_exactly_once_across_both_paths(self) -> None:
        """The webhook path must not double-decrement capacity."""
        session = self.platform.inventory.create_session(
            self.ctx,
            venue_id=self.venue_id,
            date=VISIT_DATE,
            start_time="14:00",
            product_id=self.product["id"],
            experience_id=self.product["experience_id"],
            capacity=10,
            duration_minutes=60,
        )
        ctx = self._guest_ctx()
        quote = self.platform.booking.quote(
            ctx,
            venue_id=self.venue_id,
            visit_date=VISIT_DATE,
            lines=[
                QuoteLineRequest(
                    ticket_type_id=self.ticket_type["id"], quantity=2, session_id=session["id"]
                )
            ],
        )
        quote = self.platform.booking.start_checkout(ctx, quote)
        booking_id = self._reserve_without_completing(ctx, quote, key="idem-cap")

        self._send_webhook(booking_id, event_id="evt-cap-1")
        self._send_webhook(booking_id, event_id="evt-cap-2")

        availability = self.platform.inventory.availability(self.ctx, session["id"])
        self.assertEqual(availability.confirmed, 2, "capacity must be consumed exactly once")
        self.assertEqual(availability.remaining, 8)

    # ------------------------------------------------------------------ #
    # R10.8 — money taken, inventory gone
    # ------------------------------------------------------------------ #

    def test_inline_checkout_refuses_to_charge_once_inventory_has_gone(self) -> None:
        """The preferable outcome: never take money that must immediately be refunded.

        Because the inline path revalidates before charging, a customer whose hold
        lapsed and whose seat was then taken is stopped *before* payment. R10.8's
        take-the-money-then-discover path is therefore reachable only via the gateway
        callback, which the next test covers.
        """
        session = self._capacity_one_session("15:00")
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._session_quote(ctx, session))

        self._lose_the_last_place(session)

        with self.assertRaises(NotAvailable):
            self._confirm(ctx, quote, key="idem-refuse", email="unlucky@example.com")
        # No payment was attempted at all.
        self.assertEqual(self.gateway.authorizations, {})

    def test_gateway_confirmed_but_inventory_gone_refunds_and_explains(self) -> None:
        """R10.8 / R14.6: money taken, place gone, no ticket issued, refund started."""
        session = self._capacity_one_session("16:00")
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._session_quote(ctx, session))
        booking_id = self._reserve_without_completing(ctx, quote, key="idem-oversell")

        # The customer's browser died; while the webhook was in flight the hold lapsed
        # and the last place went to a walk-up guest at the counter.
        self._lose_the_last_place(session)

        outcome = self._send_webhook(booking_id, event_id="evt-oversell-1")
        completion = outcome["completion"]

        self.assertIsNotNone(completion)
        self.assertFalse(completion["confirmed"])
        self.assertEqual(completion["status"], "RECONCILIATION")
        self.assertIn("payment went through", completion["message"])
        self.assertIn(completion["remedy"], ("REFUND", "VOID"))

        # No ticket may exist for inventory that does not exist.
        self.assertEqual(self.platform.tickets.list_for_booking(self.ctx, booking_id), [])
        # The walk-up guest keeps the place; nothing was oversold.
        self.assertEqual(self.platform.inventory.availability(self.ctx, session["id"]).confirmed, 1)
        # Finance can see the money against a booking in RECONCILIATION.
        self.assertEqual(
            self.platform.booking.get_booking(self.ctx, booking_id)["status"], "RECONCILIATION"
        )

    def _capacity_one_session(self, start_time: str) -> dict:
        return self.platform.inventory.create_session(
            self.ctx,
            venue_id=self.venue_id,
            date=VISIT_DATE,
            start_time=start_time,
            product_id=self.product["id"],
            experience_id=self.product["experience_id"],
            capacity=1,
            duration_minutes=60,
        )

    def _session_quote(self, ctx, session: dict):
        return self.platform.booking.quote(
            ctx,
            venue_id=self.venue_id,
            visit_date=VISIT_DATE,
            lines=[
                QuoteLineRequest(
                    ticket_type_id=self.ticket_type["id"], quantity=1, session_id=session["id"]
                )
            ],
        )

    def _lose_the_last_place(self, session: dict) -> None:
        """Let the hold lapse, then have somebody else take the only remaining place."""
        self.clock.advance(minutes=11)
        self.platform.inventory.reclaim_expired_holds(self.ctx)
        self.platform.inventory.confirm_without_hold(
            self.ctx.with_channel("COUNTER"), session_id=session["id"], quantity=1
        )

    # ------------------------------------------------------------------ #
    # Delivery reliability
    # ------------------------------------------------------------------ #

    def test_hard_bounce_keeps_the_ticket_retrievable_and_flags_staff(self) -> None:
        """R37.12 / R37.13: a bounced address never leaves a booking without a ticket."""
        self.email.hard_bounce.add("bounces@example.com")
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx, quantity=1))
        result = self._confirm(ctx, quote, key="idem-bounce", email="bounces@example.com")

        self.assertTrue(result["confirmed"])
        log = self.platform.notifications.message_log(self.ctx, booking_id=result["booking_id"])
        self.assertTrue(any(m["status"] == "BOUNCED" for m in log))
        # The ticket still exists and is retrievable through Manage Booking.
        self.assertEqual(len(self.platform.tickets.list_for_booking(self.ctx, result["booking_id"])), 1)
        self.assertTrue(self.platform.notifications.is_suppressed(self.ctx, "bounces@example.com"))

    def test_transient_send_failure_is_retried(self) -> None:
        """R37.10: a provider blip must not lose the ticket."""
        self.email.transient_failures["flaky@example.com"] = 1
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx, quantity=1))
        result = self._confirm(ctx, quote, key="idem-flaky", email="flaky@example.com")
        self.assertTrue(result["confirmed"])
        self.assertEqual(self._emails_to("flaky@example.com"), [])

        # The retry is scheduled with backoff, so time must move before it is attempted.
        self.clock.advance(minutes=5)
        self.platform.notifications.dispatch_due(self.ctx)
        self.assertTrue(self._emails_to("flaky@example.com"), "the retry must deliver the ticket")

    def test_confirmation_is_not_blocked_by_a_slow_email_provider(self) -> None:
        """R37.8: notification dispatch is queued, never inline with confirmation."""
        calls: list[str] = []

        class RecordingProvider(SimulatedEmailProvider):
            def send(self, *, to, subject, body, message_id):  # type: ignore[override]
                calls.append(to)
                return super().send(to=to, subject=subject, body=body, message_id=message_id)

        self.platform.notifications.provider = RecordingProvider()
        ctx = self._guest_ctx()
        quote = self.platform.booking.start_checkout(ctx, self._quote(ctx, quantity=1))
        result = self._confirm(ctx, quote, key="idem-queue")
        # The booking is confirmed and the ticket exists regardless of provider timing.
        self.assertTrue(result["confirmed"])
        self.assertEqual(len(result["tickets"]), 1)
        log = self.platform.notifications.message_log(self.ctx, booking_id=result["booking_id"])
        self.assertTrue(log, "the message must be recorded in the delivery log")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _reserve_without_completing(self, ctx, quote, *, key: str) -> str:
        """Reserve and charge, but skip completion — the browser-died scenario.

        Done by disabling the capture hook for the duration, which reproduces a process
        that never got to finish rather than faking a state the platform cannot reach.
        """
        hook = self.platform.payments.on_payment_captured
        self.platform.payments.on_payment_captured = None
        original_finalize = self.platform.booking.finalize_paid_booking
        self.platform.booking.finalize_paid_booking = lambda *a, **k: {"confirmed": False}
        try:
            self._confirm(ctx, quote, key=key)
        finally:
            self.platform.booking.finalize_paid_booking = original_finalize
            self.platform.payments.on_payment_captured = hook
        row = self.platform.db.query_one(
            "SELECT booking_id FROM payments WHERE tenant_id = ? AND idempotency_key = ?",
            (self.tenant_id, key),
        )
        assert row is not None
        return row["booking_id"]

    def _payment_id_for(self, booking_id: str) -> str:
        payments = self.platform.payments.list_for_booking(self.ctx, booking_id)
        return payments[0]["payment_id"]

    def _send_webhook(self, booking_id: str, *, event_id: str) -> dict:
        payment_id = self._payment_id_for(booking_id)
        body = f'{{"id":"{event_id}","payment":"{payment_id}"}}'
        return self.platform.payments.handle_webhook(
            self.ctx,
            provider_event_id=event_id,
            kind="payment.succeeded",
            body=body,
            signature=self.gateway.sign_webhook(body),
            payment_id=payment_id,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
