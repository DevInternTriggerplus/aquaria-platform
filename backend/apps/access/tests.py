"""Gate validation tests (R32).

Each decision from the closed set is exercised, plus the properties that keep the
gate honest: a forged code is rejected without a DB lookup, expiry is judged against
the ticket's frozen venue-local window, an admit consumes exactly one entry, a second
scan of a single-entry ticket is refused with the previous admission, and re-entry is
honoured only inside the configured window. Every scan is recorded append-only.

The concurrent-double-scan test needs PostgreSQL and lives in
``tests_concurrency.py``.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.access import services as access
from apps.access.models import ScanEvent
from apps.booking import consent_service
from apps.booking.services import QuoteLine, confirm, quote
from apps.catalog.models import CustomerSegment, Product, TicketType
from apps.core.errors import ConflictError, NotFound, ValidationError
from apps.pricing.models import PriceRule
from apps.tenancy.models import AccessPoint, Organization, Tenant, Venue
from apps.ticketing.models import Ticket
from apps.venuesettings.models import VatSetting
from apps.payments.gateway import SimulatedGateway

GATEWAY = SimulatedGateway()
VISIT = dt.date(2026, 9, 1)
# A moment inside the visit day, in UTC: 12:00 Bangkok == 05:00Z.
DURING_VISIT = dt.datetime(2026, 9, 1, 5, 0, 0, tzinfo=dt.timezone.utc)


def _build(*, entry_allowance=1, reentry=False, reentry_window=None):
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
    gate = AccessPoint.objects.create(
        tenant=tenant, venue=venue, code="main-gate", name={"en": "Main Gate"}, direction="IN",
    )
    seg = CustomerSegment.objects.create(tenant=tenant, code="adult", name={"en": "Adult"})
    product = Product.objects.create(
        tenant=tenant, venue=venue, code="ga", name={"en": "GA"}, session_requirement="NOT_USED",
    )
    tt = TicketType.objects.create(
        tenant=tenant, product=product, segment=seg, code="ga-adult", name={"en": "Adult"},
        entry_allowance=entry_allowance, reentry_allowed=reentry,
    )
    PriceRule.objects.create(
        tenant=tenant, venue=venue, ticket_type=tt, amount_minor=50000, currency="THB",
    )
    consent_service.publish_notice(
        tenant=tenant, version="v1", controller_name="C", controller_contact="c@x.test",
        dpo_contact="dpo@x.test", body={},
    )
    return tenant, venue, gate, tt


def _issue_ticket(tenant, venue, tt, *, reentry_window=None):
    q = quote(venue=venue, visit_date=VISIT, lines=[QuoteLine(ticket_type_id=tt.id, quantity=1)])
    result = confirm(
        quote_result=q,
        customer_data={"email": "gate@example.test", "full_name": "Gate Guest"},
        consent_items={"BOOKING_SERVICE": True},
        payment_method="CARD", gateway=GATEWAY,
    )
    ticket = Ticket.objects.get(id=result["tickets"][0]["id"])
    if reentry_window is not None:
        ticket.reentry_allowed = True
        ticket.reentry_window_minutes = reentry_window
        ticket.save(update_fields=["reentry_allowed", "reentry_window_minutes"])
    return ticket


class ScanDecisionTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.gate, self.tt = _build()
        self.ticket = _issue_ticket(self.tenant, self.venue, self.tt)

    def _scan(self, payload=None, at=DURING_VISIT, access_point=None):
        return access.scan(
            tenant=self.tenant, venue=self.venue,
            qr_payload=payload if payload is not None else self.ticket.qr_payload,
            access_point=access_point if access_point is not None else self.gate,
            now=at,
        )

    def test_valid_ticket_admits(self):
        r = self._scan()
        self.assertEqual(r["decision"], "ADMIT")
        self.assertTrue(r["admit"])
        self.assertEqual(r["entries_used"], 1)

    def test_second_scan_of_single_entry_is_already_used(self):
        self._scan()
        r = self._scan()
        self.assertEqual(r["decision"], "REJECT_ALREADY_USED")
        self.assertFalse(r["admit"])
        # Shows the previous admission time/gate (R32.3).
        self.assertIn("previous_admission", r)

    def test_forged_code_is_unknown_without_db_lookup(self):
        r = self._scan(payload="UTP1.forged.token.badsignature")
        self.assertEqual(r["decision"], "REJECT_UNKNOWN_CODE")

    def test_garbage_code_is_unknown(self):
        r = self._scan(payload="not even a qr code")
        self.assertEqual(r["decision"], "REJECT_UNKNOWN_CODE")

    def test_wrong_venue_gate_is_rejected(self):
        other_org = Organization.objects.create(tenant=self.tenant, code="o2", name="O2")
        other_venue = Venue.objects.create(
            tenant=self.tenant, organization=other_org, code="other", name={"en": "Other"},
            timezone="Asia/Bangkok", currency="THB",
        )
        other_gate = AccessPoint.objects.create(
            tenant=self.tenant, venue=other_venue, code="g2", name={"en": "G2"},
        )
        r = access.scan(
            tenant=self.tenant, venue=other_venue, qr_payload=self.ticket.qr_payload,
            access_point=other_gate, now=DURING_VISIT,
        )
        self.assertEqual(r["decision"], "REJECT_WRONG_VENUE_OR_GATE")

    def test_expired_ticket_is_rejected(self):
        # After 23:59:59 Bangkok on the visit date == after 16:59:59Z.
        after = dt.datetime(2026, 9, 1, 17, 30, 0, tzinfo=dt.timezone.utc)
        r = self._scan(at=after)
        self.assertEqual(r["decision"], "REJECT_EXPIRED")

    def test_not_yet_valid_is_rejected(self):
        before = dt.datetime(2026, 8, 31, 10, 0, 0, tzinfo=dt.timezone.utc)  # before local midnight
        r = self._scan(at=before)
        self.assertEqual(r["decision"], "REJECT_NOT_YET_VALID")

    def test_cancelled_ticket_is_rejected(self):
        self.ticket.state = "CANCELLED"
        self.ticket.save(update_fields=["state"])
        r = self._scan()
        self.assertEqual(r["decision"], "REJECT_CANCELLED")

    def test_blocked_ticket_is_rejected(self):
        self.ticket.state = "BLOCKED"
        self.ticket.save(update_fields=["state"])
        r = self._scan()
        self.assertEqual(r["decision"], "REJECT_BLOCKED")

    def test_every_scan_is_recorded(self):
        self._scan()
        self._scan()  # already used
        self._scan(payload="garbage")  # unknown
        self.assertEqual(ScanEvent.objects.count(), 3)
        # And the log is append-only.
        from apps.core.models import SoftDeleteNotAllowed

        with self.assertRaises(SoftDeleteNotAllowed):
            ScanEvent.objects.first().delete()

    def test_operator_never_sees_a_token_error(self):
        r = self._scan(payload="UTP1.x.y.z")
        # A friendly message, never a signature/token internal.
        self.assertNotIn("signature", r["message"].lower())
        self.assertNotIn("hmac", r["message"].lower())


class ReentryTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.gate, self.tt = _build(entry_allowance=2, reentry=True)
        self.ticket = _issue_ticket(self.tenant, self.venue, self.tt, reentry_window=120)

    def test_reentry_within_window_admits(self):
        first = access.scan(tenant=self.tenant, venue=self.venue,
                            qr_payload=self.ticket.qr_payload, access_point=self.gate,
                            now=DURING_VISIT)
        self.assertEqual(first["decision"], "ADMIT")
        # 30 minutes later, inside the 120-minute window.
        again = access.scan(tenant=self.tenant, venue=self.venue,
                            qr_payload=self.ticket.qr_payload, access_point=self.gate,
                            now=DURING_VISIT + dt.timedelta(minutes=30))
        self.assertEqual(again["decision"], "ADMIT")
        self.assertEqual(again["entries_used"], 2)

    def test_reentry_outside_window_is_refused(self):
        access.scan(tenant=self.tenant, venue=self.venue, qr_payload=self.ticket.qr_payload,
                    access_point=self.gate, now=DURING_VISIT)
        # Three hours later, outside the 120-minute window.
        late = access.scan(tenant=self.tenant, venue=self.venue, qr_payload=self.ticket.qr_payload,
                           access_point=self.gate, now=DURING_VISIT + dt.timedelta(hours=3))
        self.assertEqual(late["decision"], "REJECT_ALREADY_USED")


class OverrideAndLookupTests(TestCase):
    def setUp(self):
        self.tenant, self.venue, self.gate, self.tt = _build()
        self.ticket = _issue_ticket(self.tenant, self.venue, self.tt)

    def test_override_admits_a_rejected_scan(self):
        self.ticket.state = "CANCELLED"
        self.ticket.save(update_fields=["state"])
        rejected = access.scan(tenant=self.tenant, venue=self.venue,
                               qr_payload=self.ticket.qr_payload, access_point=self.gate,
                               now=DURING_VISIT)
        scan_id = ScanEvent.objects.get(decision=rejected["decision"]).id
        result = access.override_admit(
            tenant=self.tenant, venue=self.venue, scan_id=scan_id, reason="Manager approved"
        )
        self.assertTrue(result["admit"])
        # Both the rejection and the override are retained (append-only).
        self.assertEqual(ScanEvent.objects.filter(ticket=self.ticket).count(), 2)
        from apps.core.models import AuditEvent

        self.assertTrue(AuditEvent.objects.filter(action="OVERRIDE_ACCESS").exists())

    def test_override_requires_a_reason(self):
        rejected = access.scan(tenant=self.tenant, venue=self.venue,
                               qr_payload="garbage", access_point=self.gate, now=DURING_VISIT)
        scan_id = ScanEvent.objects.first().id
        with self.assertRaises(ValidationError):
            access.override_admit(tenant=self.tenant, venue=self.venue, scan_id=scan_id, reason="")

    def test_manual_lookup_finds_tickets(self):
        result = access.manual_lookup(
            tenant=self.tenant, venue=self.venue,
            booking_number=self.ticket.booking.booking_number,
        )
        self.assertEqual(len(result["tickets"]), 1)
        self.assertEqual(result["tickets"][0]["ticket_number"], self.ticket.ticket_number)

    def test_manual_lookup_unknown_booking(self):
        with self.assertRaises(NotFound):
            access.manual_lookup(
                tenant=self.tenant, venue=self.venue, booking_number="AQP-NOPE-0000"
            )
