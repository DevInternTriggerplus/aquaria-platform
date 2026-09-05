"""Verified, idempotent payment webhooks (R14.4–R14.7).

The provider is the source of truth. A webhook is verified, deduplicated, and
processed exactly once; any disagreement with the client-side return resolves in the
provider's favour. This is the path that matters when the browser, kiosk session or
network is lost after authorization (R14.6) — the callback still confirms the booking
and issues the ticket.

The design mirrors the reference implementation: insert the event row under a unique
constraint *before* touching payment state, so a duplicate delivery loses the insert
race and is recognised as a replay rather than producing a second state transition.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.errors import ValidationError
from apps.core.models import AuditEvent

from .gateway import PaymentGateway
from .models import Payment, PaymentEvent

CAPTURE_KINDS = frozenset({"payment.succeeded", "payment.captured", "charge.succeeded"})
FAIL_KINDS = frozenset({"payment.failed", "charge.failed"})
REFUND_KINDS = frozenset({"payment.refunded", "charge.refunded"})
VOID_KINDS = frozenset({"payment.voided", "charge.voided"})

#: Set by the composition wiring to BookingService.finalize_paid_booking, so this
#: module never imports the booking service (which imports payments).
CaptureHook = Callable[[Payment], dict[str, Any] | None]


def handle_webhook(
    *,
    tenant,
    gateway: PaymentGateway,
    provider_event_id: str,
    kind: str,
    body: str,
    signature: str,
    payment_id: str | None = None,
    idempotency_key: str | None = None,
    amount_minor: int | None = None,
    failure_code: str = "",
    on_capture: CaptureHook | None = None,
) -> dict[str, Any]:
    """Verify and process one provider callback exactly once."""
    # 1. Verify the signature before anything else. An unverified callback is
    #    rejected and recorded; we never trust an unsigned body.
    if not gateway.sign_webhook(body) == signature:
        AuditEvent.objects.create(
            tenant=tenant,
            action="WEBHOOK_SIGNATURE_INVALID",
            target_type="payment_event",
            target_id=provider_event_id,
            new_value={"provider": gateway.name, "kind": kind},
            occurred_at_utc=timezone.now(),
        )
        raise ValidationError(
            {"signature": "The callback signature is not valid."},
            message="This callback could not be verified.",
            code="webhook_signature_invalid",
        )

    payload_hash = hashlib.blake2b(body.encode("utf-8"), digest_size=16).hexdigest()
    payment = _resolve_payment(tenant, payment_id, idempotency_key)

    # 2. Insert the event under the unique constraint BEFORE any state change.
    #    A duplicate delivery loses this race and is reported as a replay.
    try:
        with transaction.atomic():
            event = PaymentEvent.objects.create(
                tenant=tenant,
                payment=payment,
                provider=gateway.name,
                provider_event_id=provider_event_id,
                kind=kind,
                signature_valid=True,
                payload_hash=payload_hash,
            )
    except IntegrityError:
        existing = PaymentEvent.objects.filter(
            tenant=tenant, provider=gateway.name, provider_event_id=provider_event_id
        ).first()
        return {
            "duplicate": True,
            "processed": False,
            "outcome": existing.outcome if existing else None,
            "event_id": existing.id if existing else None,
        }

    # 3. An event with no matching platform payment is an exception to investigate,
    #    not something to drop silently (R14.9).
    if payment is None:
        event.outcome = "ORPHANED"
        event.processed_at = timezone.now()
        event.save(update_fields=["outcome", "processed_at"])
        AuditEvent.objects.create(
            tenant=tenant,
            action="PAYMENT_ORPHANED_AUTHORIZATION",
            target_type="payment_event",
            target_id=event.id,
            new_value={"provider_event_id": provider_event_id, "kind": kind, "amount_minor": amount_minor},
            occurred_at_utc=timezone.now(),
        )
        return {"duplicate": False, "processed": True, "outcome": "ORPHANED", "event_id": event.id}

    # 4. Apply the state transition. The provider's view wins (R14.7).
    outcome = _apply(payment, kind=kind, amount_minor=amount_minor, failure_code=failure_code)
    event.outcome = outcome
    event.processed_at = timezone.now()
    event.save(update_fields=["outcome", "processed_at"])

    # 5. On capture, complete the booking through the shared idempotent finalize.
    #    Safe to call unconditionally: a replay or out-of-order delivery still yields
    #    exactly one confirmed booking (R14.6).
    completion = None
    if outcome == "CAPTURED" and on_capture is not None:
        payment.refresh_from_db()
        completion = on_capture(payment)

    return {
        "duplicate": False,
        "processed": True,
        "outcome": outcome,
        "event_id": event.id,
        "payment_id": payment.id,
        "booking_id": payment.booking_id,
        "booking_completed": bool(completion and completion.get("confirmed")),
        "tickets_issued": len(completion.get("tickets", [])) if completion else 0,
    }


def _resolve_payment(tenant, payment_id: str | None, idempotency_key: str | None) -> Payment | None:
    if payment_id:
        return Payment.objects.filter(tenant=tenant, id=payment_id).first()
    if idempotency_key:
        return Payment.objects.filter(tenant=tenant, idempotency_key=idempotency_key).first()
    return None


def _apply(payment: Payment, *, kind: str, amount_minor: int | None, failure_code: str) -> str:
    """Apply one verified event to the payment. Returns the outcome label."""
    now = timezone.now()
    if kind in CAPTURE_KINDS:
        # Amount disagreement is a reconciliation exception, not a silent capture.
        if amount_minor is not None and int(amount_minor) != int(payment.amount_minor):
            AuditEvent.objects.create(
                tenant=payment.tenant,
                action="PAYMENT_AMOUNT_MISMATCH",
                target_type="payment",
                target_id=payment.id,
                new_value={"platform": payment.amount_minor, "provider": int(amount_minor)},
                occurred_at_utc=now,
            )
            return "AMOUNT_MISMATCH"
        if payment.status not in ("AUTHORIZED", "PENDING", "CAPTURED"):
            return "IGNORED"
        payment.status = "CAPTURED"
        payment.authorized_at = payment.authorized_at or now
        payment.captured_at = now
        payment.save(update_fields=["status", "authorized_at", "captured_at", "updated_at"])
        return "CAPTURED"
    if kind in FAIL_KINDS:
        payment.status = "FAILED"
        payment.failure_code = failure_code or "processing_error"
        payment.save(update_fields=["status", "failure_code", "updated_at"])
        return "FAILED"
    if kind in REFUND_KINDS:
        payment.status = "REFUNDED"
        payment.save(update_fields=["status", "updated_at"])
        return "REFUNDED"
    if kind in VOID_KINDS:
        payment.status = "VOIDED"
        payment.save(update_fields=["status", "updated_at"])
        return "VOIDED"
    return "IGNORED"
