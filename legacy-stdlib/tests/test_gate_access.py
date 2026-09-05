"""Gate and access validation (R32).

The behaviour under test is a scan turning a QR into exactly one decision, and the
edge cases that decide whether a real gate can be trusted:

* a valid ticket admits once and the second scan is refused with the time and gate
  of the first admission (R32.3);
* the closed set of rejection reasons is produced for the right conditions
  (wrong date, expired, cancelled, blocked, unknown code) (R32.2);
* a forged or unknown code never touches inventory and never admits (R32.2);
* an unregistered or deactivated device is refused (R32.12);
* two simultaneous scans of the last allowed entry admit exactly one (R32.3);
* an offline sync that finds the same single-entry ticket used at two gates flags
  the conflict rather than discarding a record (R32.8);
* a supervisor override admits a rejected guest, with a reason, and is audited
  (R32.9).
"""

from __future__ import annotations

import threading
import unittest

from utp.app import Platform
from utp.core.clock import FixedClock
from utp.core.context import Principal, RequestContext
from utp.core.money import to_minor
from utp.services.booking import QuoteLineRequest

VISIT_DATE = "2026-09-10"


class GateAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        # 10:00 Bangkok on the visit date, so tickets are inside their window.
        self.clock = FixedClock("2026-09-10T03:00:00Z")
        self.platform = Platform(clock=self.clock)
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
        self.sys = ctx
        org = p.tenancy.create_organization(ctx, code="AQW-TH", name="Aquawalk (Thailand) Co., Ltd.")
        self.org_id = org["id"]
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

        self.gate = p.tenancy.create_access_point(
            self.ctx, venue_id=self.venue_id, code="MAIN-GATE", name={"en": "Main Entrance"}, kind="GATE"
        )
        self.gate2 = p.tenancy.create_access_point(
            self.ctx, venue_id=self.venue_id, code="SIDE-GATE", name={"en": "Side Entrance"}, kind="GATE"
        )
        self.device = p.tenancy.register_device(
            self.ctx,
            venue_id=self.venue_id,
            code="SCANNER-01",
            name="Main scanner",
            kind="SCANNER",
            channel="GATE",
            access_point_id=self.gate["id"],
        )

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
            self.ctx, scope_type="VENUE", scope_id=self.venue_id, settings={"max_days_in_advance": 365}
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
        # A supervisor who can override, and a gate operator who cannot.
        roles = p.staff.seed_role_templates(ctx, organization_id=org["id"])
        self.supervisor_id = self._make_staff("sup@aquaria.test", roles["VENUE_MANAGER"])
        self.gate_staff_id = self._make_staff("gate@aquaria.test", roles["GATE_STAFF"])

    def _make_staff(self, email: str, role_id: str) -> str:
        p = self.platform
        invited = p.staff.invite_staff(
            self.sys, email=email, first_name="X", last_name="Y", organization_id=self.org_id
        )
        p.staff.complete_enrolment(
            self.sys, staff_id=invited["id"], token=invited["enrolment_token"], credential="Pass-2026-Aa"
        )
        p.staff.assign_role(
            self.sys, staff_id=invited["id"], role_id=role_id, scope_type="VENUE", scope_id=self.venue_id
        )
        return invited["id"]

    def _staff_ctx(self, staff_id: str) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            principal=Principal(kind="STAFF", id=staff_id),
            channel="GATE",
            venue_id=self.venue_id,
        )

    def _issue_ticket(self, *, visit_date: str = VISIT_DATE, quantity: int = 1) -> list[dict]:
        p = self.platform
        gctx = p.guest_context(self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en")
        quote = p.booking.start_checkout(
            gctx,
            p.booking.quote(
                gctx,
                venue_id=self.venue_id,
                visit_date=visit_date,
                lines=[QuoteLineRequest(ticket_type_id=self.ticket_type["id"], quantity=quantity)],
            ),
        )
        result = p.booking.confirm(
            gctx,
            quote,
            customer={"email": "guest@example.com", "full_name": "Somchai Jaidee"},
            consent_items={"BOOKING_SERVICE": True},
            payment_method="CARD",
            idempotency_key=f"idem-{visit_date}-{quantity}-{id(quote)}",
        )
        return result["tickets"]

    def _scan(self, payload: str, **kw):
        ctx = self.sys.for_venue(None)
        ctx.channel = "GATE"
        return self.platform.access.scan(ctx, qr_payload=payload, **kw)

    # ------------------------------------------------------------------ #
    # Happy path and duplicate
    # ------------------------------------------------------------------ #

    def test_valid_ticket_admits_then_second_scan_is_already_used(self) -> None:
        ticket = self._issue_ticket()[0]
        first = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        self.assertEqual(first["decision"], "ADMIT")
        self.assertTrue(first["admit"])

        second = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        self.assertEqual(second["decision"], "REJECT_ALREADY_USED")
        self.assertFalse(second["admit"])
        # R32.3 — show where and when the guest already got in.
        self.assertIn("previous_admission", second)
        self.assertEqual(second["previous_admission"]["access_point"], "MAIN-GATE")

    def test_admit_with_check_when_proof_required(self) -> None:
        # A segment requiring proof flags the admit rather than blocking it.
        p = self.platform
        p.catalog.create_segment(
            self.ctx, code="SENIOR", name={"en": "Senior"}, proof_required=True,
            proof={"en": "Photo ID"},
        )
        senior_tt = p.catalog.create_ticket_type(
            self.ctx, product_id=self.product["id"], segment_code="SENIOR", code="GA-SENIOR",
            name={"en": "Senior"}, eligibility={"documents": ["Photo ID"]},
        )
        p.pricing.create_price_rule(
            self.ctx, ticket_type_id=senior_tt["id"], amount_minor=to_minor(675), currency="THB", code="S"
        )
        gctx = p.guest_context(self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en")
        quote = p.booking.start_checkout(
            gctx,
            p.booking.quote(
                gctx, venue_id=self.venue_id, visit_date=VISIT_DATE,
                lines=[QuoteLineRequest(ticket_type_id=senior_tt["id"], quantity=1)],
            ),
        )
        result = p.booking.confirm(
            gctx, quote, customer={"email": "senior@example.com", "full_name": "A B"},
            consent_items={"BOOKING_SERVICE": True}, payment_method="CARD", idempotency_key="idem-senior",
        )
        outcome = self._scan(result["tickets"][0]["qr_payload"], access_point_id=self.gate["id"])
        self.assertEqual(outcome["decision"], "ADMIT_WITH_CHECK")
        self.assertTrue(outcome["admit"])
        self.assertTrue(outcome.get("proof_required"))

    # ------------------------------------------------------------------ #
    # Rejections (R32.2)
    # ------------------------------------------------------------------ #

    def test_unknown_and_forged_codes_are_rejected(self) -> None:
        self.assertEqual(self._scan("not-a-code")["decision"], "REJECT_UNKNOWN_CODE")
        self.assertEqual(self._scan("")["decision"], "REJECT_UNKNOWN_CODE")
        # A well-formed prefix with a bad signature must not admit.
        forged = f"UTP1.{self.tenant_id}.{'0' * 64}.deadbeef"
        self.assertEqual(self._scan(forged)["decision"], "REJECT_UNKNOWN_CODE")

    def test_wrong_date_is_rejected(self) -> None:
        ticket = self._issue_ticket(visit_date="2026-09-11")[0]  # tomorrow
        outcome = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        # The ticket's window opens at 00:00 on its visit date, so scanning today is
        # before valid_from.
        self.assertEqual(outcome["decision"], "REJECT_NOT_YET_VALID")

    def _expired_ticket(self) -> dict:
        """Issue a ticket for today, then move time past its 23:59:59 expiry."""
        ticket = self._issue_ticket(visit_date=VISIT_DATE)[0]
        self.clock.set("2026-09-11T17:00:00Z")  # next day, past end-of-visit-day
        return ticket

    def test_expired_ticket_is_rejected(self) -> None:
        ticket = self._expired_ticket()
        outcome = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        self.assertEqual(outcome["decision"], "REJECT_EXPIRED")

    def test_blocked_ticket_is_rejected(self) -> None:
        tickets = self._issue_ticket()
        # Resolve the ticket id from its payload and block it.
        ticket = self.platform.tickets.find_by_qr(self.ctx, tickets[0]["qr_payload"])
        self.platform.tickets.block(self.ctx, ticket["id"], reason="Reported lost")
        outcome = self._scan(tickets[0]["qr_payload"], access_point_id=self.gate["id"])
        self.assertEqual(outcome["decision"], "REJECT_BLOCKED")

    def test_wrong_venue_or_gate_is_rejected(self) -> None:
        # Create a second venue with its own gate; scanning the ticket there fails.
        other = self.platform.tenancy.create_venue(
            self.sys, organization_id=self.org_id, venue_type_code="AQUARIUM", code="OTHER",
            short_code="OTH", name={"en": "Other"}, timezone="Asia/Bangkok", currency="THB",
        )
        other_gate = self.platform.tenancy.create_access_point(
            self.sys.for_venue(other["id"]), venue_id=other["id"], code="OG", name={"en": "OG"}, kind="GATE"
        )
        ticket = self._issue_ticket()[0]
        outcome = self._scan(ticket["qr_payload"], access_point_id=other_gate["id"])
        self.assertEqual(outcome["decision"], "REJECT_WRONG_VENUE_OR_GATE")

    # ------------------------------------------------------------------ #
    # Device authentication (R32.12)
    # ------------------------------------------------------------------ #

    def test_scan_with_valid_device_credential_admits(self) -> None:
        ticket = self._issue_ticket()[0]
        ctx = self.sys.for_venue(None)
        ctx.channel = "GATE"
        outcome = self.platform.access.scan(
            ctx,
            qr_payload=ticket["qr_payload"],
            device_code=self.device["code"],
            device_secret=self.device["secret"],
        )
        self.assertEqual(outcome["decision"], "ADMIT")

    def test_unregistered_device_is_refused(self) -> None:
        from utp.core.errors import NotFound

        ticket = self._issue_ticket()[0]
        ctx = self.sys.for_venue(None)
        with self.assertRaises(NotFound):
            self.platform.access.scan(
                ctx, qr_payload=ticket["qr_payload"], device_code="GHOST", device_secret="nope"
            )

    def test_deactivated_device_is_refused(self) -> None:
        from utp.core.errors import NotFound

        self.platform.tenancy.deactivate_device(self.ctx, self.device["id"], reason="lost")
        ticket = self._issue_ticket()[0]
        ctx = self.sys.for_venue(None)
        with self.assertRaises(NotFound):
            self.platform.access.scan(
                ctx,
                qr_payload=ticket["qr_payload"],
                device_code=self.device["code"],
                device_secret=self.device["secret"],
            )

    # ------------------------------------------------------------------ #
    # Concurrency (R32.3)
    # ------------------------------------------------------------------ #

    def test_two_simultaneous_scans_admit_exactly_one(self) -> None:
        ticket = self._issue_ticket()[0]
        payload = ticket["qr_payload"]
        outcomes: list[str] = []
        lock = threading.Lock()

        def race() -> None:
            ctx = self.platform.system_context(self.tenant_id).for_venue(None)
            ctx.channel = "GATE"
            result = self.platform.access.scan(
                ctx, qr_payload=payload, access_point_id=self.gate["id"]
            )
            with lock:
                outcomes.append(result["decision"])

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ADMIT"), 1, f"exactly one admit expected, got {outcomes}")
        self.assertTrue(all(o == "REJECT_ALREADY_USED" for o in outcomes if o != "ADMIT"))

    # ------------------------------------------------------------------ #
    # Override (R32.9)
    # ------------------------------------------------------------------ #

    def test_supervisor_can_override_a_rejection(self) -> None:
        ticket = self._expired_ticket()
        rejected = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        self.assertEqual(rejected["decision"], "REJECT_EXPIRED")

        sctx = self._staff_ctx(self.supervisor_id)
        overridden = self.platform.access.override_admit(
            sctx, scan_id=rejected["scan_id"], reason="Guest delayed by traffic, manager approved"
        )
        self.assertEqual(overridden["decision"], "ADMIT")
        self.assertTrue(overridden["admit"])
        # The override is audited with a mandatory reason (feeds the override review).
        events = self.platform.audit.search(sctx, action="OVERRIDE_ACCESS")
        self.assertTrue(events)

    def test_override_requires_permission(self) -> None:
        from utp.core.errors import AuthorizationDenied

        ticket = self._expired_ticket()
        rejected = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        gctx = self._staff_ctx(self.gate_staff_id)  # gate staff hold no OVERRIDE_ACCESS
        with self.assertRaises(AuthorizationDenied):
            self.platform.access.override_admit(
                gctx, scan_id=rejected["scan_id"], reason="please let them in"
            )

    def test_override_requires_a_reason(self) -> None:
        from utp.core.errors import ValidationError

        ticket = self._expired_ticket()
        rejected = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        sctx = self._staff_ctx(self.supervisor_id)
        with self.assertRaises(ValidationError):
            self.platform.access.override_admit(sctx, scan_id=rejected["scan_id"], reason="")

    # ------------------------------------------------------------------ #
    # Manual lookup (R32.10)
    # ------------------------------------------------------------------ #

    def test_manual_lookup_finds_the_booking_tickets(self) -> None:
        tickets = self._issue_ticket(quantity=2)
        booking_number = tickets[0]["ticket_number"].rsplit("-", 1)[0]
        sctx = self._staff_ctx(self.supervisor_id)
        found = self.platform.access.manual_lookup(sctx, booking_number=booking_number)
        self.assertEqual(len(found["tickets"]), 2)

    # ------------------------------------------------------------------ #
    # Offline sync conflict (R32.8)
    # ------------------------------------------------------------------ #

    def test_offline_sync_flags_same_ticket_at_two_gates(self) -> None:
        ticket = self._issue_ticket()[0]
        payload = ticket["qr_payload"]
        result = self.platform.access.sync_offline_scans(
            self.sys.for_venue(self.venue_id),
            scans=[
                {"qr_payload": payload, "access_point_id": self.gate["id"], "at_utc": "2026-09-10T04:00:00Z"},
                {"qr_payload": payload, "access_point_id": self.gate2["id"], "at_utc": "2026-09-10T05:00:00Z"},
            ],
        )
        # First ingested scan admits; the second sees the entry already spent, but
        # because they were captured offline at different gates the sync flags the
        # conflict and retains both records rather than discarding either (R32.8).
        self.assertEqual(result["ingested"], 2)
        self.assertTrue(result["conflicts"], "a single-entry ticket used at two gates must be flagged")
        self.assertEqual(result["conflicts"][0]["ticket_id"], ticket["id"])
        events = self.platform.audit.search(self.sys, action="OFFLINE_SCAN_CONFLICT")
        self.assertTrue(events)

    # ------------------------------------------------------------------ #
    # Immutability (R32.8 guard)
    # ------------------------------------------------------------------ #

    def test_a_recorded_decision_cannot_be_rewritten(self) -> None:
        from utp.core.db import IntegrityViolation

        ticket = self._issue_ticket()[0]
        outcome = self._scan(ticket["qr_payload"], access_point_id=self.gate["id"])
        with self.assertRaises(IntegrityViolation):
            self.platform.db.execute(
                "UPDATE scan_events SET decision = 'REJECT_BLOCKED' WHERE id = ?", (outcome["scan_id"],)
            )


if __name__ == "__main__":
    unittest.main()
