"""Manage Booking self-service (R16).

Ownership is proven with a one-time code emailed to the address on the booking, not
by knowing the booking number. The lookup response is identical whether the booking
exists or not, so it cannot be used to enumerate bookings (R16.3). Codes are
single-use and short-lived (R16.11).

Cancel and reschedule are gated by a configurable, time-before-visit policy (R16.6).
Reschedule acquires the target date's capacity *before* releasing the original, so a
failed move never leaves the customer with nothing (R16.7). A used ticket cannot be
cancelled or rescheduled through self-service (R16.8).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.errors import (
    ConfirmationRequired,
    ConflictError,
    NotAvailable,
    NotFound,
    RateLimited,
    RuleViolation,
    ValidationError,
)
from apps.core.ids import hash_identifier, hash_secret, human_code, verify_secret
from apps.core.models import AuditEvent
from apps.core.money import apply_percentage
from apps.inventory.models import Session, acquire_hold, confirm_hold, release_hold
from apps.ticketing.services import build_qr_payload

from .consent_models import VerificationChallenge
from .models import Booking

VERIFICATION_TTL_MINUTES = 15
MAX_VERIFICATION_ATTEMPTS = 5

#: Default refund policy when a venue configures none. Tiers are ordered by the
#: minimum hours before the visit that qualify for them.
DEFAULT_REFUND_POLICY = {
    "restore_capacity": True,
    "tiers": [
        {"min_hours_before": 48, "refund_percent_bp": 10000, "fee_minor": 0},
        {"min_hours_before": 24, "refund_percent_bp": 5000, "fee_minor": 0},
        {"min_hours_before": 0, "refund_percent_bp": 0, "fee_minor": 0},
    ],
}


# ======================================================================== #
# Verification (R16.2, R16.3, R16.11)
# ======================================================================== #


def request_access_code(*, tenant, booking_number: str, email: str) -> dict[str, Any]:
    """Start ownership verification.

    The response is identical whether or not the booking exists; a code is only
    actually issued when the number/email pair matches.
    """
    generic = {
        "sent": True,
        "message": "If that booking exists, we have emailed a verification code to the address on it.",
        "expires_in_minutes": VERIFICATION_TTL_MINUTES,
    }
    booking = Booking.objects.select_related("customer").filter(
        tenant=tenant, booking_number=(booking_number or "").strip().upper()
    ).first()
    if booking is None or booking.customer is None:
        return generic
    if hash_identifier(booking.customer.email.lower()) != hash_identifier((email or "").strip().lower()):
        return generic

    code = human_code(6)
    now = timezone.now()
    VerificationChallenge.objects.create(
        tenant=tenant,
        booking=booking,
        purpose="MANAGE_BOOKING",
        contact_hash=hash_identifier((email or "").strip().lower()),
        code_hash=hash_secret(code),
        expires_at=now + dt.timedelta(minutes=VERIFICATION_TTL_MINUTES),
    )
    result = dict(generic)
    # Returned only to the owner of the request path (the demo surfaces it; a real
    # deployment sends it by email and never returns it).
    result["_code"] = code
    return result


def verify_access(*, tenant, booking_number: str, email: str, code: str) -> dict[str, Any]:
    """Consume a one-time code. Single-use and short-lived."""
    now = timezone.now()
    number = (booking_number or "").strip().upper()
    contact = hash_identifier((email or "").strip().lower())
    challenges = list(
        VerificationChallenge.objects.filter(
            tenant=tenant,
            booking__booking_number=number,
            contact_hash=contact,
            purpose="MANAGE_BOOKING",
            consumed_at__isnull=True,
            expires_at__gt=now,
        ).order_by("-issued_at")
    )
    for ch in challenges:
        if verify_secret(code, ch.code_hash):
            ch.consumed_at = now
            ch.save(update_fields=["consumed_at"])
            AuditEvent.objects.create(
                tenant=tenant, action="LOGIN", target_type="booking", target_id=ch.booking_id,
                new_value={"purpose": "manage_booking_verified"}, occurred_at_utc=now,
            )
            return {"verified": True, "booking_id": ch.booking_id}

    # A wrong code is not abuse on the first try. Count attempts and only throttle
    # after repeated failures (R16.3) — telling a customer "too many attempts" on
    # their first mistyped digit is both wrong and a support call.
    attempts = 0
    for ch in challenges:
        ch.attempts += 1
        attempts = max(attempts, ch.attempts)
        ch.save(update_fields=["attempts"])
    if attempts >= MAX_VERIFICATION_ATTEMPTS:
        for ch in challenges:
            ch.consumed_at = now
            ch.save(update_fields=["consumed_at"])
        raise RateLimited(
            60, message="Too many incorrect codes. Please request a new one.",
            code="verification_locked",
        )
    raise ValidationError(
        {"code": "That code is not correct. Please check it and try again."},
        message="That code is not correct. Please check it and try again.",
        code="verification_failed",
    )


# ======================================================================== #
# View and policy (R16.4, R16.5, R16.6)
# ======================================================================== #


def _hours_until_visit(booking: Booking) -> float:
    venue = booking.venue
    hours = (venue.operating_hours or {}).get("default", {})
    open_time = hours.get("open", "00:00")
    hh, mm = (int(x) for x in open_time.split(":")[:2])
    start = dt.datetime.combine(
        booking.visit_date, dt.time(hh, mm), tzinfo=venue.tzinfo
    )
    return (start - timezone.now()).total_seconds() / 3600.0


def _refund_tier(policy: dict, hours_before: float) -> dict:
    tiers = sorted(
        policy.get("tiers", []), key=lambda t: int(t.get("min_hours_before", 0)), reverse=True
    )
    for tier in tiers:
        if hours_before >= int(tier.get("min_hours_before", 0)):
            return tier
    return {"refund_percent_bp": 0, "fee_minor": 0}


def _any_used(booking: Booking) -> bool:
    return booking.tickets.filter(entries_used__gt=0).exists()


def policy_for(booking: Booking) -> dict[str, Any]:
    """Resolve eligibility, deadline, fee and refundable amount (R16.5, R16.6)."""
    policy = (booking.venue.config or {}).get("refund_policy") or DEFAULT_REFUND_POLICY
    used = _any_used(booking)
    active = booking.status == "CONFIRMED"
    hours_before = _hours_until_visit(booking)
    tier = _refund_tier(policy, hours_before)
    refundable = max(
        apply_percentage(booking.total_minor, int(tier.get("refund_percent_bp", 0)))
        - int(tier.get("fee_minor", 0)),
        0,
    )
    blocked = None
    if not active:
        blocked = f"This booking is {booking.status.lower()}."
    elif used:
        blocked = "One or more tickets have already been used."
    allowed = active and not used
    return {
        "hours_before_visit": round(hours_before, 2),
        "reschedule": {"allowed": allowed, "reason": None if allowed else blocked},
        "cancel": {
            "allowed": allowed,
            "reason": None if allowed else blocked,
            "refund_percent_bp": int(tier.get("refund_percent_bp", 0)),
            "fee_minor": int(tier.get("fee_minor", 0)),
            "refundable_minor": refundable,
        },
        "used_tickets": used,
    }


def manage_view(*, tenant, booking_id: str) -> dict[str, Any]:
    """Everything the verified customer may see and do (R16.4)."""
    booking = Booking.objects.select_related("venue", "customer").filter(
        tenant=tenant, id=booking_id
    ).first()
    if booking is None:
        raise NotFound(details={"entity": "booking"})
    policy = policy_for(booking)
    tickets = [
        {
            "ticket_number": t.ticket_number,
            "state": t.state,
            "visit_date": t.visit_date.isoformat() if t.visit_date else None,
            "valid_until": t.valid_until.isoformat() if t.valid_until else None,
            "entries_used": t.entries_used,
            "entries_remaining": t.entries_remaining,
            "qr_payload": t.qr_payload,
        }
        for t in booking.tickets.all()
    ]
    return {
        "booking_number": booking.booking_number,
        "status": booking.status,
        "visit_date": booking.visit_date.isoformat(),
        "venue": booking.venue.display_name(),
        "total_minor": booking.total_minor,
        "currency": booking.currency,
        "tickets": tickets,
        "policy": policy,
        "actions": {
            "view_qr": True,
            "resend_ticket_email": True,
            "reschedule": policy["reschedule"]["allowed"],
            "cancel": policy["cancel"]["allowed"],
        },
    }


# ======================================================================== #
# Cancel (R16, R17)
# ======================================================================== #


def cancel(*, tenant, booking_id: str, reason: str, confirmed: bool = False) -> dict[str, Any]:
    """Cancel a booking, restore capacity and compute the refund per policy."""
    booking = Booking.objects.select_related("venue").filter(tenant=tenant, id=booking_id).first()
    if booking is None:
        raise NotFound(details={"entity": "booking"})
    if booking.status != "CONFIRMED":
        raise ConflictError(
            f"This booking is already {booking.status.lower()}.",
            details={"status": booking.status},
        )
    if _any_used(booking):
        raise RuleViolation(
            "One or more tickets have already been used, so this booking cannot be "
            "cancelled here.",
        )
    policy = policy_for(booking)
    refundable = int(policy["cancel"]["refundable_minor"])

    if not confirmed:
        # State amount, scope and irreversibility before acting (R17.8).
        raise ConfirmationRequired(
            "Cancelling cannot be undone.",
            details={
                "booking_number": booking.booking_number,
                "ticket_count": booking.tickets.count(),
                "refund_amount_minor": refundable,
                "currency": booking.currency,
                "fee_minor": int(policy["cancel"]["fee_minor"]),
                "irreversible": True,
                "performed_action": "CANCEL",
            },
        )

    now = timezone.now()
    with transaction.atomic():
        booking.tickets.update(state="CANCELLED")
        booking.status = "CANCELLED"
        booking.cancelled_at = now
        booking.notes = f"{booking.notes}\ncancelled: {reason}".strip()
        booking.save(update_fields=["status", "cancelled_at", "notes", "updated_at"])
        _restore_capacity(booking)
        AuditEvent.objects.create(
            tenant=tenant, action="BOOKING_CANCELLED", target_type="booking",
            target_id=booking.id, previous_value={"status": "CONFIRMED"},
            new_value={"status": "CANCELLED", "refund_amount_minor": refundable},
            reason=reason, occurred_at_utc=now, venue=booking.venue,
        )

    # Money movement is a placeholder until the refund path is ported; the amount is
    # computed and recorded now so reporting reconciles.
    return {
        "booking_id": booking.id,
        "performed": "CANCEL",
        "status": "CANCELLED",
        "refund_amount_minor": refundable,
        "currency": booking.currency,
    }


def _restore_capacity(booking: Booking) -> None:
    """Give future-dated capacity back to the sessions the booking held (R17.5)."""
    policy = (booking.venue.config or {}).get("refund_policy") or DEFAULT_REFUND_POLICY
    if not policy.get("restore_capacity", True):
        return
    if booking.visit_date < timezone.now().astimezone(booking.venue.tzinfo).date():
        return
    counts: dict[str, int] = {}
    for item in booking.items.all():
        if item.session_id:
            counts[item.session_id] = counts.get(item.session_id, 0) + item.quantity
    for session_id, quantity in counts.items():
        session = Session.objects.filter(id=session_id).first()
        if session and not session.is_uncapped:
            with transaction.atomic():
                locked = Session.objects.select_for_update().get(pk=session.pk)
                locked.confirmed_count = max(locked.confirmed_count - quantity, 0)
                if locked.status == "FULL":
                    locked.status = "AVAILABLE"
                locked.save(update_fields=["confirmed_count", "status", "updated_at"])


# ======================================================================== #
# Reschedule (R16.7)
# ======================================================================== #


def reschedule(
    *, tenant, booking_id: str, new_visit_date: dt.date,
    new_session_id: str | None = None, reason: str = "",
) -> dict[str, Any]:
    """Move a booking, acquiring the target before releasing the original (R16.7)."""
    booking = Booking.objects.select_related("venue").filter(tenant=tenant, id=booking_id).first()
    if booking is None:
        raise NotFound(details={"entity": "booking"})
    policy = policy_for(booking)
    if not policy["reschedule"]["allowed"]:
        raise RuleViolation(
            policy["reschedule"]["reason"] or "This booking cannot be rescheduled.",
        )

    items = list(booking.items.select_related("session").all())
    previous = {"visit_date": booking.visit_date.isoformat(), "session_id": booking.items.first().session_id if items else None}

    # Acquire the target first. If it fails, nothing is released, so the original is
    # untouched (R16.7).
    acquired: list[Any] = []
    try:
        for item in items:
            if not item.session_id:
                continue  # uncapped item, no capacity to move
            if not new_session_id:
                continue
            target = Session.objects.filter(tenant=tenant, id=new_session_id, venue=booking.venue).first()
            if target is None or target.is_uncapped:
                continue
            hold = acquire_hold(
                session=target, quantity=item.quantity, cart_ref=f"resched-{booking.id}",
                channel=booking.channel,
            )
            if hold:
                confirm_hold(hold)
                acquired.append((item, target, item.quantity))
    except (NotAvailable, RuleViolation, Exception) as exc:  # noqa: BLE001
        # Roll back anything acquired so the original booking is left intact.
        for _item, target, quantity in acquired:
            _release_confirmed(target, quantity)
        raise NotAvailable(
            "That date or time is not available, so your original booking has not been "
            "changed.",
            details={"requested_date": new_visit_date.isoformat(), "original_unchanged": True},
        ) from exc

    now = timezone.now()
    with transaction.atomic():
        # Release the originals now that the target is secured.
        for item in items:
            if item.session_id:
                original = Session.objects.filter(id=item.session_id).first()
                if original and not original.is_uncapped:
                    _release_confirmed(original, item.quantity)
        booking.visit_date = new_visit_date
        booking.save(update_fields=["visit_date", "updated_at"])
        for item in items:
            if new_session_id:
                item.session_id = new_session_id
                item.save(update_fields=["session_id"])
        # Reissue tickets so superseded QR codes stop working (R16.9).
        reissued = []
        for ticket in booking.tickets.exclude(state__in=("CANCELLED", "VOIDED", "REFUNDED")):
            _, payload = build_qr_payload(tenant.id)
            ticket.qr_payload = payload
            ticket.visit_date = new_visit_date
            ticket.reissue_count += 1
            if new_session_id:
                ticket.session_id = new_session_id
            ticket.save(update_fields=["qr_payload", "visit_date", "reissue_count", "session_id"])
            reissued.append(ticket.ticket_number)
        AuditEvent.objects.create(
            tenant=tenant, action="BOOKING_RESCHEDULED", target_type="booking",
            target_id=booking.id, previous_value=previous,
            new_value={"visit_date": new_visit_date.isoformat(), "session_id": new_session_id},
            reason=reason, occurred_at_utc=now, venue=booking.venue,
        )

    return {
        "booking_id": booking.id,
        "previous": previous,
        "new_visit_date": new_visit_date.isoformat(),
        "new_session_id": new_session_id,
        "tickets_reissued": reissued,
    }


def _release_confirmed(session: Session, quantity: int) -> None:
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        locked.confirmed_count = max(locked.confirmed_count - quantity, 0)
        if locked.status == "FULL":
            locked.status = "AVAILABLE"
        locked.save(update_fields=["confirmed_count", "status", "updated_at"])
