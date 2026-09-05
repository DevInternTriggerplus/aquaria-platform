"""Booking orchestration: quote → checkout → confirm → e-ticket.

Confirm runs in this order, and the order is the requirement:

  1. revalidate dates, prices and promotions (R13.7);
  2. consent, before any personal data is persisted (R12.2);
  3. persist the customer;
  4. take payment;
  5. convert holds into confirmed capacity (or re-acquire late, R10.9);
  6. write the booking, its items and its tickets;
  7. return the result.

Step 5 is where R10.8 lives: if payment succeeded but the hold lapsed and inventory
has gone, the booking is *not* confirmed. It goes to RECONCILIATION and an automatic
refund is initiated. If equivalent inventory is still free, the capacity is
re-acquired and the late confirmation is recorded.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Product, TicketType
from apps.core.errors import (
    ConfirmationRequired,
    ConflictError,
    HoldExpired,
    JustSoldOut,
    PaymentFailed,
    ValidationError,
)
from apps.core.ids import booking_number as make_booking_number, secure_token
from apps.core.models import AuditEvent
from apps.inventory.models import Session, acquire_hold, confirm_hold
from apps.payments.gateway import PaymentGateway
from apps.payments.services import start_payment
from apps.pricing.services import resolve_price
from apps.tenancy.models import Venue
from apps.ticketing.services import issue_for_booking
from apps.venuesettings.services import compute_order_charges

from . import consent_service
from .models import Booking, BookingItem, Customer


# ======================================================================== #
# Quote
# ======================================================================== #


@dataclass
class QuoteLine:
    ticket_type_id: str
    quantity: int
    session_id: str | None = None


@dataclass
class QuoteResult:
    """The response from a quote request: what the customer would pay, and the
    server-side state needed to confirm it."""

    venue: Venue
    visit_date: dt.date
    lines: list[dict[str, Any]]
    total_minor: int
    currency: str
    charges: dict[str, Any]
    holds: list[Any]
    cart_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "visit_date": self.visit_date.isoformat(),
            "currency": self.currency,
            "total_minor": self.total_minor,
            "summary": {
                "visit_date": self.visit_date.isoformat(),
                "currency": self.currency,
                "gross_minor": sum(l["gross_minor"] for l in self.lines),
                "lines": self.lines,
            },
            "charges": self.charges,
            "holds": [
                {"id": h.id, "session_id": h.session_id, "remaining_seconds": max(
                    int((h.expires_at - timezone.now()).total_seconds()), 0
                )} for h in self.holds if h is not None
            ],
            "cart_id": self.cart_id,
        }


def quote(
    *,
    venue: Venue,
    visit_date: dt.date,
    lines: list[QuoteLine],
    channel: str = "ONLINE",
    cart_id: str | None = None,
) -> QuoteResult:
    """Resolve prices, acquire holds, compute charges.

    For uncapped inventory (Aquaria's general admission) no hold is created, which is
    correct — a hold on inventory that can't sell out wastes a database row and a
    timeout. Holds are created only for capacity-controlled sessions (R10.1).
    """
    if not lines:
        raise ValidationError({"lines": "Choose at least one ticket."})

    cart_id = cart_id or secure_token(16)
    result_lines: list[dict[str, Any]] = []
    holds: list[Any] = []
    gross_minor = 0

    for i, line in enumerate(lines):
        if line.quantity < 1:
            raise ValidationError({f"lines[{i}].quantity": "Choose at least one ticket."})

        tt = TicketType.objects.select_related("product", "segment").filter(
            tenant=venue.tenant, id=line.ticket_type_id, status="ACTIVE"
        ).first()
        if tt is None:
            raise ValidationError({f"lines[{i}].ticket_type_id": "Ticket type not found."})

        product = tt.product
        if not product.allows_channel(channel):
            raise ValidationError({f"lines[{i}]": "That item is not sold through this channel."})

        # Quantity limits (R11.13, R3.5).
        maximum = tt.max_quantity or product.max_per_booking
        if maximum and line.quantity > maximum:
            raise ValidationError(
                {f"lines[{i}].quantity": f"Up to {maximum} of this ticket per booking."},
                message=f"Maximum {maximum} for {tt.code}.",
            )

        rule = resolve_price(
            venue=venue,
            ticket_type_id=tt.id,
            on_date=visit_date,
            channel=channel,
            quantity=line.quantity,
            session_id=line.session_id,
        )
        if rule is None:
            raise ValidationError(
                {f"lines[{i}]": "No price is available for this ticket on the selected date."},
                message=f"{tt.code} is not available for {visit_date}.",
            )

        line_gross = rule.amount_minor * line.quantity
        gross_minor += line_gross

        # Hold capacity-controlled inventory only (analysis C.1).
        hold = None
        if line.session_id:
            session = Session.objects.filter(
                tenant=venue.tenant, id=line.session_id, venue=venue
            ).first()
            if session and not session.is_uncapped:
                hold = acquire_hold(
                    session=session,
                    quantity=line.quantity,
                    cart_ref=cart_id,
                    channel=channel,
                )

        result_lines.append({
            "ticket_type_id": tt.id,
            "ticket_type_code": tt.code,
            "product_id": product.id,
            "product_code": product.code,
            "segment_id": tt.segment_id,
            "segment_code": tt.segment.code if tt.segment else "",
            "quantity": line.quantity,
            "unit_price_minor": rule.amount_minor,
            "gross_minor": line_gross,
            "currency": rule.currency,
            "price_rule_id": rule.id,
            "session_id": line.session_id,
            "hold_id": hold.id if hold else None,
        })
        holds.append(hold)

    breakdown = compute_order_charges(
        venue=venue, base_minor=gross_minor, on_date=visit_date
    )

    return QuoteResult(
        venue=venue,
        visit_date=visit_date,
        lines=result_lines,
        total_minor=breakdown.grand_total_minor,
        currency=venue.currency,
        charges=breakdown.as_dict(),
        holds=[h for h in holds if h is not None],
        cart_id=cart_id,
    )


# ======================================================================== #
# Confirm
# ======================================================================== #


def confirm(
    *,
    quote_result: QuoteResult,
    customer_data: dict[str, str],
    consent_items: dict[str, bool],
    payment_method: str,
    gateway: PaymentGateway,
    idempotency_key: str = "",
    channel: str = "ONLINE",
    language: str = "en",
) -> dict[str, Any]:
    """Turn a quote into a confirmed booking with issued tickets.

    Ordering matches the confirmed path in the reference implementation. The booking
    is persisted in ``AWAITING_PAYMENT`` before the gateway is called, so a dropped
    connection after authorization still produces a confirmed booking and a ticket on
    the webhook path.
    """
    venue = quote_result.venue
    tenant = venue.tenant
    email = (customer_data.get("email") or "").strip().lower()
    if "@" not in email:
        raise ValidationError({"email": "Enter a valid email address so we can send your ticket."})

    # --- 1. consent, before any personal data is persisted (R12.2) -------- #
    consent_service.check_required(consent_items)

    # --- 2. revalidate charges -------------------------------------------- #
    breakdown = compute_order_charges(
        venue=venue, base_minor=sum(l["gross_minor"] for l in quote_result.lines),
        on_date=quote_result.visit_date,
    )
    total_minor = breakdown.grand_total_minor

    idempotency_key = idempotency_key or secure_token(16)

    # Check for an already-confirmed booking with this key (R14.3).
    existing = Booking.objects.filter(
        tenant=tenant, idempotency_key=idempotency_key
    ).first()
    if existing and existing.is_confirmed:
        tickets = list(existing.tickets.all())
        return _confirmed_result(existing, tickets, already=True)

    with transaction.atomic():
        # --- 3. persist the customer -------------------------------------- #
        customer, _ = Customer.objects.get_or_create(
            tenant=tenant, email=email,
            defaults={
                "full_name": customer_data.get("full_name", ""),
                "phone": customer_data.get("phone", ""),
                "language": language,
            },
        )

        consent_record = consent_service.capture(
            tenant=tenant,
            venue=venue,
            items=consent_items,
            language=language,
            channel=channel,
            contact=email,
            customer=customer,
        )

        # --- 4. persist the booking as AWAITING_PAYMENT ------------------- #
        bkg = Booking.objects.create(
            tenant=tenant,
            booking_number=make_booking_number(venue.code.upper()),
            venue=venue,
            customer=customer,
            channel=channel,
            visit_date=quote_result.visit_date,
            status="AWAITING_PAYMENT",
            language=language,
            currency=venue.currency,
            gross_minor=sum(l["gross_minor"] for l in quote_result.lines),
            discount_minor=breakdown.line_discount_minor + breakdown.order_discount_minor,
            service_charge_minor=breakdown.service_charge_minor,
            tax_minor=breakdown.vat_minor,
            rounding_adjustment_minor=breakdown.rounding_adjustment_minor,
            total_minor=total_minor,
            amount_paid_minor=0,
            charge_snapshot=breakdown.snapshot(),
            idempotency_key=idempotency_key,
            cart_ref=quote_result.cart_id,
        )

        for line in quote_result.lines:
            BookingItem.objects.create(
                tenant=tenant,
                booking=bkg,
                product_id=line["product_id"],
                ticket_type_id=line["ticket_type_id"],
                segment_id=line.get("segment_id"),
                session_id=line.get("session_id"),
                quantity=line["quantity"],
                unit_price_minor=line["unit_price_minor"],
                gross_minor=line["gross_minor"],
                currency=line["currency"],
                price_rule_id=line.get("price_rule_id"),
            )

    # --- 5. take payment (outside the booking transaction) --------- #
    payment = start_payment(
        booking=bkg,
        amount_minor=total_minor,
        currency=venue.currency,
        method=payment_method,
        channel=channel,
        gateway=gateway,
        idempotency_key=idempotency_key,
    )

    if payment.status != "AUTHORIZED":
        # The hold stays alive so the customer can retry with a new key (R14.8).
        raise PaymentFailed(
            "The payment could not be completed. Please try another method.",
            details={"failure_code": payment.failure_code},
        )

    # --- 6. finalize: capacity + booking status + tickets ----------- #
    return finalize_paid_booking(booking=bkg, payment=payment, gateway=gateway)


def finalize_paid_booking(
    *, booking: Booking, payment, gateway: PaymentGateway | None = None
) -> dict[str, Any]:
    """Idempotent completion: confirm capacity, set CONFIRMED, issue tickets.

    Called by the inline path and by the webhook handler, so both produce the
    same result for the same booking.
    """
    if booking.is_confirmed:
        tickets = list(booking.tickets.all())
        return _confirmed_result(booking, tickets, already=True)

    venue = booking.venue

    # Convert holds into confirmed capacity.
    late = False
    for item in booking.items.select_related("session").all():
        if not item.session_id or item.session is None:
            continue
        if item.session.is_uncapped:
            continue
        # This booking's own holds only: scoped to its cart, so one cart's hold is
        # never consumed by another's confirmation (R10.10).
        holds = list(item.session.holds.filter(
            cart_ref=booking.cart_ref,
            state="HELD",
        ).order_by("created_at"))
        if holds:
            try:
                was_late = confirm_hold(holds[0])
                late = late or was_late
            except (HoldExpired, JustSoldOut):
                # R10.8: payment succeeded, inventory gone. Reconciliation state.
                with transaction.atomic():
                    booking.status = "RECONCILIATION"
                    booking.notes = "Payment authorized but inventory no longer available."
                    booking.save(update_fields=["status", "notes", "updated_at"])
                return {
                    "status": "RECONCILIATION",
                    "confirmed": False,
                    "booking_id": booking.id,
                    "booking_number": booking.booking_number,
                    "message": "We received your payment but the last place has just gone. "
                               "A refund will be issued automatically.",
                    "total_minor": booking.total_minor,
                    "currency": booking.currency,
                }

    with transaction.atomic():
        now = timezone.now()
        booking.status = "CONFIRMED"
        booking.confirmed_at = now
        booking.amount_paid_minor = payment.amount_minor
        booking.late_confirmation = late
        booking.save(update_fields=[
            "status", "confirmed_at", "amount_paid_minor", "late_confirmation", "updated_at"
        ])

        tickets = issue_for_booking(booking=booking)

    AuditEvent.objects.create(
        tenant=booking.tenant,
        action="BOOKING_CONFIRMED",
        target_type="booking",
        target_id=booking.id,
        new_value={
            "booking_number": booking.booking_number,
            "total_minor": booking.total_minor,
            "currency": booking.currency,
            "ticket_count": len(tickets),
            "late_confirmation": late,
            "channel": booking.channel,
        },
        occurred_at_utc=timezone.now(),
        venue=booking.venue,
    )

    return _confirmed_result(booking, tickets, already=False, late=late, payment=payment)


def on_payment_captured(payment) -> dict[str, Any] | None:
    """Capture hook for the webhook path (R14.6).

    Given a captured payment, complete its booking through the same idempotent
    finalize the inline path uses. This is what confirms a booking whose browser or
    kiosk session died after authorization. A second capture for the same booking is
    a duplicate to flag, not a second confirmation (R14.5).
    """
    booking = payment.booking
    if booking is None:
        return None
    if booking.is_confirmed:
        # Already completed inline or by an earlier delivery. If this payment is a
        # second successful charge for the booking, flag the surplus for refund.
        _flag_duplicate_payment(booking)
        tickets = list(booking.tickets.all())
        return _confirmed_result(booking, tickets, already=True)
    return finalize_paid_booking(booking=booking, payment=payment)


def _flag_duplicate_payment(booking: Booking) -> None:
    """Record a surplus successful payment on a booking for finance follow-up (R14.5)."""
    from apps.payments.models import Payment

    successful = list(
        Payment.objects.filter(
            tenant=booking.tenant, booking=booking, status__in=("AUTHORIZED", "CAPTURED")
        ).order_by("created_at")
    )
    if len(successful) <= 1:
        return
    AuditEvent.objects.create(
        tenant=booking.tenant,
        action="PAYMENT_DUPLICATE_DETECTED",
        target_type="booking",
        target_id=booking.id,
        new_value={
            "booking_number": booking.booking_number,
            "payment_ids": [p.id for p in successful],
            "surplus_payment_ids": [p.id for p in successful[1:]],
        },
        occurred_at_utc=timezone.now(),
        venue=booking.venue,
    )


def _confirmed_result(
    booking: Booking, tickets, *, already: bool, late: bool = False, payment=None
) -> dict[str, Any]:
    return {
        "status": "CONFIRMED",
        "confirmed": True,
        "already_confirmed": already,
        "booking_id": booking.id,
        "booking_number": booking.booking_number,
        "total_minor": booking.total_minor,
        "amount_paid_minor": booking.amount_paid_minor,
        "currency": booking.currency,
        "late_confirmation": late,
        "payment": {
            "payment_id": payment.id,
            "status": payment.status,
            "method": payment.method,
            "amount_minor": payment.amount_minor,
            "provider_ref": payment.provider_ref,
        } if payment else None,
        "tickets": [
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "visit_date": t.visit_date.isoformat(),
                "valid_from": t.valid_from.isoformat() if t.valid_from else None,
                "valid_until": t.valid_until.isoformat() if t.valid_until else None,
                "validity_timezone": t.validity_timezone,
                "entry_allowance": t.entry_allowance,
                "entries_used": t.entries_used,
                "qr_payload": t.qr_payload,
                "state": t.state,
            }
            for t in tickets
        ],
        "message_key": "success.booking_confirmed",
    }
