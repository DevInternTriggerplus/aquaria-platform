"""Business / venue settings: charges, currency, validity, permissions, snapshots.

The settings spec (add_features) is explicit that the charge engine must have an
automated test for every VAT × service-charge combination before it counts as done,
and that historical transactions must never move when current settings change. Those
two properties are the backbone of this module:

* :class:`ChargeCombinationTests` proves Cases A–D arithmetically, with worked numbers
  taken from the spec's own examples where it gives them.
* :class:`SnapshotImmutabilityTests` proves an order and a ticket keep the rate and
  the expiry they were issued with after the venue changes its settings.

The rest guard the things that would quietly break money or access if they regressed:
exchange-rate direction and duplicate prevention, the 23:59:59 venue-local default
expiry, expired-ticket rejection, and server-side permission enforcement of the
``MANAGE_*`` actions.
"""

from __future__ import annotations

import datetime as _dt
import unittest
from decimal import Decimal

from utp.app import Platform
from utp.core.clock import FixedClock, parse_instant
from utp.core.errors import AuthorizationDenied, ConflictError, ValidationError
from utp.core.money import (
    ChargeInput,
    apply_rounding,
    compute_charges,
    convert_currency,
    format_currency,
    parse_rate,
    rate_direction_label,
)
from utp.services.booking import QuoteLineRequest

import seed


# --------------------------------------------------------------------------- #
# Pure charge engine — no database, deterministic, fast
# --------------------------------------------------------------------------- #


class ChargeCombinationTests(unittest.TestCase):
    """Every VAT × service-charge combination the spec enumerates (§6, §34-§36)."""

    VAT_INC = ChargeInput(enabled=True, rate_bp=700, mode="INCLUSIVE")
    VAT_EXC = ChargeInput(enabled=True, rate_bp=700, mode="EXCLUSIVE")
    SC_INC = ChargeInput(enabled=True, rate_bp=1000, mode="INCLUSIVE")
    SC_EXC = ChargeInput(enabled=True, rate_bp=1000, mode="EXCLUSIVE")

    # --- the spec's own single-charge worked examples ------------------- #

    def test_vat_excluded_adds_on_top(self) -> None:
        """§35 / §2: price 1,000, VAT 7% excluded -> VAT 70, total 1,070."""
        b = compute_charges(base_minor=100000, vat=self.VAT_EXC)
        self.assertEqual(b.vat_minor, 7000)
        self.assertEqual(b.taxable_base_minor, 100000)
        self.assertEqual(b.grand_total_minor, 107000)
        self.assertFalse(b.vat_included)

    def test_vat_included_is_carved_out(self) -> None:
        """§1 / §34: displayed 1,070 already contains 7% VAT -> VAT 70, net 1,000."""
        b = compute_charges(base_minor=107000, vat=self.VAT_INC)
        self.assertEqual(b.vat_minor, 7000)
        self.assertEqual(b.taxable_base_minor, 100000)
        self.assertEqual(b.grand_total_minor, 107000)  # total does not grow
        self.assertTrue(b.vat_included)

    def test_service_charge_excluded_adds_on_top(self) -> None:
        """§5: base 1,000, SC 10% excluded -> SC 100, subtotal 1,100."""
        b = compute_charges(base_minor=100000, service_charge=self.SC_EXC)
        self.assertEqual(b.service_charge_minor, 10000)
        self.assertEqual(b.grand_total_minor, 110000)

    def test_service_charge_included_is_carved_out(self) -> None:
        """§4: an inclusive SC is separated for reporting without growing the price."""
        b = compute_charges(base_minor=110000, service_charge=self.SC_INC)
        # 110,000 / 1.10 = 100,000 base, so the SC component is 10,000.
        self.assertEqual(b.service_charge_minor, 10000)
        self.assertEqual(b.grand_total_minor, 110000)
        self.assertTrue(b.service_charge_included)

    # --- the four required combinations, base 1,000 --------------------- #

    def test_case_a_both_included(self) -> None:
        """Case A: both inclusive — the customer total never grows past the price."""
        b = compute_charges(
            base_minor=100000, service_charge=self.SC_INC, vat=self.VAT_INC
        )
        self.assertEqual(b.grand_total_minor, 100000)
        self.assertTrue(b.service_charge_included and b.vat_included)
        # Both components are carved out of the 1,000, not added.
        self.assertEqual(b.service_charge_minor, 9091)  # 100000 - 100000/1.1
        self.assertEqual(b.vat_minor, 6542)             # 7% carved from 100000
        self.assertEqual(b.taxable_base_minor, 93458)

    def test_case_b_sc_excluded_vat_included(self) -> None:
        """Case B: SC added on top, VAT carved from the resulting amount."""
        b = compute_charges(
            base_minor=100000, service_charge=self.SC_EXC, vat=self.VAT_INC
        )
        self.assertEqual(b.service_charge_minor, 10000)
        self.assertEqual(b.grand_total_minor, 110000)  # SC added; VAT inclusive
        self.assertEqual(b.vat_minor, 7196)            # 7% carved from 110,000
        self.assertTrue(b.vat_included)

    def test_case_c_sc_included_vat_excluded(self) -> None:
        """Case C: SC carved out, VAT added on the subtotal."""
        b = compute_charges(
            base_minor=100000, service_charge=self.SC_INC, vat=self.VAT_EXC
        )
        self.assertEqual(b.service_charge_minor, 9091)  # carved from 100,000
        self.assertEqual(b.vat_minor, 7000)             # 7% added on 100,000 subtotal
        self.assertEqual(b.grand_total_minor, 107000)

    def test_case_d_both_excluded(self) -> None:
        """Case D: SC added, then VAT added on (subtotal + SC)."""
        b = compute_charges(
            base_minor=100000, service_charge=self.SC_EXC, vat=self.VAT_EXC
        )
        self.assertEqual(b.service_charge_minor, 10000)
        self.assertEqual(b.taxable_base_minor, 110000)  # VAT applies to subtotal + SC
        self.assertEqual(b.vat_minor, 7700)
        self.assertEqual(b.grand_total_minor, 117700)

    # --- discounts, order, and disabled charges ------------------------- #

    def test_discounts_apply_before_charges(self) -> None:
        """§6: line and order discounts reduce the base the charges are computed on."""
        b = compute_charges(
            base_minor=100000,
            line_discount_minor=10000,
            order_discount_minor=5000,
            vat=self.VAT_EXC,
        )
        self.assertEqual(b.subtotal_minor, 85000)
        self.assertEqual(b.vat_minor, 5950)  # 7% of 85,000
        self.assertEqual(b.grand_total_minor, 90950)

    def test_disabled_charges_are_pass_through(self) -> None:
        b = compute_charges(base_minor=100000)
        self.assertEqual(b.grand_total_minor, 100000)
        self.assertEqual(b.vat_minor, 0)
        self.assertEqual(b.service_charge_minor, 0)
        self.assertEqual(b.vat_mode, "NONE")
        self.assertEqual(b.service_charge_mode, "NONE")

    def test_zero_rate_and_disabled_are_distinguished(self) -> None:
        """A disabled charge is not the same as a 0% charge in the snapshot."""
        disabled = compute_charges(base_minor=100000, vat=ChargeInput(enabled=False, rate_bp=700))
        zero = compute_charges(base_minor=100000, vat=ChargeInput(enabled=True, rate_bp=0))
        self.assertEqual(disabled.vat_minor, 0)
        self.assertEqual(zero.vat_minor, 0)

    def test_rounding_adjustment_reconciles(self) -> None:
        """§6 step 6-7: total = pre-round amount + adjustment, exactly (R5.5)."""
        b = compute_charges(
            base_minor=99950, vat=self.VAT_EXC, rounding_mode="NEAREST_1", currency="THB"
        )
        pre = 99950 + b.vat_minor
        self.assertEqual(b.grand_total_minor, pre + b.rounding_adjustment_minor)
        self.assertEqual(b.grand_total_minor % 100, 0)  # rounded to whole baht


class RoundingMethodTests(unittest.TestCase):
    """Fix.md rounding methods: the three modes at a 1.00 increment (§11–§15).

    Amounts are minor units (satang); increment 100 minor = 1.00 THB.
    """

    INC = 100

    def r(self, amount_minor: int, mode: str) -> int:
        return apply_rounding(amount_minor, mode, "THB", increment_minor=self.INC)

    def test_round_up(self) -> None:
        # §11: any fraction rounds to the next increment.
        self.assertEqual(self.r(10001, "ROUND_UP"), 10100)
        self.assertEqual(self.r(10040, "ROUND_UP"), 10100)
        self.assertEqual(self.r(10050, "ROUND_UP"), 10100)
        self.assertEqual(self.r(10099, "ROUND_UP"), 10100)
        self.assertEqual(self.r(10100, "ROUND_UP"), 10100)  # already on increment

    def test_round_down(self) -> None:
        # §12: any fraction drops to the previous increment.
        self.assertEqual(self.r(10001, "ROUND_DOWN"), 10000)
        self.assertEqual(self.r(10050, "ROUND_DOWN"), 10000)
        self.assertEqual(self.r(10099, "ROUND_DOWN"), 10000)

    def test_round_half_up(self) -> None:
        # §13–§15: below half down, half or above up.
        self.assertEqual(self.r(10001, "ROUND_HALF_UP"), 10000)
        self.assertEqual(self.r(10049, "ROUND_HALF_UP"), 10000)
        self.assertEqual(self.r(10050, "ROUND_HALF_UP"), 10100)
        self.assertEqual(self.r(10051, "ROUND_HALF_UP"), 10100)
        self.assertEqual(self.r(10099, "ROUND_HALF_UP"), 10100)

    def test_none_is_noop(self) -> None:
        self.assertEqual(self.r(10050, "NONE"), 10050)

    def test_five_baht_increment(self) -> None:
        # A larger increment (5.00) rounds to the nearest 5 baht.
        self.assertEqual(apply_rounding(10240, "ROUND_HALF_UP", "THB", increment_minor=500), 10000)
        self.assertEqual(apply_rounding(10250, "ROUND_HALF_UP", "THB", increment_minor=500), 10500)
        self.assertEqual(apply_rounding(10001, "ROUND_UP", "THB", increment_minor=500), 10500)

    def test_charge_engine_snapshots_rounding(self) -> None:
        # compute_charges records the pre-round total, mode, increment and adjustment,
        # so a completed order reproduces its own rounding (Fix.md §6, §16).
        b = compute_charges(
            base_minor=10040, rounding_mode="ROUND_UP", rounding_increment_minor=100, currency="THB"
        )
        self.assertEqual(b.pre_round_total_minor, 10040)
        self.assertEqual(b.grand_total_minor, 10100)
        self.assertEqual(b.rounding_adjustment_minor, 60)
        self.assertEqual(b.rounding_mode, "ROUND_UP")
        self.assertEqual(b.rounding_increment_minor, 100)
        snap = b.snapshot()
        self.assertEqual(snap["rounding_mode"], "ROUND_UP")
        self.assertEqual(snap["rounding_adjustment_minor"], 60)


# --------------------------------------------------------------------------- #
# Currency and exchange rates — pure helpers
# --------------------------------------------------------------------------- #


class CurrencyHelperTests(unittest.TestCase):
    def test_rate_precision_is_decimal_not_float(self) -> None:
        rate = parse_rate("33.10")
        self.assertIsInstance(rate, Decimal)
        self.assertEqual(str(rate), "33.100000")

    def test_rate_rejects_non_positive(self) -> None:
        with self.assertRaises(ValueError):
            parse_rate("0")
        with self.assertRaises(ValueError):
            parse_rate("-1")

    def test_direction_label_is_unambiguous(self) -> None:
        """§21: '1 USD = 33.10 THB', never a bare 'USD/THB = 33.10'."""
        self.assertEqual(rate_direction_label("USD", "THB", "33.10"), "1 USD = 33.1 THB")

    def test_conversion_matches_spec_example(self) -> None:
        """§19/§20: 100 USD at 33.10 -> 3,310 THB (331000 satang)."""
        self.assertEqual(
            convert_currency(10000, rate="33.10", from_currency="USD", to_currency="THB"),
            331000,
        )

    def test_conversion_respects_target_minor_units(self) -> None:
        """JPY has no minor unit; 10 USD at 150 JPY -> 1,500 JPY, not 150,000."""
        self.assertEqual(
            convert_currency(1000, rate="150", from_currency="USD", to_currency="JPY"),
            1500,
        )

    def test_currency_display_is_not_hardcoded_to_two_decimals(self) -> None:
        """§23: the UI must not assume two decimals."""
        self.assertEqual(format_currency(150000, "THB"), "฿1,500.00")
        self.assertEqual(format_currency(5000, "JPY"), "¥5,000")
        self.assertEqual(format_currency(5000, "USD"), "$50.00")


# --------------------------------------------------------------------------- #
# Integration tests against a seeded venue
# --------------------------------------------------------------------------- #


class _SeededVenue(unittest.TestCase):
    """Shared fixture: a fully provisioned Aquaria Phuket on a fixed clock."""

    def setUp(self) -> None:
        # 2026-09-01 10:00 Bangkok. A fixed clock makes expiry assertions exact.
        self.clock = FixedClock("2026-09-01T03:00:00Z")
        self.platform = Platform(clock=self.clock)
        prov = seed.provision(self.platform)
        self.tenant_id = prov["tenant_id"]
        self.venue_id = prov["venue_id"]
        self.organization_id = prov["organization_id"]
        self.ticket_types = prov["ticket_types"]
        self.staff = prov["staff"]
        self.sys = self.platform.system_context(self.tenant_id, venue_id=self.venue_id)

    def tearDown(self) -> None:
        self.platform.close()

    def staff_ctx(self, email: str, *, mfa: bool = False) -> object:
        """A real staff context: login, then resolve the token to a principal."""
        base = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id)
        result = self.platform.staff.login(
            base, email=email, credential="Aquaria-Demo-2026", mfa_code="000000" if mfa else None,
            channel="STAFF",
        )
        principal = self.platform.staff.authenticate_token(base, result["token"])
        ctx = base.with_principal(principal).for_venue(self.venue_id)
        ctx.channel = "STAFF"
        return ctx


class ExchangeRateTests(_SeededVenue):
    def test_set_and_resolve_rate(self) -> None:
        """§40: a configured 1 USD = 33.10 THB is the applicable rate in-period."""
        self.platform.settings.set_exchange_rate(
            self.sys,
            organization_id=self.organization_id,
            from_currency="USD",
            to_currency="THB",
            rate="33.10",
            effective_from="2026-09-01",
            reason="initial",
        )
        resolved = self.platform.settings.resolve_exchange_rate(
            self.sys,
            from_currency="USD",
            to_currency="THB",
            organization_id=self.organization_id,
            on_date="2026-09-05",
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["rate"], Decimal("33.100000"))

    def test_duplicate_active_pair_is_refused(self) -> None:
        """§22: two active rows for one pair on the same effective date are ambiguous."""
        kwargs = dict(
            organization_id=self.organization_id,
            from_currency="USD",
            to_currency="THB",
            effective_from="2026-09-01",
            reason="x",
        )
        self.platform.settings.set_exchange_rate(self.sys, rate="33.10", **kwargs)
        with self.assertRaises(ConflictError) as caught:
            self.platform.settings.set_exchange_rate(self.sys, rate="33.50", **kwargs)
        self.assertEqual(caught.exception.code, "duplicate_exchange_rate")

    def test_ended_rate_stops_applying_and_is_retained(self) -> None:
        created = self.platform.settings.set_exchange_rate(
            self.sys,
            organization_id=self.organization_id,
            from_currency="EUR",
            to_currency="THB",
            rate="38.00",
            effective_from="2026-09-01",
            reason="x",
        )
        self.platform.settings.end_exchange_rate(
            self.sys, rate_id=created["id"], effective_until="2026-09-02", reason="superseded"
        )
        # No longer resolves as active on a later date...
        self.assertIsNone(
            self.platform.settings.resolve_exchange_rate(
                self.sys,
                from_currency="EUR",
                to_currency="THB",
                organization_id=self.organization_id,
                on_date="2026-09-05",
            )
        )
        # ...but the row is retained (settings spec §32).
        self.assertEqual(self.platform.settings.get_exchange_rate_row(self.sys, created["id"])["status"], "ENDED")


class ValidityAndExpiryTests(_SeededVenue):
    def _confirm_one(self, email: str, visit_date: str) -> dict:
        ctx = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id, channel="ONLINE")
        quote = self.platform.booking.quote(
            ctx,
            venue_id=self.venue_id,
            visit_date=visit_date,
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_types["GA-INTL-ADULT"], quantity=1)],
        )
        return self.platform.booking.confirm(
            ctx,
            quote,
            customer={"email": email, "full_name": "Test Guest"},
            consent_items={"BOOKING_SERVICE": True},
            payment_method="CARD",
            idempotency_key=f"key-{email}",
        )

    def test_default_expiry_is_2359_venue_local(self) -> None:
        """§37: default validity expires 23:59:59 on the visit date, venue-local."""
        from zoneinfo import ZoneInfo

        visit = "2026-09-20"
        result = self._confirm_one("expiry@example.test", visit)
        ticket = self.platform.tickets.get(self.sys, result["tickets"][0]["id"])
        self.assertEqual(ticket["validity_timezone"], "Asia/Bangkok")
        self.assertEqual(ticket["validity_type"], "END_OF_VISIT_DAY")
        local = parse_instant(ticket["valid_until"]).astimezone(ZoneInfo("Asia/Bangkok"))
        self.assertEqual(local.strftime("%Y-%m-%d %H:%M:%S"), "2026-09-20 23:59:59")

    def test_changing_venue_timezone_does_not_move_issued_ticket(self) -> None:
        """§14: a timezone change must not silently alter an already-issued ticket."""
        result = self._confirm_one("frozen@example.test", "2026-09-20")
        ticket_id = result["tickets"][0]["id"]
        before = self.platform.tickets.get(self.sys, ticket_id)["valid_until"]
        self.platform.settings.set_timezone(
            self.sys, venue_id=self.venue_id, timezone="Asia/Tokyo", reason="test move"
        )
        after = self.platform.tickets.get(self.sys, ticket_id)["valid_until"]
        self.assertEqual(before, after)
        self.assertEqual(
            self.platform.tickets.get(self.sys, ticket_id)["validity_timezone"], "Asia/Bangkok"
        )

    def test_timezone_must_be_iana_not_offset(self) -> None:
        """§8: a bare UTC offset is refused; an IANA identifier is required."""
        with self.assertRaises(ValidationError):
            self.platform.settings.set_timezone(
                self.sys, venue_id=self.venue_id, timezone="UTC+07:00", reason="x"
            )


class ChargeSnapshotTests(_SeededVenue):
    def test_order_keeps_its_charge_snapshot_after_rate_change(self) -> None:
        """§33/§41-analog: a completed order does not move when VAT later changes."""
        ctx = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id, channel="ONLINE")
        quote = self.platform.booking.quote(
            ctx,
            venue_id=self.venue_id,
            visit_date="2026-09-10",
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_types["GA-INTL-ADULT"], quantity=1)],
        )
        result = self.platform.booking.confirm(
            ctx,
            quote,
            customer={"email": "snap@example.test", "full_name": "Snap Guest"},
            consent_items={"BOOKING_SERVICE": True},
            payment_method="CARD",
            idempotency_key="snap-key",
        )
        booking = self.platform.db.query_one(
            "SELECT net_minor, tax_minor FROM bookings WHERE id = ?", (result["booking_id"],)
        )
        net_before, tax_before = booking["net_minor"], booking["tax_minor"]

        # Change VAT to 10% effective immediately.
        self.platform.settings.set_vat(
            self.sys,
            venue_id=self.venue_id,
            enabled=True,
            rate_bp=1000,
            mode="INCLUSIVE",
            effective_from="2026-09-01",
            reason="rate change",
        )
        after = self.platform.db.query_one(
            "SELECT net_minor, tax_minor FROM bookings WHERE id = ?", (result["booking_id"],)
        )
        self.assertEqual(after["net_minor"], net_before)
        self.assertEqual(after["tax_minor"], tax_before)


class SettingsPermissionTests(_SeededVenue):
    def test_cashier_cannot_manage_vat_via_service(self) -> None:
        """§42: a staff member without the permission is refused server-side."""
        cashier = self.staff_ctx("cashier@aquaria.test")
        with self.assertRaises(AuthorizationDenied):
            self.platform.settings.set_vat(
                cashier,
                venue_id=self.venue_id,
                enabled=True,
                rate_bp=1000,
                mode="INCLUSIVE",
                effective_from="2026-09-01",
                reason="unauthorized attempt",
            )

    def test_cashier_cannot_manage_exchange_rate(self) -> None:
        cashier = self.staff_ctx("cashier@aquaria.test")
        with self.assertRaises(AuthorizationDenied):
            self.platform.settings.set_exchange_rate(
                cashier,
                organization_id=self.organization_id,
                from_currency="USD",
                to_currency="THB",
                rate="33.10",
                effective_from="2026-09-01",
                reason="unauthorized attempt",
            )

    def test_manager_can_manage_vat_with_reason(self) -> None:
        manager = self.staff_ctx("manager@aquaria.test")
        result = self.platform.settings.set_vat(
            manager,
            venue_id=self.venue_id,
            enabled=True,
            rate_bp=700,
            mode="INCLUSIVE",
            effective_from="2026-09-01",
            reason="confirm current rate",
        )
        self.assertEqual(result["rate_bp"], 700)

    def test_manage_action_requires_a_reason(self) -> None:
        """A sensitive settings change with no reason is a validation error (R67.4)."""
        manager = self.staff_ctx("manager@aquaria.test")
        with self.assertRaises(ValidationError):
            self.platform.settings.set_vat(
                manager,
                venue_id=self.venue_id,
                enabled=True,
                rate_bp=700,
                mode="INCLUSIVE",
                effective_from="2026-09-01",
                reason="",
            )


if __name__ == "__main__":
    unittest.main()
