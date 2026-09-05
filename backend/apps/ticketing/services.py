"""Ticket issuance: one signed, individually redeemable artefact per admitted unit.

The QR payload is ``UTP1.<tenant>.<token>.<signature>`` — an opaque reference plus a
detached HMAC, never encoded personal data and never a guessable sequence (R15.2).
A forged code fails signature verification before any database lookup, which is part
of how the gate meets its latency target later.

Validity is **frozen at issue time**: ``valid_from``/``valid_until`` are computed
once from the venue's policy and timezone and stored on the ticket, so changing the
policy or the venue timezone afterwards never moves an issued ticket (settings §14).
"""

from __future__ import annotations

import datetime as dt

from django.utils import timezone

from apps.core.ids import (
    new_id,
    platform_signing_key,
    secure_token,
    sign_payload,
    ticket_number as make_ticket_number,
)

from .models import Ticket, end_of_visit_day, start_of_visit_day

QR_PREFIX = "UTP1"


def build_qr_payload(tenant_id: str) -> tuple[str, str]:
    """Return ``(token, full_payload)`` for a new ticket."""
    token = secure_token(32)
    body = f"{QR_PREFIX}.{tenant_id}.{token}"
    signature = sign_payload(platform_signing_key(), body)
    return token, f"{body}.{signature}"


def _validity_window(policy: dict, visit_date: dt.date, timezone_name: str) -> tuple[dt.datetime, dt.datetime, str]:
    """Resolve (valid_from, valid_until, validity_type) from the policy primitives.

    Deriving the window from primitives — a day count, a duration, a fixed range —
    rather than per-model code means a new admission model added as configuration
    gets a correct window for free.
    """
    vtype = policy.get("validity_type", "END_OF_VISIT_DAY")
    tz = timezone_name

    if vtype == "NUMBER_OF_DAYS":
        days = max(int(policy.get("number_of_days") or 1), 1)
        starts = start_of_visit_day(visit_date, tz)
        ends = end_of_visit_day(visit_date + dt.timedelta(days=days - 1), tz)
        return starts, ends, vtype

    if vtype == "FIXED_DURATION":
        minutes = int(policy.get("duration_minutes") or 0)
        starts = start_of_visit_day(visit_date, tz)
        ends = starts + dt.timedelta(minutes=minutes) if minutes else end_of_visit_day(visit_date, tz)
        return starts, ends, vtype

    # END_OF_VISIT_DAY and every other type default to the visit day, venue-local.
    return start_of_visit_day(visit_date, tz), end_of_visit_day(visit_date, tz), "END_OF_VISIT_DAY"


def issue_for_booking(*, booking, policy_by_product: dict[str, dict] | None = None) -> list[Ticket]:
    """Issue one ticket per admitted unit across the booking's items (R15.1).

    Idempotent: if tickets already exist for the booking they are returned unchanged,
    so a replayed confirmation does not mint duplicates.
    """
    existing = list(Ticket.objects.filter(tenant=booking.tenant, booking=booking))
    if existing:
        return existing

    policy_by_product = policy_by_product or {}
    tenant = booking.tenant
    venue = booking.venue
    tz = venue.timezone
    tickets: list[Ticket] = []
    index = 0

    items = booking.items.select_related("product", "ticket_type", "segment", "session").all()
    for item in items:
        policy = policy_by_product.get(item.product_id) or _default_policy(item.ticket_type)
        starts, ends, vtype = _validity_window(policy, booking.visit_date, tz)
        for _ in range(item.quantity):
            index += 1
            token, payload = build_qr_payload(tenant.id)
            tickets.append(
                Ticket(
                    # bulk_create bypasses Model.save(), so the string primary key must
                    # be assigned explicitly here rather than in save().
                    id=new_id("tck"),
                    tenant=tenant,
                    booking=booking,
                    booking_item=item,
                    venue=venue,
                    product=item.product,
                    ticket_type=item.ticket_type,
                    segment=item.segment,
                    session=item.session,
                    ticket_number=make_ticket_number(booking.booking_number, index),
                    state="VALID",
                    visit_date=booking.visit_date,
                    valid_from=starts,
                    valid_until=ends,
                    validity_timezone=tz,
                    validity_type=vtype,
                    validity_policy=policy,
                    entry_allowance=item.ticket_type.entry_allowance,
                    reentry_allowed=item.ticket_type.reentry_allowed,
                    proof_required=bool((item.segment and item.segment.proof_required_at_entry)),
                    qr_payload=payload,
                    issued_at=timezone.now(),
                )
            )

    Ticket.objects.bulk_create(tickets)
    return tickets


def record_entry(ticket: Ticket, *, at: dt.datetime | None = None) -> Ticket:
    """Atomically consume one entry, or raise if the allowance is spent.

    The crux of never-double-admitting: the row is locked with ``select_for_update``
    so two simultaneous scans of the last allowed entry serialize — one increments
    and admits, the other sees the allowance spent and raises :class:`ConflictError`,
    which the gate maps to REJECT_ALREADY_USED (R32.3).

    ``at`` is the scan moment so a fixed-time test gets consistent results rather
    than mixing a test moment with ``timezone.now()``.
    """
    from django.db import transaction

    from apps.core.errors import ConflictError

    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        unlimited = locked.entry_allowance == 0
        if not unlimited and locked.entries_used >= locked.entry_allowance:
            raise ConflictError(
                "This ticket has already been used.",
                details={"entries_used": locked.entries_used, "allowance": locked.entry_allowance},
            )
        now = at or timezone.now()
        locked.entries_used += 1
        if locked.first_entry_at is None:
            locked.first_entry_at = now
        locked.last_entry_at = now
        if not unlimited and locked.entries_used >= locked.entry_allowance:
            locked.state = "USED"
        elif locked.entries_used > 0:
            locked.state = "PARTIALLY_USED"
        locked.save(update_fields=[
            "entries_used", "first_entry_at", "last_entry_at", "state", "updated_at"
        ])
        return locked


def _default_policy(ticket_type) -> dict:
    """A ticket type's own allowance, expressed as the default validity policy."""
    return {
        "validity_type": "END_OF_VISIT_DAY",
        "number_of_days": 1,
        "duration_minutes": None,
        "max_entries": ticket_type.entry_allowance,
        "reentry_allowed": ticket_type.reentry_allowed,
    }
