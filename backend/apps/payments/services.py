"""Start a payment and mark it authorized or failed.

There is one important structural choice: the booking is persisted in
``AWAITING_PAYMENT`` **before** the gateway is called. That means a browser that
dies after a successful authorization, or a kiosk whose session is killed, still
gets a confirmed booking and a delivered ticket when the gateway's webhook
confirms the charge (R14.6, R14.7). The webhook path calls the same
``finalize_paid_booking`` as the inline path, producing the same result.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.core.errors import PaymentFailed, ValidationError
from apps.core.ids import secure_token

from .gateway import PaymentGateway
from .models import Payment


def start_payment(
    *,
    booking,
    amount_minor: int,
    currency: str,
    method: str,
    channel: str = "ONLINE",
    gateway: PaymentGateway,
    idempotency_key: str = "",
    device=None,
    actor=None,
) -> Payment:
    """Authorize a charge, idempotently.

    If a Payment already exists for this idempotency key, it is returned as-is rather
    than hitting the gateway again (R14.3). A key is required; callers that omit it
    get a generated one, but the API layer should always pass the one the client sent.
    """
    if not idempotency_key:
        idempotency_key = secure_token(16)
    if amount_minor < 0:
        raise ValidationError({"amount_minor": "The amount cannot be negative."})

    # Idempotent: the same key returns the same result without double-charging.
    existing = Payment.objects.filter(
        tenant=booking.tenant, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        return existing

    result = gateway.authorize(
        amount_minor=amount_minor,
        currency=currency,
        idempotency_key=idempotency_key,
        method=method,
    )
    now = timezone.now()
    with transaction.atomic():
        return Payment.objects.create(
            tenant=booking.tenant,
            booking=booking,
            method=method,
            provider=result.provider,
            provider_ref=result.provider_ref,
            amount_minor=amount_minor,
            currency=currency,
            status=result.status,
            channel=channel,
            device=device,
            actor=actor,
            idempotency_key=idempotency_key,
            failure_code=result.failure_code,
            failure_message=result.failure_message,
            authorized_at=now if result.status == "AUTHORIZED" else None,
        )
