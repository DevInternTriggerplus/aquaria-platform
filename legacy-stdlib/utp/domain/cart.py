"""Cart model shared by pricing, promotions, consent and booking.

A cart is a *value*, not a stored entity. It is built from resolved prices, passed
through the promotion engine, revalidated immediately before payment, and only
then written as a booking. Keeping it immutable-ish and explicit is what makes
"revalidate everything before charging" (R13.7, R57.11) a cheap operation rather
than a re-derivation of state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..core.money import allocate


@dataclass(slots=True)
class CartLine:
    """One priced line: a ticket type at a quantity, optionally on a session/seat."""

    index: int
    product_id: str
    product_code: str
    ticket_type_id: str
    ticket_type_code: str
    segment_id: str
    segment_code: str
    quantity: int
    unit_price_minor: int
    currency: str
    price_rule_id: str | None = None
    session_id: str | None = None
    seat_id: str | None = None
    zone_id: str | None = None
    tax_rate_bp: int = 0
    tax_model: str = "INCLUSIVE"
    is_addon: bool = False
    parent_index: int | None = None
    price_unit: str = "PER_PERSON"
    consumes_capacity: bool = True
    is_complimentary: bool = False
    hold_id: str | None = None
    #: Promotions applied to this line, in application order (R13.9).
    promotions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def gross_minor(self) -> int:
        return int(self.unit_price_minor) * int(self.quantity)

    @property
    def discount_minor(self) -> int:
        return sum(int(p.get("amount_minor", 0)) for p in self.promotions)

    @property
    def net_minor(self) -> int:
        return max(self.gross_minor - self.discount_minor, 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "product_id": self.product_id,
            "product_code": self.product_code,
            "ticket_type_id": self.ticket_type_id,
            "ticket_type_code": self.ticket_type_code,
            "segment_id": self.segment_id,
            "segment_code": self.segment_code,
            "quantity": self.quantity,
            "unit_price_minor": self.unit_price_minor,
            "gross_minor": self.gross_minor,
            "discount_minor": self.discount_minor,
            "net_minor": self.net_minor,
            "currency": self.currency,
            "session_id": self.session_id,
            "seat_id": self.seat_id,
            "zone_id": self.zone_id,
            "price_rule_id": self.price_rule_id,
            "price_unit": self.price_unit,
            "is_addon": self.is_addon,
            "is_complimentary": self.is_complimentary,
            "promotions": list(self.promotions),
        }


@dataclass(slots=True)
class CartSnapshot:
    """A complete, priced cart at a point in time."""

    venue_id: str
    organization_id: str
    currency: str
    channel: str
    visit_date: str | None
    lines: list[CartLine] = field(default_factory=list)
    cart_id: str = ""
    partner_id: str | None = None
    payment_method: str | None = None
    customer_key: str | None = None
    language: str = "en"
    promotion_codes: list[str] = field(default_factory=list)
    #: Cart-level discounts that were not attributable to a single line.
    cart_promotions: list[dict[str, Any]] = field(default_factory=list)
    #: Stored-value / payment-instrument redemptions (gift cards, cash coupons that
    #: were sold, points-as-cash). These are NOT sales discounts: they reduce what
    #: the customer pays by card while leaving revenue and VAT computed on the full
    #: price (add_features §16, §68). Kept separate from ``cart_promotions`` so the
    #: two can never be conflated in reporting or tax.
    settlements: list[dict[str, Any]] = field(default_factory=list)
    #: Free gifts / rewards granted with this order (add_features §11-§12). A gift is
    #: a zero-cost reward attached to the order — not a discount on the paid items —
    #: so it never touches ``discount_minor`` or the amount payable.
    gifts: list[dict[str, Any]] = field(default_factory=list)
    tax_model: str = "INCLUSIVE"
    tax_rate_bp: int = 0
    rounding_mode: str = "NONE"

    # ---------------------------------------------------------------- #

    @property
    def gross_minor(self) -> int:
        return sum(line.gross_minor for line in self.lines)

    @property
    def line_discount_minor(self) -> int:
        return sum(line.discount_minor for line in self.lines)

    @property
    def discount_minor(self) -> int:
        """Every discount on the cart, whether attributed to a line or still cart-level.

        One definition, used by the order summary and by the total the customer is
        charged, so the two cannot drift apart (R5.5).
        """
        return self.line_discount_minor + sum(
            int(p.get("amount_minor", 0)) for p in self.cart_promotions
        )

    @property
    def settlement_minor(self) -> int:
        """Stored-value applied to the order — reduces payment, not revenue (§16)."""
        return sum(int(s.get("amount_minor", 0)) for s in self.settlements)

    @property
    def total_quantity(self) -> int:
        return sum(int(line.quantity) for line in self.lines)

    def line(self, index: int) -> CartLine:
        for candidate in self.lines:
            if candidate.index == index:
                return candidate
        raise KeyError(index)

    def clone(self) -> CartSnapshot:
        """Deep-enough copy so promotion evaluation never mutates the caller's cart."""
        return replace(
            self,
            lines=[replace(line, promotions=[dict(p) for p in line.promotions]) for line in self.lines],
            cart_promotions=[dict(p) for p in self.cart_promotions],
            settlements=[dict(s) for s in self.settlements],
            gifts=[dict(g) for g in self.gifts],
            promotion_codes=list(self.promotion_codes),
        )

    def reset_promotions(self) -> None:
        for line in self.lines:
            line.promotions = []
        self.cart_promotions = []
        self.settlements = []
        self.gifts = []

    def push_cart_discount_to_lines(self) -> None:
        """Spread any cart-level discount across lines without losing a satang.

        Partial refunds and per-item reporting both need every discount attributed
        to a line, so largest-remainder allocation is applied once, here (R13.9).
        """
        for promotion in self.cart_promotions:
            amount = int(promotion.get("amount_minor", 0))
            if amount <= 0:
                continue
            eligible = [line for line in self.lines if line.net_minor > 0]
            if not eligible:
                continue
            weights = [line.net_minor for line in eligible]
            for line, share in zip(eligible, allocate(amount, weights)):
                if share <= 0:
                    continue
                line.promotions.append({**promotion, "amount_minor": share, "allocated_from_cart": True})
        self.cart_promotions = []

    def summary(self) -> dict[str, Any]:
        """Persistent order summary shown at every step from selection onward (R11.3)."""
        applied: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        for line in self.lines:
            for promotion in line.promotions:
                key = str(promotion.get("promotion_id") or promotion.get("name"))
                entry = seen.get(key)
                if entry is None:
                    entry = {
                        "promotion_id": promotion.get("promotion_id"),
                        "name": promotion.get("name"),
                        "code": promotion.get("code"),
                        "mechanic": promotion.get("mechanic"),
                        "amount_minor": 0,
                    }
                    seen[key] = entry
                    applied.append(entry)
                entry["amount_minor"] += int(promotion.get("amount_minor", 0))
        for promotion in self.cart_promotions:
            applied.append(
                {
                    "promotion_id": promotion.get("promotion_id"),
                    "name": promotion.get("name"),
                    "code": promotion.get("code"),
                    "mechanic": promotion.get("mechanic"),
                    "amount_minor": int(promotion.get("amount_minor", 0)),
                }
            )
        discount = self.discount_minor
        settlement = self.settlement_minor
        revenue_total = max(self.gross_minor - discount, 0)
        return {
            "cart_id": self.cart_id,
            "venue_id": self.venue_id,
            "currency": self.currency,
            "channel": self.channel,
            "visit_date": self.visit_date,
            "tax_model": self.tax_model,
            "lines": [line.as_dict() for line in self.lines],
            "gross_minor": self.gross_minor,
            "discount_minor": discount,
            # ``total_minor`` is the revenue total (net of discounts, before tax
            # combination which the charge engine finalises). Stored value does not
            # change it; it only reduces ``amount_payable_minor`` (§16, §68).
            "total_minor": revenue_total,
            "settlement_minor": settlement,
            "amount_payable_minor": max(revenue_total - settlement, 0),
            "settlements": [dict(s) for s in self.settlements],
            "gifts": [dict(g) for g in self.gifts],
            "total_quantity": self.total_quantity,
            "applied_promotions": applied,
        }


__all__ = ["CartLine", "CartSnapshot"]
