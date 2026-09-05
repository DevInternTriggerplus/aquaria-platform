"""Promotion engine.

The 22 mechanics required by R13.1 are operator-facing *labels* with sensible
defaults. Underneath, each maps onto one of six **effect kinds**, which are the
only things that actually compute money:

===============  ==========================================================
Effect kind      Computation
===============  ==========================================================
``PERCENT``      percentage of the selected lines' net amount
``FIXED``        flat amount off the selected lines (or the whole cart)
``UNIT_PRICE``   replace the unit price on selected lines
``FREE_UNITS``   make N units free, cheapest-first by default
``PACKAGE_PRICE``set a total for a required combination of ticket types
``TIERED``       quantity bands, each with a percent or fixed effect
===============  ==========================================================

Collapsing 22 names onto 6 computations is deliberate: it means a marketing user
can add "Songkran Family Weekend" as configuration, and there is one place — not
twenty-two — where a rounding or attribution bug could live.

Selection and stacking
----------------------
Applicable promotions are combined only when every member is ``stackable`` and
none excludes another (R13.3). Among valid combinations the engine picks the one
the tenant configured as preferred, defaulting to best-for-customer. Ties are
broken deterministically — fewest promotions, then highest summed priority, then
lexicographic internal code — which resolves ambiguity C.2 of the requirements
analysis rather than leaving it to dictionary ordering.

Caps under concurrency
----------------------
Usage counts and budget consumption are incremented with the same conditional
UPDATE used for capacity, and the ``promotions`` table carries CHECK constraints
on both. A cap therefore cannot be exceeded by concurrent redemption (R13.6).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, parse_date, to_iso, weekday_code
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, RuleViolation, ValidationError
from ..core.i18n import text as i18n_text
from ..core.ids import new_id
from ..core.money import allocate, apply_percentage
from ..domain.cart import CartLine, CartSnapshot
from .authz import AuthorizationService

#: Mechanic → default effect kind (R13.1 labels onto real computations).
MECHANIC_EFFECTS: dict[str, str] = {
    "PERCENT_DISCOUNT": "PERCENT",
    "FIXED_DISCOUNT": "FIXED",
    "SPECIAL_PRICE": "UNIT_PRICE",
    "BUY_X_GET_Y": "FREE_UNITS",
    "FAMILY_PACKAGE": "PACKAGE_PRICE",
    "GROUP_PACKAGE": "PACKAGE_PRICE",
    "CHILD_FREE": "FREE_UNITS",
    "SENIOR_PROMOTION": "PERCENT",
    "MEMBER_PROMOTION": "PERCENT",
    "COUPON_CODE": "PERCENT",
    "VOUCHER": "FIXED",
    "BANK_PROMOTION": "PERCENT",
    "PAYMENT_METHOD_PROMOTION": "PERCENT",
    "PARTNER_PROMOTION": "PERCENT",
    "EARLY_BIRD": "PERCENT",
    "LAST_MINUTE": "PERCENT",
    "WEEKDAY": "PERCENT",
    "WEEKEND": "PERCENT",
    "SEASONAL": "PERCENT",
    "CAMPAIGN": "PERCENT",
    "BUNDLE": "PACKAGE_PRICE",
    "QUANTITY_DISCOUNT": "TIERED",
    # add_features §4/§61 — nth-item discounts ("2nd ticket 50% off", "cheapest
    # item half price"). A distinct effect because the discount targets specific
    # *units*, not whole lines, and must choose those units deterministically.
    "SECOND_ITEM_DISCOUNT": "NTH_ITEM",
    # add_features §11/§12 — a free gift or voucher granted when a condition is met
    # (spend threshold, product/category purchased). The gift is a zero-cost reward
    # attached to the order, not a discount on the paid items, and its stock is
    # tracked so it cannot be over-granted.
    "FREE_GIFT": "FREE_GIFT",
}

#: Which units an NTH_ITEM promotion discounts (add_features §4).
NTH_ITEM_TARGETS: frozenset[str] = frozenset(
    {"SECOND", "THIRD", "NTH", "CHEAPEST", "MOST_EXPENSIVE"}
)

#: How a redeemed value is treated for accounting (add_features §16).
#:
#: * ``DISCOUNT`` — reduces sales revenue (the default, and how every existing
#:   promotion behaves).
#: * ``STORED_VALUE`` / ``PAYMENT`` — a payment instrument (a gift card, or a cash
#:   coupon that was previously *sold* to the customer). It settles part of the
#:   bill and MUST NOT be booked as a sales discount (§16 Example B, §68).
#: * ``LIABILITY`` — draws down a recorded voucher liability; treated like stored
#:   value for the customer, distinguished for the ledger.
#: * ``COMPLIMENTARY`` — a marketing expense rather than a price reduction.
ACCOUNTING_TREATMENTS: frozenset[str] = frozenset(
    {"DISCOUNT", "STORED_VALUE", "PAYMENT", "LIABILITY", "COMPLIMENTARY"}
)

#: Treatments that settle the bill as a payment instrument rather than discounting
#: revenue. Their value goes to ``cart.settlements``, never ``cart_promotions``.
SETTLEMENT_TREATMENTS: frozenset[str] = frozenset({"STORED_VALUE", "PAYMENT", "LIABILITY"})

#: Rule dimensions a promotion may constrain (R13.2).
RULE_KEYS: frozenset[str] = frozenset(
    {
        "date_from",
        "date_to",
        "purchase_from",
        "purchase_to",
        "time_from",
        "time_to",
        "weekdays",
        "channels",
        "venue_ids",
        "product_ids",
        "ticket_type_ids",
        "segment_codes",
        "payment_methods",
        "partner_ids",
        "min_purchase_minor",
        "min_quantity",
        "days_before_visit_min",
        "days_before_visit_max",
        "requires_code",
    }
)

#: Beyond this many applicable promotions the engine stops enumerating every
#: combination and falls back to a priority-ordered greedy pass. Keeps the
#: promotion step's latency bounded on a pathological configuration.
_MAX_COMBINATORIAL_CANDIDATES = 12


@dataclass(slots=True)
class RejectedCode:
    """Why a specific code was refused (R13.5) — never why others exist."""

    code: str
    reason_code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason_code": self.reason_code, "message": self.message}


@dataclass(slots=True)
class PromotionOutcome:
    """Result of evaluating promotions against a cart."""

    cart: CartSnapshot
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[RejectedCode] = field(default_factory=list)
    discount_minor: int = 0
    considered: int = 0
    combination_key: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        revenue_total = max(self.cart.gross_minor - self.discount_minor, 0)
        settlement = self.cart.settlement_minor
        return {
            "applied": self.applied,
            "settlements": [dict(s) for s in self.cart.settlements],
            "rejected": [r.as_dict() for r in self.rejected],
            "discount_minor": self.discount_minor,
            "settlement_minor": settlement,
            "total_minor": revenue_total,
            "amount_payable_minor": max(revenue_total - settlement, 0),
            "considered": self.considered,
            "summary": self.cart.summary(),
        }


class PromotionService:
    """Promotion administration, evaluation and redemption accounting."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config

    # ------------------------------------------------------------------ #
    # Administration
    # ------------------------------------------------------------------ #

    def create_promotion(
        self,
        ctx: RequestContext,
        *,
        internal_code: str,
        name: Any,
        mechanic: str,
        config: dict[str, Any],
        rules: dict[str, Any] | None = None,
        code: str | None = None,
        priority: int = 0,
        stackable: bool = False,
        exclusions: Iterable[str] = (),
        usage_limit: int | None = None,
        per_customer_limit: int | None = None,
        per_code_limit: int | None = None,
        budget_minor: int | None = None,
        restoring: bool = True,
        accounting_treatment: str = "DISCOUNT",
        status: str = "ACTIVE",
    ) -> dict[str, Any]:
        """Create a promotion. ``rules`` keys are validated against R13.2."""
        self.authz.require_page(ctx, "Promotions", "ADD")
        if accounting_treatment not in ACCOUNTING_TREATMENTS:
            raise ValidationError(
                {"accounting_treatment": f"Choose one of: {', '.join(sorted(ACCOUNTING_TREATMENTS))}."},
                message="That accounting treatment is not recognised.",
            )
        if mechanic not in MECHANIC_EFFECTS:
            raise ValidationError(
                {"mechanic": f"Choose one of: {', '.join(sorted(MECHANIC_EFFECTS))}."},
                message="That promotion mechanic is not recognised.",
            )
        rules = dict(rules or {})
        unknown = sorted(set(rules) - RULE_KEYS)
        if unknown:
            raise ValidationError(
                {"rules": f"Unknown rule dimension(s): {', '.join(unknown)}."},
                message="One or more promotion rules are not recognised.",
            )
        self._validate_effect_config(mechanic, config)
        if self.db.query_one(
            "SELECT 1 FROM promotions WHERE tenant_id = ? AND internal_code = ?",
            (ctx.tenant_id, internal_code),
        ):
            raise ConflictError(f"Promotion {internal_code!r} already exists.")
        promotion_id = new_id("pro")
        self.db.insert(
            "promotions",
            {
                "id": promotion_id,
                "tenant_id": ctx.tenant_id,
                "code": code.upper() if code else None,
                "internal_code": internal_code,
                "name_json": name if isinstance(name, dict) else {"en": str(name)},
                "mechanic": mechanic,
                "config_json": config,
                "rules_json": rules,
                "priority": int(priority),
                "stackable": 1 if stackable else 0,
                "exclusions_json": list(exclusions),
                "usage_limit": usage_limit,
                "usage_count": 0,
                "per_customer_limit": per_customer_limit,
                "per_code_limit": per_code_limit,
                "budget_minor": budget_minor,
                "budget_used_minor": 0,
                "restoring": 1 if restoring else 0,
                "accounting_treatment": accounting_treatment,
                "status": status,
                "created_at": to_iso(self.clock.now()),
            },
        )
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="promotion",
            target_id=promotion_id,
            new={
                "internal_code": internal_code,
                "mechanic": mechanic,
                "priority": priority,
                "stackable": stackable,
                "has_public_code": bool(code),
                "usage_limit": usage_limit,
                "budget_minor": budget_minor,
                "accounting_treatment": accounting_treatment,
            },
        )
        return self.get_promotion(ctx, promotion_id)

    def _validate_effect_config(self, mechanic: str, config: dict[str, Any]) -> None:
        """Catch a misconfigured promotion at save time, not at checkout."""
        effect = MECHANIC_EFFECTS[mechanic]
        problems: dict[str, str] = {}
        if effect == "PERCENT" and not config.get("percent_bp"):
            problems["percent_bp"] = "Enter the discount percentage in basis points, e.g. 1000 for 10%."
        if effect == "FIXED" and not config.get("amount_minor"):
            problems["amount_minor"] = "Enter the discount amount."
        if effect == "UNIT_PRICE" and config.get("unit_price_minor") is None:
            problems["unit_price_minor"] = "Enter the special unit price."
        if effect == "FREE_UNITS":
            if not config.get("free_quantity") and not config.get("get_quantity"):
                problems["free_quantity"] = "Enter how many units become free."
        if effect == "PACKAGE_PRICE":
            if config.get("package_price_minor") is None:
                problems["package_price_minor"] = "Enter the package price."
            if not config.get("requires"):
                problems["requires"] = "List the ticket types and quantities the package requires."
        if effect == "TIERED" and not config.get("tiers"):
            problems["tiers"] = "Add at least one quantity tier."
        if effect == "NTH_ITEM":
            target = config.get("target", "SECOND")
            if target not in NTH_ITEM_TARGETS:
                problems["target"] = f"Choose one of: {', '.join(sorted(NTH_ITEM_TARGETS))}."
            if target == "NTH" and int(config.get("nth") or 0) < 2:
                problems["nth"] = "Enter which item (2 or more) gets the discount."
            if not config.get("percent_bp") and not config.get("amount_minor"):
                problems["percent_bp"] = "Enter the discount as a percentage or a fixed amount."
        if effect == "FREE_GIFT":
            reward = config.get("reward") or {}
            if not reward.get("name") and not reward.get("sku"):
                problems["reward"] = "Describe the free gift (a name or product/SKU reference)."
            kind = reward.get("kind", "PRODUCT")
            if kind not in ("PRODUCT", "ADDON", "VOUCHER", "TICKET", "SERVICE", "ACTIVITY"):
                problems["reward.kind"] = "Choose a valid reward kind."
        if problems:
            raise ValidationError(problems, message="This promotion's settings are incomplete.")

    def get_promotion(self, ctx: RequestContext, promotion_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "promotions", promotion_id, entity="promotion")
        record["name"] = decode(record.pop("name_json"), {})
        record["config"] = decode(record.pop("config_json"), {})
        record["rules"] = decode(record.pop("rules_json"), {})
        record["exclusions"] = decode(record.pop("exclusions_json"), [])
        record["effect"] = MECHANIC_EFFECTS.get(record["mechanic"], "PERCENT")
        record["redemption_count"] = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM promotion_redemptions WHERE tenant_id = ? AND promotion_id = ? "
                "AND state = 'APPLIED'",
                (ctx.tenant_id, promotion_id),
                default=0,
            )
        )
        return record

    def list_promotions(self, ctx: RequestContext, *, status: str | None = None) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Promotions", "VIEW")
        sql = "SELECT id FROM promotions WHERE tenant_id = ?"
        params: list[Any] = [ctx.tenant_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY priority DESC, internal_code"
        return [self.get_promotion(ctx, row["id"]) for row in self.db.query(sql, params)]

    def set_status(
        self, ctx: RequestContext, promotion_id: str, status: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Pause, resume or end a promotion early (R13.10)."""
        if status not in ("ACTIVE", "PAUSED", "ENDED", "ARCHIVED"):
            raise ValidationError({"status": "Status must be ACTIVE, PAUSED, ENDED or ARCHIVED."})
        self.authz.require_page(ctx, "Promotions", "EDIT", target_type="promotion", target_id=promotion_id)
        before = self.get_promotion(ctx, promotion_id)
        self.db.update("promotions", promotion_id, {"status": status}, tenant_id=ctx.tenant_id)
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="promotion",
            target_id=promotion_id,
            previous={"status": before["status"]},
            new={"status": status},
            reason=reason,
        )
        return self.get_promotion(ctx, promotion_id)

    def clone_promotion(
        self, ctx: RequestContext, promotion_id: str, *, internal_code: str, code: str | None = None
    ) -> dict[str, Any]:
        source = self.get_promotion(ctx, promotion_id)
        return self.create_promotion(
            ctx,
            internal_code=internal_code,
            name=source["name"],
            mechanic=source["mechanic"],
            config=source["config"],
            rules=source["rules"],
            code=code,
            priority=int(source["priority"]),
            stackable=bool(source["stackable"]),
            exclusions=source["exclusions"],
            usage_limit=source["usage_limit"],
            per_customer_limit=source["per_customer_limit"],
            per_code_limit=source["per_code_limit"],
            budget_minor=source["budget_minor"],
            restoring=bool(source["restoring"]),
            accounting_treatment=source.get("accounting_treatment", "DISCOUNT"),
            status="PAUSED",
        )

    def delete_promotion(
        self, ctx: RequestContext, promotion_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """DELETE maps to archive once a promotion has any redemption (R13.10)."""
        self.authz.require_page(ctx, "Promotions", "DELETE", target_type="promotion", target_id=promotion_id)
        promotion = self.get_promotion(ctx, promotion_id)
        if promotion["redemption_count"]:
            self.db.update("promotions", promotion_id, {"status": "ARCHIVED"}, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx,
                "CONFIG_CHANGE",
                target_type="promotion",
                target_id=promotion_id,
                previous={"status": promotion["status"]},
                new={"status": "ARCHIVED", "performed": "ARCHIVE"},
                reason=reason,
            )
            return {
                "requested": "DELETE",
                "performed": "ARCHIVE",
                "reason": "This promotion has redemptions, which are retained for reconciliation.",
                "redemption_count": promotion["redemption_count"],
            }
        self.db.execute("DELETE FROM promotions WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, promotion_id))
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="promotion",
            target_id=promotion_id,
            previous={"internal_code": promotion["internal_code"]},
            new={"performed": "DELETE"},
            reason=reason,
        )
        return {"requested": "DELETE", "performed": "DELETE", "promotion_id": promotion_id}

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        ctx: RequestContext,
        cart: CartSnapshot,
        *,
        codes: Sequence[str] = (),
        preview: bool = False,
    ) -> PromotionOutcome:
        """Apply the best valid promotion combination to a copy of ``cart``.

        Never mutates the caller's cart; the outcome carries the priced clone so a
        rejected re-confirmation can fall back to the original.
        """
        working = cart.clone()
        working.reset_promotions()
        working.promotion_codes = [c.strip().upper() for c in codes if c and c.strip()]

        automatic = self._candidate_promotions(ctx, working, code_required=False)
        coded, rejected = self._resolve_codes(ctx, working, working.promotion_codes)
        candidates = automatic + coded
        applicable = [c for c in candidates if self._applies(ctx, c, working)]

        # Settlement-treatment coupons (gift cards / sold cash coupons) are the
        # customer's own money, not a discount to be optimised. They are applied
        # separately and never compete in the discount-maximising combination
        # (add_features §16, §68), so a discount promotion is never dropped in
        # favour of the customer spending their own stored value, or vice versa.
        # Free gifts are zero-cost rewards, not discounts, so — like settlements —
        # they must not compete in the discount-maximising combination or they would
        # be dropped for scoring 0 (add_features §11). They are applied separately.
        gift_candidates = [c for c in applicable if MECHANIC_EFFECTS[c["mechanic"]] == "FREE_GIFT"]
        non_gift = [c for c in applicable if MECHANIC_EFFECTS[c["mechanic"]] != "FREE_GIFT"]
        discount_candidates = [
            c for c in non_gift if c.get("accounting_treatment", "DISCOUNT") not in SETTLEMENT_TREATMENTS
        ]
        settlement_candidates = [
            c for c in non_gift if c.get("accounting_treatment", "DISCOUNT") in SETTLEMENT_TREATMENTS
        ]

        combination = self._choose_combination(ctx, working, discount_candidates)
        applied: list[dict[str, Any]] = []
        for promotion in combination:
            amount = self._apply_effect(working, promotion)
            if amount <= 0:
                continue
            applied.append(
                {
                    "promotion_id": promotion["id"],
                    "internal_code": promotion["internal_code"],
                    "code": promotion.get("code"),
                    "name": i18n_text(promotion["name"], working.language, fallback=promotion["internal_code"]),
                    "mechanic": promotion["mechanic"],
                    "amount_minor": amount,
                    "priority": int(promotion["priority"]),
                    "sequence": len(applied),
                }
            )
        working.push_cart_discount_to_lines()
        # Apply settlements after discounts, highest priority first, so the payable
        # amount they draw down is the post-discount revenue total.
        for promotion in sorted(settlement_candidates, key=lambda p: (-int(p["priority"]), p["internal_code"])):
            self._apply_effect(working, promotion)
        # Grant eligible free gifts, highest priority first. Stock is enforced at
        # commit time via the usage cap, and depleted rewards are already excluded
        # here because an exhausted promotion is not "applicable" (§12).
        gift_applied: list[dict[str, Any]] = []
        for promotion in sorted(gift_candidates, key=lambda p: (-int(p["priority"]), p["internal_code"])):
            self._apply_effect(working, promotion)
            gift_applied.append(
                {
                    "promotion_id": promotion["id"],
                    "internal_code": promotion["internal_code"],
                    "name": i18n_text(promotion["name"], working.language, fallback=promotion["internal_code"]),
                    "mechanic": promotion["mechanic"],
                    "amount_minor": 0,
                    "priority": int(promotion["priority"]),
                    "sequence": len(applied) + len(gift_applied),
                }
            )
        applied.extend(gift_applied)
        discount = working.line_discount_minor
        return PromotionOutcome(
            cart=working,
            applied=applied,
            rejected=rejected,
            discount_minor=discount,
            considered=len(applicable),
            combination_key=tuple(sorted(p["internal_code"] for p in combination)),
        )

    def preview(
        self, ctx: RequestContext, cart: CartSnapshot, *, codes: Sequence[str] = ()
    ) -> dict[str, Any]:
        """Test a promotion against a sample cart without recording redemption (R13.11)."""
        self.authz.require_page(ctx, "Promotions", "VIEW")
        outcome = self.evaluate(ctx, cart, codes=codes, preview=True)
        return {"preview": True, "recorded": False, **outcome.as_dict()}

    def _candidate_promotions(
        self, ctx: RequestContext, cart: CartSnapshot, *, code_required: bool
    ) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id FROM promotions WHERE tenant_id = ? AND status = 'ACTIVE' "
            "AND (code IS NULL OR code = '') ORDER BY priority DESC, internal_code",
            (ctx.tenant_id,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            promotion = self.get_promotion(ctx, row["id"])
            if promotion["rules"].get("requires_code"):
                continue
            out.append(promotion)
        return out

    def _resolve_codes(
        self, ctx: RequestContext, cart: CartSnapshot, codes: Sequence[str]
    ) -> tuple[list[dict[str, Any]], list[RejectedCode]]:
        """Look up entered codes, rejecting each with a specific reason (R13.5).

        The rejection never mentions any other promotion, so a customer cannot probe
        the promotion catalogue by entering guesses.
        """
        accepted: list[dict[str, Any]] = []
        rejected: list[RejectedCode] = []
        for code in codes:
            row = self.db.query_one(
                "SELECT id FROM promotions WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
            )
            if row is None:
                rejected.append(RejectedCode(code, "unknown", "That code is not recognised."))
                continue
            promotion = self.get_promotion(ctx, row["id"])
            reason = self._code_rejection_reason(ctx, promotion, cart)
            if reason is not None:
                rejected.append(reason)
                continue
            accepted.append(promotion)
        return accepted, rejected

    def _code_rejection_reason(
        self, ctx: RequestContext, promotion: dict[str, Any], cart: CartSnapshot
    ) -> RejectedCode | None:
        code = str(promotion.get("code") or "")
        status = promotion["status"]
        if status == "PAUSED":
            return RejectedCode(code, "paused", "That code is not active at the moment.")
        if status in ("ENDED", "ARCHIVED"):
            return RejectedCode(code, "ended", "That code has expired.")
        rules = promotion["rules"]
        today = to_iso(self.clock.now())[:10]
        if rules.get("purchase_from") and today < str(rules["purchase_from"]):
            return RejectedCode(code, "not_yet_active", "That code is not valid yet.")
        if rules.get("purchase_to") and today > str(rules["purchase_to"]):
            return RejectedCode(code, "expired", "That code has expired.")
        if self._is_exhausted(ctx, promotion):
            return RejectedCode(code, "exhausted", "That code has reached its usage limit.")
        if cart.customer_key and promotion["per_customer_limit"] is not None:
            used = int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM promotion_redemptions WHERE tenant_id = ? AND promotion_id = ? "
                    "AND customer_key = ? AND state = 'APPLIED'",
                    (ctx.tenant_id, promotion["id"], cart.customer_key),
                    default=0,
                )
            )
            if used >= int(promotion["per_customer_limit"]):
                return RejectedCode(code, "customer_limit", "You have already used that code.")
        channels = promotion["rules"].get("channels")
        if channels and cart.channel not in channels:
            return RejectedCode(code, "channel_restricted", "That code cannot be used here.")
        if not self._applies(ctx, promotion, cart):
            return RejectedCode(
                code, "not_applicable", "That code does not apply to the items in your order."
            )
        return None

    def _is_exhausted(self, ctx: RequestContext, promotion: dict[str, Any]) -> bool:
        if promotion["usage_limit"] is not None and int(promotion["usage_count"]) >= int(
            promotion["usage_limit"]
        ):
            return True
        if promotion["budget_minor"] is not None and int(promotion["budget_used_minor"]) >= int(
            promotion["budget_minor"]
        ):
            return True
        return False

    def _applies(self, ctx: RequestContext, promotion: dict[str, Any], cart: CartSnapshot) -> bool:
        """Do all the promotion's rule dimensions hold for this cart? (R13.2)"""
        if promotion["status"] != "ACTIVE" or self._is_exhausted(ctx, promotion):
            return False
        rules = promotion["rules"]
        now = self.clock.now()
        today = to_iso(now)[:10]
        clock_time = to_iso(now)[11:16]

        if rules.get("purchase_from") and today < str(rules["purchase_from"]):
            return False
        if rules.get("purchase_to") and today > str(rules["purchase_to"]):
            return False
        if cart.visit_date:
            if rules.get("date_from") and cart.visit_date < str(rules["date_from"]):
                return False
            if rules.get("date_to") and cart.visit_date > str(rules["date_to"]):
                return False
            weekdays = rules.get("weekdays")
            if weekdays and weekday_code(cart.visit_date) not in weekdays:
                return False
            lo = rules.get("days_before_visit_min")
            hi = rules.get("days_before_visit_max")
            if lo is not None or hi is not None:
                days_ahead = (parse_date(cart.visit_date) - parse_date(today)).days
                if lo is not None and days_ahead < int(lo):
                    return False
                if hi is not None and days_ahead > int(hi):
                    return False
        if rules.get("time_from") and clock_time < str(rules["time_from"]):
            return False
        if rules.get("time_to") and clock_time > str(rules["time_to"]):
            return False
        if rules.get("channels") and cart.channel not in rules["channels"]:
            return False
        if rules.get("venue_ids") and cart.venue_id not in rules["venue_ids"]:
            return False
        if rules.get("payment_methods"):
            if not cart.payment_method or cart.payment_method not in rules["payment_methods"]:
                return False
        if rules.get("partner_ids"):
            if not cart.partner_id or cart.partner_id not in rules["partner_ids"]:
                return False
        if rules.get("min_purchase_minor") and cart.gross_minor < int(rules["min_purchase_minor"]):
            return False
        if rules.get("min_quantity") and cart.total_quantity < int(rules["min_quantity"]):
            return False
        selected = self._select_lines(promotion, cart)
        if not selected:
            return False
        effect = MECHANIC_EFFECTS[promotion["mechanic"]]
        if effect == "PACKAGE_PRICE":
            return self._package_satisfied(promotion, cart) is not None
        if effect == "NTH_ITEM":
            # There must be enough qualifying units for at least one to be discounted:
            # a "second item" offer needs at least two units in the selected lines.
            units = sum(int(line.quantity) for line in selected)
            required = self._nth_required_units(promotion["config"])
            return units >= required
        return True

    def _select_lines(self, promotion: dict[str, Any], cart: CartSnapshot) -> list[CartLine]:
        """Lines a promotion targets. No targeting rules means the whole cart."""
        rules = promotion["rules"]
        products = rules.get("product_ids")
        ticket_types = rules.get("ticket_type_ids")
        segments = rules.get("segment_codes")
        out: list[CartLine] = []
        for line in cart.lines:
            if line.is_complimentary or line.unit_price_minor <= 0:
                continue
            if products and line.product_id not in products:
                continue
            if ticket_types and line.ticket_type_id not in ticket_types:
                continue
            if segments and line.segment_code not in segments:
                continue
            out.append(line)
        return out

    # ------------------------------------------------------------------ #
    # Combination selection (R13.3)
    # ------------------------------------------------------------------ #

    def _choose_combination(
        self, ctx: RequestContext, cart: CartSnapshot, applicable: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not applicable:
            return []
        preference = self.config.get(
            ctx, "promotion.resolution_preference", venue_id=cart.venue_id
        ) or "BEST_FOR_CUSTOMER"
        max_stacked = max(1, self.config.get_int(ctx, "promotion.max_stacked", venue_id=cart.venue_id))

        if len(applicable) > _MAX_COMBINATORIAL_CANDIDATES:
            return self._greedy_combination(applicable, max_stacked)

        valid: list[list[dict[str, Any]]] = []
        for size in range(1, min(max_stacked, len(applicable)) + 1):
            for combo in itertools.combinations(applicable, size):
                if self._combination_allowed(combo):
                    valid.append(list(combo))
        if not valid:
            return []

        # Score every valid combination on a fully-ordered tuple, then take the
        # minimum. Because the last element of the tuple is the sorted internal-code
        # sequence — which is unique per combination — no two candidates can ever
        # compare equal. That is what makes the outcome deterministic rather than
        # dependent on iteration order (analysis C.2).
        scored: list[tuple[tuple[int, int, int, tuple[str, ...]], list[dict[str, Any]]]] = []
        for combo in valid:
            trial = cart.clone()
            trial.reset_promotions()
            total = 0
            for promotion in self._application_order(combo):
                total += self._apply_effect(trial, promotion)
            priority_sum = sum(int(p["priority"]) for p in combo)
            key = tuple(sorted(p["internal_code"] for p in combo))
            if preference == "HIGHEST_PRIORITY":
                rank = (-priority_sum, -total, len(combo), key)
            else:
                # Best for customer: largest discount wins; then fewest promotions
                # (simplest explanation on the receipt); then highest priority.
                rank = (-total, len(combo), -priority_sum, key)
            scored.append((rank, combo))

        scored.sort(key=lambda entry: entry[0])
        return self._application_order(scored[0][1])

    @staticmethod
    def _application_order(combo: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Promotions are applied in priority order, ties by code (R13.3, R13.9)."""
        return sorted(combo, key=lambda p: (-int(p["priority"]), p["internal_code"]))

    def _greedy_combination(
        self, applicable: list[dict[str, Any]], max_stacked: int
    ) -> list[dict[str, Any]]:
        chosen: list[dict[str, Any]] = []
        for promotion in sorted(applicable, key=lambda p: (-int(p["priority"]), p["internal_code"])):
            if len(chosen) >= max_stacked:
                break
            if self._combination_allowed([*chosen, promotion]):
                chosen.append(promotion)
        return chosen

    def _combination_allowed(self, combo: Sequence[dict[str, Any]]) -> bool:
        """Stackable-only, and no member may exclude another (R13.3)."""
        if len(combo) == 1:
            return True
        if any(not bool(p["stackable"]) for p in combo):
            return False
        codes = {p["internal_code"] for p in combo}
        for promotion in combo:
            excluded = set(promotion.get("exclusions") or [])
            if excluded & (codes - {promotion["internal_code"]}):
                return False
        return True

    # ------------------------------------------------------------------ #
    # Effects
    # ------------------------------------------------------------------ #

    def _apply_effect(self, cart: CartSnapshot, promotion: dict[str, Any]) -> int:
        effect = MECHANIC_EFFECTS[promotion["mechanic"]]
        config = promotion["config"]
        treatment = promotion.get("accounting_treatment", "DISCOUNT")
        stamp = {
            "promotion_id": promotion["id"],
            "internal_code": promotion["internal_code"],
            "code": promotion.get("code"),
            "name": i18n_text(promotion["name"], cart.language, fallback=promotion["internal_code"]),
            "mechanic": promotion["mechanic"],
            "effect": effect,
            "accounting_treatment": treatment,
        }
        lines = self._select_lines(promotion, cart)
        if not lines:
            return 0

        # A cash coupon / gift card configured as a payment instrument settles part
        # of the bill instead of discounting revenue (add_features §16, §68). Its
        # value is recorded on ``cart.settlements`` and returns 0 here so it never
        # counts toward ``discount_minor`` — VAT and revenue stay on the full price.
        if treatment in SETTLEMENT_TREATMENTS:
            face_value = int(config.get("amount_minor", 0))
            if face_value <= 0:
                return 0
            # Never settle more than the revenue total owed on this cart.
            already_settled = cart.settlement_minor
            payable = max(cart.gross_minor - cart.discount_minor - already_settled, 0)
            applied = min(face_value, payable)
            if applied <= 0:
                return 0
            cart.settlements.append({**stamp, "amount_minor": applied, "face_value_minor": face_value})
            return 0

        if effect == "PERCENT":
            percent = int(config.get("percent_bp", 0))
            total = 0
            cap = config.get("max_discount_minor")
            for line in lines:
                amount = apply_percentage(line.net_minor, percent)
                if amount <= 0:
                    continue
                line.promotions.append({**stamp, "amount_minor": amount})
                total += amount
            if cap is not None and total > int(cap):
                total = self._trim_to_cap(lines, promotion["id"], total, int(cap))
            return total

        if effect == "FIXED":
            amount = min(int(config.get("amount_minor", 0)), sum(line.net_minor for line in lines))
            if amount <= 0:
                return 0
            cart.cart_promotions.append({**stamp, "amount_minor": amount})
            return amount

        if effect == "UNIT_PRICE":
            special = int(config.get("unit_price_minor", 0))
            total = 0
            for line in lines:
                if line.unit_price_minor <= special:
                    continue
                per_unit = line.unit_price_minor - special
                limit = config.get("max_units")
                units = min(int(line.quantity), int(limit)) if limit is not None else int(line.quantity)
                amount = per_unit * units
                amount = min(amount, line.net_minor)
                if amount <= 0:
                    continue
                line.promotions.append({**stamp, "amount_minor": amount, "units": units})
                total += amount
            return total

        if effect == "FREE_UNITS":
            return self._apply_free_units(cart, promotion, lines, stamp)

        if effect == "PACKAGE_PRICE":
            return self._apply_package(cart, promotion, stamp)

        if effect == "TIERED":
            quantity = sum(int(line.quantity) for line in lines)
            best: dict[str, Any] | None = None
            for tier in config.get("tiers", []):
                if quantity >= int(tier.get("min_quantity", 0)):
                    if best is None or int(tier["min_quantity"]) > int(best["min_quantity"]):
                        best = tier
            if best is None:
                return 0
            total = 0
            if best.get("percent_bp"):
                for line in lines:
                    amount = apply_percentage(line.net_minor, int(best["percent_bp"]))
                    if amount <= 0:
                        continue
                    line.promotions.append({**stamp, "amount_minor": amount, "tier": best["min_quantity"]})
                    total += amount
            elif best.get("amount_minor"):
                amount = min(int(best["amount_minor"]), sum(line.net_minor for line in lines))
                if amount > 0:
                    cart.cart_promotions.append(
                        {**stamp, "amount_minor": amount, "tier": best["min_quantity"]}
                    )
                    total = amount
            return total

        if effect == "NTH_ITEM":
            return self._apply_nth_item(cart, promotion, lines, stamp)

        if effect == "FREE_GIFT":
            reward = dict(config.get("reward") or {})
            quantity = int(config.get("reward_quantity") or 1)
            cart.gifts.append(
                {
                    **stamp,
                    "reward": reward,
                    "quantity": quantity,
                    "amount_minor": 0,  # a gift is free to the customer (§11)
                }
            )
            return 0

        return 0

    @staticmethod
    def _nth_required_units(config: dict[str, Any]) -> int:
        """Minimum qualifying units before an nth-item discount can apply."""
        target = config.get("target", "SECOND")
        if target == "SECOND":
            return 2
        if target == "THIRD":
            return 3
        if target == "NTH":
            return max(int(config.get("nth") or 2), 2)
        # CHEAPEST / MOST_EXPENSIVE need at least two so there is a "cheaper" one.
        return 2

    def _apply_nth_item(
        self,
        cart: CartSnapshot,
        promotion: dict[str, Any],
        lines: list[CartLine],
        stamp: dict[str, Any],
    ) -> int:
        """Discount specific *units* — the 2nd/3rd/nth, or the cheapest/priciest.

        The selected lines are expanded into individual units so the discount can
        land on a precise unit regardless of how the customer grouped quantities.
        Units are ordered by price (ascending) with the line index as a stable
        tie-break, which makes "the second ticket" and "the cheapest ticket"
        deterministic (add_features §4, §61).
        """
        config = promotion["config"]
        target = config.get("target", "SECOND")
        percent = int(config.get("percent_bp") or 0)
        fixed = int(config.get("amount_minor") or 0)
        # How many units receive the discount (default 1). "count" repeats the offer:
        # e.g. count=2 with target SECOND discounts the 2nd and 4th unit across pairs.
        count = max(int(config.get("count") or 1), 1)

        # (unit_price, line, sequence-within-line) for every payable unit.
        units: list[tuple[int, CartLine, int]] = []
        for line in lines:
            for _ in range(int(line.quantity)):
                units.append((int(line.unit_price_minor), line, len(units)))
        if len(units) < self._nth_required_units(config):
            return 0

        by_price = sorted(units, key=lambda u: (u[0], u[2]))  # cheapest first, stable
        chosen: list[tuple[int, CartLine, int]] = []
        if target == "CHEAPEST":
            chosen = by_price[:count]
        elif target == "MOST_EXPENSIVE":
            chosen = list(reversed(by_price))[:count]
        else:
            # SECOND / THIRD / NTH: the customer-friendly reading discounts the
            # cheaper qualifying units, taken in bundles. For "second item", each
            # bundle of 2 units yields one discounted (the cheaper of the pair).
            nth = 2 if target == "SECOND" else 3 if target == "THIRD" else int(config.get("nth") or 2)
            bundles = len(units) // nth
            take = min(count * bundles, bundles) if config.get("count") else bundles
            chosen = by_price[: max(take, 0)]

        total = 0
        for unit_price, line, _seq in chosen:
            amount = apply_percentage(unit_price, percent) if percent else min(fixed, unit_price)
            if amount <= 0:
                continue
            # Guard against over-discounting the line beyond its net value.
            already = sum(int(p.get("amount_minor", 0)) for p in line.promotions)
            headroom = max(line.gross_minor - already, 0)
            amount = min(amount, headroom)
            if amount <= 0:
                continue
            line.promotions.append({**stamp, "amount_minor": amount, "nth_target": target})
            total += amount
        return total

    def _trim_to_cap(
        self, lines: list[CartLine], promotion_id: str, total: int, cap: int
    ) -> int:
        """Reduce a per-line percentage discount so the configured cap is respected."""
        excess = total - cap
        for line in reversed(lines):
            if excess <= 0:
                break
            for stamp in reversed(line.promotions):
                if stamp.get("promotion_id") != promotion_id:
                    continue
                reduce_by = min(excess, int(stamp["amount_minor"]))
                stamp["amount_minor"] = int(stamp["amount_minor"]) - reduce_by
                excess -= reduce_by
                break
        for line in lines:
            line.promotions = [p for p in line.promotions if int(p.get("amount_minor", 0)) > 0]
        return cap

    def _apply_free_units(
        self,
        cart: CartSnapshot,
        promotion: dict[str, Any],
        lines: list[CartLine],
        stamp: dict[str, Any],
    ) -> int:
        """Buy X Get Y and Child Free.

        Both reduce to "make N units free", where N depends on qualifying paid
        quantity. Free units are taken cheapest-first by default, which is the
        conservative reading for the venue; ``free_from='HIGHEST'`` flips it when a
        tenant wants the customer-friendly interpretation.
        """
        config = promotion["config"]
        buy_qty = int(config.get("buy_quantity", 0))
        get_qty = int(config.get("get_quantity", config.get("free_quantity", 0)))
        qualify_segments = config.get("qualifying_segment_codes")
        free_segments = config.get("free_segment_codes")

        if qualify_segments:
            qualifying = sum(
                int(line.quantity) for line in cart.lines if line.segment_code in qualify_segments
            )
        else:
            qualifying = sum(int(line.quantity) for line in lines)

        target_lines = (
            [line for line in cart.lines if line.segment_code in free_segments and line.unit_price_minor > 0]
            if free_segments
            else lines
        )
        if not target_lines:
            return 0

        if buy_qty > 0:
            bundles = qualifying // buy_qty
            free_units = bundles * max(get_qty, 1)
        else:
            free_units = min(get_qty, qualifying) if qualify_segments else get_qty
        if free_units <= 0:
            return 0

        order = sorted(
            target_lines,
            key=lambda line: line.unit_price_minor,
            reverse=config.get("free_from", "LOWEST") == "HIGHEST",
        )
        total = 0
        for line in order:
            if free_units <= 0:
                break
            units = min(free_units, int(line.quantity))
            amount = min(line.unit_price_minor * units, line.net_minor)
            if amount <= 0:
                continue
            line.promotions.append({**stamp, "amount_minor": amount, "free_units": units})
            total += amount
            free_units -= units
        return total

    def _package_satisfied(
        self, promotion: dict[str, Any], cart: CartSnapshot
    ) -> list[tuple[CartLine, int]] | None:
        """Does the cart contain the package's required combination?"""
        requirements = promotion["config"].get("requires") or []
        picked: list[tuple[CartLine, int]] = []
        for requirement in requirements:
            needed = int(requirement.get("quantity", 1))
            matches = [
                line
                for line in cart.lines
                if (
                    requirement.get("ticket_type_id") in (None, line.ticket_type_id)
                    and requirement.get("segment_code") in (None, line.segment_code)
                    and line.unit_price_minor > 0
                )
            ]
            available = sum(int(line.quantity) for line in matches)
            if available < needed:
                return None
            remaining = needed
            for line in sorted(matches, key=lambda candidate: -candidate.unit_price_minor):
                if remaining <= 0:
                    break
                take = min(remaining, int(line.quantity))
                picked.append((line, take))
                remaining -= take
        return picked or None

    def _apply_package(
        self, cart: CartSnapshot, promotion: dict[str, Any], stamp: dict[str, Any]
    ) -> int:
        picked = self._package_satisfied(promotion, cart)
        if picked is None:
            return 0
        package_price = int(promotion["config"].get("package_price_minor", 0))
        component_value = sum(line.unit_price_minor * units for line, units in picked)
        discount = component_value - package_price
        if discount <= 0:
            return 0
        # Attribute the package saving across its own components only, so a partial
        # refund of one component is provably correct.
        weights = [line.unit_price_minor * units for line, units in picked]
        for (line, units), share in zip(picked, allocate(discount, weights)):
            if share <= 0:
                continue
            line.promotions.append({**stamp, "amount_minor": share, "package_units": units})
        return discount

    # ------------------------------------------------------------------ #
    # Redemption accounting (R13.6, R13.8, R13.9)
    # ------------------------------------------------------------------ #

    def commit_redemptions(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        outcome: PromotionOutcome,
    ) -> list[dict[str, Any]]:
        """Record redemptions and consume caps atomically.

        Returns the promotions that could not be committed because a cap was reached
        between evaluation and confirmation; the booking service treats a non-empty
        result as "recalculate and re-confirm" (R13.7).
        """
        failures: list[dict[str, Any]] = []
        cart = outcome.cart
        with self.db.transaction(immediate=True):
            for entry in outcome.applied:
                promotion_id = entry["promotion_id"]
                amount = int(entry["amount_minor"])
                promotion = self.db.query_one(
                    "SELECT * FROM promotions WHERE id = ? AND tenant_id = ?",
                    (promotion_id, ctx.tenant_id),
                )
                if promotion is None or promotion["status"] != "ACTIVE":
                    failures.append({**entry, "reason_code": "inactive"})
                    continue
                granted = self.db.compare_and_increment(
                    "promotions",
                    promotion_id,
                    counter="usage_count",
                    delta=1,
                    limit_column="usage_limit",
                    tenant_id=ctx.tenant_id,
                )
                if not granted:
                    failures.append({**entry, "reason_code": "usage_limit"})
                    continue
                if amount > 0:
                    budget_ok = self.db.compare_and_increment(
                        "promotions",
                        promotion_id,
                        counter="budget_used_minor",
                        delta=amount,
                        limit_column="budget_minor",
                        tenant_id=ctx.tenant_id,
                    )
                    if not budget_ok:
                        # Roll the usage increment back so a rejected redemption does
                        # not silently consume a slot.
                        self.db.compare_and_increment(
                            "promotions",
                            promotion_id,
                            counter="usage_count",
                            delta=-1,
                            tenant_id=ctx.tenant_id,
                        )
                        failures.append({**entry, "reason_code": "budget_cap"})
                        continue
                for line in cart.lines:
                    for stamped in line.promotions:
                        if stamped.get("promotion_id") != promotion_id:
                            continue
                        self.db.insert(
                            "promotion_redemptions",
                            {
                                "id": new_id("prr"),
                                "tenant_id": ctx.tenant_id,
                                "promotion_id": promotion_id,
                                "booking_id": booking_id,
                                "booking_item_id": None,
                                "customer_key": cart.customer_key,
                                "amount_minor": int(stamped.get("amount_minor", 0)),
                                "sequence": int(entry.get("sequence", 0)),
                                "state": "APPLIED",
                                "created_at": to_iso(self.clock.now()),
                            },
                        )
        return failures

    def commit_from_booking(self, ctx: RequestContext, *, booking_id: str) -> list[dict[str, Any]]:
        """Commit redemptions from persisted booking items rather than a live cart.

        The gateway-driven completion path (R14.6) has no cart in memory: the customer's
        browser is gone and a webhook is finishing the booking. Because every applied
        promotion was frozen onto ``booking_items.promotions_json`` at reservation time
        (R13.9), redemption can be reconstructed exactly.

        Idempotent: a redemption row already recorded for this booking is not counted
        twice, so a duplicate webhook cannot consume a promotion's budget twice.
        """
        already = {
            row["promotion_id"]
            for row in self.db.query(
                "SELECT DISTINCT promotion_id FROM promotion_redemptions "
                "WHERE tenant_id = ? AND booking_id = ? AND state = 'APPLIED'",
                (ctx.tenant_id, booking_id),
            )
        }
        items = self.db.query(
            "SELECT id, promotions_json FROM booking_items WHERE tenant_id = ? AND booking_id = ?",
            (ctx.tenant_id, booking_id),
        )
        # promotion_id -> [(booking_item_id, amount_minor, sequence)]
        grouped: dict[str, list[tuple[str, int, int]]] = {}
        for item in items:
            for stamp in decode(item["promotions_json"], []) or []:
                promotion_id = stamp.get("promotion_id")
                if not promotion_id or promotion_id in already:
                    continue
                grouped.setdefault(promotion_id, []).append(
                    (item["id"], int(stamp.get("amount_minor", 0)), int(stamp.get("sequence", 0)))
                )
        # Stored-value / gift-card settlements are promotions too, but their value
        # lives on the booking row (settlements_json), not on line items. Recording
        # a redemption row here is what makes a gift card single-use: its usage_limit
        # is consumed exactly once, so a second booking cannot spend the same card
        # (add_features §16, §73/§74 usage limits apply to coupons as well).
        booking = self.db.query_one(
            "SELECT settlements_json, gifts_json FROM bookings WHERE id = ? AND tenant_id = ?",
            (booking_id, ctx.tenant_id),
        )
        if booking is not None:
            for stamp in decode(booking["settlements_json"], []) or []:
                promotion_id = stamp.get("promotion_id")
                if not promotion_id or promotion_id in already:
                    continue
                grouped.setdefault(promotion_id, []).append(
                    (None, int(stamp.get("amount_minor", 0)), 0)
                )
            # Free gifts consume the reward promotion's usage cap so stock cannot be
            # over-granted (add_features §12). The value is 0 (the gift is free), so
            # only the usage counter moves, not the budget.
            for stamp in decode(booking["gifts_json"], []) or []:
                promotion_id = stamp.get("promotion_id")
                if not promotion_id or promotion_id in already:
                    continue
                grouped.setdefault(promotion_id, []).append((None, 0, 0))
        failures: list[dict[str, Any]] = []
        if not grouped:
            return failures
        now = to_iso(self.clock.now())
        with self.db.transaction(immediate=True):
            for promotion_id, stamps in grouped.items():
                promotion = self.db.query_one(
                    "SELECT * FROM promotions WHERE id = ? AND tenant_id = ?",
                    (promotion_id, ctx.tenant_id),
                )
                if promotion is None or promotion["status"] != "ACTIVE":
                    failures.append({"promotion_id": promotion_id, "reason_code": "inactive"})
                    continue
                total = sum(amount for _, amount, _ in stamps)
                if not self.db.compare_and_increment(
                    "promotions",
                    promotion_id,
                    counter="usage_count",
                    delta=1,
                    limit_column="usage_limit",
                    tenant_id=ctx.tenant_id,
                ):
                    failures.append({"promotion_id": promotion_id, "reason_code": "usage_limit"})
                    continue
                if total > 0 and not self.db.compare_and_increment(
                    "promotions",
                    promotion_id,
                    counter="budget_used_minor",
                    delta=total,
                    limit_column="budget_minor",
                    tenant_id=ctx.tenant_id,
                ):
                    self.db.compare_and_increment(
                        "promotions", promotion_id, counter="usage_count", delta=-1, tenant_id=ctx.tenant_id
                    )
                    failures.append({"promotion_id": promotion_id, "reason_code": "budget_cap"})
                    continue
                for booking_item_id, amount, sequence in stamps:
                    self.db.insert(
                        "promotion_redemptions",
                        {
                            "id": new_id("prr"),
                            "tenant_id": ctx.tenant_id,
                            "promotion_id": promotion_id,
                            "booking_id": booking_id,
                            "booking_item_id": booking_item_id,
                            "customer_key": None,
                            "amount_minor": amount,
                            "sequence": sequence,
                            "state": "APPLIED",
                            "created_at": now,
                        },
                    )
        return failures

    def restore_redemptions(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        reason: str = "cancelled",
        amount_minor: int | None = None,
    ) -> dict[str, Any]:
        """Give usage and budget back on cancellation or refund (R13.8).

        A promotion configured as non-restoring keeps its consumption, which is how
        a tenant models a single-use voucher that is spent even if the booking is
        later cancelled.
        """
        rows = self.db.query(
            "SELECT * FROM promotion_redemptions WHERE tenant_id = ? AND booking_id = ? AND state = 'APPLIED'",
            (ctx.tenant_id, booking_id),
        )
        restored = 0
        skipped = 0
        now = to_iso(self.clock.now())
        with self.db.transaction(immediate=True):
            by_promotion: dict[str, list[Any]] = {}
            for row in rows:
                by_promotion.setdefault(row["promotion_id"], []).append(row)
            for promotion_id, entries in by_promotion.items():
                promotion = self.db.query_one(
                    "SELECT restoring FROM promotions WHERE id = ? AND tenant_id = ?",
                    (promotion_id, ctx.tenant_id),
                )
                if promotion is None or not bool(promotion["restoring"]):
                    skipped += len(entries)
                    continue
                total = sum(int(e["amount_minor"]) for e in entries)
                self.db.compare_and_increment(
                    "promotions", promotion_id, counter="usage_count", delta=-1, tenant_id=ctx.tenant_id
                )
                if total:
                    self.db.compare_and_increment(
                        "promotions",
                        promotion_id,
                        counter="budget_used_minor",
                        delta=-total,
                        tenant_id=ctx.tenant_id,
                    )
                for entry in entries:
                    self.db.update(
                        "promotion_redemptions",
                        entry["id"],
                        {"state": "RESTORED", "restored_at": now},
                        tenant_id=ctx.tenant_id,
                    )
                restored += len(entries)
        return {"booking_id": booking_id, "restored": restored, "skipped_non_restoring": skipped, "reason": reason}

    def revalidate(
        self,
        ctx: RequestContext,
        cart: CartSnapshot,
        *,
        expected_discount_minor: int,
        codes: Sequence[str] = (),
    ) -> PromotionOutcome:
        """Re-evaluate immediately before payment authorization (R13.7).

        Raises when the total has moved, so the caller must show the customer the
        new figure and obtain explicit re-confirmation before charging.
        """
        outcome = self.evaluate(ctx, cart, codes=codes)
        if outcome.discount_minor != int(expected_discount_minor):
            raise RuleViolation(
                "The promotions on your order have changed. Please review the new total.",
                code="promotion_revalidation_changed",
                details={
                    "previous_discount_minor": int(expected_discount_minor),
                    "new_discount_minor": outcome.discount_minor,
                    "new_total_minor": max(cart.gross_minor - outcome.discount_minor, 0),
                    "applied": [
                        {"name": a["name"], "amount_minor": a["amount_minor"]} for a in outcome.applied
                    ],
                    "requires_reconfirmation": True,
                },
            )
        return outcome

    def usage_report(self, ctx: RequestContext, promotion_id: str) -> dict[str, Any]:
        promotion = self.get_promotion(ctx, promotion_id)
        return {
            "promotion_id": promotion_id,
            "internal_code": promotion["internal_code"],
            "status": promotion["status"],
            "usage_count": int(promotion["usage_count"]),
            "usage_limit": promotion["usage_limit"],
            "budget_used_minor": int(promotion["budget_used_minor"]),
            "budget_minor": promotion["budget_minor"],
            "redemption_count": promotion["redemption_count"],
            "exhausted": self._is_exhausted(ctx, promotion),
        }


__all__ = [
    "ACCOUNTING_TREATMENTS",
    "MECHANIC_EFFECTS",
    "NTH_ITEM_TARGETS",
    "PromotionOutcome",
    "PromotionService",
    "RULE_KEYS",
    "SETTLEMENT_TREATMENTS",
    "RejectedCode",
]
