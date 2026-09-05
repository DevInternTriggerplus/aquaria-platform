"""Advanced promotion engine additions (add_features).

Two behaviours are under test, both genuine gaps in the base engine:

* **Nth-item discounts** (§4, §61): "second ticket 50% off", "cheapest item half
  price". The discount must land on the right *unit*, deterministically.
* **Cash-coupon accounting treatment** (§16, §68): a coupon configured as a
  discount reduces revenue; a coupon configured as stored value / a payment
  instrument settles part of the bill and must NOT be booked as a sales discount.
  This is the requirement the spec marks critical.
"""

from __future__ import annotations

import unittest

from utp.app import Platform
from utp.core.errors import ValidationError
from utp.core.money import to_minor
from utp.domain.cart import CartLine, CartSnapshot
from utp.services.booking import QuoteLineRequest


class AdvancedPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = Platform()
        p = self.platform
        tenant = p.tenancy.create_tenant(
            code="aquaria", name="Aquawalk Thailand", default_language="en", languages=["en"]
        )
        self.tenant_id = tenant["id"]
        ctx = p.system_context(self.tenant_id)
        org = p.tenancy.create_organization(ctx, code="AQW-TH", name="Aquawalk (Thailand) Co., Ltd.")
        self.org_id = org["id"]
        p.tenancy.create_venue_type(
            None, code="AQUARIUM", name="Aquarium", platform_level=True,
            template={"tax_model": "INCLUSIVE", "tax_rate_bp": 700},
        )
        venue = p.tenancy.create_venue(
            ctx, organization_id=org["id"], venue_type_code="AQUARIUM", code="AQP", short_code="AQP",
            name={"en": "Aquaria Phuket"}, timezone="Asia/Bangkok", currency="THB",
        )
        self.venue_id = venue["id"]
        self.ctx = ctx.for_venue(self.venue_id)

    def tearDown(self) -> None:
        self.platform.close()

    # ------------------------------------------------------------------ #

    def _cart(self, quantity: int = 2, unit: int = 1000, **kw) -> CartSnapshot:
        lines = [
            CartLine(
                index=0, product_id="p", product_code="GA", ticket_type_id="tt",
                ticket_type_code="ADULT", segment_id="s", segment_code="ADULT",
                quantity=quantity, unit_price_minor=to_minor(unit), currency="THB", tax_rate_bp=700,
            )
        ]
        return CartSnapshot(
            venue_id=self.venue_id, organization_id=self.org_id, currency="THB", channel="ONLINE",
            visit_date="2027-01-10", lines=lines, cart_id="c", customer_key="cust",
            tax_model="INCLUSIVE", tax_rate_bp=700, **kw,
        )

    def _two_price_cart(self) -> CartSnapshot:
        lines = [
            CartLine(index=0, product_id="p", product_code="GA", ticket_type_id="adult",
                     ticket_type_code="ADULT", segment_id="sa", segment_code="ADULT",
                     quantity=1, unit_price_minor=to_minor(1000), currency="THB", tax_rate_bp=700),
            CartLine(index=1, product_id="p", product_code="GA", ticket_type_id="child",
                     ticket_type_code="CHILD", segment_id="sc", segment_code="CHILD",
                     quantity=1, unit_price_minor=to_minor(600), currency="THB", tax_rate_bp=700),
        ]
        return CartSnapshot(
            venue_id=self.venue_id, organization_id=self.org_id, currency="THB", channel="ONLINE",
            visit_date="2027-01-10", lines=lines, cart_id="c2", customer_key="cust",
            tax_model="INCLUSIVE", tax_rate_bp=700,
        )

    # ------------------------------------------------------------------ #
    # Nth-item (§4, §61)
    # ------------------------------------------------------------------ #

    def test_second_item_50_percent(self) -> None:
        """§61: buy 1, second eligible item at 50% off — only the second is discounted."""
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="SECOND-50", name={"en": "Second ticket 50% off"},
            mechanic="SECOND_ITEM_DISCOUNT", config={"target": "SECOND", "percent_bp": 5000}, priority=30,
        )
        out = self.platform.promotions.evaluate(self.ctx, self._cart(quantity=2, unit=1000))
        self.assertEqual(out.discount_minor, to_minor(500), "half of one 1,000 ticket")

    def test_second_item_needs_two_units(self) -> None:
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="SECOND-50", name={"en": "Second 50%"},
            mechanic="SECOND_ITEM_DISCOUNT", config={"target": "SECOND", "percent_bp": 5000},
        )
        out = self.platform.promotions.evaluate(self.ctx, self._cart(quantity=1, unit=1000))
        self.assertEqual(out.discount_minor, 0, "a single item does not qualify")

    def test_cheapest_item_discount_targets_the_cheaper_unit(self) -> None:
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="CHEAPEST-100", name={"en": "Cheapest 100 off"},
            mechanic="SECOND_ITEM_DISCOUNT", config={"target": "CHEAPEST", "amount_minor": to_minor(100)},
        )
        out = self.platform.promotions.evaluate(self.ctx, self._two_price_cart())
        # The 100 THB comes off, attributed to the cheaper (child) unit.
        self.assertEqual(out.discount_minor, to_minor(100))
        child_line = next(line for line in out.cart.lines if line.ticket_type_code == "CHILD")
        self.assertEqual(child_line.discount_minor, to_minor(100))

    def test_second_item_across_two_pairs(self) -> None:
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="SECOND-50", name={"en": "Second 50%"},
            mechanic="SECOND_ITEM_DISCOUNT", config={"target": "SECOND", "percent_bp": 5000},
        )
        out = self.platform.promotions.evaluate(self.ctx, self._cart(quantity=4, unit=1000))
        self.assertEqual(out.discount_minor, to_minor(1000), "two of four tickets discounted")

    def test_nth_item_config_is_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.promotions.create_promotion(
                self.ctx, internal_code="BAD", name={"en": "Bad"},
                mechanic="SECOND_ITEM_DISCOUNT", config={"target": "NOPE", "percent_bp": 5000},
            )

    # ------------------------------------------------------------------ #
    # Cash-coupon accounting treatment (§16, §67, §68)
    # ------------------------------------------------------------------ #

    def test_coupon_as_discount_reduces_revenue(self) -> None:
        """§67: a coupon with treatment DISCOUNT reduces the sales total."""
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="DISC-500", name={"en": "500 off"}, mechanic="VOUCHER",
            config={"amount_minor": to_minor(500)}, code="DISC500",
            accounting_treatment="DISCOUNT", rules={"requires_code": True},
        )
        out = self.platform.promotions.evaluate(self.ctx, self._cart(quantity=1, unit=2000), codes=["DISC500"])
        d = out.as_dict()
        self.assertEqual(d["discount_minor"], to_minor(500))
        self.assertEqual(d["settlement_minor"], 0)
        self.assertEqual(d["total_minor"], to_minor(1500), "revenue reduced by the discount")

    def test_coupon_as_stored_value_is_not_a_discount(self) -> None:
        """§68: stored value settles the bill; it is NOT classified as a sales discount."""
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="GIFT-500", name={"en": "Gift card 500"}, mechanic="VOUCHER",
            config={"amount_minor": to_minor(500)}, code="GIFT500",
            accounting_treatment="STORED_VALUE", rules={"requires_code": True},
        )
        out = self.platform.promotions.evaluate(self.ctx, self._cart(quantity=1, unit=2000), codes=["GIFT500"])
        d = out.as_dict()
        self.assertEqual(d["discount_minor"], 0, "stored value must never be a discount")
        self.assertEqual(d["settlement_minor"], to_minor(500))
        self.assertEqual(d["total_minor"], to_minor(2000), "revenue stays on the full price")
        self.assertEqual(d["amount_payable_minor"], to_minor(1500), "the card pays the remainder")

    def test_stored_value_is_capped_at_amount_owed(self) -> None:
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="GIFT-BIG", name={"en": "Gift card 5000"}, mechanic="VOUCHER",
            config={"amount_minor": to_minor(5000)}, code="GIFTBIG",
            accounting_treatment="STORED_VALUE", rules={"requires_code": True},
        )
        out = self.platform.promotions.evaluate(self.ctx, self._cart(quantity=1, unit=2000), codes=["GIFTBIG"])
        d = out.as_dict()
        self.assertEqual(d["settlement_minor"], to_minor(2000), "never settle more than is owed")
        self.assertEqual(d["amount_payable_minor"], 0)

    def test_invalid_accounting_treatment_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.promotions.create_promotion(
                self.ctx, internal_code="BAD-T", name={"en": "Bad"}, mechanic="VOUCHER",
                config={"amount_minor": to_minor(500)}, accounting_treatment="NONSENSE",
            )

    def test_default_treatment_is_discount(self) -> None:
        promo = self.platform.promotions.create_promotion(
            self.ctx, internal_code="DEFAULT-T", name={"en": "Default"}, mechanic="VOUCHER",
            config={"amount_minor": to_minor(100)},
        )
        self.assertEqual(promo.get("accounting_treatment"), "DISCOUNT")

    # ------------------------------------------------------------------ #
    # Permissions (§58)
    # ------------------------------------------------------------------ #

    def test_new_permission_pages_and_actions_exist(self) -> None:
        from utp.domain import permissions as perms

        for page in ("Coupon Codes", "Cash Coupons", "Member Rewards", "Partner Benefits"):
            self.assertIn(page, perms.PAGES_BY_KEY, f"{page} must be in the registry")
        for action in (
            "PUBLISH_PROMOTION", "PAUSE_PROMOTION", "OVERRIDE_PROMOTION",
            "MANAGE_PROMOTION_BUDGET", "MANAGE_ACCOUNTING_TREATMENT",
            "APPLY_PARTNER_DISCOUNT", "APPLY_COMPLIMENTARY",
        ):
            self.assertIn(action, perms.ACTIONS_BY_KEY, f"{action} must be in the registry")

    def test_new_permissions_default_denied_for_a_minimal_role(self) -> None:
        from utp.domain import permissions as perms

        # A brand-new custom role holds nothing until granted (R40.11, R44.1).
        template = perms.RoleTemplate(code="X", name="X", authority_level=10)
        self.assertNotIn("Cash Coupons.VIEW", template.resolve())
        self.assertNotIn(f"{perms.ACTION_PREFIX}MANAGE_ACCOUNTING_TREATMENT", template.resolve())


class StoredValueCheckoutTests(unittest.TestCase):
    """End-to-end: a stored-value coupon reduces the actual gateway charge (§16, §68).

    Revenue stays on the full price; the card is charged only the remainder; the
    difference is recorded as settlement on the booking, not as a discount.
    """

    VISIT_DATE = "2026-09-10"

    def setUp(self) -> None:
        from utp.core.clock import FixedClock
        from utp.services.notifications import SimulatedEmailProvider
        from utp.services.payments import SimulatedProvider

        self.clock = FixedClock("2026-09-01T03:00:00Z")
        self.email = SimulatedEmailProvider()
        self.gateway = SimulatedProvider()
        self.platform = Platform(clock=self.clock, payment_provider=self.gateway, email_provider=self.email)
        p = self.platform
        tenant = p.tenancy.create_tenant(
            code="aquaria", name="Aquawalk Thailand", default_language="en", languages=["en"]
        )
        self.tenant_id = tenant["id"]
        ctx = p.system_context(self.tenant_id)
        org = p.tenancy.create_organization(ctx, code="AQW-TH", name="Aquawalk (Thailand) Co., Ltd.")
        p.tenancy.create_venue_type(
            None, code="AQUARIUM", name="Aquarium", platform_level=True,
            template={"tax_model": "INCLUSIVE", "tax_rate_bp": 700},
        )
        venue = p.tenancy.create_venue(
            ctx, organization_id=org["id"], venue_type_code="AQUARIUM", code="AQP", short_code="AQP",
            name={"en": "Aquaria Phuket"}, timezone="Asia/Bangkok", currency="THB",
        )
        self.venue_id = venue["id"]
        self.ctx = ctx.for_venue(self.venue_id)
        p.catalog.create_segment(self.ctx, code="ADULT", name={"en": "Adult"})
        experience = p.catalog.create_experience(
            self.ctx, venue_id=self.venue_id, code="GA", name={"en": "General Admission"}
        )
        product = p.catalog.create_product(
            self.ctx, venue_id=self.venue_id, code="GA-DAY", name={"en": "General Admission"},
            admission_model="GENERAL_ADMISSION", experience_id=experience["id"],
        )
        self.ticket_type = p.catalog.create_ticket_type(
            self.ctx, product_id=product["id"], segment_code="ADULT", code="GA-ADULT", name={"en": "Adult"}
        )
        p.pricing.create_price_rule(
            self.ctx, ticket_type_id=self.ticket_type["id"], amount_minor=to_minor(1000),
            currency="THB", code="ONLINE",
        )
        p.calendar.set_booking_rules(
            self.ctx, scope_type="VENUE", scope_id=self.venue_id, settings={"max_days_in_advance": 90}
        )
        p.consent.publish_notice(
            self.ctx, version="2026.1", consent_text_version="ct-2026.1", language="en",
            controller={"name": "Aquawalk (Thailand) Co., Ltd.", "contact": "privacy@aquaria.test"},
            purposes=[{"code": "BOOKING_SERVICE", "description": "Deliver the booking"}],
            retention={"bookings_years": 10},
            recipients=[{"name": "Payment provider", "role": "processor"}],
            rights=["access", "erasure"], dpo_contact="dpo@aquaria.test",
            notice_url="https://aquaria.test/privacy",
        )
        # A 500 THB gift card (stored value), redeemed by code.
        p.promotions.create_promotion(
            self.ctx, internal_code="GIFT-500", name={"en": "Gift card 500"}, mechanic="VOUCHER",
            config={"amount_minor": to_minor(500)}, code="GIFT500",
            accounting_treatment="STORED_VALUE", rules={"requires_code": True},
        )

    def tearDown(self) -> None:
        self.platform.close()

    def _guest_ctx(self):
        return self.platform.guest_context(
            self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en"
        )

    def test_gift_card_reduces_the_card_charge_not_the_revenue(self) -> None:
        ctx = self._guest_ctx()
        # Two adults at 1,000 = 2,000 revenue; a 500 gift card settles part of it.
        quote = self.platform.booking.quote(
            ctx, venue_id=self.venue_id, visit_date=self.VISIT_DATE,
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_type["id"], quantity=2)],
            promotion_codes=["GIFT500"],
        )
        checkout = self.platform.booking.start_checkout(ctx, quote)
        result = self.platform.booking.confirm(
            ctx, checkout,
            customer={"email": "guest@example.com", "full_name": "Somchai Jaidee"},
            consent_items={"BOOKING_SERVICE": True},
            payment_method="CARD", idempotency_key="idem-gift",
            expected_total_minor=checkout.total_minor,
        )
        self.assertTrue(result["confirmed"])
        # Revenue is the full price; the gift card is not a discount (§68).
        self.assertEqual(result["total_minor"], to_minor(2000))
        self.assertEqual(result["settlement_minor"], to_minor(500))
        self.assertEqual(result["amount_paid_minor"], to_minor(1500))
        # The gateway was actually asked to authorize only 1,500, not 2,000.
        authorizations = [c for c in self.gateway.calls if c["op"] == "authorize"]
        self.assertTrue(authorizations)
        self.assertEqual(authorizations[-1]["amount_minor"], to_minor(1500),
                         "the card must be charged only the remainder after stored value")

    def test_gift_card_covering_the_whole_bill_needs_no_gateway(self) -> None:
        ctx = self._guest_ctx()
        # One adult at 1,000; a 500 card plus... use a bigger card to cover it all.
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="GIFT-2000", name={"en": "Gift card 2000"}, mechanic="VOUCHER",
            config={"amount_minor": to_minor(2000)}, code="GIFT2000",
            accounting_treatment="STORED_VALUE", rules={"requires_code": True},
        )
        quote = self.platform.booking.quote(
            ctx, venue_id=self.venue_id, visit_date=self.VISIT_DATE,
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_type["id"], quantity=1)],
            promotion_codes=["GIFT2000"],
        )
        checkout = self.platform.booking.start_checkout(ctx, quote)
        before = len([c for c in self.gateway.calls if c["op"] == "authorize"])
        result = self.platform.booking.confirm(
            ctx, checkout,
            customer={"email": "fullcover@example.com", "full_name": "A B"},
            consent_items={"BOOKING_SERVICE": True},
            payment_method="CARD", idempotency_key="idem-full",
            expected_total_minor=checkout.total_minor,
        )
        after = len([c for c in self.gateway.calls if c["op"] == "authorize"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["amount_paid_minor"], 0)
        self.assertEqual(result["settlement_minor"], to_minor(1000), "capped at the amount owed")
        self.assertEqual(before, after, "a fully-covered bill must not call the payment gateway")


class _BookableVenue:
    """Mixin: provisions a full bookable venue for end-to-end checkout tests."""

    VISIT_DATE = "2026-09-10"

    def _provision(self):
        from utp.core.clock import FixedClock
        from utp.services.notifications import SimulatedEmailProvider
        from utp.services.payments import SimulatedProvider

        self.clock = FixedClock("2026-09-01T03:00:00Z")
        self.email = SimulatedEmailProvider()
        self.gateway = SimulatedProvider()
        self.platform = Platform(clock=self.clock, payment_provider=self.gateway, email_provider=self.email)
        p = self.platform
        tenant = p.tenancy.create_tenant(
            code="aquaria", name="Aquawalk Thailand", default_language="en", languages=["en"]
        )
        self.tenant_id = tenant["id"]
        ctx = p.system_context(self.tenant_id)
        self.sys = ctx
        org = p.tenancy.create_organization(ctx, code="AQW-TH", name="Aquawalk (Thailand) Co., Ltd.")
        self.org_id = org["id"]
        p.tenancy.create_venue_type(
            None, code="AQUARIUM", name="Aquarium", platform_level=True,
            template={"tax_model": "INCLUSIVE", "tax_rate_bp": 700},
        )
        venue = p.tenancy.create_venue(
            ctx, organization_id=org["id"], venue_type_code="AQUARIUM", code="AQP", short_code="AQP",
            name={"en": "Aquaria Phuket"}, timezone="Asia/Bangkok", currency="THB",
        )
        self.venue_id = venue["id"]
        self.ctx = ctx.for_venue(self.venue_id)
        p.catalog.create_segment(self.ctx, code="ADULT", name={"en": "Adult"})
        experience = p.catalog.create_experience(
            self.ctx, venue_id=self.venue_id, code="GA", name={"en": "General Admission"}
        )
        product = p.catalog.create_product(
            self.ctx, venue_id=self.venue_id, code="GA-DAY", name={"en": "General Admission"},
            admission_model="GENERAL_ADMISSION", experience_id=experience["id"],
        )
        self.ticket_type = p.catalog.create_ticket_type(
            self.ctx, product_id=product["id"], segment_code="ADULT", code="GA-ADULT", name={"en": "Adult"}
        )
        p.pricing.create_price_rule(
            self.ctx, ticket_type_id=self.ticket_type["id"], amount_minor=to_minor(1000),
            currency="THB", code="ONLINE",
        )
        p.calendar.set_booking_rules(
            self.ctx, scope_type="VENUE", scope_id=self.venue_id, settings={"max_days_in_advance": 90}
        )
        p.consent.publish_notice(
            self.ctx, version="2026.1", consent_text_version="ct-2026.1", language="en",
            controller={"name": "Aquawalk (Thailand) Co., Ltd.", "contact": "privacy@aquaria.test"},
            purposes=[{"code": "BOOKING_SERVICE", "description": "Deliver the booking"}],
            retention={"bookings_years": 10},
            recipients=[{"name": "Payment provider", "role": "processor"}],
            rights=["access", "erasure"], dpo_contact="dpo@aquaria.test",
            notice_url="https://aquaria.test/privacy",
        )

    def _guest_ctx(self):
        return self.platform.guest_context(
            self.tenant_id, venue_id=self.venue_id, channel="ONLINE", language="en"
        )

    def _checkout(self, ctx, *, quantity=1, codes=(), points_redeem=None, email="guest@example.com",
                  key="idem-x"):
        quote = self.platform.booking.quote(
            ctx, venue_id=self.venue_id, visit_date=self.VISIT_DATE,
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_type["id"], quantity=quantity)],
            promotion_codes=list(codes),
        )
        checkout = self.platform.booking.start_checkout(ctx, quote)
        return self.platform.booking.confirm(
            ctx, checkout, customer={"email": email, "full_name": "Somchai Jaidee"},
            consent_items={"BOOKING_SERVICE": True}, payment_method="CARD",
            idempotency_key=key, expected_total_minor=checkout.total_minor,
            points_redeem=points_redeem,
        )


class MemberPointsCheckoutTests(_BookableVenue, unittest.TestCase):
    """Loyalty points redeemed at checkout settle the bill (§32, §33, §69)."""

    def setUp(self) -> None:
        self._provision()
        # 1 point = 1 THB (100 satang per point).
        self.platform.members.set_conversion_rate(self.ctx, rate="100", venue_id=self.venue_id)

    def tearDown(self) -> None:
        self.platform.close()

    def test_points_redeemed_at_checkout_reduce_the_card_charge(self) -> None:
        member = self.platform.members.enrol(self.ctx, email="guest@example.com", tier="GOLD")
        self.platform.members.earn(self.ctx, member_id=member["id"], points=300, reason="signup")

        ctx = self._guest_ctx()
        # One adult @1,000; redeem 300 points = 300 THB toward it.
        result = self._checkout(ctx, quantity=2, points_redeem=300, key="idem-pts")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["total_minor"], to_minor(2000), "revenue unaffected by points")
        self.assertEqual(result["settlement_minor"], to_minor(300))
        self.assertEqual(result["amount_paid_minor"], to_minor(1700))
        authorizations = [c for c in self.gateway.calls if c["op"] == "authorize"]
        self.assertEqual(authorizations[-1]["amount_minor"], to_minor(1700))
        self.assertEqual(self.platform.members.balance(self.ctx, member["id"]), 0, "points spent")

    def test_points_spent_exactly_once_and_restored_on_cancel(self) -> None:
        member = self.platform.members.enrol(self.ctx, email="guest@example.com")
        self.platform.members.earn(self.ctx, member_id=member["id"], points=500)
        ctx = self._guest_ctx()
        result = self._checkout(ctx, quantity=1, points_redeem=100, key="idem-cancel")
        self.assertEqual(self.platform.members.balance(self.ctx, member["id"]), 400)
        # Cancelling the booking gives the points back (§69 — redemption belongs to a
        # committed, still-valid transaction).
        self.platform.booking.cancel(
            self.ctx,
            result["booking_id"],
            reason="customer changed plans",
            refund=False,
            confirmed=True,
        )
        self.assertEqual(self.platform.members.balance(self.ctx, member["id"]), 500)

    def test_redeeming_more_than_owed_is_capped(self) -> None:
        member = self.platform.members.enrol(self.ctx, email="guest@example.com")
        self.platform.members.earn(self.ctx, member_id=member["id"], points=5000)
        ctx = self._guest_ctx()
        # One adult @1,000; try to redeem 5,000 points (5,000 THB) — only 1,000 owed.
        result = self._checkout(ctx, quantity=1, points_redeem=5000, key="idem-cap")
        self.assertEqual(result["settlement_minor"], to_minor(1000))
        self.assertEqual(result["amount_paid_minor"], 0)
        # Only the points needed to cover the bill were spent.
        self.assertEqual(self.platform.members.balance(self.ctx, member["id"]), 4000)


class FreeGiftTests(_BookableVenue, unittest.TestCase):
    """Free-gift promotions grant a reward at zero price with stock control (§11-§12)."""

    def setUp(self) -> None:
        self._provision()

    def tearDown(self) -> None:
        self.platform.close()

    def test_spend_threshold_grants_a_free_gift_without_discounting(self) -> None:
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="GIFT-TOTE", name={"en": "Free tote bag"}, mechanic="FREE_GIFT",
            config={"reward": {"kind": "PRODUCT", "name": "Tote Bag"}, "reward_quantity": 1},
            rules={"min_purchase_minor": to_minor(2000)}, priority=10,
        )
        ctx = self._guest_ctx()
        result = self._checkout(ctx, quantity=2, key="idem-gift")  # 2,000 spend
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["total_minor"], to_minor(2000), "the gift is free, not a discount")
        self.assertTrue(result["gifts"], "a gift must be recorded on the order")
        self.assertEqual(result["gifts"][0]["reward"]["name"], "Tote Bag")

    def test_gift_not_granted_below_threshold(self) -> None:
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="GIFT-TOTE", name={"en": "Free tote bag"}, mechanic="FREE_GIFT",
            config={"reward": {"kind": "PRODUCT", "name": "Tote Bag"}},
            rules={"min_purchase_minor": to_minor(2000)},
        )
        ctx = self._guest_ctx()
        result = self._checkout(ctx, quantity=1, key="idem-nogift")  # only 1,000 spend
        self.assertEqual(result["gifts"], [], "below the threshold, no gift")

    def test_reward_stock_prevents_over_granting(self) -> None:
        # usage_limit doubles as reward stock: only one tote exists.
        self.platform.promotions.create_promotion(
            self.ctx, internal_code="GIFT-ONE", name={"en": "Free tote (1 only)"}, mechanic="FREE_GIFT",
            config={"reward": {"kind": "PRODUCT", "name": "Tote Bag"}},
            rules={"min_purchase_minor": to_minor(1000)}, usage_limit=1,
        )
        ctx = self._guest_ctx()
        first = self._checkout(ctx, quantity=1, email="a@example.com", key="idem-g1")
        self.assertTrue(first["gifts"], "the first customer gets the gift")
        # Stock is now depleted; the next evaluation must not offer it.
        quote = self.platform.booking.quote(
            self._guest_ctx(), venue_id=self.venue_id, visit_date=self.VISIT_DATE,
            lines=[QuoteLineRequest(ticket_type_id=self.ticket_type["id"], quantity=1)],
        )
        checkout = self.platform.booking.start_checkout(self._guest_ctx(), quote)
        self.assertEqual(checkout.as_dict()["summary"]["gifts"], [], "out of stock: no gift offered")

    def test_free_gift_config_requires_a_reward(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.promotions.create_promotion(
                self.ctx, internal_code="GIFT-BAD", name={"en": "Bad"}, mechanic="FREE_GIFT",
                config={"reward_quantity": 1},
            )


if __name__ == "__main__":
    unittest.main()
