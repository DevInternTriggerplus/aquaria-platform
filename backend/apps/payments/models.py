"""Payment types and payments.

Raw card data never enters this system: no PAN, no CVV, no magnetic stripe, at any
time (R14.2). Only a provider token reference is ever stored.

Idempotency is structural rather than best-effort — the unique constraint on
``idempotency_key`` means a repeated submission of the same attempt cannot create a
second charge (R14.3), and webhook processing is keyed on the provider event id so
duplicate or out-of-order deliveries produce exactly one state transition (R14.4).
"""

from __future__ import annotations

from django.db import models

from apps.core.models import ProtectedManager, ProtectedModel, TenantScopedModel


class PaymentType(TenantScopedModel):
    """A customer-facing payment method, per channel (update spec §39)."""

    id_prefix = "pty"

    METHOD_CHOICES = [
        ("CARD", "Card"),
        ("QR_BANK_TRANSFER", "QR / bank transfer"),
        ("EWALLET", "E-wallet"),
        ("CASH", "Cash"),
        ("STORED_VALUE", "Stored value"),
        ("INVOICE", "Invoice / offline settlement"),
        ("COMPLIMENTARY", "Complimentary"),
    ]

    venue = models.ForeignKey(
        "tenancy.Venue", on_delete=models.PROTECT, related_name="payment_types"
    )
    code = models.SlugField(max_length=40)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    display_name = models.JSONField(default=dict)
    description = models.JSONField(default=dict, blank=True)
    icon = models.CharField(max_length=30, blank=True)
    provider = models.CharField(max_length=40, blank=True)
    #: Reference to the provider credential in the secret store — never the secret.
    provider_config_ref = models.CharField(max_length=120, blank=True)
    supported_currencies = models.JSONField(
        default=list, blank=True, help_text="Empty means every currency."
    )
    web_enabled = models.BooleanField(default=True)
    kiosk_enabled = models.BooleanField(default=False)
    counter_enabled = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)
    status = models.CharField(
        max_length=12,
        choices=[("ACTIVE", "Active"), ("DISABLED", "Disabled"), ("ARCHIVED", "Archived")],
        default="ACTIVE",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "code"], name="uniq_payment_type_code_per_venue"
            ),
        ]
        ordering = ["display_order", "code"]

    def __str__(self) -> str:
        if isinstance(self.display_name, dict):
            return self.display_name.get("en", self.code)
        return str(self.display_name or self.code)

    def available_for(self, channel: str, currency: str | None = None) -> bool:
        if self.status != "ACTIVE":
            return False
        if currency and self.supported_currencies and currency.upper() not in self.supported_currencies:
            return False
        return {
            "ONLINE": self.web_enabled,
            "KIOSK": self.kiosk_enabled,
            "COUNTER": self.counter_enabled,
        }.get(channel, False)


class Payment(TenantScopedModel, ProtectedModel):
    """One payment attempt and its outcome (R14.10)."""

    id_prefix = "pay"

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("AUTHORIZED", "Authorized"),
        ("CAPTURED", "Captured"),
        ("FAILED", "Failed"),
        ("VOIDED", "Voided"),
        ("REFUNDED", "Refunded"),
        ("PARTIALLY_REFUNDED", "Partially refunded"),
    ]

    booking = models.ForeignKey(
        "booking.Booking", on_delete=models.PROTECT, related_name="payments"
    )
    payment_type = models.ForeignKey(
        PaymentType, null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )
    method = models.CharField(max_length=20)
    provider = models.CharField(max_length=40, default="simulated")
    #: The provider's own reference. Not a token, not card data.
    provider_ref = models.CharField(max_length=120, blank=True, db_index=True)
    #: Provider event id, so a replayed webhook is recognised (R14.4).
    provider_event_id = models.CharField(max_length=120, blank=True)

    amount_minor = models.IntegerField()
    currency = models.CharField(max_length=3, default="THB")
    tendered_minor = models.IntegerField(null=True, blank=True)
    change_minor = models.IntegerField(null=True, blank=True)
    refunded_minor = models.IntegerField(default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    channel = models.CharField(max_length=12, default="ONLINE")
    device = models.ForeignKey(
        "tenancy.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    idempotency_key = models.CharField(max_length=64)
    failure_code = models.CharField(max_length=60, blank=True)
    #: Mapped, customer-safe text. A raw provider payload is never shown (R14.12).
    failure_message = models.CharField(max_length=200, blank=True)

    authorized_at = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    objects = ProtectedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"], name="uniq_payment_idempotency_key"
            ),
            models.UniqueConstraint(
                fields=["tenant", "provider", "provider_event_id"],
                condition=~models.Q(provider_event_id=""),
                name="uniq_provider_event",
            ),
            models.CheckConstraint(
                condition=models.Q(refunded_minor__lte=models.F("amount_minor")),
                name="refund_never_exceeds_payment",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "status"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.method} {self.amount_minor} {self.currency} [{self.status}]"


class PaymentEvent(TenantScopedModel, ProtectedModel):
    """A provider webhook delivery, recorded exactly once (R14.4).

    The event is inserted under a unique ``(tenant, provider, provider_event_id)``
    constraint *before* any payment state changes. That ordering is what makes
    duplicate or out-of-order deliveries safe: the second delivery loses the insert
    race and is recognised as a replay, so the state transition happens once.
    """

    id_prefix = "pev"

    payment = models.ForeignKey(
        Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    provider = models.CharField(max_length=40)
    provider_event_id = models.CharField(max_length=120)
    kind = models.CharField(max_length=40)
    signature_valid = models.BooleanField(default=False)
    #: A hash of the raw body, for audit — never the body itself, which may echo
    #: provider internals.
    payload_hash = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(max_length=30, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    objects = ProtectedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider", "provider_event_id"],
                name="uniq_provider_event_delivery",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "provider", "provider_event_id"])]
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.provider_event_id} [{self.outcome or 'pending'}]"
