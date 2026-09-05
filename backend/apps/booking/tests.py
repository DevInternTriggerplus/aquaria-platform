"""Booking write-path tests: quote → consent → payment → confirm → e-ticket.

These prove the guarantees that make the flow safe:

* consent is required before any personal data is persisted (R12.2, R12.8);
* the booking total comes from the authoritative charge engine;
* confirmation is idempotent — a replayed request never double-books or double-charges;
* a completed booking snapshots its charge configuration and does not move when
  settings later change (R5.3);
* a declined payment leaves no confirmed booking;
* tickets are issued one per admitted unit, signed, with a frozen venue-local window.

Capacity-under-contention through the confirm path lives in
``tests_concurrency.py`` and needs PostgreSQL.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase

from apps.catalog.models import CustomerSegment, Product, TicketType
from apps.core.errors import ConsentRequired, PaymentFailed, ValidationError
from apps.payments.gateway import SimulatedGateway
from apps.payments.models import Payment
from apps.pricing.models import PriceRule
from apps.tenancy.models import Organization, Tenant, Venue
from apps.ticketing.services import QR_PREFIX
from apps.venuesettings.models import VatSetting

from . import consent_service
from .models import Booking, ConsentRecord, Customer
from .services import QuoteLine, confirm, quote

GATEWAY = SimulatedGateway()


def _build():
    tenant = Tenant.objects.create(code="aquaria", name="Aquaria")
    org = Organization.objects.create(tenant=tenant, code="aquawalk", name="Aquawalk")
    venue = Venue.objects.create(
        tenant=tenant, organization=org, code="aqp", name={"en": "Aquaria Phuket"},
        venue_type="AQUARIUM", timezone="Asia/Bangkok", currency="THB",
        tax_model="INCLUSIVE", tax_rate_bp=700,
    )
    VatSetting.objects.create(
        tenant=tenant, venue=venue, enabled=True, rate_bp=700, mode="INCLUSIVE",
        effective_from=dt.date(2026, 1, 1),
    )
    adult = CustomerSegment.objects.create(tenant=tenant, code="adult", name={"en": "Adult"})
    product = Product.objects.create(
        tenant=tenant, venue=venue, code="ga-intl", name={"en": "General Admission"},
        session_requirement="NOT_USED", max_per_booking=10,
    )
    tt = TicketType.objects.create(
        tenant=tenant, product=product, segment=adult, code="ga-intl-adult",
        name={"en": "Adult"}, entry_allowance=1,
    )
    PriceRule.objects.create(
        tenant=tenant, venue=venue, ticket_type=tt, amount_minor=125100, currency="THB",
    )
    consent_service.publish_notice(
        tenant=tenant, version="2026-01", controller_name="Aquawalk Thailand",
        controller_contact="privacy@aquaria.test", dpo_contact="dpo@aquaria.test",
        body={"purposes": ["booking"]},
    )
    return tenant, venue, product, tt


def _consent(**overrides):
    base = {"BOOKING_SERVICE": True, "MARKETING": False, "ANALYTICS": False}
    base.update(overrides)
    return base


class QuoteTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()

    def test_quote_prices_from_the_rule(self):
        q = quote(
            venue=self.venue, visit_date=dt.date(2026, 9, 1),
            lines=[QuoteLine(ticket_type_id=self.tt.id, quantity=2)],
        )
        self.assertEqual(q.total_minor, 250200)
        self.assertEqual(q.currency, "THB")
        self.assertEqual(q.lines[0]["price_rule_id"], PriceRule.objects.first().id)

    def test_uncapped_admission_takes_no_hold(self):
        q = quote(
            venue=self.venue, visit_date=dt.date(2026, 9, 1),
            lines=[QuoteLine(ticket_type_id=self.tt.id, quantity=3)],
        )
        self.assertEqual(q.holds, [])

    def test_no_price_rule_makes_the_ticket_unavailable(self):
        # A date with no matching rule (rule is undated so this needs a dated rule).
        PriceRule.objects.all().delete()
        PriceRule.objects.create(
            tenant=self.tenant, venue=self.venue, ticket_type=self.tt,
            amount_minor=125100, currency="THB", date_from=dt.date(2027, 1, 1),
        )
        with self.assertRaises(ValidationError):
            quote(
                venue=self.venue, visit_date=dt.date(2026, 9, 1),
                lines=[QuoteLine(ticket_type_id=self.tt.id, quantity=1)],
            )

    def test_quantity_over_max_is_refused(self):
        with self.assertRaises(ValidationError):
            quote(
                venue=self.venue, visit_date=dt.date(2026, 9, 1),
                lines=[QuoteLine(ticket_type_id=self.tt.id, quantity=99)],
            )


class ConfirmTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()

    def _quote(self, qty=2):
        return quote(
            venue=self.venue, visit_date=dt.date(2026, 9, 1),
            lines=[QuoteLine(ticket_type_id=self.tt.id, quantity=qty)],
        )

    def test_happy_path_confirms_and_issues_tickets(self):
        result = confirm(
            quote_result=self._quote(2),
            customer_data={"email": "guest@example.test", "full_name": "Web Guest"},
            consent_items=_consent(),
            payment_method="CARD",
            gateway=GATEWAY,
        )
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertTrue(result["confirmed"])
        self.assertEqual(len(result["tickets"]), 2)
        self.assertEqual(result["total_minor"], 250200)
        # QR payloads are signed, opaque, and carry no personal data.
        for t in result["tickets"]:
            self.assertTrue(t["qr_payload"].startswith(QR_PREFIX + "."))
            self.assertNotIn("guest@example.test", t["qr_payload"])
        # A consent record was written before the customer.
        self.assertEqual(ConsentRecord.objects.count(), 1)

    def test_confirm_refused_without_required_consent(self):
        with self.assertRaises(ConsentRequired):
            confirm(
                quote_result=self._quote(1),
                customer_data={"email": "declined@example.test", "full_name": "No Consent"},
                consent_items={"BOOKING_SERVICE": False},
                payment_method="CARD",
                gateway=GATEWAY,
            )
        # R12.8: declining leaves no personal data behind.
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(Booking.objects.count(), 0)

    def test_blank_email_is_a_field_error(self):
        try:
            confirm(
                quote_result=self._quote(1),
                customer_data={"email": "", "full_name": "No Email"},
                consent_items=_consent(),
                payment_method="CARD",
                gateway=GATEWAY,
            )
            self.fail("expected ValidationError")
        except ValidationError as exc:
            self.assertIn("email", exc.field_errors)

    def test_declined_payment_leaves_no_confirmed_booking(self):
        # The sentinel amount (…13 satang) is declined by the simulated gateway.
        PriceRule.objects.all().update(amount_minor=100013)
        with self.assertRaises(PaymentFailed):
            confirm(
                quote_result=self._quote(1),
                customer_data={"email": "declined-pay@example.test", "full_name": "Declined"},
                consent_items=_consent(),
                payment_method="CARD",
                gateway=GATEWAY,
            )
        self.assertFalse(Booking.objects.filter(status="CONFIRMED").exists())

    def test_confirm_is_idempotent_on_the_same_key(self):
        key = "idem-key-123"
        first = confirm(
            quote_result=self._quote(2),
            customer_data={"email": "idem@example.test", "full_name": "Idem"},
            consent_items=_consent(),
            payment_method="CARD",
            gateway=GATEWAY,
            idempotency_key=key,
        )
        second = confirm(
            quote_result=self._quote(2),
            customer_data={"email": "idem@example.test", "full_name": "Idem"},
            consent_items=_consent(),
            payment_method="CARD",
            gateway=GATEWAY,
            idempotency_key=key,
        )
        self.assertEqual(first["booking_number"], second["booking_number"])
        self.assertTrue(second["already_confirmed"])
        # Exactly one booking, one payment.
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_completed_booking_snapshots_its_charges(self):
        result = confirm(
            quote_result=self._quote(2),
            customer_data={"email": "snap@example.test", "full_name": "Snap"},
            consent_items=_consent(),
            payment_method="CARD",
            gateway=GATEWAY,
        )
        booking = Booking.objects.get(id=result["booking_id"])
        original_total = booking.total_minor
        original_snapshot = dict(booking.charge_snapshot)

        # Raise VAT next year. The stored booking must not move.
        VatSetting.objects.create(
            tenant=self.tenant, venue=self.venue, enabled=True, rate_bp=1000,
            mode="INCLUSIVE", effective_from=dt.date(2027, 1, 1),
        )
        booking.refresh_from_db()
        self.assertEqual(booking.total_minor, original_total)
        self.assertEqual(booking.charge_snapshot, original_snapshot)
        self.assertEqual(booking.charge_snapshot["vat_rate_bp"], 700)

    def test_ticket_expiry_is_end_of_visit_day_bangkok(self):
        result = confirm(
            quote_result=self._quote(1),
            customer_data={"email": "expiry@example.test", "full_name": "Expiry"},
            consent_items=_consent(),
            payment_method="CARD",
            gateway=GATEWAY,
        )
        t = result["tickets"][0]
        # End of the visit day in Bangkok: 23:59:59 +07:00, which is 16:59:59Z. Compare
        # the instant rather than the string, since either representation is valid.
        valid_until = dt.datetime.fromisoformat(t["valid_until"]).astimezone(dt.timezone.utc)
        self.assertEqual(
            valid_until, dt.datetime(2026, 9, 1, 16, 59, 59, tzinfo=dt.timezone.utc)
        )
        self.assertEqual(t["validity_timezone"], "Asia/Bangkok")
        # And the stored value is the same instant after a DB round-trip.
        from apps.ticketing.models import Ticket

        stored = Ticket.objects.get(id=t["id"])
        self.assertEqual(
            stored.valid_until.astimezone(dt.timezone.utc),
            dt.datetime(2026, 9, 1, 16, 59, 59, tzinfo=dt.timezone.utc),
        )
