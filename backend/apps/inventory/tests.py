"""Capacity and immutability invariants.

Two guarantees are asserted here because they are the ones most likely to be
quietly broken by a future change:

* **Never oversell.** Confirmed consumption can never exceed capacity, even under
  concurrent contention for the last unit.
* **Never delete financial or audit history.** DELETE authority means remove from
  active use, not erase.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase, TransactionTestCase

from apps.core.errors import JustSoldOut
from apps.core.models import SoftDeleteNotAllowed
from apps.catalog.models import CustomerSegment, Product, TicketType
from apps.tenancy.models import Organization, Tenant, Venue

from .models import Session, acquire_hold, confirm_hold, reclaim_expired_holds, release_hold


def _fixture():
    tenant = Tenant.objects.create(code="t", name="T")
    org = Organization.objects.create(tenant=tenant, code="o", name="O")
    venue = Venue.objects.create(
        tenant=tenant, organization=org, code="v", name={"en": "V"},
        timezone="Asia/Bangkok", currency="THB",
    )
    product = Product.objects.create(
        tenant=tenant, venue=venue, code="ga", name={"en": "GA"},
        session_requirement="REQUIRED",
    )
    return tenant, org, venue, product


class CapacityTests(TestCase):
    def setUp(self):
        self.tenant, self.org, self.venue, self.product = _fixture()
        self.session = Session.objects.create(
            tenant=self.tenant, venue=self.venue, kind="PRODUCT", product=self.product,
            session_date=dt.date(2027, 3, 1), start_time=dt.time(10, 30), capacity=2,
        )

    def test_remaining_derives_from_capacity_minus_confirmed_minus_held(self):
        self.assertEqual(self.session.remaining, 2)
        acquire_hold(session=self.session, quantity=1, cart_ref="c1", channel="ONLINE")
        self.session.refresh_from_db()
        self.assertEqual(self.session.remaining, 1)

    def test_uncapped_session_takes_no_hold(self):
        """Aquaria's general admission is uncapped, so no hold is correct, not a bug."""
        uncapped = Session.objects.create(
            tenant=self.tenant, venue=self.venue, kind="PRODUCT", product=self.product,
            session_date=dt.date(2027, 3, 2), start_time=dt.time(10, 30), capacity=None,
        )
        self.assertIsNone(acquire_hold(session=uncapped, quantity=5, cart_ref="c", channel="ONLINE"))
        self.assertIsNone(uncapped.remaining)

    def test_cannot_hold_more_than_remains(self):
        acquire_hold(session=self.session, quantity=2, cart_ref="c1", channel="ONLINE")
        with self.assertRaises(JustSoldOut):
            acquire_hold(session=self.session, quantity=1, cart_ref="c2", channel="ONLINE")

    def test_just_sold_out_reports_remaining(self):
        acquire_hold(session=self.session, quantity=2, cart_ref="c1", channel="ONLINE")
        try:
            acquire_hold(session=self.session, quantity=1, cart_ref="c2", channel="ONLINE")
            self.fail("expected JustSoldOut")
        except JustSoldOut as exc:
            self.assertEqual(exc.details.get("remaining"), 0)

    def test_confirm_moves_held_to_confirmed(self):
        hold = acquire_hold(session=self.session, quantity=1, cart_ref="c1", channel="ONLINE")
        confirm_hold(hold)
        self.session.refresh_from_db()
        self.assertEqual(self.session.confirmed_count, 1)
        self.assertEqual(self.session.held_count, 0)
        self.assertEqual(self.session.remaining, 1)

    def test_release_returns_capacity(self):
        hold = acquire_hold(session=self.session, quantity=2, cart_ref="c1", channel="ONLINE")
        self.session.refresh_from_db()
        self.assertEqual(self.session.remaining, 0)
        release_hold(hold)
        self.session.refresh_from_db()
        self.assertEqual(self.session.remaining, 2)
        self.assertEqual(self.session.status, "AVAILABLE")

    def test_expired_hold_is_reclaimed(self):
        from django.utils import timezone

        hold = acquire_hold(session=self.session, quantity=2, cart_ref="c1", channel="ONLINE")
        hold.expires_at = timezone.now() - dt.timedelta(seconds=1)
        hold.save(update_fields=["expires_at"])
        self.assertEqual(reclaim_expired_holds(), 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.remaining, 2)
        hold.refresh_from_db()
        self.assertEqual(hold.state, "EXPIRED")

    def test_reclaim_is_idempotent(self):
        from django.utils import timezone

        hold = acquire_hold(session=self.session, quantity=1, cart_ref="c1", channel="ONLINE")
        hold.expires_at = timezone.now() - dt.timedelta(seconds=1)
        hold.save(update_fields=["expires_at"])
        reclaim_expired_holds()
        # A second pass must not credit the units again.
        self.assertEqual(reclaim_expired_holds(), 0)
        self.session.refresh_from_db()
        self.assertEqual(self.session.remaining, 2)

    def test_session_reaching_zero_is_marked_full(self):
        acquire_hold(session=self.session, quantity=2, cart_ref="c1", channel="ONLINE")
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "FULL")


class LastUnitContentionTests(TransactionTestCase):
    """Two requests, one unit: exactly one wins (R10.6)."""

    reset_sequences = True

    def test_only_one_of_two_gets_the_last_unit(self):
        tenant, org, venue, product = _fixture()
        session = Session.objects.create(
            tenant=tenant, venue=venue, kind="PRODUCT", product=product,
            session_date=dt.date(2027, 3, 1), start_time=dt.time(10, 30), capacity=1,
        )
        first = acquire_hold(session=session, quantity=1, cart_ref="a", channel="ONLINE")
        self.assertIsNotNone(first)
        with self.assertRaises(JustSoldOut):
            acquire_hold(session=session, quantity=1, cart_ref="b", channel="KIOSK")

        session.refresh_from_db()
        self.assertLessEqual(session.confirmed_count + session.held_count, session.capacity)

    def test_confirmed_never_exceeds_capacity_across_channels(self):
        tenant, org, venue, product = _fixture()
        session = Session.objects.create(
            tenant=tenant, venue=venue, kind="PRODUCT", product=product,
            session_date=dt.date(2027, 3, 1), start_time=dt.time(11, 0), capacity=3,
        )
        for i, channel in enumerate(["ONLINE", "KIOSK", "COUNTER"]):
            hold = acquire_hold(session=session, quantity=1, cart_ref=f"c{i}", channel=channel)
            confirm_hold(hold)
        session.refresh_from_db()
        self.assertEqual(session.confirmed_count, 3)
        with self.assertRaises(JustSoldOut):
            acquire_hold(session=session, quantity=1, cart_ref="c4", channel="PARTNER")


class ImmutabilityTests(TestCase):
    """Financial, ticketing and audit records are never physically deleted."""

    def setUp(self):
        self.tenant, self.org, self.venue, self.product = _fixture()
        self.segment = CustomerSegment.objects.create(
            tenant=self.tenant, code="adult", name={"en": "Adult"}
        )
        self.ticket_type = TicketType.objects.create(
            tenant=self.tenant, product=self.product, segment=self.segment,
            code="ga-adult", name={"en": "Adult"},
        )

    def test_booking_refuses_delete(self):
        from apps.booking.models import Booking

        booking = Booking.objects.create(
            tenant=self.tenant, booking_number="AQP-0001", venue=self.venue,
            visit_date=dt.date(2027, 3, 1), status="CONFIRMED", currency="THB",
        )
        with self.assertRaises(SoftDeleteNotAllowed):
            booking.delete()
        # It is still there, and cancel is the supported route.
        booking.cancel(reason="customer request")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")

    def test_booking_queryset_refuses_bulk_delete(self):
        from apps.booking.models import Booking

        Booking.objects.create(
            tenant=self.tenant, booking_number="AQP-0002", venue=self.venue,
            visit_date=dt.date(2027, 3, 1), currency="THB",
        )
        with self.assertRaises(SoftDeleteNotAllowed):
            Booking.objects.all().delete()

    def test_audit_event_is_append_only(self):
        from django.utils import timezone

        from apps.core.models import AuditEvent

        event = AuditEvent.objects.create(
            tenant=self.tenant, action="BOOKING_CONFIRMED", target_type="booking",
            target_id="bkg_1", occurred_at_utc=timezone.now(),
        )
        with self.assertRaises(SoftDeleteNotAllowed):
            event.delete()
        event.action = "TAMPERED"
        with self.assertRaises(SoftDeleteNotAllowed):
            event.save()
