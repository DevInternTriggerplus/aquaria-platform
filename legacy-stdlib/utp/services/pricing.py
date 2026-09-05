"""Pricing.

A price is *resolved*, never assumed. Rules are keyed by any combination of date
range, weekday, session, channel, partner, segment, quantity band and currency
(R5.1). When several rules match, the highest ``priority`` wins and ties break on
the most specific scope (R5.2) — and the winning rule id is recorded on the
booking item so a finance query can always answer "why was this the price?".

Two behaviours are deliberate and easy to get wrong:

* **No fallback price.** If nothing matches for the requested ticket type, date
  and channel, the ticket type is simply unavailable for that request. The
  platform never invents a price (R5.6).
* **Prices are frozen at confirmation.** :func:`quote` computes; the booking
  service stores the resolved unit price, tax, discount and net on the item.
  Changing a rule later never rewrites history (R5.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.audit import AuditLog
from ..core.clock import Clock, parse_date, to_iso, weekday_code
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import NotAvailable, ValidationError
from ..core.ids import new_id
from ..core.money import TaxSplit, split_tax
from .authz import AuthorizationService

#: Narrowing dimensions, most specific first. Used for the R5.2 tie-break.
_SPECIFICITY_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("session_id", 64),
    ("partner_id", 32),
    ("channel", 16),
    ("segment_id", 8),
    ("qty_min", 4),
    ("qty_max", 4),
    ("date_from", 2),
    ("date_to", 2),
    ("weekdays_json", 1),
)


@dataclass(frozen=True, slots=True)
class PriceResolution:
    """The outcome of resolving one ticket type's price."""

    ticket_type_id: str
    currency: str
    unit_price_minor: int
    price_rule_id: str
    price_rule_code: str | None
    tax: TaxSplit
    specificity: int
    priority: int
    price_unit: str = "PER_PERSON"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_type_id": self.ticket_type_id,
            "currency": self.currency,
            "unit_price_minor": self.unit_price_minor,
            "price_rule_id": self.price_rule_id,
            "price_rule_code": self.price_rule_code,
            "tax": self.tax.as_dict(),
            "priority": self.priority,
            "specificity": self.specificity,
            "price_unit": self.price_unit,
        }


@dataclass(slots=True)
class PriceRequest:
    """Everything a price rule may key on."""

    ticket_type_id: str
    date: str
    channel: str
    quantity: int = 1
    session_id: str | None = None
    partner_id: str | None = None
    segment_id: str | None = None
    currency: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class PricingService:
    """Price rule administration and resolution."""

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

    def create_price_rule(
        self,
        ctx: RequestContext,
        *,
        ticket_type_id: str,
        amount_minor: int,
        currency: str,
        code: str | None = None,
        priority: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        weekdays: Iterable[str] | None = None,
        session_id: str | None = None,
        channel: str | None = None,
        partner_id: str | None = None,
        segment_id: str | None = None,
        qty_min: int | None = None,
        qty_max: int | None = None,
    ) -> dict[str, Any]:
        """Create a price rule. Amount is in minor units — never a float."""
        self.authz.require_page(ctx, "Pricing", "ADD")
        ticket_type = self.authz.load_scoped(ctx, "ticket_types", ticket_type_id, entity="ticket_type")
        if int(amount_minor) < 0:
            raise ValidationError({"amount_minor": "A price cannot be negative."})
        if date_from and date_to and parse_date(date_from) > parse_date(date_to):
            raise ValidationError({"date_to": "The end date must not be before the start date."})
        rule_id = new_id("prc")
        self.db.insert(
            "price_rules",
            {
                "id": rule_id,
                "tenant_id": ctx.tenant_id,
                "ticket_type_id": ticket_type_id,
                "code": code,
                "currency": currency.upper(),
                "amount_minor": int(amount_minor),
                "priority": int(priority),
                "date_from": date_from,
                "date_to": date_to,
                "weekdays_json": list(weekdays) if weekdays else None,
                "session_id": session_id,
                "channel": channel,
                "partner_id": partner_id,
                "segment_id": segment_id,
                "qty_min": qty_min,
                "qty_max": qty_max,
                "status": "ACTIVE",
                "created_at": to_iso(self.clock.now()),
            },
        )
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="price_rule",
            target_id=rule_id,
            new={
                "ticket_type_id": ticket_type_id,
                "amount_minor": int(amount_minor),
                "currency": currency.upper(),
                "priority": int(priority),
                "scope": {
                    "date_from": date_from,
                    "date_to": date_to,
                    "channel": channel,
                    "partner_id": partner_id,
                    "session_id": session_id,
                },
            },
        )
        _ = ticket_type
        return self.get_price_rule(ctx, rule_id)

    def get_price_rule(self, ctx: RequestContext, rule_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "price_rules", rule_id, entity="price_rule")
        record["weekdays"] = decode(record.pop("weekdays_json"), None)
        record["specificity"] = _specificity(record)
        return record

    def list_price_rules(self, ctx: RequestContext, ticket_type_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id FROM price_rules WHERE tenant_id = ? AND ticket_type_id = ? ORDER BY priority DESC",
            (ctx.tenant_id, ticket_type_id),
        )
        return [self.get_price_rule(ctx, row["id"]) for row in rows]

    def deactivate_price_rule(self, ctx: RequestContext, rule_id: str) -> dict[str, Any]:
        self.authz.require_page(ctx, "Pricing", "DELETE", target_type="price_rule", target_id=rule_id)
        before = self.get_price_rule(ctx, rule_id)
        self.db.update("price_rules", rule_id, {"status": "INACTIVE"}, tenant_id=ctx.tenant_id)
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="price_rule",
            target_id=rule_id,
            previous={"status": before["status"]},
            new={"status": "INACTIVE", "performed": "DEACTIVATE"},
        )
        return {"requested": "DELETE", "performed": "DEACTIVATE", "price_rule_id": rule_id}

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #

    def resolve(
        self,
        ctx: RequestContext,
        request: PriceRequest,
        *,
        venue: dict[str, Any] | None = None,
    ) -> PriceResolution:
        """Resolve the applicable price, or raise ``NotAvailable`` (R5.1, R5.2, R5.6)."""
        ticket_type = self.authz.load_scoped(
            ctx, "ticket_types", request.ticket_type_id, entity="ticket_type"
        )
        if ticket_type["status"] != "ACTIVE":
            raise NotAvailable(
                "This ticket is not currently on sale.",
                details={"ticket_type_id": request.ticket_type_id, "reason": "inactive"},
            )
        channels = decode(ticket_type["channels_json"], []) or []
        if channels and request.channel not in channels:
            raise NotAvailable(
                "This ticket is not sold through this channel.",
                details={"ticket_type_id": request.ticket_type_id, "channel": request.channel},
            )
        venue_record = venue or self._venue_for_ticket_type(ctx, ticket_type)
        currency = (request.currency or venue_record["currency"]).upper()
        segment_id = request.segment_id or ticket_type["segment_id"]

        candidates = self.db.query(
            "SELECT * FROM price_rules WHERE tenant_id = ? AND ticket_type_id = ? "
            "AND status = 'ACTIVE' AND currency = ?",
            (ctx.tenant_id, request.ticket_type_id, currency),
        )
        weekday = weekday_code(request.date)
        matches: list[tuple[int, int, dict[str, Any]]] = []
        for row in candidates:
            rule = dict(row)
            if not _rule_matches(rule, request, weekday=weekday, segment_id=segment_id):
                continue
            matches.append((int(rule["priority"]), _specificity(rule), rule))

        if not matches:
            # R5.6 — no arbitrary fallback. The ticket type is unavailable, and the
            # message tells the operator/customer exactly which combination failed.
            raise NotAvailable(
                "This ticket is not available for the selected date.",
                details={
                    "ticket_type_id": request.ticket_type_id,
                    "date": request.date,
                    "channel": request.channel,
                    "currency": currency,
                    "reason": "no_price_rule",
                },
            )

        # Highest priority, then most specific scope, then oldest rule for a fully
        # deterministic outcome.
        matches.sort(key=lambda m: (m[0], m[1], m[2]["created_at"]), reverse=True)
        priority, specificity, winner = matches[0]

        tax_model = venue_record["tax_model"]
        rate_bp = int(venue_record["tax_rate_bp"] or 0)
        if ticket_type["tax_treatment"] == "ZERO_RATED":
            rate_bp = 0
        elif ticket_type["tax_treatment"] == "EXEMPT":
            rate_bp = 0
        tax = split_tax(int(winner["amount_minor"]), rate_bp=rate_bp, model=tax_model)

        return PriceResolution(
            ticket_type_id=request.ticket_type_id,
            currency=currency,
            unit_price_minor=int(winner["amount_minor"]),
            price_rule_id=winner["id"],
            price_rule_code=winner.get("code"),
            tax=tax,
            specificity=specificity,
            priority=priority,
        )

    def try_resolve(
        self, ctx: RequestContext, request: PriceRequest, *, venue: dict[str, Any] | None = None
    ) -> PriceResolution | None:
        """``resolve`` that returns ``None`` instead of raising.

        Used when building a price list, where an unavailable ticket type should be
        omitted rather than failing the whole page.
        """
        try:
            return self.resolve(ctx, request, venue=venue)
        except NotAvailable:
            return None

    def price_list(
        self,
        ctx: RequestContext,
        *,
        product_id: str,
        date: str,
        channel: str,
        session_id: str | None = None,
        partner_id: str | None = None,
        quantity: int = 1,
    ) -> list[dict[str, Any]]:
        """Sellable ticket types with resolved prices for a date/channel."""
        rows = self.db.query(
            "SELECT id FROM ticket_types WHERE tenant_id = ? AND product_id = ? AND status = 'ACTIVE' "
            "ORDER BY display_order, code",
            (ctx.tenant_id, product_id),
        )
        venue = None
        out: list[dict[str, Any]] = []
        for row in rows:
            ticket_type = self.authz.load_scoped(ctx, "ticket_types", row["id"], entity="ticket_type")
            if venue is None:
                venue = self._venue_for_ticket_type(ctx, ticket_type)
            resolution = self.try_resolve(
                ctx,
                PriceRequest(
                    ticket_type_id=row["id"],
                    date=date,
                    channel=channel,
                    session_id=session_id,
                    partner_id=partner_id,
                    quantity=quantity,
                ),
                venue=venue,
            )
            if resolution is None:
                continue
            out.append(
                {
                    "ticket_type_id": row["id"],
                    "code": ticket_type["code"],
                    "name": decode(ticket_type["name_json"], {}),
                    "segment_id": ticket_type["segment_id"],
                    "min_quantity": ticket_type["min_quantity"],
                    "max_quantity": ticket_type["max_quantity"],
                    **resolution.as_dict(),
                }
            )
        return out

    # ------------------------------------------------------------------ #

    def _venue_for_ticket_type(self, ctx: RequestContext, ticket_type: dict[str, Any]) -> dict[str, Any]:
        row = self.db.query_one(
            """
            SELECT v.* FROM venues v
            JOIN products p ON p.venue_id = v.id AND p.tenant_id = v.tenant_id
            WHERE p.id = ? AND p.tenant_id = ?
            """,
            (ticket_type["product_id"], ctx.tenant_id),
        )
        return self.authz.assert_same_tenant(ctx, row, entity="venue")


# --------------------------------------------------------------------------- #
# Matching helpers
# --------------------------------------------------------------------------- #


def _rule_matches(
    rule: dict[str, Any], request: PriceRequest, *, weekday: str, segment_id: str | None
) -> bool:
    """A rule matches when every dimension it constrains is satisfied.

    Unset dimensions are wildcards, which is what makes a single "standard adult
    price" rule cover every date and channel until a narrower rule overrides it.
    """
    if rule["date_from"] and request.date < rule["date_from"]:
        return False
    if rule["date_to"] and request.date > rule["date_to"]:
        return False
    weekdays = decode(rule["weekdays_json"], None)
    if weekdays and weekday not in weekdays:
        return False
    if rule["session_id"] and rule["session_id"] != request.session_id:
        return False
    if rule["channel"] and rule["channel"] != request.channel:
        return False
    if rule["partner_id"] and rule["partner_id"] != request.partner_id:
        return False
    if rule["segment_id"] and rule["segment_id"] != segment_id:
        return False
    if rule["qty_min"] is not None and request.quantity < int(rule["qty_min"]):
        return False
    if rule["qty_max"] is not None and request.quantity > int(rule["qty_max"]):
        return False
    return True


def _specificity(rule: dict[str, Any]) -> int:
    """Weighted count of constrained dimensions (R5.2 tie-break)."""
    score = 0
    for column, weight in _SPECIFICITY_WEIGHTS:
        value = rule.get(column)
        if value not in (None, "", "null"):
            score += weight
    return score


__all__ = ["PriceRequest", "PriceResolution", "PricingService"]
