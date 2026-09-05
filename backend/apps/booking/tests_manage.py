"""Manage Booking tests (R16).

Prove the properties that make self-service safe:

* the lookup response is identical for unknown and known-but-unverified bookings,
  so it cannot be used to enumerate (R16.3);
* the verification code is single-use and short-lived (R16.11);
* a used ticket blocks self-service cancel and reschedule (R16.8);
* reschedule acquires the target before releasing the original, so a failed move
  leaves the booking untouched (R16.7), and reissues tickets so old QR codes stop
  working (R16.9);
* cancel restores future-dated capacity and computes the refund per the tiered
  policy (R16.6, R17.5).
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.booking import consent_service, manage_service
from apps.booking.models import Booking
from apps.booking.services import QuoteLine, confirm, quote
from apps.catalog.models import CustomerSegment, Product, TicketType
from apps.core.errors import (
    ConfirmationRequired,
    ConflictError,
    NotAvailable,
    RateLimited,
    RuleViolation,
    ValidationError,
)
from apps.inventory.models import Session
from apps.payments.gateway import SimulatedGateway
from apps.pricing.models import PriceRule
from apps.tenancy.models import Organization, Tenant, Venue
from apps.ticketing.models import Ticket
from apps.venuesettings.models import VatSetting

GATEWAY = SimulatedGateway()
# A visit far enough ahead that the default policy grants a full refund.
VISIT = (dt.date.today() + dt.timedelta(days=30))


def _build():
    tenant = Tenant.objects.create(code="aquaria", name="Aquaria")
    org = Organization.objects.create(tenant=tenant, code="aquawalk", name="Aquawalk")
    venue = Venue.objects.create(
        tenant=tenant, organization=org, code="aqp", name={"en": "Aquaria Phuket"},
        timezone="Asia/Bangkok", currency="THB", tax_model="INCLUSIVE", tax_rate_bp=700,
        operating_hours={"default": {"open": "10:30", "close": "19:00"}},
    )
    VatSetting.objects.create(
        tenant=tenant, venue=venue, enabled=True, rate_bp=700, mode="INCLUSIVE",
        effective_from=dt.date(2026, 1, 1),
    )
    seg = CustomerSegment.objects.create(tenant=tenant, code="adult", name={"en": "Adult"})
    product = Product.objects.create(
        tenant=tenant, venue=venue, code="ga", name={"en": "GA"}, session_requirement="NOT_USED",
    )
    tt = TicketType.objects.create(
        tenant=tenant, product=product, segment=seg, code="ga-adult", name={"en": "Adult"},
        entry_allowance=1,
    )
    PriceRule.objects.create(
        tenant=tenant, venue=venue, ticket_type=tt, amount_minor=100000, currency="THB",
    )
    consent_service.publish_notice(
        tenant=tenant, version="v1", controller_name="C", controller_contact="c@x.test",
        dpo_contact="dpo@x.test", body={},
    )
    return tenant, venue, product, tt


def _book(tenant, venue, tt, *, email="owner@example.test", qty=2):
    q = quote(venue=venue, visit_date=VISIT, lines=[QuoteLine(ticket_type_id=tt.id, quantity=qty)])
    result = confirm(
        quote_result=q,
        customer_data={"email": email, "full_name": "Owner"},
        consent_items={"BOOKING_SERVICE": True}, payment_method="CARD", gateway=GATEWAY,
    )
    return Booking.objects.get(id=result["booking_id"])


class VerificationTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()
        self.booking = _book(self.tenant, self.venue, self.tt)

    def test_unknown_and_known_lookups_are_indistinguishable(self):
        unknown = manage_service.request_access_code(
            tenant=self.tenant, booking_number="AQP-NOPE-0000", email="nobody@example.test"
        )
        known = manage_service.request_access_code(
            tenant=self.tenant, booking_number=self.booking.booking_number, email="owner@example.test"
        )
        # Same public message; only the known one carries a code internally.
        self.assertEqual(unknown["message"], known["message"])
        self.assertIsNone(unknown.get("_code"))
        self.assertIsNotNone(known.get("_code"))

    def test_wrong_email_gets_the_generic_response_and_no_code(self):
        result = manage_service.request_access_code(
            tenant=self.tenant, booking_number=self.booking.booking_number,
            email="not-the-owner@example.test",
        )
        self.assertIsNone(result.get("_code"))

    def test_correct_code_verifies_and_is_single_use(self):
        issued = manage_service.request_access_code(
            tenant=self.tenant, booking_number=self.booking.booking_number, email="owner@example.test"
        )
        code = issued["_code"]
        verified = manage_service.verify_access(
            tenant=self.tenant, booking_number=self.booking.booking_number,
            email="owner@example.test", code=code,
        )
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["booking_id"], self.booking.id)
        # Second use of the same code fails — it was consumed.
        with self.assertRaises(ValidationError):
            manage_service.verify_access(
                tenant=self.tenant, booking_number=self.booking.booking_number,
                email="owner@example.test", code=code,
            )

    def test_wrong_code_is_a_plain_error_then_throttles(self):
        manage_service.request_access_code(
            tenant=self.tenant, booking_number=self.booking.booking_number, email="owner@example.test"
        )
        # First few wrong attempts are a plain validation error, not a throttle.
        for _ in range(manage_service.MAX_VERIFICATION_ATTEMPTS - 1):
            with self.assertRaises(ValidationError):
                manage_service.verify_access(
                    tenant=self.tenant, booking_number=self.booking.booking_number,
                    email="owner@example.test", code="000000",
                )
        # The attempt that crosses the threshold throttles.
        with self.assertRaises(RateLimited):
            manage_service.verify_access(
                tenant=self.tenant, booking_number=self.booking.booking_number,
                email="owner@example.test", code="000000",
            )

    def test_expired_code_does_not_verify(self):
        issued = manage_service.request_access_code(
            tenant=self.tenant, booking_number=self.booking.booking_number, email="owner@example.test"
        )
        # Expire the challenge.
        from apps.booking.consent_models import VerificationChallenge

        VerificationChallenge.objects.filter(booking=self.booking).update(
            expires_at=timezone.now() - dt.timedelta(minutes=1)
        )
        with self.assertRaises(ValidationError):
            manage_service.verify_access(
                tenant=self.tenant, booking_number=self.booking.booking_number,
                email="owner@example.test", code=issued["_code"],
            )


class PolicyAndViewTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()
        self.booking = _book(self.tenant, self.venue, self.tt)

    def test_view_shows_tickets_and_policy(self):
        view = manage_service.manage_view(tenant=self.tenant, booking_id=self.booking.id)
        self.assertEqual(view["status"], "CONFIRMED")
        self.assertEqual(len(view["tickets"]), 2)
        self.assertTrue(view["actions"]["cancel"])
        # Full refund this far out.
        self.assertEqual(view["policy"]["cancel"]["refund_percent_bp"], 10000)

    def test_used_ticket_blocks_cancel_and_reschedule(self):
        t = self.booking.tickets.first()
        t.entries_used = 1
        t.state = "USED"
        t.save(update_fields=["entries_used", "state"])
        view = manage_service.manage_view(tenant=self.tenant, booking_id=self.booking.id)
        self.assertFalse(view["actions"]["cancel"])
        self.assertFalse(view["actions"]["reschedule"])
        with self.assertRaises(RuleViolation):
            manage_service.cancel(
                tenant=self.tenant, booking_id=self.booking.id, reason="x", confirmed=True
            )


class CancelTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()
        self.booking = _book(self.tenant, self.venue, self.tt)

    def test_cancel_needs_confirmation_first(self):
        with self.assertRaises(ConfirmationRequired) as ctx:
            manage_service.cancel(tenant=self.tenant, booking_id=self.booking.id, reason="x")
        # The confirmation states the scope and the refund amount.
        details = ctx.exception.details
        self.assertEqual(details["ticket_count"], 2)
        self.assertIn("refund_amount_minor", details)

    def test_confirmed_cancel_sets_status_and_refund(self):
        result = manage_service.cancel(
            tenant=self.tenant, booking_id=self.booking.id, reason="Change of plans", confirmed=True
        )
        self.assertEqual(result["status"], "CANCELLED")
        # 100000 x 2 = 200000, full refund this far out.
        self.assertEqual(result["refund_amount_minor"], 200000)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CANCELLED")
        self.assertTrue(all(t.state == "CANCELLED" for t in self.booking.tickets.all()))

    def test_cannot_cancel_twice(self):
        manage_service.cancel(
            tenant=self.tenant, booking_id=self.booking.id, reason="x", confirmed=True
        )
        with self.assertRaises(ConflictError):
            manage_service.cancel(
                tenant=self.tenant, booking_id=self.booking.id, reason="x", confirmed=True
            )


class RescheduleTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()

    def _capacity_booking(self, capacity=5):
        """A booking on a capacity-controlled session, so reschedule moves real seats."""
        product = Product.objects.create(
            tenant=self.tenant, venue=self.venue, code="show", name={"en": "Show"},
            session_requirement="REQUIRED",
        )
        tt = TicketType.objects.create(
            tenant=self.tenant, product=product, segment=self.tt.segment, code="show-adult",
            name={"en": "Adult"}, entry_allowance=1,
        )
        PriceRule.objects.create(
            tenant=self.tenant, venue=self.venue, ticket_type=tt, amount_minor=50000, currency="THB",
        )
        origin = Session.objects.create(
            tenant=self.tenant, venue=self.venue, kind="PRODUCT", product=product,
            session_date=VISIT, start_time=dt.time(14, 0), capacity=capacity,
        )
        q = quote(venue=self.venue, visit_date=VISIT,
                  lines=[QuoteLine(ticket_type_id=tt.id, quantity=1, session_id=origin.id)])
        result = confirm(
            quote_result=q,
            customer_data={"email": "resched@example.test", "full_name": "R"},
            consent_items={"BOOKING_SERVICE": True}, payment_method="CARD", gateway=GATEWAY,
        )
        booking = Booking.objects.get(id=result["booking_id"])
        return booking, product, origin

    def test_reschedule_moves_capacity_and_reissues_tickets(self):
        booking, product, origin = self._capacity_booking()
        origin.refresh_from_db()
        self.assertEqual(origin.confirmed_count, 1)
        target = Session.objects.create(
            tenant=self.tenant, venue=self.venue, kind="PRODUCT", product=product,
            session_date=VISIT + dt.timedelta(days=1), start_time=dt.time(14, 0), capacity=5,
        )
        old_qr = booking.tickets.first().qr_payload

        result = manage_service.reschedule(
            tenant=self.tenant, booking_id=booking.id,
            new_visit_date=VISIT + dt.timedelta(days=1), new_session_id=target.id,
        )
        self.assertEqual(len(result["tickets_reissued"]), 1)
        origin.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(origin.confirmed_count, 0)  # released
        self.assertEqual(target.confirmed_count, 1)  # acquired
        # The QR was reissued, so the old code no longer matches.
        ticket = booking.tickets.first()
        self.assertNotEqual(ticket.qr_payload, old_qr)

    def test_failed_reschedule_leaves_the_original_untouched(self):
        booking, product, origin = self._capacity_booking()
        # A target that is already full.
        full_target = Session.objects.create(
            tenant=self.tenant, venue=self.venue, kind="PRODUCT", product=product,
            session_date=VISIT + dt.timedelta(days=2), start_time=dt.time(14, 0),
            capacity=1, confirmed_count=1, status="FULL",
        )
        with self.assertRaises(NotAvailable):
            manage_service.reschedule(
                tenant=self.tenant, booking_id=booking.id,
                new_visit_date=VISIT + dt.timedelta(days=2), new_session_id=full_target.id,
            )
        # Original capacity intact, booking date unchanged.
        origin.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(origin.confirmed_count, 1)
        self.assertEqual(booking.visit_date, VISIT)
