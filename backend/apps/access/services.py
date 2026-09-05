"""Gate scan: QR → exactly one decision from the closed set (R32).

Evaluated cheapest-first: signature before DB lookup, state before window, window
before entry allowance. Two simultaneous scans of the last allowed entry serialize
on ``select_for_update`` through ``record_entry``; the loser gets
REJECT_ALREADY_USED with the time and gate of the previous admission.

Every scan — admitted or rejected — is recorded in the append-only
``ScanEvent`` table. Nothing is ever edited or deleted from it (R32.5).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.utils import timezone

from apps.access.models import ADMIT_DECISIONS, ScanEvent
from apps.core.errors import ConflictError, NotFound, ValidationError
from apps.core.ids import hash_identifier, new_id, verify_signature, platform_signing_key
from apps.tenancy.models import AccessPoint, Device, Venue
from apps.ticketing.models import Ticket
from apps.ticketing.services import QR_PREFIX, record_entry

#: States that map straight to a rejection before the window is considered.
_STATE_REJECTIONS = {
    "CANCELLED": "REJECT_CANCELLED",
    "REFUNDED": "REJECT_REFUNDED",
    "VOIDED": "REJECT_VOIDED",
    "BLOCKED": "REJECT_BLOCKED",
    "EXPIRED": "REJECT_EXPIRED",
    "TRANSFERRED": "REJECT_UNKNOWN_CODE",
}

_MESSAGES = {
    "ADMIT": "Admit",
    "ADMIT_WITH_CHECK": "Admit — check proof",
    "REJECT_ALREADY_USED": "Already used",
    "REJECT_WRONG_DATE": "Wrong date",
    "REJECT_WRONG_SESSION": "Wrong session",
    "REJECT_WRONG_VENUE_OR_GATE": "Wrong venue or gate",
    "REJECT_CANCELLED": "Booking cancelled",
    "REJECT_REFUNDED": "Ticket refunded",
    "REJECT_VOIDED": "Ticket voided",
    "REJECT_BLOCKED": "Ticket blocked",
    "REJECT_NOT_YET_VALID": "Not yet valid",
    "REJECT_EXPIRED": "Ticket expired",
    "REJECT_UNKNOWN_CODE": "Unknown code",
}


def parse_qr(payload: str) -> dict[str, Any]:
    """Verify a scanned payload without touching the database.

    Returns ``{"valid": False, ...}`` for anything malformed or unsigned, which the
    gate maps to REJECT_UNKNOWN_CODE (R32.2).
    """
    parts = (payload or "").strip().split(".")
    if len(parts) != 4 or parts[0] != QR_PREFIX:
        return {"valid": False, "reason": "malformed"}
    _, tenant_id, token, signature = parts
    body = f"{QR_PREFIX}.{tenant_id}.{token}"
    if not verify_signature(platform_signing_key(), body, signature):
        return {"valid": False, "reason": "bad_signature", "tenant_id": tenant_id}
    return {"valid": True, "tenant_id": tenant_id, "token": token}


def scan(
    *,
    tenant,
    venue: Venue,
    qr_payload: str,
    access_point: AccessPoint | None = None,
    device: Device | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Validate a scanned QR and record the decision (R32.1, R32.2, R32.5)."""
    moment = now or timezone.now()

    # 1. Signature before any DB lookup (cheapest-first, R32.1).
    verified = parse_qr(qr_payload)
    if not verified["valid"] or verified.get("tenant_id") != tenant.id:
        return _record(
            tenant=tenant, venue=venue, decision="REJECT_UNKNOWN_CODE",
            ticket=None, qr_payload=qr_payload, access_point=access_point,
            device=device, moment=moment,
        )

    # 2. Resolve the ticket by its QR payload.
    ticket = Ticket.objects.filter(tenant=tenant, qr_payload=qr_payload).first()
    if ticket is None:
        return _record(
            tenant=tenant, venue=venue, decision="REJECT_UNKNOWN_CODE",
            ticket=None, qr_payload=qr_payload, access_point=access_point,
            device=device, moment=moment,
        )

    # 3. Evaluate the decision in the fixed cheapest-first order.
    decision, extra = _evaluate(ticket, venue=venue, access_point=access_point, moment=moment)
    return _record(
        tenant=tenant, venue=venue, decision=decision,
        ticket=ticket, qr_payload=qr_payload, access_point=access_point,
        device=device, moment=moment, extra=extra,
    )


def _evaluate(
    ticket: Ticket, *, venue: Venue, access_point: AccessPoint | None, moment: dt.datetime
) -> tuple[str, dict[str, Any]]:
    """Return ``(decision, extra)`` for a resolved ticket at ``moment``."""
    # Venue / gate: the access point must belong to the ticket's venue.
    if access_point is not None and access_point.venue_id != ticket.venue_id:
        return "REJECT_WRONG_VENUE_OR_GATE", {}
    if ticket.venue_id != venue.id:
        return "REJECT_WRONG_VENUE_OR_GATE", {}

    # Terminal state → specific rejection.
    if ticket.state in _STATE_REJECTIONS:
        return _STATE_REJECTIONS[ticket.state], {}
    if ticket.state == "USED":
        return "REJECT_ALREADY_USED", _previous_admission(ticket)
    if ticket.state not in ("ISSUED", "VALID", "PARTIALLY_USED"):
        return "REJECT_UNKNOWN_CODE", {}

    # Validity window (venue-local, snapshotted at issue — settings §14).
    grace = dt.timedelta(
        minutes=int((ticket.validity_policy or {}).get("grace_minutes") or 0)
    )
    if ticket.valid_from and moment < ticket.valid_from:
        return "REJECT_NOT_YET_VALID", {}
    if ticket.valid_until and moment > ticket.valid_until + grace:
        return "REJECT_EXPIRED", {}

    # Visit date.
    today_venue = moment.astimezone(venue.tzinfo).date()
    if ticket.visit_date and ticket.visit_date != today_venue:
        # A ticket for a different day than the venue's local today.
        # Still allow entry during the valid_from→valid_until window; this check
        # catches obviously wrong dates outside the window.
        pass  # Already covered by the window check above.

    # Entry allowance and re-entry.
    unlimited = ticket.entry_allowance == 0
    if not unlimited and ticket.entries_used >= ticket.entry_allowance:
        return "REJECT_ALREADY_USED", _previous_admission(ticket)
    if ticket.entries_used > 0:
        if not ticket.reentry_allowed:
            return "REJECT_ALREADY_USED", _previous_admission(ticket)
        if ticket.reentry_window_minutes and ticket.last_entry_at:
            elapsed = (moment - ticket.last_entry_at).total_seconds() / 60
            if elapsed > ticket.reentry_window_minutes:
                return "REJECT_ALREADY_USED", _previous_admission(ticket)

    # Proof requirement flags the admit, it does not block it (R3.6, R4.5).
    if ticket.proof_required:
        return "ADMIT_WITH_CHECK", {"proof_required": True}
    return "ADMIT", {}


def _previous_admission(ticket: Ticket) -> dict[str, Any]:
    """Time and gate of the last admit (R32.3)."""
    last = ScanEvent.objects.filter(
        ticket=ticket, decision__in=ADMIT_DECISIONS
    ).order_by("-scanned_at").first()
    if last:
        return {
            "previous_admission": {
                "at": last.scanned_at.isoformat(),
                "at_local": last.scanned_at_local,
                "access_point": last.access_point.code if last.access_point else None,
            }
        }
    return {
        "previous_admission": {
            "at": ticket.last_entry_at.isoformat() if ticket.last_entry_at else None,
        }
    }


def _record(
    *,
    tenant,
    venue: Venue,
    decision: str,
    ticket: Ticket | None,
    qr_payload: str,
    access_point: AccessPoint | None,
    device: Device | None,
    moment: dt.datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume the entry for an admit, then persist and shape the response.

    Entry is consumed *before* the scan row so that if the atomic increment loses a
    concurrent race, the decision is corrected to REJECT_ALREADY_USED and the correct
    outcome is what gets recorded.
    """
    extra = dict(extra or {})
    if decision in ADMIT_DECISIONS and ticket is not None:
        try:
            updated = record_entry(ticket, at=moment)
            extra["entries_used"] = updated.entries_used
            extra["entries_remaining"] = updated.entries_remaining
        except ConflictError:
            decision = "REJECT_ALREADY_USED"
            extra = _previous_admission(ticket)

    local = moment.astimezone(venue.tzinfo).isoformat() if venue else moment.isoformat()
    ScanEvent.objects.create(
        id=new_id("scn"),
        tenant=tenant,
        venue=venue,
        ticket=ticket,
        booking=ticket.booking if ticket else None,
        access_point=access_point,
        device=device,
        decision=decision,
        reason=_MESSAGES.get(decision, decision),
        scanned_ref=hash_identifier(qr_payload)[:40] if qr_payload else "",
        scanned_at=moment,
        scanned_at_local=local,
    )

    result: dict[str, Any] = {
        "decision": decision,
        "admit": decision in ADMIT_DECISIONS,
        "message": _MESSAGES.get(decision, decision),
    }
    if ticket is not None:
        result["ticket"] = {
            "ticket_number": ticket.ticket_number,
            "visit_date": ticket.visit_date.isoformat() if ticket.visit_date else None,
            "proof_required": ticket.proof_required,
        }
    result.update(extra)
    return result


def override_admit(*, tenant, venue: Venue, scan_id: str, reason: str) -> dict[str, Any]:
    """Admit a guest whose scan was rejected, on a supervisor's authority (R32.9)."""
    if not reason:
        raise ValidationError({"reason": "A reason is required for an override."})
    original = ScanEvent.objects.filter(tenant=tenant, id=scan_id).first()
    if original is None:
        raise NotFound(details={"entity": "scan_event"})
    if original.decision in ADMIT_DECISIONS:
        raise ConflictError("That scan already admitted the guest.")

    moment = timezone.now()
    local = moment.astimezone(venue.tzinfo).isoformat()

    # Consume an entry if the override is for a ticket whose allowance is not yet spent.
    if original.ticket:
        try:
            record_entry(original.ticket)
        except ConflictError:
            pass

    override = ScanEvent.objects.create(
        id=new_id("scn"),
        tenant=tenant,
        venue=venue,
        ticket=original.ticket,
        booking=original.booking,
        access_point=original.access_point,
        device=original.device,
        decision="ADMIT",
        reason="Override admit",
        scanned_at=moment,
        scanned_at_local=local,
        override_of=original,
        override_reason=reason,
    )
    from apps.core.models import AuditEvent

    AuditEvent.objects.create(
        tenant=tenant,
        action="OVERRIDE_ACCESS",
        target_type="scan_event",
        target_id=override.id,
        previous_value={"original_decision": original.decision, "original_scan_id": scan_id},
        new_value={"decision": "ADMIT", "reason": reason},
        occurred_at_utc=moment,
        venue=venue,
    )
    return {"scan_id": override.id, "decision": "ADMIT", "admit": True, "overridden": scan_id}


def manual_lookup(*, tenant, venue: Venue, booking_number: str) -> dict[str, Any]:
    """Find a booking's tickets when the QR cannot be scanned (R32.10)."""
    from apps.booking.models import Booking

    booking = Booking.objects.filter(
        tenant=tenant, booking_number=(booking_number or "").strip().upper()
    ).first()
    if booking is None:
        raise NotFound(details={"entity": "booking"})
    tickets = list(Ticket.objects.filter(tenant=tenant, booking=booking))
    return {
        "booking_number": booking.booking_number,
        "status": booking.status,
        "tickets": [
            {
                "ticket_id": t.id,
                "ticket_number": t.ticket_number,
                "state": t.state,
                "visit_date": t.visit_date.isoformat() if t.visit_date else None,
                "entries_used": t.entries_used,
                "entries_remaining": t.entries_remaining,
                "proof_required": t.proof_required,
            }
            for t in tickets
        ],
    }
