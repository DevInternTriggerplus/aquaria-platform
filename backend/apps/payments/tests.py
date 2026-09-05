"""Payment webhook tests: signature, idempotency, and the browser-died path.

The webhook is the provider's authoritative confirmation (R14.7). These prove:

* an unverified callback is rejected and changes nothing;
* a captured event completes a booking whose inline finalize never ran — the
  browser-died-after-authorization case (R14.6);
* a duplicate delivery of the same event produces exactly one confirmed booking;
* an amount that disagrees with the platform is a reconciliation exception, not a
  silent capture (R14.7);
* a second successful charge for a confirmed booking is flagged, not double-confirmed
  (R14.5).
"""

from __future__ import annotations

import datetime as dt
import json

from django.test import TestCase

from apps.booking import consent_service
from apps.booking.models import Booking, BookingItem, Customer
from apps.booking.services import on_payment_captured
from apps.catalog.models import CustomerSegment, Product, TicketType
from apps.core.errors import ValidationError
from apps.core.ids import booking_number as make_booking_number, secure_token
from apps.core.models import AuditEvent
from apps.pricing.models import PriceRule
from apps.tenancy.models import Organization, Tenant, Venue
from apps.venuesettings.models import VatSetting

from .gateway import SimulatedGateway
from .models import Payment, PaymentEvent
from .services import start_payment
from .webhook import handle_webhook

GATEWAY = SimulatedGateway()


def _build():
    tenant = Tenant.objects.create(code="aquaria", name="Aquaria")
    org = Organization.objects.create(tenant=tenant, code="aquawalk", name="Aquawalk")
    venue = Venue.objects.create(
        tenant=tenant, organization=org, code="aqp", name={"en": "Aquaria Phuket"},
        timezone="Asia/Bangkok", currency="THB", tax_model="INCLUSIVE", tax_rate_bp=700,
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
        tenant=tenant, venue=venue, ticket_type=tt, amount_minor=125100, currency="THB",
    )
    consent_service.publish_notice(
        tenant=tenant, version="v1", controller_name="C", controller_contact="c@x.test",
        dpo_contact="dpo@x.test", body={},
    )
    return tenant, venue, product, tt


def _awaiting_booking_with_payment(tenant, venue, product, tt, *, key="k1", amount=250200):
    """Simulate the state after the browser died: an AWAITING_PAYMENT booking with an
    AUTHORIZED payment, and no inline finalize having run."""
    customer = Customer.objects.create(tenant=tenant, email="died@example.test", full_name="Died")
    consent_service.capture(
        tenant=tenant, venue=venue, items={"BOOKING_SERVICE": True}, contact="died@example.test",
        customer=customer,
    )
    booking = Booking.objects.create(
        tenant=tenant, booking_number=make_booking_number(venue.code.upper()), venue=venue,
        customer=customer, channel="ONLINE", visit_date=dt.date(2026, 9, 1),
        status="AWAITING_PAYMENT", currency="THB", gross_minor=amount, total_minor=amount,
        idempotency_key=key, cart_ref=secure_token(8),
        charge_snapshot={"vat_rate_bp": 700, "vat_mode": "INCLUSIVE"},
    )
    BookingItem.objects.create(
        tenant=tenant, booking=booking, product=product, ticket_type=tt, segment=tt.segment,
        quantity=2, unit_price_minor=125100, gross_minor=amount, currency="THB",
    )
    payment = start_payment(
        booking=booking, amount_minor=amount, currency="THB", method="CARD",
        gateway=GATEWAY, idempotency_key=key,
    )
    return booking, payment


def _signed(gateway, payload: dict) -> tuple[str, str]:
    body = json.dumps(payload)
    return body, gateway.sign_webhook(body)


class WebhookSignatureTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()
        self.booking, self.payment = _awaiting_booking_with_payment(
            self.tenant, self.venue, self.product, self.tt
        )

    def test_unverified_callback_is_rejected_and_changes_nothing(self):
        with self.assertRaises(ValidationError):
            handle_webhook(
                tenant=self.tenant, gateway=GATEWAY, provider_event_id="evt_1",
                kind="payment.succeeded", body='{"event_id":"evt_1"}', signature="wrong",
                payment_id=self.payment.id, on_capture=on_payment_captured,
            )
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "AWAITING_PAYMENT")
        self.assertFalse(PaymentEvent.objects.exists())


class WebhookCaptureTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.product, self.tt = _build()
        self.booking, self.payment = _awaiting_booking_with_payment(
            self.tenant, self.venue, self.product, self.tt
        )

    def _fire(self, event_id="evt_1", kind="payment.succeeded", amount=None):
        payload = {"event_id": event_id, "kind": kind, "payment_id": self.payment.id,
                   "amount_minor": amount}
        body, sig = _signed(GATEWAY, payload)
        return handle_webhook(
            tenant=self.tenant, gateway=GATEWAY, provider_event_id=event_id, kind=kind,
            body=body, signature=sig, payment_id=self.payment.id, amount_minor=amount,
            on_capture=on_payment_captured,
        )

    def test_capture_completes_a_booking_whose_browser_died(self):
        result = self._fire()
        self.assertEqual(result["outcome"], "CAPTURED")
        self.assertTrue(result["booking_completed"])
        self.assertEqual(result["tickets_issued"], 2)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")

    def test_duplicate_delivery_confirms_exactly_once(self):
        first = self._fire(event_id="evt_dup")
        second = self._fire(event_id="evt_dup")
        self.assertTrue(first["processed"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["processed"])
        # One event row, one confirmed booking, one set of tickets.
        self.assertEqual(PaymentEvent.objects.filter(provider_event_id="evt_dup").count(), 1)
        self.assertEqual(self.booking.tickets.count(), 0 or self.booking.tickets.count())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")
        self.assertEqual(self.booking.tickets.count(), 2)

    def test_out_of_order_redelivery_is_still_one_confirmation(self):
        # Two different event ids for the same payment: the second capture finds the
        # booking already confirmed and does not re-issue tickets.
        self._fire(event_id="evt_a")
        self._fire(event_id="evt_b")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")
        self.assertEqual(self.booking.tickets.count(), 2)

    def test_amount_mismatch_is_a_reconciliation_exception_not_a_capture(self):
        result = self._fire(event_id="evt_mismatch", amount=999999)
        self.assertEqual(result["outcome"], "AMOUNT_MISMATCH")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "AWAITING_PAYMENT")
        self.assertTrue(
            AuditEvent.objects.filter(action="PAYMENT_AMOUNT_MISMATCH").exists()
        )

    def test_orphaned_authorization_is_flagged(self):
        payload = {"event_id": "evt_orphan", "kind": "payment.succeeded"}
        body, sig = _signed(GATEWAY, payload)
        result = handle_webhook(
            tenant=self.tenant, gateway=GATEWAY, provider_event_id="evt_orphan",
            kind="payment.succeeded", body=body, signature=sig,
            payment_id="pay_does_not_exist", on_capture=on_payment_captured,
        )
        self.assertEqual(result["outcome"], "ORPHANED")
        self.assertTrue(
            AuditEvent.objects.filter(action="PAYMENT_ORPHANED_AUTHORIZATION").exists()
        )

    def test_second_charge_on_confirmed_booking_is_flagged_not_double_confirmed(self):
        # First capture confirms the booking.
        self._fire(event_id="evt_first")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "CONFIRMED")

        # A second, distinct successful payment arrives for the same booking.
        second_payment = start_payment(
            booking=self.booking, amount_minor=self.booking.total_minor, currency="THB",
            method="CARD", gateway=GATEWAY, idempotency_key="k2-second",
        )
        payload = {"event_id": "evt_second", "kind": "payment.succeeded",
                   "payment_id": second_payment.id}
        body, sig = _signed(GATEWAY, payload)
        result = handle_webhook(
            tenant=self.tenant, gateway=GATEWAY, provider_event_id="evt_second",
            kind="payment.succeeded", body=body, signature=sig, payment_id=second_payment.id,
            on_capture=on_payment_captured,
        )
        self.assertEqual(result["outcome"], "CAPTURED")
        # Still exactly two tickets, and the surplus is flagged for finance.
        self.assertEqual(self.booking.tickets.count(), 2)
        self.assertTrue(AuditEvent.objects.filter(action="PAYMENT_DUPLICATE_DETECTED").exists())
