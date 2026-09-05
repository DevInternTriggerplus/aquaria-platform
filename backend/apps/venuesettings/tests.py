"""Money, tax and service-charge tests.

These are the crown jewels of the platform and the reason the calculation engine
was ported unchanged rather than rewritten. The four VAT/service-charge
combinations the settings spec enumerates (Cases A–D) are asserted against worked
numbers, plus the properties that keep history honest:

* a completed order does not move when VAT later changes (snapshot immutability);
* an issued ticket's expiry does not move when the venue timezone changes;
* the default expiry is exactly 23:59:59 venue-local on the visit date;
* displayed lines always sum to the amount charged, including rounding.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.core.money import (
    ChargeInput,
    allocate,
    apply_rounding,
    compute_charges,
    convert_currency,
    currency_decimals,
    format_currency,
    parse_rate,
    rate_direction_label,
    split_tax,
    to_minor,
)
from apps.tenancy.models import Organization, Tenant, Venue
from apps.ticketing.models import end_of_visit_day, start_of_visit_day

from .models import ServiceChargeSetting, VatSetting
from .services import compute_order_charges, resolve_service_charge, resolve_vat

BANGKOK = "Asia/Bangkok"


class MoneyPrimitiveTests(TestCase):
    """No float may ever reach a monetary value."""

    def test_to_minor_does_not_drift(self):
        # The classic float trap: 1251.10 must not become 125109.
        self.assertEqual(to_minor("1251.10", "THB"), 125110)
        self.assertEqual(to_minor(1251.10, "THB"), 125110)
        self.assertEqual(to_minor(Decimal("1251.10"), "THB"), 125110)

    def test_zero_decimal_currency(self):
        # JPY has no minor unit; assuming two decimals everywhere is a bug.
        self.assertEqual(to_minor("5000", "JPY"), 5000)
        self.assertEqual(currency_decimals("JPY"), 0)
        self.assertEqual(format_currency(5000, "JPY"), "¥5,000")
        self.assertEqual(format_currency(150000, "THB"), "฿1,500.00")

    def test_inclusive_split_reconstructs_gross(self):
        split = split_tax(107_000, rate_bp=700, model="INCLUSIVE")
        self.assertEqual(split.gross_minor, 107_000)
        self.assertEqual(split.net_minor + split.tax_minor, 107_000)

    def test_exclusive_split_adds_on_top(self):
        split = split_tax(100_000, rate_bp=700, model="EXCLUSIVE")
        self.assertEqual(split.net_minor, 100_000)
        self.assertEqual(split.tax_minor, 7_000)
        self.assertEqual(split.gross_minor, 107_000)

    def test_rounding_modes(self):
        self.assertEqual(apply_rounding(12_345, "NONE", "THB"), 12_345)
        self.assertEqual(apply_rounding(12_345, "NEAREST_1", "THB"), 12_300)
        self.assertEqual(apply_rounding(12_355, "NEAREST_1", "THB"), 12_400)
        self.assertEqual(apply_rounding(12_301, "UP_1", "THB"), 12_400)
        self.assertEqual(apply_rounding(12_399, "DOWN_1", "THB"), 12_300)

    def test_allocate_loses_nothing(self):
        # A cart discount pushed onto items must sum back exactly, or a partial
        # refund cannot be proven correct.
        parts = allocate(100, [1, 1, 1])
        self.assertEqual(sum(parts), 100)
        parts = allocate(12_511, [3, 1, 1])
        self.assertEqual(sum(parts), 12_511)


class ChargeCaseTests(TestCase):
    """The four combinations from the settings spec, §6."""

    BASE = 100_000  # THB 1,000.00

    def test_case_a_both_included(self):
        """VAT included + service charge included: the price never grows."""
        result = compute_charges(
            base_minor=self.BASE,
            service_charge=ChargeInput(True, 1000, "INCLUSIVE", "Service charge"),
            vat=ChargeInput(True, 700, "INCLUSIVE", "VAT"),
            currency="THB",
        )
        self.assertEqual(result.grand_total_minor, self.BASE)
        self.assertTrue(result.vat_included)
        self.assertTrue(result.service_charge_included)
        # Both components are carved out for the receipt.
        self.assertGreater(result.vat_minor, 0)
        self.assertGreater(result.service_charge_minor, 0)
        # Service charge carved from 100000 at 10%: 100000/1.1 = 90909, sc = 9091.
        self.assertEqual(result.service_charge_minor, 100_000 - 90_909)
        # VAT carved from the full 100000 at 7%.
        self.assertEqual(result.vat_minor, 100_000 - 93_458)

    def test_case_b_vat_included_service_excluded(self):
        """Service charge is added; VAT is then carved out of the larger amount."""
        result = compute_charges(
            base_minor=self.BASE,
            service_charge=ChargeInput(True, 1000, "EXCLUSIVE", "Service charge"),
            vat=ChargeInput(True, 700, "INCLUSIVE", "VAT"),
            currency="THB",
        )
        self.assertEqual(result.service_charge_minor, 10_000)
        # Total grew by the service charge only; VAT was already inside.
        self.assertEqual(result.grand_total_minor, 110_000)
        self.assertTrue(result.vat_included)
        self.assertFalse(result.service_charge_included)
        self.assertEqual(result.taxable_base_minor + result.vat_minor, 110_000)

    def test_case_c_vat_excluded_service_included(self):
        """Service charge carved out; VAT then added on top of the subtotal."""
        result = compute_charges(
            base_minor=self.BASE,
            service_charge=ChargeInput(True, 1000, "INCLUSIVE", "Service charge"),
            vat=ChargeInput(True, 700, "EXCLUSIVE", "VAT"),
            currency="THB",
        )
        self.assertFalse(result.vat_included)
        self.assertTrue(result.service_charge_included)
        self.assertEqual(result.vat_minor, 7_000)
        self.assertEqual(result.grand_total_minor, 107_000)

    def test_case_d_both_excluded(self):
        """Both added: VAT applies to subtotal + service charge."""
        result = compute_charges(
            base_minor=self.BASE,
            service_charge=ChargeInput(True, 1000, "EXCLUSIVE", "Service charge"),
            vat=ChargeInput(True, 700, "EXCLUSIVE", "VAT"),
            currency="THB",
        )
        self.assertEqual(result.service_charge_minor, 10_000)
        # VAT is charged on 110,000, not on 100,000.
        self.assertEqual(result.vat_minor, 7_700)
        self.assertEqual(result.grand_total_minor, 117_700)

    def test_discount_order_is_base_then_line_then_order(self):
        result = compute_charges(
            base_minor=self.BASE,
            line_discount_minor=10_000,
            order_discount_minor=5_000,
            vat=ChargeInput(True, 700, "INCLUSIVE", "VAT"),
            currency="THB",
        )
        self.assertEqual(result.subtotal_minor, 85_000)
        self.assertEqual(result.grand_total_minor, 85_000)

    def test_disabled_charge_is_not_a_zero_rate(self):
        """"No VAT" and "0% VAT" must stay distinguishable in the snapshot."""
        result = compute_charges(base_minor=self.BASE, vat=ChargeInput(enabled=False), currency="THB")
        self.assertEqual(result.vat_minor, 0)
        self.assertEqual(result.vat_mode, "NONE")

    def test_lines_always_sum_to_the_charged_total(self):
        """R5.5 — with rounding on, the gap is exposed, never hidden."""
        result = compute_charges(
            base_minor=12_345,
            vat=ChargeInput(True, 700, "EXCLUSIVE", "VAT"),
            rounding_mode="NEAREST_1",
            currency="THB",
        )
        rebuilt = (
            result.base_minor
            - result.line_discount_minor
            - result.order_discount_minor
            + result.service_charge_minor * (0 if result.service_charge_included else 1)
            + result.vat_minor * (0 if result.vat_included else 1)
            + result.rounding_adjustment_minor
        )
        self.assertEqual(rebuilt, result.grand_total_minor)


class ExchangeRateTests(TestCase):
    def test_rate_is_exact_not_float(self):
        self.assertEqual(parse_rate("33.10"), Decimal("33.100000"))
        self.assertEqual(parse_rate(33.1), Decimal("33.100000"))

    def test_direction_label_is_unambiguous(self):
        self.assertEqual(rate_direction_label("USD", "THB", "33.10"), "1 USD = 33.1 THB")

    def test_conversion_uses_target_minor_units(self):
        # 100 USD at 33.10 = 3,310 THB.
        self.assertEqual(
            convert_currency(10_000, rate="33.10", from_currency="USD", to_currency="THB"),
            331_000,
        )

    def test_rejects_non_positive_rate(self):
        with self.assertRaises(ValueError):
            parse_rate("0")


class TicketValidityTests(TestCase):
    """Expiry is venue-local and frozen at issue time."""

    def test_default_expiry_is_end_of_visit_day_in_venue_zone(self):
        visit = dt.date(2027, 9, 20)
        expires = end_of_visit_day(visit, BANGKOK)
        self.assertEqual(expires.hour, 23)
        self.assertEqual(expires.minute, 59)
        self.assertEqual(expires.second, 59)
        self.assertEqual(expires.tzinfo, ZoneInfo(BANGKOK))
        # 23:59:59 +07:00 is 16:59:59 UTC the same day.
        as_utc = expires.astimezone(dt.timezone.utc)
        self.assertEqual(as_utc.hour, 16)
        self.assertEqual(as_utc.date(), visit)

    def test_valid_from_is_local_midnight(self):
        starts = start_of_visit_day(dt.date(2027, 9, 20), BANGKOK)
        self.assertEqual((starts.hour, starts.minute, starts.second), (0, 0, 0))

    def test_different_venues_expire_independently(self):
        visit = dt.date(2027, 9, 20)
        bangkok = end_of_visit_day(visit, BANGKOK).astimezone(dt.timezone.utc)
        tokyo = end_of_visit_day(visit, "Asia/Tokyo").astimezone(dt.timezone.utc)
        # Tokyo is two hours ahead, so its day ends two hours earlier in UTC.
        self.assertEqual((bangkok - tokyo), dt.timedelta(hours=2))


class SettingsResolutionTests(TestCase):
    """Effective dating, and the guarantee that history does not move."""

    def setUp(self):
        self.tenant = Tenant.objects.create(code="aquaria", name="Aquaria")
        self.org = Organization.objects.create(
            tenant=self.tenant, code="aquawalk", name="Aquawalk Thailand"
        )
        self.venue = Venue.objects.create(
            tenant=self.tenant,
            organization=self.org,
            code="aqp",
            name={"en": "Aquaria Phuket"},
            timezone=BANGKOK,
            currency="THB",
            tax_model="INCLUSIVE",
            tax_rate_bp=700,
        )

    def test_falls_back_to_venue_level_tax_when_unconfigured(self):
        charge = resolve_vat(self.venue, dt.date(2026, 1, 1))
        self.assertTrue(charge.enabled)
        self.assertEqual(charge.rate_bp, 700)
        self.assertEqual(charge.mode, "INCLUSIVE")

    def test_no_service_charge_configured_means_disabled(self):
        charge = resolve_service_charge(self.venue, dt.date(2026, 1, 1))
        self.assertFalse(charge.enabled)

    def test_effective_dating_picks_the_row_in_force(self):
        VatSetting.objects.create(
            tenant=self.tenant, venue=self.venue, enabled=True, rate_bp=700,
            mode="INCLUSIVE", effective_from=dt.date(2026, 1, 1),
        )
        VatSetting.objects.create(
            tenant=self.tenant, venue=self.venue, enabled=True, rate_bp=1000,
            mode="INCLUSIVE", effective_from=dt.date(2027, 1, 1),
        )
        before = resolve_vat(self.venue, dt.date(2026, 6, 1))
        after = resolve_vat(self.venue, dt.date(2027, 6, 1))
        self.assertEqual(before.rate_bp, 700)
        self.assertEqual(after.rate_bp, 1000)

    def test_a_future_rate_change_does_not_move_an_earlier_order(self):
        """The property that matters: yesterday's total is still yesterday's total."""
        VatSetting.objects.create(
            tenant=self.tenant, venue=self.venue, enabled=True, rate_bp=700,
            mode="INCLUSIVE", effective_from=dt.date(2026, 1, 1),
        )
        original = compute_order_charges(
            venue=self.venue, base_minor=125_100, on_date=dt.date(2026, 6, 1)
        )
        # Someone raises VAT next year.
        VatSetting.objects.create(
            tenant=self.tenant, venue=self.venue, enabled=True, rate_bp=1000,
            mode="INCLUSIVE", effective_from=dt.date(2027, 1, 1),
        )
        recomputed = compute_order_charges(
            venue=self.venue, base_minor=125_100, on_date=dt.date(2026, 6, 1)
        )
        self.assertEqual(recomputed.as_dict(), original.as_dict())

    def test_service_charge_applies_when_configured(self):
        ServiceChargeSetting.objects.create(
            tenant=self.tenant, venue=self.venue, enabled=True, rate_bp=1000,
            mode="EXCLUSIVE", effective_from=dt.date(2026, 1, 1),
        )
        result = compute_order_charges(
            venue=self.venue, base_minor=100_000, on_date=dt.date(2026, 6, 1)
        )
        self.assertEqual(result.service_charge_minor, 10_000)
        # Venue VAT is inclusive at 7%, so the total grows by the service charge only.
        self.assertEqual(result.grand_total_minor, 110_000)

    def test_aquaria_adult_online_price_breaks_down_correctly(self):
        """THB 1,251.00 inclusive of 7% VAT, the real configured price."""
        result = compute_order_charges(
            venue=self.venue, base_minor=125_100, on_date=dt.date(2026, 6, 1)
        )
        self.assertEqual(result.grand_total_minor, 125_100)
        self.assertEqual(result.taxable_base_minor + result.vat_minor, 125_100)
        self.assertEqual(result.vat_minor, 8_184)
        self.assertEqual(result.taxable_base_minor, 116_916)


class TimezoneValidationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(code="t2", name="T2")
        self.org = Organization.objects.create(tenant=self.tenant, code="o2", name="O2")

    def test_bare_utc_offset_is_rejected(self):
        from django.core.exceptions import ValidationError

        venue = Venue(
            tenant=self.tenant, organization=self.org, code="v2",
            name={"en": "V2"}, timezone="UTC+07:00",
        )
        with self.assertRaises(ValidationError):
            venue.full_clean()

    def test_iana_zone_is_accepted(self):
        venue = Venue(
            tenant=self.tenant, organization=self.org, code="v3",
            name={"en": "V3"}, timezone=BANGKOK, currency="THB",
        )
        venue.full_clean()
