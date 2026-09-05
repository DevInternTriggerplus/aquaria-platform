"""Booking orchestration: quote, checkout, confirmation, manage, reschedule, refund.

This is the module where the platform's guarantees have to hold together, so the
sequencing is deliberate and worth reading before changing:

**Quote** validates the date against booking rules, resolves a session per line,
resolves a price per line (with no fallback), then evaluates promotions. Nothing is
written.

**Checkout** acquires capacity holds. Only capacity-controlled lines get holds;
unlimited inventory takes none (analysis C.1).

**Confirm** runs in this order, and the order is the requirement:

1. revalidate dates, sessions, prices and promotions (R13.7, R57.11);
2. capture consent — *before* any personal data is persisted (R12.2), using a
   booking identifier reserved in advance because consent records are immutable;
3. persist the customer;
4. take payment;
5. convert holds into confirmed capacity;
6. write the booking, its items and its tickets;
7. commit promotion redemptions;
8. issue the receipt and queue notifications.

Step 5 is where R10.8 lives: if payment succeeded but the hold lapsed and the
inventory has gone, the booking is *not* confirmed. It goes to RECONCILIATION, an
automatic refund or void is initiated, and the customer is told what happened and
what to do next. If equivalent inventory is still free, the capacity is re-acquired
and the late confirmation is recorded (R10.9).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, combine_local, minutes_between, parse_date, parse_instant, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import (
    ConfirmationRequired,
    ConflictError,
    ConsentRequired,
    HoldExpired,
    JustSoldOut,
    NotAvailable,
    NotFound,
    PaymentFailed,
    RateLimited,
    RuleViolation,
    ValidationError,
)
from ..core.i18n import text as i18n_text
from ..core.ids import booking_number as make_booking_number, hash_identifier, human_code, hash_secret, new_id, verify_secret
from ..core.money import apply_percentage, apply_rounding, compute_charges, split_tax
from ..domain import enums
from ..domain.cart import CartLine, CartSnapshot
from .authz import AuthorizationService
from .calendar_rules import CalendarService
from .catalog import CatalogService
from .consent import ConsentCapture, ConsentService
from .customers import CustomerService
from .inventory import InventoryService
from .payments import PaymentService
from .pricing import PriceRequest, PricingService
from .promotions import PromotionService
from .tickets import TicketService

#: The customer flow's steps, in order (R11.1). The Time Slot step is omitted from
#: a quote's ``steps`` when no selected product uses a session (R11.5).
FLOW_STEPS: tuple[str, ...] = (
    "CHOOSE_EXPERIENCE",
    "SELECT_DATE",
    "SELECT_TIME",
    "SELECT_TICKETS",
    "SELECT_ADDONS",
    "APPLY_PROMOTION",
    "CUSTOMER_INFORMATION",
    "REVIEW_ORDER",
    "PAYMENT",
    "CONFIRMATION",
)


@dataclass(slots=True)
class QuoteLineRequest:
    """One requested line."""

    ticket_type_code: str | None = None
    ticket_type_id: str | None = None
    quantity: int = 1
    session_id: str | None = None
    seat_ids: list[str] = field(default_factory=list)
    zone_id: str | None = None
    parent_index: int | None = None


@dataclass(slots=True)
class Quote:
    """A validated, priced cart plus everything the customer must be told."""

    cart: CartSnapshot
    steps: list[str]
    applied_promotions: list[dict[str, Any]]
    rejected_codes: list[dict[str, Any]]
    total_minor: int
    breakdown: Any = None  # money.ChargeBreakdown
    holds: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_steps: dict[str, Any] = field(default_factory=dict)

    @property
    def rounding_adjustment_minor(self) -> int:
        """The gap between the pre-rounding arithmetic and the amount charged (R5.5)."""
        if self.breakdown is not None:
            return self.breakdown.rounding_adjustment_minor
        return self.total_minor - max(self.cart.gross_minor - self.cart.discount_minor, 0)

    def as_dict(self) -> dict[str, Any]:
        # The charge breakdown drives the customer price display (settings spec §7):
        # subtotal, discount, service charge, VAT, total, each labelled, so nothing
        # surprises the customer at the payment step.
        charges = self.breakdown.as_dict() if self.breakdown is not None else {}
        return {
            "cart_id": self.cart.cart_id,
            "steps": self.steps,
            "summary": self.cart.summary(),
            "applied_promotions": self.applied_promotions,
            "rejected_codes": self.rejected_codes,
            "total_minor": self.total_minor,
            "rounding_adjustment_minor": self.rounding_adjustment_minor,
            "charges": charges,
            "holds": self.holds,
            "warnings": self.warnings,
            "next_steps": self.next_steps,
        }


class BookingService:
    """The customer and staff booking lifecycle."""

    #: Wired by :class:`utp.app.Platform` after construction to avoid import cycles.
    notifications: Any = None
    documents: Any = None
    seating: Any = None
    settings: Any = None
    members: Any = None

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
        *,
        catalog: CatalogService,
        pricing: PricingService,
        calendar: CalendarService,
        inventory: InventoryService,
        promotions: PromotionService,
        consent: ConsentService,
        customers: CustomerService,
        payments: PaymentService,
        tickets: TicketService,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config
        self.catalog = catalog
        self.pricing = pricing
        self.calendar = calendar
        self.inventory = inventory
        self.promotions = promotions
        self.consent = consent
        self.customers = customers
        self.payments = payments
        self.tickets = tickets

    # ------------------------------------------------------------------ #
    # Quote (R11.1 - R11.13)
    # ------------------------------------------------------------------ #

    def quote(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        visit_date: str,
        lines: Sequence[QuoteLineRequest],
        promotion_codes: Sequence[str] = (),
        payment_method: str | None = None,
        partner_id: str | None = None,
        customer_key: str | None = None,
        cart_id: str | None = None,
    ) -> Quote:
        """Validate and price a requested cart without writing anything."""
        if not lines:
            raise ValidationError({"lines": "Choose at least one ticket."})
        venue = self._venue(ctx, venue_id)
        if self.config.get_bool(ctx, "cart.single_venue_only", venue_id=venue_id):
            # Analysis C.13 — Phase 1 restricts an order to one venue so currency,
            # timezone and tax identity are unambiguous.
            pass
        parse_date(visit_date)
        cart = CartSnapshot(
            venue_id=venue_id,
            organization_id=venue["organization_id"],
            currency=venue["currency"],
            channel=ctx.channel,
            visit_date=visit_date,
            cart_id=cart_id or new_id("crt"),
            partner_id=partner_id,
            payment_method=payment_method,
            customer_key=customer_key,
            language=ctx.language,
            tax_model=venue["tax_model"],
            tax_rate_bp=int(venue["tax_rate_bp"] or 0),
            rounding_mode=venue["rounding_mode"],
        )

        uses_session = False
        total_by_type: dict[str, int] = {}
        for index, request in enumerate(lines):
            ticket_type = self._ticket_type(ctx, request)
            product = self.catalog.get_product(ctx, ticket_type["product_id"])
            if product["venue_id"] != venue_id:
                raise ValidationError(
                    {f"lines[{index}]": "That ticket belongs to a different venue."},
                    message="All items in an order must be for the same venue.",
                )
            self._assert_channel(ctx, product, ticket_type, index)
            quantity = int(request.quantity)
            if quantity <= 0:
                raise ValidationError({f"lines[{index}].quantity": "Choose at least one ticket."})
            self._assert_quantity_limits(ctx, product, ticket_type, quantity, index)
            total_by_type[ticket_type["id"]] = total_by_type.get(ticket_type["id"], 0) + quantity

            rules = self.calendar.resolve_rules(
                ctx,
                venue_id=venue_id,
                channel=ctx.channel,
                experience_id=product.get("experience_id"),
                product_id=product["id"],
                session_id=request.session_id,
            )
            self.calendar.assert_bookable(
                ctx,
                venue=venue,
                date=visit_date,
                channel=ctx.channel,
                product_id=product["id"],
                experience_id=product.get("experience_id"),
                session_id=request.session_id,
            )
            session = self.inventory.resolve_inventory_session(
                ctx,
                venue=venue,
                product=product,
                date=visit_date,
                channel=ctx.channel,
                session_id=request.session_id,
                rules=rules,
            )
            if session is not None:
                if request.session_id:
                    uses_session = True
                self.inventory.assert_session_bookable(
                    ctx, session, timezone=venue["timezone"], quantity=quantity
                )
            if product["session_requirement"] != "NOT_USED":
                uses_session = True

            resolution = self.pricing.resolve(
                ctx,
                PriceRequest(
                    ticket_type_id=ticket_type["id"],
                    date=visit_date,
                    channel=ctx.channel,
                    quantity=quantity,
                    session_id=request.session_id,
                    partner_id=partner_id,
                    segment_id=ticket_type["segment_id"],
                    currency=venue["currency"],
                ),
                venue=venue,
            )
            segment = self.catalog.get_segment(ctx, ticket_type["segment_id"])
            cart.lines.append(
                CartLine(
                    index=index,
                    product_id=product["id"],
                    product_code=product["code"],
                    ticket_type_id=ticket_type["id"],
                    ticket_type_code=ticket_type["code"],
                    segment_id=segment["id"],
                    segment_code=segment["code"],
                    quantity=quantity,
                    unit_price_minor=resolution.unit_price_minor,
                    currency=resolution.currency,
                    price_rule_id=resolution.price_rule_id,
                    session_id=session["id"] if session is not None else None,
                    seat_id=request.seat_ids[0] if request.seat_ids else None,
                    zone_id=request.zone_id,
                    tax_rate_bp=resolution.tax.rate_bp,
                    tax_model=resolution.tax.model,
                    is_addon=request.parent_index is not None,
                    parent_index=request.parent_index,
                    consumes_capacity=bool(ticket_type["consumes_capacity"]),
                    is_complimentary=bool(ticket_type["is_complimentary"]),
                )
            )

        self._assert_booking_totals(ctx, venue, cart)
        outcome = self.promotions.evaluate(ctx, cart, codes=promotion_codes)
        priced = outcome.cart
        breakdown = self._compute_breakdown(ctx, venue=venue, cart=priced, on_date=visit_date)
        steps = [s for s in FLOW_STEPS if s != "SELECT_TIME" or uses_session]
        return Quote(
            cart=priced,
            steps=steps,
            applied_promotions=outcome.applied,
            rejected_codes=[r.as_dict() for r in outcome.rejected],
            total_minor=breakdown.grand_total_minor,
            breakdown=breakdown,
            next_steps=self._next_steps(ctx, venue=venue, cart=priced),
        )

    def _compute_breakdown(
        self, ctx: RequestContext, *, venue: dict[str, Any], cart: CartSnapshot, on_date: str
    ):
        """Run the authoritative charge engine for a cart (settings spec §2, §6).

        VAT and service charge come from the settings service at the visit date, so
        the rate applied is the one effective on that date; the discount split is
        already on the cart. This is the single place the total is computed, so cart,
        payment, receipt and reports agree.
        """
        sc_input, vat_input = self.settings.charge_inputs(ctx, venue_id=venue["id"], on_date=on_date)
        rounding_mode, rounding_increment = self.settings.rounding_policy(
            ctx, venue_id=venue["id"], fallback_mode=venue["rounding_mode"]
        )
        return compute_charges(
            base_minor=cart.gross_minor,
            line_discount_minor=cart.line_discount_minor,
            order_discount_minor=cart.discount_minor - cart.line_discount_minor,
            service_charge=sc_input,
            vat=vat_input,
            rounding_mode=rounding_mode,
            rounding_increment_minor=rounding_increment,
            currency=venue["currency"],
        )

    def _next_steps(
        self, ctx: RequestContext, *, venue: dict[str, Any], cart: CartSnapshot
    ) -> dict[str, Any]:
        """What the customer is told before paying (R11.9)."""
        hours = (venue.get("operating_hours") or {}).get("default", {})
        refund_policy = self.config.get(ctx, "refund.policy", venue_id=venue["id"]) or {}
        tiers = refund_policy.get("tiers", [])
        return {
            "delivery": "Your e-ticket and QR code are emailed immediately after payment, "
            "and are always available from Manage Booking.",
            "validity": f"Valid on {cart.visit_date}"
            + (f" between {hours.get('open')} and {hours.get('close')}." if hours else "."),
            "entry_location": "Show the QR code at the entrance gate.",
            "tax_model": venue["tax_model"],
            "tax_note": (
                "Prices include VAT." if venue["tax_model"] == "INCLUSIVE" else "VAT is added at payment."
            ),
            "cancellation_policy": [
                {
                    "from_hours_before": int(tier.get("min_hours_before", 0)),
                    "refund_percent_bp": int(tier.get("refund_percent_bp", 0)),
                    "fee_minor": int(tier.get("fee_minor", 0)),
                }
                for tier in tiers
            ],
            "reschedule_allowed": self.config.get_bool(
                ctx, "manage_booking.reschedule_enabled", venue_id=venue["id"]
            ),
            "cancel_allowed": self.config.get_bool(
                ctx, "manage_booking.cancel_enabled", venue_id=venue["id"]
            ),
        }

    def _venue(self, ctx: RequestContext, venue_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM venues WHERE id = ?", (venue_id,))
        venue = self.authz.assert_same_tenant(ctx, row, entity="venue", record_id=venue_id)
        venue["name"] = decode(venue.pop("name_json"), {})
        venue["address"] = decode(venue.pop("address_json"), {})
        venue["contact"] = decode(venue.pop("contact_json"), {})
        venue["operating_hours"] = decode(venue.pop("operating_hours_json"), {})
        if venue["status"] != "ACTIVE":
            raise NotAvailable("This venue is not currently selling tickets.")
        return venue

    def _ticket_type(self, ctx: RequestContext, request: QuoteLineRequest) -> dict[str, Any]:
        if request.ticket_type_id:
            return self.catalog.get_ticket_type(ctx, request.ticket_type_id)
        if request.ticket_type_code:
            return self.catalog.ticket_type_by_code(ctx, request.ticket_type_code)
        raise ValidationError({"ticket_type": "Choose a ticket type."})

    def _assert_channel(
        self, ctx: RequestContext, product: dict[str, Any], ticket_type: dict[str, Any], index: int
    ) -> None:
        if product["channels"] and ctx.channel not in product["channels"]:
            raise NotAvailable(
                "That item is not sold through this channel.",
                details={"line": index, "channel": ctx.channel, "product_code": product["code"]},
            )
        if ticket_type["channels"] and ctx.channel not in ticket_type["channels"]:
            raise NotAvailable(
                "That ticket is not sold through this channel.",
                details={"line": index, "channel": ctx.channel, "ticket_type_code": ticket_type["code"]},
            )

    def _assert_quantity_limits(
        self,
        ctx: RequestContext,
        product: dict[str, Any],
        ticket_type: dict[str, Any],
        quantity: int,
        index: int,
    ) -> None:
        """R11.13 / R3.5 — explain any limit that is reached."""
        minimum = int(ticket_type["min_quantity"] or 0)
        if minimum and quantity < minimum:
            raise ValidationError(
                {f"lines[{index}].quantity": f"This ticket must be bought in quantities of at least {minimum}."},
                message=f"Minimum {minimum} for {ticket_type['code']}.",
            )
        maximum = ticket_type["max_quantity"] or product["max_per_booking"]
        if maximum is not None and quantity > int(maximum):
            raise ValidationError(
                {f"lines[{index}].quantity": f"Up to {maximum} of this ticket per booking."},
                message=f"Maximum {maximum} for {ticket_type['code']}.",
            )

    def _assert_booking_totals(
        self, ctx: RequestContext, venue: dict[str, Any], cart: CartSnapshot
    ) -> None:
        limit = self.config.get_int(ctx, "booking.max_per_booking", venue_id=venue["id"])
        if limit and cart.total_quantity > limit:
            raise ValidationError(
                {"lines": f"Up to {limit} tickets per booking. Please split into separate bookings."},
                message=f"Maximum {limit} tickets per booking.",
            )

    # ------------------------------------------------------------------ #
    # Checkout holds (R10.1)
    # ------------------------------------------------------------------ #

    def start_checkout(self, ctx: RequestContext, quote: Quote) -> Quote:
        """Acquire holds for capacity-controlled lines and report the countdown."""
        cart = quote.cart
        holds: list[dict[str, Any]] = []
        for line in cart.lines:
            if not line.consumes_capacity or line.session_id is None:
                continue
            hold = self.inventory.acquire_hold(
                ctx,
                session_id=line.session_id,
                quantity=line.quantity,
                cart_id=cart.cart_id,
                zone_id=line.zone_id,
                partner_id=cart.partner_id,
                venue_id=cart.venue_id,
                product_id=line.product_id,
            )
            if hold is None:
                continue
            line.hold_id = hold.id
            status = self.inventory.hold_status(ctx, hold.id)
            holds.append({**hold.as_dict(), **status})
        quote.holds = holds
        return quote

    def abandon_checkout(self, ctx: RequestContext, cart_id: str) -> dict[str, Any]:
        released = self.inventory.release_cart_holds(ctx, cart_id, reason="abandoned")
        if self.seating is not None:
            released += self.seating.release_cart_holds(ctx, cart_id, reason="abandoned")
        return {"cart_id": cart_id, "released": released}

    # ------------------------------------------------------------------ #
    # Confirmation
    # ------------------------------------------------------------------ #

    def confirm(
        self,
        ctx: RequestContext,
        quote: Quote,
        *,
        customer: dict[str, Any],
        consent_items: dict[str, bool],
        payment_method: str,
        idempotency_key: str,
        expected_total_minor: int | None = None,
        promotion_codes: Sequence[str] = (),
        tendered_minor: int | None = None,
        shift_id: str | None = None,
        partner_attestation: dict[str, Any] | None = None,
        guardian_attestation: str | None = None,
        authority_attestation: str | None = None,
        reconfirmed: bool = False,
        allow_late: bool = True,
        points_redeem: int | None = None,
    ) -> dict[str, Any]:
        """Turn a quote into a confirmed booking with issued tickets."""
        cart = quote.cart
        venue = self._venue(ctx, cart.venue_id)
        email = str(customer.get("email") or "").strip().lower()
        # Customer details are channel-aware (update spec §12-§14, §45, §46). The
        # website must collect a real contact so the e-ticket and confirmation can be
        # sent; a self-service kiosk must not require it — the ticket is printed and
        # shown on screen, and contact may be offered *after* payment. When a kiosk
        # sale has no email we mint a synthetic, non-deliverable contact so the
        # append-only consent/customer chain stays consistent without emailing anyone.
        kiosk = (ctx.channel or "").upper() == "KIOSK"
        emailless_kiosk = kiosk and "@" not in email
        if emailless_kiosk:
            email = f"kiosk+{cart.cart_id}@no-delivery.local"
        elif "@" not in email:
            raise ValidationError({"email": "Enter a valid email address so we can send your ticket."})

        # --- 1. revalidate ------------------------------------------------ #
        self._revalidate(ctx, cart, venue=venue)
        expected = quote.total_minor if expected_total_minor is None else int(expected_total_minor)
        outcome = self.promotions.evaluate(ctx, cart, codes=promotion_codes or cart.promotion_codes)
        # Recompute through the same authoritative charge engine as the quote, at the
        # visit date, so a promotion change or a scheduled VAT change is caught here
        # before the card is charged (R13.7, settings spec §2).
        breakdown = self._compute_breakdown(ctx, venue=venue, cart=outcome.cart, on_date=cart.visit_date)
        new_total = breakdown.grand_total_minor
        if new_total != expected and not reconfirmed:
            # R13.7 — the customer must see and accept the new figure before charging.
            raise ConfirmationRequired(
                "The total for your order has changed. Please review it before paying.",
                code="total_changed",
                details={
                    "previous_total_minor": expected,
                    "new_total_minor": new_total,
                    "applied_promotions": [
                        {"name": a["name"], "amount_minor": a["amount_minor"]} for a in outcome.applied
                    ],
                    "requires_reconfirmation": True,
                },
            )
        cart = outcome.cart
        quote.cart = cart
        total_minor = new_total
        # Stored-value coupons (gift cards) settle part of the bill without reducing
        # revenue (§16, §68). The gateway is charged only the remainder; net_minor
        # stays the full revenue so tax and reporting are unaffected. Loyalty points
        # redeemed at checkout settle the bill the same way and are added below once
        # the booking id exists to attribute the redemption to.
        settlement_minor = cart.settlement_minor
        amount_to_charge = max(total_minor - settlement_minor, 0)
        points_redemption: dict[str, Any] | None = None

        # --- 2. consent, before any personal data is persisted (R12.2) ---- #
        booking_id = new_id("bkg")
        notice = self.consent.current_notice(ctx, language=ctx.language)
        self.consent.check_required(ctx, consent_items)
        threshold = self.config.get_int(ctx, "consent.minor_age_threshold", venue_id=venue["id"])
        is_minor = bool(customer.get("age") is not None and int(customer["age"]) < threshold)
        if is_minor and not guardian_attestation:
            raise ValidationError(
                {"guardian_attestation": "A parent or legal guardian must confirm this consent."},
                message="Guardian consent is required.",
                code="guardian_consent_required",
            )
        if cart.total_quantity > 1 and not authority_attestation and customer.get("booking_for_others"):
            raise ValidationError(
                {"authority_attestation": "Confirm you may provide the other visitors' details."},
                message="Confirmation of authority is required.",
                code="authority_attestation_required",
            )
        consent_record = self.consent.capture(
            ctx,
            ConsentCapture(
                items=consent_items,
                notice_version=notice["version"],
                consent_text_version=notice["consent_text_version"],
                language=ctx.language,
                contact=email,
                guardian_attestation=guardian_attestation,
                authority_attestation=authority_attestation,
            ),
            venue_id=venue["id"],
            booking_id=booking_id,
            venue_timezone=venue["timezone"],
            partner_attestation=partner_attestation,
        )

        # --- 3. customer -------------------------------------------------- #
        customer_record = self.customers.upsert(
            ctx,
            consent_record_id=consent_record["id"],
            email=email,
            full_name=customer.get("full_name"),
            phone=customer.get("phone"),
            language=customer.get("language") or ctx.language,
            extra={k: v for k, v in (customer.get("extra") or {}).items()},
            is_minor=is_minor,
        )

        # --- 3b. loyalty points redemption (settles like a gift card) ----- #
        if points_redeem and int(points_redeem) > 0:
            if self.members is None:
                raise ValidationError({"points_redeem": "Loyalty points are not available."})
            member = self.members.find_by_email(ctx, email)
            if member is None:
                raise ValidationError(
                    {"points_redeem": "No membership was found for this email."},
                    message="We could not find a membership to redeem points from.",
                )
            # Never redeem more value than remains payable after other settlements.
            payable_now = max(total_minor - cart.settlement_minor, 0)
            rate = self.members.conversion_rate(ctx, venue_id=venue["id"])
            requested_value = self.members.points_to_minor(int(points_redeem), rate)
            points_to_spend = int(points_redeem)
            if requested_value > payable_now:
                # Trim to exactly cover the remaining bill, rounding points up.
                points_to_spend = min(points_to_spend, self.members.minor_to_points(payable_now, rate))
            if points_to_spend > 0:
                points_redemption = self.members.redeem_points(
                    ctx,
                    member_id=member["id"],
                    points=points_to_spend,
                    booking_id=booking_id,
                    venue_id=venue["id"],
                    currency=venue["currency"],
                    reason="Checkout redemption",
                )
                applied_value = min(int(points_redemption["value_minor"]), payable_now)
                cart.settlements.append(
                    {
                        "kind": "LOYALTY_POINTS",
                        "member_id": member["id"],
                        "points": points_to_spend,
                        "rate_text": points_redemption["rate_text"],
                        "amount_minor": applied_value,
                        "ledger_id": points_redemption["ledger_id"],
                        "accounting_treatment": "STORED_VALUE",
                    }
                )
                settlement_minor = cart.settlement_minor
                amount_to_charge = max(total_minor - settlement_minor, 0)

        # --- 4. reserve the booking BEFORE charging ----------------------- #
        # The order matters. If the gateway is called first and the process dies before
        # the booking is written, the money exists and the order does not — and the
        # arriving webhook has nothing to complete. Persisting the intent as
        # AWAITING_PAYMENT first means the webhook always has something to finish,
        # which is what makes R14.6 achievable rather than aspirational.
        with self.db.transaction():
            self._insert_booking_row(
                ctx,
                booking_id=booking_id,
                venue=venue,
                cart=cart,
                customer_id=customer_record["id"],
                consent_record_id=consent_record["id"],
                total_minor=total_minor,
                breakdown=breakdown,
                status="AWAITING_PAYMENT",
                confirmed_at=None,
                settlement_minor=settlement_minor,
                settlements=[dict(s) for s in cart.settlements],
                gifts=[dict(g) for g in cart.gifts],
            )
            self._insert_booking_items(ctx, booking_id=booking_id, cart=cart, venue=venue)

        # --- 5. payment --------------------------------------------------- #
        # Charge only the remainder after stored value; if a gift card covers the
        # whole bill there is nothing to take from the gateway, so settle it as a
        # stored-value payment that needs no external authorization (§16, §68).
        charge_method = payment_method
        if amount_to_charge <= 0 and settlement_minor > 0:
            charge_method = "STORED_VALUE"
        try:
            payment = self.payments.start_payment(
                ctx,
                booking_id=booking_id,
                amount_minor=amount_to_charge,
                currency=venue["currency"],
                method=charge_method,
                idempotency_key=idempotency_key,
                venue_id=venue["id"],
                tendered_minor=tendered_minor,
                shift_id=shift_id,
                metadata={"cart_id": cart.cart_id, "settlement_minor": settlement_minor},
            )
        except PaymentFailed:
            # R14.8 — the cart and its holds stay intact so the customer can retry with
            # a new idempotency key. The reservation is marked, not deleted, so a late
            # provider confirmation for this attempt can still be reconciled.
            self.db.update(
                "bookings",
                booking_id,
                {"status": "PENDING", "notes": "Payment attempt failed; awaiting retry."},
                tenant_id=ctx.tenant_id,
            )
            # Give redeemed points back: the customer must not lose loyalty value on a
            # payment that never completed (§69 — redemption belongs to a committed
            # transaction only).
            if points_redemption is not None and self.members is not None:
                self.members.restore_for_booking(ctx, booking_id=booking_id, reason="payment_failed")
            raise

        # --- 6. complete: capacity, tickets, receipt, e-ticket email ------ #
        return self.finalize_paid_booking(
            ctx,
            booking_id=booking_id,
            payment_id=payment["payment_id"],
            allow_late=allow_late,
            cart=cart,
        )

    # ------------------------------------------------------------------ #
    # Gateway-driven completion (R14.6, R14.7)
    # ------------------------------------------------------------------ #

    def finalize_paid_booking(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        payment_id: str | None = None,
        allow_late: bool = True,
        cart: CartSnapshot | None = None,
    ) -> dict[str, Any]:
        """Complete a paid booking: capacity, tickets, receipt, e-ticket email.

        This is the **single** completion path. The inline checkout calls it directly;
        the payment webhook calls it when the customer's browser never came back
        (R14.6). Having one function rather than two is what guarantees the guest gets
        an identical ticket and email either way.

        Idempotent by design, because a provider may deliver the same event more than
        once and out of order (R14.4):

        * a booking already CONFIRMED returns its existing tickets and sends nothing;
        * tickets are issued only if none exist for the booking;
        * promotion redemptions are reconstructed from persisted items, skipping any
          already recorded;
        * notification de-duplication (R36.13) suppresses a repeat e-ticket email.
        """
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])

        # Already done: return the same result without re-issuing or re-emailing.
        if booking["status"] == "CONFIRMED":
            return {
                "status": "CONFIRMED",
                "confirmed": True,
                "already_confirmed": True,
                "booking_id": booking_id,
                "booking_number": booking["booking_number"],
                "total_minor": int(booking["net_minor"]),
                "currency": booking["currency"],
                "tickets": self.tickets.list_for_booking(ctx, booking_id, include_qr=True),
                "message_key": "success.booking_confirmed",
            }
        if booking["status"] not in ("AWAITING_PAYMENT", "PENDING", "RECONCILIATION"):
            raise ConflictError(
                f"This booking is {booking['status'].lower()} and cannot be completed.",
                details={"status": booking["status"]},
            )

        payment = (
            self.payments.get_payment(ctx, payment_id)
            if payment_id
            else next(
                (
                    p
                    for p in self.payments.list_for_booking(ctx, booking_id)
                    if p["status"] in ("AUTHORIZED", "CAPTURED")
                ),
                None,
            )
        )
        if payment is None:
            raise ConflictError(
                "There is no successful payment for this booking yet.",
                details={"booking_id": booking_id},
            )

        # Capacity is confirmed from persisted state, so the webhook path needs no cart.
        try:
            late = self._confirm_capacity_for_booking(ctx, booking, allow_late=allow_late)
        except (HoldExpired, JustSoldOut) as exc:
            return self._payment_without_inventory(
                ctx,
                booking_id=booking_id,
                venue=venue,
                cart=cart,
                payment=payment,
                total_minor=int(booking["net_minor"]),
                failure=exc,
            )

        items = [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM booking_items WHERE tenant_id = ? AND booking_id = ? ORDER BY created_at",
                (ctx.tenant_id, booking_id),
            )
        ]
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "bookings",
                booking_id,
                {
                    "status": "CONFIRMED",
                    "confirmed_at": now,
                    "late_confirmation": 1 if late else 0,
                    "notes": None,
                },
                tenant_id=ctx.tenant_id,
            )
            self.db.execute(
                "UPDATE booking_items SET state = 'ACTIVE' WHERE tenant_id = ? AND booking_id = ?",
                (ctx.tenant_id, booking_id),
            )
            existing_tickets = self.tickets.list_for_booking(ctx, booking_id)
            if existing_tickets:
                tickets = existing_tickets
            else:
                refreshed = dict(
                    self.db.query_one(
                        "SELECT * FROM bookings WHERE id = ? AND tenant_id = ?",
                        (booking_id, ctx.tenant_id),
                    )
                )
                tickets = self.tickets.issue_for_booking(
                    ctx, booking=refreshed, venue=venue, items=items
                )
                if self.seating is not None and cart is not None:
                    self.seating.bind_reservations_to_booking(
                        ctx, cart=cart, booking_id=booking_id, items=items, tickets=tickets
                    )
            self.audit.record(
                ctx.for_venue(venue["id"]),
                "BOOKING_CONFIRMED",
                target_type="booking",
                target_id=booking_id,
                previous={"status": booking["status"]},
                new={
                    "booking_number": booking["booking_number"],
                    "total_minor": int(booking["net_minor"]),
                    "channel": booking["channel"],
                    "payment_id": payment["payment_id"],
                    "late_confirmation": late,
                    "ticket_count": len(tickets),
                    "completed_via": "inline" if cart is not None else "gateway_callback",
                },
                venue_timezone=venue["timezone"],
            )

        promotion_failures = self.promotions.commit_from_booking(ctx, booking_id=booking_id)
        self.payments.mark_captured(ctx, payment["payment_id"], notify=False)
        duplicate = self.payments.detect_duplicate_payment(ctx, booking_id)

        receipt = self.documents.issue_receipt(ctx, booking_id=booking_id) if self.documents else None

        # --- e-ticket delivery -------------------------------------------- #
        recipient = (
            self.customers.contact_email(ctx, booking["customer_id"]) if booking["customer_id"] else None
        )
        # A kiosk sale with no contact has a synthetic non-deliverable address; the
        # ticket is printed and shown on screen, so there is nothing to email (§14, §46).
        if recipient and recipient.endswith("@no-delivery.local"):
            recipient = None
        language = booking["language"]
        combined = self.config.get_bool(
            ctx, "notification.combine_confirmation_and_ticket", venue_id=venue["id"]
        )
        delivery: list[dict[str, Any]] = []
        for event in (
            ("BOOKING_CONFIRMATION",)
            if combined
            else ("BOOKING_CONFIRMATION", "PAYMENT_CONFIRMATION", "ETICKET_DELIVERY")
        ):
            delivery.append({"event": event, **self._notify_result(ctx, event, booking_id, venue, recipient, language)})
        if self.notifications is not None:
            self.notifications.schedule_reminders(ctx, booking_id=booking_id, venue=venue)
            # Send immediately rather than waiting for the next maintenance pass: the
            # guest is entitled to their ticket now, and the queue exists so a slow
            # provider cannot delay confirmation, not to delay the ticket itself.
            self.notifications.dispatch_due(ctx)

        return {
            "status": "CONFIRMED",
            "confirmed": True,
            "already_confirmed": False,
            "booking_id": booking_id,
            "booking_number": booking["booking_number"],
            "total_minor": int(booking["net_minor"]),
            # What the customer's payment method actually collected, after any
            # stored value settled part of the bill (§16, §68).
            "settlement_minor": int(booking["settlement_minor"] or 0),
            "amount_paid_minor": max(int(booking["net_minor"]) - int(booking["settlement_minor"] or 0), 0),
            "gifts": decode(booking["gifts_json"], []) or [],
            "currency": booking["currency"],
            "payment": payment,
            "tickets": self.tickets.list_for_booking(ctx, booking_id, include_qr=True),
            "receipt": receipt,
            "late_confirmation": late,
            "promotion_failures": promotion_failures,
            "duplicate_payment": duplicate,
            "ticket_delivery": delivery,
            "message_key": "success.booking_confirmed",
        }

    def _notify_result(
        self,
        ctx: RequestContext,
        event: str,
        booking_id: str,
        venue: dict[str, Any],
        recipient: str | None,
        language: str,
    ) -> dict[str, Any]:
        if self.notifications is None or not recipient:
            return {"queued": False, "reason": "no_recipient" if not recipient else "no_notifier"}
        return self.notifications.enqueue(
            ctx,
            event_type=event,
            booking_id=booking_id,
            recipient=recipient,
            language=language,
            venue_id=venue["id"],
        )

    def _confirm_capacity_for_booking(
        self, ctx: RequestContext, booking: dict[str, Any], *, allow_late: bool
    ) -> bool:
        """Confirm capacity from persisted booking items and the cart's holds.

        The inline path could use the in-memory cart, but driving both paths from
        persisted state means the webhook path is not a second, less-tested
        implementation of the same rule.
        """
        channel_ctx = ctx.with_channel(booking["channel"])
        holds_by_session: dict[str, list[str]] = {}
        if booking.get("cart_id"):
            for row in self.db.query(
                "SELECT id, session_id FROM holds WHERE tenant_id = ? AND cart_id = ? "
                "AND state IN ('ACTIVE','EXPIRED') AND session_id IS NOT NULL",
                (ctx.tenant_id, booking["cart_id"]),
            ):
                holds_by_session.setdefault(row["session_id"], []).append(row["id"])

        late = False
        for item in self.db.query(
            "SELECT * FROM booking_items WHERE tenant_id = ? AND booking_id = ?",
            (ctx.tenant_id, booking["id"]),
        ):
            if not item["session_id"]:
                continue
            ticket_type = self.catalog.get_ticket_type(ctx, item["ticket_type_id"])
            if not ticket_type["consumes_capacity"]:
                continue
            session = self.inventory.get_session(ctx, item["session_id"])
            if session["capacity"] is None:
                continue
            hold_ids = holds_by_session.get(item["session_id"], [])
            if hold_ids:
                result = self.inventory.confirm_hold(
                    channel_ctx, hold_ids.pop(0), allow_late=allow_late, partner_id=booking["partner_id"]
                )
                late = late or bool(result.get("late"))
            else:
                self.inventory.confirm_without_hold(
                    channel_ctx,
                    session_id=item["session_id"],
                    quantity=int(item["quantity"]),
                    partner_id=booking["partner_id"],
                )
        return late

    def _revalidate(self, ctx: RequestContext, cart: CartSnapshot, *, venue: dict[str, Any]) -> None:
        """Re-check dates, sessions and prices immediately before charging."""
        for line in cart.lines:
            product = self.catalog.get_product(ctx, line.product_id)
            self.calendar.assert_bookable(
                ctx,
                venue=venue,
                date=cart.visit_date or "",
                channel=cart.channel,
                product_id=line.product_id,
                experience_id=product.get("experience_id"),
                session_id=line.session_id,
                # Skip the sold-out test only while the line's own hold is still live:
                # a guest holding the last place must not be blocked by their own hold.
                # Once the hold has lapsed it protects nothing, so availability is
                # rechecked and the guest is stopped *before* being charged — better than
                # taking money the platform must immediately refund.
                include_availability=not self._has_live_hold(ctx, line),
            )
            if line.session_id:
                session = self.inventory.get_session(ctx, line.session_id)
                if session["status"] == "CANCELLED":
                    raise NotAvailable(
                        "One of the sessions in your order has been cancelled.",
                        details={"session_id": line.session_id, "line": line.index},
                    )
                if not line.hold_id:
                    self.inventory.assert_session_bookable(
                        ctx, session, timezone=venue["timezone"], quantity=line.quantity
                    )
            resolution = self.pricing.resolve(
                ctx,
                PriceRequest(
                    ticket_type_id=line.ticket_type_id,
                    date=cart.visit_date or "",
                    channel=cart.channel,
                    quantity=line.quantity,
                    session_id=line.session_id,
                    partner_id=cart.partner_id,
                    segment_id=line.segment_id,
                    currency=cart.currency,
                ),
                venue=venue,
            )
            if resolution.unit_price_minor != line.unit_price_minor:
                raise ConfirmationRequired(
                    "The price of an item in your order has changed. Please review it.",
                    code="price_changed",
                    details={
                        "line": line.index,
                        "previous_unit_price_minor": line.unit_price_minor,
                        "new_unit_price_minor": resolution.unit_price_minor,
                        "requires_reconfirmation": True,
                    },
                )

    def _has_live_hold(self, ctx: RequestContext, line: CartLine) -> bool:
        """Is this line's hold still ACTIVE and unexpired?"""
        if not line.hold_id:
            return False
        row = self.db.query_one(
            "SELECT state, expires_at FROM holds WHERE id = ? AND tenant_id = ?",
            (line.hold_id, ctx.tenant_id),
        )
        if row is None or row["state"] != "ACTIVE":
            return False
        return to_iso(self.clock.now()) <= row["expires_at"]

    def _confirm_capacity(self, ctx: RequestContext, cart: CartSnapshot, *, allow_late: bool) -> bool:
        """Turn holds into confirmed capacity; take capacity directly where none was held."""
        late = False
        for line in cart.lines:
            if not line.consumes_capacity or line.session_id is None:
                continue
            if line.hold_id:
                result = self.inventory.confirm_hold(
                    ctx, line.hold_id, allow_late=allow_late, partner_id=cart.partner_id
                )
                late = late or bool(result.get("late"))
            else:
                session = self.inventory.get_session(ctx, line.session_id)
                if session["capacity"] is None:
                    continue
                self.inventory.confirm_without_hold(
                    ctx,
                    session_id=line.session_id,
                    quantity=line.quantity,
                    partner_id=cart.partner_id,
                )
        if self.seating is not None:
            late = self.seating.confirm_cart_seats(ctx, cart) or late
        return late

    def _payment_without_inventory(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        venue: dict[str, Any],
        payment: dict[str, Any],
        total_minor: int,
        failure: Exception,
        cart: CartSnapshot | None = None,
    ) -> dict[str, Any]:
        """R10.8 / R57.12 — money taken, inventory gone.

        The booking already exists (it was reserved before charging), so this moves it
        to RECONCILIATION rather than creating it. Finance can see the money, an
        automatic refund or void starts per configuration, and the customer is told
        plainly what happened and what to do next.
        """
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        now = to_iso(self.clock.now())
        remedy = self.config.get(ctx, "payment.oversell_remedy", venue_id=venue["id"], default="REFUND")
        with self.db.transaction():
            self.db.update(
                "bookings",
                booking_id,
                {
                    "status": "RECONCILIATION",
                    "notes": "Inventory unavailable after payment; remedy in progress.",
                },
                tenant_id=ctx.tenant_id,
            )
            self.db.execute(
                "UPDATE booking_items SET state = 'VOID' WHERE tenant_id = ? AND booking_id = ?",
                (ctx.tenant_id, booking_id),
            )
            self.db.update(
                "payments",
                payment["payment_id"],
                {"reconciliation_state": "ORPHANED_AUTHORIZATION"},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(venue["id"]),
                "BOOKING_CANCELLED",
                target_type="booking",
                target_id=booking_id,
                new={
                    "status": "RECONCILIATION",
                    "reason": "inventory_unavailable_after_payment",
                    "payment_id": payment["payment_id"],
                    "remedy": remedy,
                },
                reason=str(getattr(failure, "code", "hold_expired")),
                severity="WARNING",
                venue_timezone=venue["timezone"],
            )
        cart_id = cart.cart_id if cart is not None else booking.get("cart_id")
        if cart_id:
            self.inventory.release_cart_holds(ctx, cart_id, reason="inventory_unavailable")

        remedy_result: dict[str, Any]
        if remedy == "VOID":
            remedy_result = self.payments.void_payment(
                ctx, payment["payment_id"], reason="Inventory unavailable after payment"
            )
        else:
            refund_id = new_id("rfd")
            self.db.insert(
                "refunds",
                {
                    "id": refund_id,
                    "tenant_id": ctx.tenant_id,
                    "booking_id": booking_id,
                    "payment_id": payment["payment_id"],
                    "kind": "REFUND",
                    "amount_minor": total_minor,
                    "status": "PENDING",
                    "reason": "Inventory unavailable after payment",
                    "actor_id": ctx.principal.id or "system",
                    "created_at": now,
                },
            )
            remedy_result = self.payments.execute_refund(
                ctx,
                refund_id=refund_id,
                payment_id=payment["payment_id"],
                amount_minor=total_minor,
                currency=venue["currency"],
            )

        self._notify(
            ctx,
            event="BOOKING_CANCELLED",
            booking_id=booking_id,
            venue=venue,
            recipient=(
                self.customers.contact_email(ctx, booking["customer_id"])
                if booking.get("customer_id")
                else None
            ),
            language=booking.get("language") or ctx.language,
            variables={
                "reason": "The place you selected was taken before your payment completed.",
                "remedy": remedy,
                "amount_minor": total_minor,
            },
        )
        # Derived from the persisted booking, not the cart: on the gateway-callback path
        # there is no cart in memory because the customer's browser is long gone.
        first_product = self.db.scalar(
            "SELECT product_id FROM booking_items WHERE tenant_id = ? AND booking_id = ? "
            "ORDER BY created_at LIMIT 1",
            (ctx.tenant_id, booking_id),
        )
        alternatives = self.calendar.next_bookable_dates(
            ctx,
            venue=venue,
            channel=booking["channel"],
            product_id=first_product,
            after=booking.get("visit_date"),
        )
        return {
            "status": "RECONCILIATION",
            "confirmed": False,
            "booking_id": booking_id,
            "payment": payment,
            "remedy": remedy,
            "remedy_result": remedy_result,
            "message": (
                "Your payment went through, but the place you selected was taken moments before. "
                "We have started returning your money and will email you the details."
            ),
            "nearest_available_dates": alternatives,
            "reason_code": getattr(failure, "code", "hold_expired"),
        }

    def _insert_booking_row(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        venue: dict[str, Any],
        cart: CartSnapshot,
        customer_id: str,
        consent_record_id: str,
        total_minor: int,
        status: str,
        confirmed_at: str | None,
        breakdown: Any = None,
        late: bool = False,
        settlement_minor: int = 0,
        settlements: list[dict[str, Any]] | None = None,
        gifts: list[dict[str, Any]] | None = None,
    ) -> str:
        discount = cart.discount_minor
        # Prefer the authoritative engine's split; fall back to the venue's inclusive
        # VAT if no breakdown was supplied (e.g. a legacy caller).
        if breakdown is not None:
            tax_minor = breakdown.vat_minor
            service_charge_minor = breakdown.service_charge_minor
            charge_snapshot = breakdown.snapshot()
        else:
            tax_minor = split_tax(
                total_minor, rate_bp=int(venue["tax_rate_bp"] or 0), model=venue["tax_model"]
            ).tax_minor
            service_charge_minor = 0
            charge_snapshot = None
        sequence = int(
            self.db.scalar("SELECT COUNT(*) FROM bookings WHERE tenant_id = ?", (ctx.tenant_id,), default=0)
        )
        booking_no = make_booking_number(venue["short_code"], sequence)
        while self.db.query_one(
            "SELECT 1 FROM bookings WHERE tenant_id = ? AND booking_number = ?", (ctx.tenant_id, booking_no)
        ):
            booking_no = make_booking_number(venue["short_code"])
        self.db.insert(
            "bookings",
            {
                "id": booking_id,
                "tenant_id": ctx.tenant_id,
                "organization_id": venue["organization_id"],
                "venue_id": venue["id"],
                "booking_number": booking_no,
                "customer_id": customer_id,
                "channel": cart.channel,
                "partner_id": cart.partner_id,
                "device_id": ctx.device_id,
                "staff_actor_id": ctx.principal.id if ctx.principal.is_staff else None,
                "status": status,
                "currency": venue["currency"],
                "gross_minor": cart.gross_minor,
                "discount_minor": discount,
                "service_charge_minor": service_charge_minor,
                "tax_minor": tax_minor,
                "net_minor": total_minor,
                # Stored value settles the bill without reducing revenue (§16, §68):
                # net_minor is the revenue; the payment method collects
                # net_minor - settlement_minor. Snapshotted so a later coupon config
                # change cannot move this order (§56).
                "settlement_minor": int(settlement_minor or 0),
                "settlements_json": settlements or None,
                "gifts_json": gifts or None,
                # Historical configuration snapshot (settings spec §33): the exact
                # VAT/service-charge rates and modes used, so changing current
                # settings never moves this order.
                "charge_snapshot_json": charge_snapshot,
                "transaction_currency": venue["currency"],
                "base_currency": venue["currency"],
                "language": cart.language,
                "visit_date": cart.visit_date,
                "session_id": next((l.session_id for l in cart.lines if l.session_id), None),
                "consent_record_id": consent_record_id,
                "cart_id": cart.cart_id,
                "correlation_id": ctx.correlation_id,
                "created_at": to_iso(self.clock.now()),
                "confirmed_at": confirmed_at,
                "late_confirmation": 1 if late else 0,
            },
        )
        return booking_no

    def _insert_booking_items(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        cart: CartSnapshot,
        venue: dict[str, Any],
        state: str = "ACTIVE",
    ) -> list[dict[str, Any]]:
        """Freeze price, tax, discount and applied promotions per item (R5.3, R13.9)."""
        items: list[dict[str, Any]] = []
        for line in cart.lines:
            item_id = new_id("bit")
            net = line.net_minor
            tax = split_tax(net, rate_bp=line.tax_rate_bp, model=line.tax_model)
            self.db.insert(
                "booking_items",
                {
                    "id": item_id,
                    "tenant_id": ctx.tenant_id,
                    "booking_id": booking_id,
                    "product_id": line.product_id,
                    "ticket_type_id": line.ticket_type_id,
                    "segment_id": line.segment_id,
                    "session_id": line.session_id,
                    "seat_id": line.seat_id,
                    "zone_id": line.zone_id,
                    "quantity": line.quantity,
                    "unit_price_minor": line.unit_price_minor,
                    "gross_minor": line.gross_minor,
                    "discount_minor": line.discount_minor,
                    "tax_minor": tax.tax_minor,
                    "net_minor": net,
                    "price_rule_id": line.price_rule_id,
                    "promotions_json": line.promotions,
                    "price_unit": line.price_unit,
                    "state": state,
                    "created_at": to_iso(self.clock.now()),
                },
            )
            items.append(
                {
                    "id": item_id,
                    "product_id": line.product_id,
                    "ticket_type_id": line.ticket_type_id,
                    "segment_id": line.segment_id,
                    "session_id": line.session_id,
                    "seat_id": line.seat_id,
                    "quantity": line.quantity,
                    "net_minor": net,
                }
            )
        return items

    def _notify(
        self,
        ctx: RequestContext,
        *,
        event: str,
        booking_id: str,
        venue: dict[str, Any],
        recipient: str | None,
        language: str,
        variables: dict[str, Any] | None = None,
    ) -> None:
        if self.notifications is None or not recipient:
            return
        self.notifications.enqueue(
            ctx,
            event_type=event,
            booking_id=booking_id,
            recipient=recipient,
            language=language,
            venue_id=venue["id"],
            extra_variables=variables or {},
        )

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def get_booking(self, ctx: RequestContext, booking_id: str, *, mask: bool = True) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        record["items"] = [
            {**dict(row), "promotions": decode(row["promotions_json"], [])}
            for row in self.db.query(
                "SELECT * FROM booking_items WHERE tenant_id = ? AND booking_id = ? ORDER BY created_at",
                (ctx.tenant_id, booking_id),
            )
        ]
        for item in record["items"]:
            item.pop("promotions_json", None)
        record["payments"] = self.payments.list_for_booking(ctx, booking_id)
        record["tickets"] = self.tickets.list_for_booking(ctx, booking_id)
        if record.get("customer_id"):
            customer = self.customers.get(ctx, record["customer_id"], mask=mask)
            record["customer"] = customer
        return record

    def find_by_number(self, ctx: RequestContext, booking_number: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM bookings WHERE tenant_id = ? AND booking_number = ?",
            (ctx.tenant_id, booking_number.strip().upper()),
        )
        if row is None:
            raise NotFound(details={"entity": "booking"})
        return self.get_booking(ctx, row["id"])

    def list_bookings(
        self,
        ctx: RequestContext,
        *,
        venue_ids: list[str] | None = None,
        status: str | None = None,
        visit_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Bookings", "VIEW")
        scoped = venue_ids if venue_ids is not None else self.authz.scoped_venue_ids(ctx)
        sql = ["SELECT id FROM bookings WHERE tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if scoped is not None:
            if not scoped:
                return []
            sql.append(f"AND venue_id IN ({', '.join('?' for _ in scoped)})")
            params.extend(scoped)
        if status:
            sql.append("AND status = ?")
            params.append(status)
        if visit_date:
            sql.append("AND visit_date = ?")
            params.append(visit_date)
        sql.append("ORDER BY created_at DESC LIMIT ?")
        params.append(int(limit))
        return [self.get_booking(ctx, row["id"]) for row in self.db.query(" ".join(sql), params)]

    # ------------------------------------------------------------------ #
    # Manage Booking (R16)
    # ------------------------------------------------------------------ #

    def request_access_code(
        self, ctx: RequestContext, *, booking_number: str, email: str
    ) -> dict[str, Any]:
        """Start ownership verification.

        The response is identical whether the booking exists or not, which is what
        prevents enumeration (R16.3). A code is only actually sent when the
        booking-number/email pair matches.
        """
        self._rate_limit(ctx, bucket=f"manage:{booking_number.strip().upper()}")
        self._rate_limit(ctx, bucket=f"manage-ip:{ctx.ip_address or 'unknown'}")
        row = self.db.query_one(
            """
            SELECT b.id, b.venue_id, c.email_hash FROM bookings b
            LEFT JOIN customers c ON c.id = b.customer_id AND c.tenant_id = b.tenant_id
            WHERE b.tenant_id = ? AND b.booking_number = ?
            """,
            (ctx.tenant_id, booking_number.strip().upper()),
        )
        generic = {
            "sent": True,
            "message": "If that booking exists, we have emailed a verification code to the address on it.",
            "expires_in_minutes": self.config.get_int(ctx, "manage_booking.verification_ttl_minutes"),
        }
        if row is None or row["email_hash"] != hash_identifier(email):
            return generic
        code = human_code(6)
        ttl = self.config.get_int(ctx, "manage_booking.verification_ttl_minutes")
        now = self.clock.now()
        self.db.insert(
            "verification_challenges",
            {
                "id": new_id("vch"),
                "tenant_id": ctx.tenant_id,
                "booking_id": row["id"],
                "purpose": "MANAGE_BOOKING",
                "contact_hash": hash_identifier(email),
                "code_hash": hash_secret(code),
                "issued_at": to_iso(now),
                "expires_at": to_iso(add_minutes(now, ttl)),
            },
        )
        venue = self._venue(ctx, row["venue_id"])
        self._notify(
            ctx,
            event="ETICKET_DELIVERY",
            booking_id=row["id"],
            venue=venue,
            recipient=email,
            language=ctx.language,
            variables={"verification_code": code, "purpose": "Manage Booking"},
        )
        result = dict(generic)
        result["_code"] = code  # returned only to the caller that owns the request path
        return result

    def verify_access(
        self, ctx: RequestContext, *, booking_number: str, email: str, code: str
    ) -> dict[str, Any]:
        """Consume a one-time code. Single-use and short-lived (R16.11)."""
        self._rate_limit(ctx, bucket=f"verify:{booking_number.strip().upper()}")
        now = to_iso(self.clock.now())
        rows = self.db.query(
            """
            SELECT v.* FROM verification_challenges v
            JOIN bookings b ON b.id = v.booking_id AND b.tenant_id = v.tenant_id
            WHERE v.tenant_id = ? AND b.booking_number = ? AND v.contact_hash = ?
              AND v.purpose = 'MANAGE_BOOKING' AND v.consumed_at IS NULL AND v.expires_at > ?
            ORDER BY v.issued_at DESC
            """,
            (ctx.tenant_id, booking_number.strip().upper(), hash_identifier(email), now),
        )
        for row in rows:
            if verify_secret(code, row["code_hash"]):
                self.db.update(
                    "verification_challenges", row["id"], {"consumed_at": now}, tenant_id=ctx.tenant_id
                )
                booking = self.get_booking(ctx, row["booking_id"], mask=False)
                self.audit.record(
                    ctx.for_venue(booking["venue_id"]),
                    "LOGIN",
                    target_type="booking",
                    target_id=booking["id"],
                    new={"purpose": "manage_booking_verified", "channel": ctx.channel},
                )
                return {"verified": True, "booking_id": booking["id"]}
        # A wrong code is not the same thing as abuse. R16.3 asks for exponential
        # backoff *after repeated failures*, so the first mistyped digit gets a plain
        # "that code is not valid" and only a persistent attacker gets throttled.
        # Telling a customer "too many attempts" on their first try is both wrong and
        # the kind of thing that generates a support call.
        attempts = 0
        for row in rows:
            attempts = max(attempts, int(row["attempts"]) + 1)
            self.db.update(
                "verification_challenges", row["id"], {"attempts": int(row["attempts"]) + 1},
                tenant_id=ctx.tenant_id,
            )
        allowed = self.config.get_int(ctx, "manage_booking.max_verification_attempts")
        if attempts >= allowed:
            # Burn the outstanding challenges so a guessed code cannot land later.
            for row in rows:
                self.db.update(
                    "verification_challenges", row["id"], {"consumed_at": now}, tenant_id=ctx.tenant_id
                )
            self.audit.security(
                ctx,
                "LOGIN_FAILED",
                target_type="booking",
                target_id=booking_number.strip().upper(),
                detail={"purpose": "manage_booking", "attempts": attempts},
            )
            raise RateLimited(
                60,
                message="Too many incorrect codes. Please request a new one.",
                code="verification_locked",
            )
        raise ValidationError(
            {"code": "That code is not correct. Please check it and try again."},
            message="That code is not correct. Please check it and try again.",
            code="verification_failed",
        )

    def manage_view(self, ctx: RequestContext, booking_id: str, *, language: str | None = None) -> dict[str, Any]:
        """Everything the verified customer may see and do (R16.4)."""
        lang = language or ctx.language
        booking = self.get_booking(ctx, booking_id, mask=False)
        venue = self._venue(ctx, booking["venue_id"])
        policy = self.reschedule_and_cancel_policy(ctx, booking_id)
        tickets = [
            self.tickets.presentation(ctx, ticket["id"], language=lang) for ticket in booking["tickets"]
        ]
        used = self.tickets.any_used(ctx, booking_id)
        return {
            "booking_number": booking["booking_number"],
            "status": booking["status"],
            "visit_date": booking["visit_date"],
            "venue": i18n_text(venue["name"], lang, fallback=venue["code"]),
            "total_minor": int(booking["net_minor"]),
            "currency": booking["currency"],
            "tickets": tickets,
            "used_tickets": used,
            "policy": policy,
            "actions": {
                "download_tickets": True,
                "view_qr": True,
                "resend_ticket_email": True,
                "request_tax_invoice": booking["status"] == "CONFIRMED",
                "reschedule": policy["reschedule"]["allowed"],
                "cancel": policy["cancel"]["allowed"],
                "manage_consent": True,
                "view_show_timetable": True,
            },
        }

    def reschedule_and_cancel_policy(self, ctx: RequestContext, booking_id: str) -> dict[str, Any]:
        """Resolve eligibility, deadline, fee and refundable amount (R16.5, R16.6)."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])
        used = self.tickets.any_used(ctx, booking_id)
        policy = self.config.get(ctx, "refund.policy", venue_id=venue["id"]) or {}
        hours_before = self._hours_until_visit(booking, venue)
        tier = self._refund_tier(policy, hours_before)
        net = int(booking["net_minor"])
        already = int(booking["refunded_minor"] or 0)
        refundable = max(
            apply_percentage(net, int(tier.get("refund_percent_bp", 0))) - int(tier.get("fee_minor", 0)) - already,
            0,
        )
        reschedule_enabled = self.config.get_bool(
            ctx, "manage_booking.reschedule_enabled", venue_id=venue["id"]
        )
        cancel_enabled = self.config.get_bool(ctx, "manage_booking.cancel_enabled", venue_id=venue["id"])
        active = booking["status"] == "CONFIRMED"
        blocked_reason = None
        if not active:
            blocked_reason = f"This booking is {booking['status'].lower()}."
        elif used:
            blocked_reason = "One or more tickets have already been used."
        return {
            "hours_before_visit": round(hours_before, 2),
            "reschedule": {
                "allowed": bool(reschedule_enabled and active and not used),
                "reason": blocked_reason if not (reschedule_enabled and active and not used) else None,
            },
            "cancel": {
                "allowed": bool(cancel_enabled and active and not used),
                "reason": blocked_reason if not (cancel_enabled and active and not used) else None,
                "refund_percent_bp": int(tier.get("refund_percent_bp", 0)),
                "fee_minor": int(tier.get("fee_minor", 0)),
                "refundable_minor": refundable,
                "deadline_note": self._deadline_note(policy, venue),
            },
            "used_tickets": used,
            "partial_action_allowed": bool(used) and bool(policy.get("allow_partial_when_used", True)),
        }

    def _hours_until_visit(self, booking: dict[str, Any], venue: dict[str, Any]) -> float:
        if not booking.get("visit_date"):
            return 9999.0
        hours = (venue.get("operating_hours") or {}).get("default", {})
        start = combine_local(booking["visit_date"], hours.get("open", "00:00"), venue["timezone"])
        if booking.get("session_id"):
            session = self.db.query_one(
                "SELECT date, start_time FROM sessions WHERE id = ?", (booking["session_id"],)
            )
            if session is not None:
                start = combine_local(session["date"], session["start_time"], venue["timezone"])
        return minutes_between(self.clock.now(), start) / 60.0

    @staticmethod
    def _refund_tier(policy: dict[str, Any], hours_before: float) -> dict[str, Any]:
        tiers = sorted(
            policy.get("tiers", []), key=lambda t: int(t.get("min_hours_before", 0)), reverse=True
        )
        for tier in tiers:
            if hours_before >= int(tier.get("min_hours_before", 0)):
                return tier
        return {"refund_percent_bp": 0, "fee_minor": 0}

    @staticmethod
    def _deadline_note(policy: dict[str, Any], venue: dict[str, Any]) -> str | None:
        tiers = sorted(policy.get("tiers", []), key=lambda t: int(t.get("min_hours_before", 0)))
        full = [t for t in tiers if int(t.get("refund_percent_bp", 0)) == 10000]
        if not full:
            return None
        return f"Full refund up to {int(full[0]['min_hours_before'])} hours before your visit."

    # ------------------------------------------------------------------ #
    # Reschedule (R16.7)
    # ------------------------------------------------------------------ #

    def reschedule(
        self,
        ctx: RequestContext,
        booking_id: str,
        *,
        new_visit_date: str,
        new_session_id: str | None = None,
        actor_is_staff: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Move a booking, acquiring the target before releasing the original (R16.7)."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])
        if actor_is_staff:
            self.authz.require_action(
                ctx.for_venue(venue["id"]), "RESCHEDULE", target_type="booking", target_id=booking_id
            )
        policy = self.reschedule_and_cancel_policy(ctx, booking_id)
        if not policy["reschedule"]["allowed"]:
            raise RuleViolation(
                policy["reschedule"]["reason"] or "This booking cannot be rescheduled.",
                details={"booking_id": booking_id, "used_tickets": policy["used_tickets"]},
            )
        items = self.db.query(
            "SELECT * FROM booking_items WHERE tenant_id = ? AND booking_id = ? AND state = 'ACTIVE'",
            (ctx.tenant_id, booking_id),
        )
        previous = {"visit_date": booking["visit_date"], "session_id": booking["session_id"]}

        # Acquire first. If anything fails, nothing has been released, so the original
        # booking is untouched.
        acquired: list[tuple[str, int]] = []
        try:
            self.calendar.assert_bookable(
                ctx,
                venue=venue,
                date=new_visit_date,
                channel=booking["channel"],
                product_id=items[0]["product_id"] if items else None,
                session_id=new_session_id,
            )
            for item in items:
                product = self.catalog.get_product(ctx, item["product_id"])
                ticket_type = self.catalog.get_ticket_type(ctx, item["ticket_type_id"])
                if not ticket_type["consumes_capacity"]:
                    continue
                rules = self.calendar.resolve_rules(
                    ctx,
                    venue_id=venue["id"],
                    channel=booking["channel"],
                    experience_id=product.get("experience_id"),
                    product_id=product["id"],
                    session_id=new_session_id,
                )
                target = self.inventory.resolve_inventory_session(
                    ctx,
                    venue=venue,
                    product=product,
                    date=new_visit_date,
                    channel=booking["channel"],
                    session_id=new_session_id,
                    rules=rules,
                )
                if target is None:
                    continue
                self.inventory.assert_session_bookable(
                    ctx, target, timezone=venue["timezone"], quantity=int(item["quantity"])
                )
                self.inventory.confirm_without_hold(
                    ctx.with_channel(booking["channel"]),
                    session_id=target["id"],
                    quantity=int(item["quantity"]),
                    partner_id=booking["partner_id"],
                )
                acquired.append((target["id"], int(item["quantity"])))
        except (JustSoldOut, NotAvailable, RuleViolation) as exc:
            for session_id, quantity in acquired:
                self.inventory.release_confirmed(
                    ctx.with_channel(booking["channel"]), session_id=session_id, quantity=quantity
                )
            raise NotAvailable(
                "That date or time is not available, so your original booking has not been changed.",
                details={
                    "booking_id": booking_id,
                    "requested_date": new_visit_date,
                    "original_unchanged": True,
                    "reason_code": getattr(exc, "code", "not_available"),
                },
            ) from exc

        released: list[str] = []
        with self.db.transaction():
            for item in items:
                if item["session_id"]:
                    self.inventory.release_confirmed(
                        ctx.with_channel(booking["channel"]),
                        session_id=item["session_id"],
                        quantity=int(item["quantity"]),
                        partner_id=booking["partner_id"],
                    )
                    released.append(item["session_id"])
            target_session_id = acquired[0][0] if acquired else new_session_id
            self.db.update(
                "bookings",
                booking_id,
                {"visit_date": new_visit_date, "session_id": target_session_id},
                tenant_id=ctx.tenant_id,
            )
            for index, item in enumerate(items):
                new_session = acquired[index][0] if index < len(acquired) else target_session_id
                self.db.update(
                    "booking_items", item["id"], {"session_id": new_session}, tenant_id=ctx.tenant_id
                )
            self.audit.record(
                ctx.for_venue(venue["id"]),
                "BOOKING_RESCHEDULED",
                target_type="booking",
                target_id=booking_id,
                previous=previous,
                new={"visit_date": new_visit_date, "session_id": target_session_id},
                reason=reason,
                venue_timezone=venue["timezone"],
            )

        # Reissue tickets so superseded QR codes stop working (R16.9).
        reissued = []
        for ticket in self.tickets.list_for_booking(ctx, booking_id):
            if ticket["state"] in ("CANCELLED", "VOIDED", "REFUNDED"):
                continue
            self.tickets.refresh_validity(
                ctx,
                ticket["id"],
                venue=venue,
                visit_date=new_visit_date,
                session_id=acquired[0][0] if acquired else new_session_id,
            )
            reissued.append(
                self.tickets.reissue(ctx, ticket["id"], reason=reason or "Booking rescheduled")["id"]
            )

        customer_id = booking.get("customer_id")
        self._notify(
            ctx,
            event="BOOKING_RESCHEDULED",
            booking_id=booking_id,
            venue=venue,
            recipient=self.customers.contact_email(ctx, customer_id) if customer_id else None,
            language=booking["language"],
            variables={
                "previous_visit_date": previous["visit_date"],
                "new_visit_date": new_visit_date,
            },
        )
        return {
            "booking_id": booking_id,
            "previous": previous,
            "new_visit_date": new_visit_date,
            "new_session_id": acquired[0][0] if acquired else new_session_id,
            "tickets_reissued": reissued,
        }

    # ------------------------------------------------------------------ #
    # Cancel / void / refund (R17)
    # ------------------------------------------------------------------ #

    def cancel(
        self,
        ctx: RequestContext,
        booking_id: str,
        *,
        reason: str,
        confirmed: bool = False,
        actor_is_staff: bool = False,
        refund: bool = True,
        ticket_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Cancel a booking or specific tickets, then refund per policy (R17.1)."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])
        if actor_is_staff:
            self.authz.require_action(
                ctx.for_venue(venue["id"]),
                "CANCEL_BOOKING",
                target_type="booking",
                target_id=booking_id,
                reason=reason,
            )
        if booking["status"] not in ("CONFIRMED", "PARTIALLY_REFUNDED"):
            raise ConflictError(
                f"This booking is already {booking['status'].lower()}.",
                details={"status": booking["status"]},
            )
        policy = self.reschedule_and_cancel_policy(ctx, booking_id)
        tickets = self.tickets.list_for_booking(ctx, booking_id)
        targets = [t for t in tickets if ticket_ids is None or t["id"] in set(ticket_ids)]
        used = [t for t in targets if int(t["entries_used"]) > 0]
        if used and not actor_is_staff:
            # R16.8 — self-service cannot cancel a used ticket, but may process the rest.
            targets = [t for t in targets if int(t["entries_used"]) == 0]
            if not targets:
                raise RuleViolation(
                    "Those tickets have already been used, so they cannot be cancelled here.",
                    details={
                        "used_tickets": [
                            {"ticket_number": t["ticket_number"], "used_at": t["last_entry_at"]} for t in used
                        ]
                    },
                )
        refundable = int(policy["cancel"]["refundable_minor"]) if refund else 0
        if ticket_ids is not None and tickets:
            refundable = int(refundable * len(targets) / len(tickets))
        if not confirmed:
            # R17.8 / R67.2 — state amount, scope and irreversibility before acting.
            raise ConfirmationRequired(
                "Cancelling cannot be undone.",
                details={
                    "booking_number": booking["booking_number"],
                    "tickets_affected": [t["ticket_number"] for t in targets],
                    "ticket_count": len(targets),
                    "refund_amount_minor": refundable,
                    "currency": booking["currency"],
                    "fee_minor": int(policy["cancel"]["fee_minor"]),
                    "irreversible": True,
                    "performed_action": "CANCEL",
                },
            )
        now = to_iso(self.clock.now())
        partial = ticket_ids is not None and len(targets) < len(tickets)
        with self.db.transaction():
            self.tickets.bulk_set_state(
                ctx, [t["id"] for t in targets], "CANCELLED", reason=reason, audit_action="BOOKING_CANCELLED"
            )
            if not partial:
                self.db.update(
                    "bookings",
                    booking_id,
                    {"status": "CANCELLED", "cancelled_at": now, "cancel_reason": reason},
                    tenant_id=ctx.tenant_id,
                )
                self.db.execute(
                    "UPDATE booking_items SET state = 'CANCELLED' WHERE tenant_id = ? AND booking_id = ?",
                    (ctx.tenant_id, booking_id),
                )
            self.audit.record(
                ctx.for_venue(venue["id"]),
                "BOOKING_CANCELLED",
                target_type="booking",
                target_id=booking_id,
                previous={"status": booking["status"]},
                new={
                    "status": "CANCELLED" if not partial else booking["status"],
                    "tickets_cancelled": len(targets),
                    "partial": partial,
                },
                reason=reason,
                severity="WARNING",
                venue_timezone=venue["timezone"],
            )
        self._restore_capacity(ctx, booking, venue, targets)
        if not partial:
            self.promotions.restore_redemptions(ctx, booking_id=booking_id, reason="cancelled")
            if self.members is not None:
                self.members.restore_for_booking(ctx, booking_id=booking_id, reason="cancelled")

        refund_result = None
        if refund and refundable > 0:
            refund_result = self.refund(
                ctx,
                booking_id,
                amount_minor=refundable,
                reason=f"Cancellation: {reason}",
                confirmed=True,
                actor_is_staff=actor_is_staff,
                ticket_ids=[t["id"] for t in targets],
                skip_capacity_restore=True,
            )
        customer_id = booking.get("customer_id")
        self._notify(
            ctx,
            event="BOOKING_CANCELLED",
            booking_id=booking_id,
            venue=venue,
            recipient=self.customers.contact_email(ctx, customer_id) if customer_id else None,
            language=booking["language"],
            variables={"reason": reason, "refund_amount_minor": refundable},
        )
        return {
            "booking_id": booking_id,
            "performed": "CANCEL",
            "partial": partial,
            "tickets_cancelled": [t["ticket_number"] for t in targets],
            "refund": refund_result,
        }

    def _restore_capacity(
        self,
        ctx: RequestContext,
        booking: dict[str, Any],
        venue: dict[str, Any],
        tickets: Sequence[dict[str, Any]],
    ) -> None:
        """R17.5 — give future-dated capacity back where configured."""
        policy = self.config.get(ctx, "refund.policy", venue_id=venue["id"]) or {}
        if not policy.get("restore_capacity", True):
            return
        today = to_iso(self.clock.now())[:10]
        if booking.get("visit_date") and booking["visit_date"] < today:
            return
        counts: dict[str, int] = {}
        for ticket in tickets:
            if ticket.get("session_id"):
                counts[ticket["session_id"]] = counts.get(ticket["session_id"], 0) + 1
        for session_id, quantity in counts.items():
            self.inventory.release_confirmed(
                ctx.with_channel(booking["channel"]),
                session_id=session_id,
                quantity=quantity,
                partner_id=booking["partner_id"],
            )
            offer = self.inventory.offer_waiting_list(ctx, session_id)
            if offer and self.notifications is not None:
                self.notifications.enqueue(
                    ctx,
                    event_type="WAITING_LIST_OFFER",
                    booking_id=None,
                    recipient=None,
                    language=ctx.language,
                    venue_id=venue["id"],
                    extra_variables=offer,
                    contact_hash=offer.get("contact_hash"),
                )
        if self.seating is not None:
            self.seating.release_reservations_for_tickets(ctx, [t["id"] for t in tickets])

    def refund(
        self,
        ctx: RequestContext,
        booking_id: str,
        *,
        amount_minor: int,
        reason: str,
        confirmed: bool = False,
        actor_is_staff: bool = True,
        ticket_ids: Sequence[str] | None = None,
        approver_id: str | None = None,
        skip_capacity_restore: bool = False,
    ) -> dict[str, Any]:
        """Return money, enforcing the aggregate ceiling and the used-ticket rule."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])
        scoped = ctx.for_venue(venue["id"])
        if actor_is_staff:
            self.authz.require_action(scoped, "REFUND", target_type="booking", target_id=booking_id, reason=reason)

        collected = self.payments.captured_total(ctx, booking_id)
        already = int(booking["refunded_minor"] or 0)
        if int(amount_minor) <= 0:
            raise ValidationError({"amount_minor": "Enter an amount greater than zero."})
        if already + int(amount_minor) > collected:
            # R17.6 — never refund more than was collected, in aggregate.
            raise ConflictError(
                "That would refund more than was collected for this booking.",
                details={
                    "collected_minor": collected,
                    "already_refunded_minor": already,
                    "requested_minor": int(amount_minor),
                    "maximum_minor": max(collected - already, 0),
                },
            )

        tickets = self.tickets.list_for_booking(ctx, booking_id)
        targets = [t for t in tickets if ticket_ids is None or t["id"] in set(ticket_ids)]
        used = [t for t in targets if int(t["entries_used"]) > 0]
        if used:
            # R17.3 — refunding a used ticket needs REFUND *plus* APPROVE and a reason.
            self.authz.require_action(
                scoped, "APPROVE", target_type="booking", target_id=booking_id, reason=reason
            )
            if not (reason or "").strip():
                raise ValidationError({"reason": "A reason is mandatory when refunding a used ticket."})
        if not confirmed:
            raise ConfirmationRequired(
                "Refunds cannot be undone.",
                details={
                    "booking_number": booking["booking_number"],
                    "amount_minor": int(amount_minor),
                    "currency": booking["currency"],
                    "tickets_affected": [t["ticket_number"] for t in targets],
                    "used_tickets": [t["ticket_number"] for t in used],
                    "irreversible": True,
                    "performed_action": "REFUND",
                },
            )

        payments = [p for p in self.payments.list_for_booking(ctx, booking_id) if p["status"] in ("AUTHORIZED", "CAPTURED")]
        if not payments:
            raise ConflictError("There is no captured payment to refund on this booking.")
        payment = payments[0]
        refund_id = new_id("rfd")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.insert(
                "refunds",
                {
                    "id": refund_id,
                    "tenant_id": ctx.tenant_id,
                    "booking_id": booking_id,
                    "payment_id": payment["payment_id"],
                    "kind": "REFUND",
                    "amount_minor": int(amount_minor),
                    "status": "PENDING",
                    "reason": reason,
                    "tickets_json": [t["id"] for t in targets],
                    "actor_id": ctx.principal.id or "system",
                    "approver_id": approver_id,
                    "created_at": now,
                },
            )
            self.audit.record(
                scoped,
                "REFUND",
                target_type="booking",
                target_id=booking_id,
                new={
                    "refund_id": refund_id,
                    "amount_minor": int(amount_minor),
                    "tickets": [t["ticket_number"] for t in targets],
                    "used_ticket_override": bool(used),
                    "approver_id": approver_id,
                },
                reason=reason,
                severity="WARNING",
                venue_timezone=venue["timezone"],
            )

        result = self.payments.execute_refund(
            ctx,
            refund_id=refund_id,
            payment_id=payment["payment_id"],
            amount_minor=int(amount_minor),
            currency=booking["currency"],
        )
        if result["status"] != "COMPLETED":
            # R17.7 — retryable, and the booking is NOT marked refunded.
            return {
                "refund_id": refund_id,
                "status": result["status"],
                "amount_minor": int(amount_minor),
                "booking_status_unchanged": True,
                "failure_code": result.get("failure_code"),
            }

        total_refunded = already + int(amount_minor)
        with self.db.transaction():
            self.db.update(
                "bookings",
                booking_id,
                {
                    "refunded_minor": total_refunded,
                    "status": "REFUNDED" if total_refunded >= collected else "PARTIALLY_REFUNDED",
                },
                tenant_id=ctx.tenant_id,
            )
            # R17.4 — invalidate exactly the refunded tickets.
            self.tickets.bulk_set_state(
                ctx, [t["id"] for t in targets], "REFUNDED", reason=reason, audit_action="REFUND"
            )
        if not skip_capacity_restore:
            self._restore_capacity(ctx, booking, venue, targets)
            self.promotions.restore_redemptions(ctx, booking_id=booking_id, reason="refunded")
            if self.members is not None:
                self.members.restore_for_booking(ctx, booking_id=booking_id, reason="refunded")

        customer_id = booking.get("customer_id")
        self._notify(
            ctx,
            event="REFUND_COMPLETED",
            booking_id=booking_id,
            venue=venue,
            recipient=self.customers.contact_email(ctx, customer_id) if customer_id else None,
            language=booking["language"],
            variables={
                "refund_amount_minor": int(amount_minor),
                "refund_method": payment["method"],
                "refund_reference": refund_id,
            },
        )
        return {
            "refund_id": refund_id,
            "status": "COMPLETED",
            "amount_minor": int(amount_minor),
            "total_refunded_minor": total_refunded,
            "booking_status": "REFUNDED" if total_refunded >= collected else "PARTIALLY_REFUNDED",
            "tickets_invalidated": [t["ticket_number"] for t in targets],
        }

    def void(
        self,
        ctx: RequestContext,
        booking_id: str,
        *,
        reason: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Same-day reversal before settlement — distinct from cancel and refund (R17.1)."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])
        scoped = ctx.for_venue(venue["id"])
        self.authz.require_action(scoped, "VOID", target_type="booking", target_id=booking_id, reason=reason)
        today = to_iso(self.clock.now())[:10]
        if (booking["confirmed_at"] or "")[:10] != today:
            raise ConflictError(
                "A void applies only to a sale made today. Use a refund instead.",
                details={"confirmed_on": (booking["confirmed_at"] or "")[:10], "today": today},
            )
        tickets = self.tickets.list_for_booking(ctx, booking_id)
        if not confirmed:
            raise ConfirmationRequired(
                "Voiding cannot be undone.",
                details={
                    "booking_number": booking["booking_number"],
                    "amount_minor": int(booking["net_minor"]),
                    "currency": booking["currency"],
                    "ticket_count": len(tickets),
                    "irreversible": True,
                    "performed_action": "VOID",
                },
            )
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "bookings",
                booking_id,
                {"status": "VOIDED", "cancelled_at": now, "cancel_reason": reason},
                tenant_id=ctx.tenant_id,
            )
            self.db.execute(
                "UPDATE booking_items SET state = 'VOID' WHERE tenant_id = ? AND booking_id = ?",
                (ctx.tenant_id, booking_id),
            )
            self.tickets.bulk_set_state(
                ctx, [t["id"] for t in tickets], "VOIDED", reason=reason, audit_action="VOID"
            )
            self.audit.record(
                scoped,
                "VOID",
                target_type="booking",
                target_id=booking_id,
                previous={"status": booking["status"]},
                new={"status": "VOIDED", "ticket_count": len(tickets)},
                reason=reason,
                severity="WARNING",
                venue_timezone=venue["timezone"],
            )
        for payment in self.payments.list_for_booking(ctx, booking_id):
            if payment["status"] in ("AUTHORIZED", "CAPTURED"):
                self.payments.void_payment(ctx, payment["payment_id"], reason=reason)
        self._restore_capacity(ctx, booking, venue, tickets)
        self.promotions.restore_redemptions(ctx, booking_id=booking_id, reason="voided")
        if self.members is not None:
            self.members.restore_for_booking(ctx, booking_id=booking_id, reason="voided")
        return {"booking_id": booking_id, "performed": "VOID", "tickets_voided": len(tickets)}

    def delete(self, ctx: RequestContext, booking_id: str, *, reason: str) -> dict[str, Any]:
        """DELETE on a booking. Always executes as Cancel (R46.2, R67.6)."""
        self.authz.require_page(ctx, "Bookings", "DELETE", target_type="booking", target_id=booking_id)
        result = self.cancel(ctx, booking_id, reason=reason, confirmed=True, actor_is_staff=True)
        return {
            "requested": "DELETE",
            "performed": "CANCEL",
            "explanation": "Bookings are retained for audit and finance; DELETE cancels the booking.",
            **result,
        }

    # ------------------------------------------------------------------ #

    def _rate_limit(self, ctx: RequestContext, *, bucket: str, limit: int | None = None) -> None:
        """Fixed-window counter with exponential backoff signalling (R16.3, R73.5)."""
        ceiling = limit or self.config.get_int(ctx, "manage_booking.max_attempts_per_hour")
        window = to_iso(self.clock.now())[:13]  # hour granularity
        key = f"{ctx.tenant_id}:{bucket}"
        row = self.db.query_one(
            "SELECT id, count FROM rate_limit_counters WHERE bucket = ? AND window_start = ?", (key, window)
        )
        if row is None:
            self.db.insert(
                "rate_limit_counters",
                {"id": new_id("rlc"), "bucket": key, "window_start": window, "count": 1},
            )
            return
        count = int(row["count"]) + 1
        self.db.update("rate_limit_counters", row["id"], {"count": count})
        if count > ceiling:
            backoff = min(60 * (2 ** (count - ceiling)), 3600)
            self.audit.security(
                ctx, "AUTHORIZATION_DENIED", reason="rate_limited", detail={"bucket": bucket, "count": count}
            )
            raise RateLimited(backoff)


__all__ = ["FLOW_STEPS", "BookingService", "Quote", "QuoteLineRequest"]
