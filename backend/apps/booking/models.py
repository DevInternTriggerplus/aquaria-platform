"""Bookings and their items.

Two things here are load-bearing:

* **The charge snapshot.** ``charge_snapshot`` freezes the VAT/service-charge rate
  and mode, the currency and any exchange rate at the moment of confirmation. A
  completed order never moves when settings later change (R5.3, settings §33).
* **The lifecycle.** A booking is persisted as ``AWAITING_PAYMENT`` *before* the
  gateway is charged, and a single idempotent completion step finishes it. That is
  why a browser dying after authorization still yields a confirmed booking and a
  delivered ticket (R14.6).

Bookings are never physically deleted; DELETE means cancel (R46.1, R46.2).
"""

from __future__ import annotations

from django.db import models

from apps.core.models import ProtectedManager, ProtectedModel, TenantScopedModel

BOOKING_STATUS_CHOICES = [
    ("DRAFT", "Draft"),
    ("AWAITING_PAYMENT", "Awaiting payment"),
    ("CONFIRMED", "Confirmed"),
    ("CANCELLED", "Cancelled"),
    ("REFUNDED", "Refunded"),
    ("VOIDED", "Voided"),
    ("RECONCILIATION", "Held for reconciliation"),
]

CHANNEL_CHOICES = [
    ("ONLINE", "Online"),
    ("KIOSK", "Kiosk"),
    ("COUNTER", "Counter"),
    ("PARTNER", "Partner"),
    ("STAFF", "Staff-assisted"),
    ("API", "API"),
]


class Customer(TenantScopedModel):
    """A booking contact.

    Personal data is minimized to what a configured purpose needs (R12.23) and is
    masked from principals without ``VIEW_PII`` at the serializer layer (R12.24).
    """

    id_prefix = "cus"

    email = models.EmailField(max_length=254, db_index=True)
    full_name = models.CharField(max_length=160, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    language = models.CharField(max_length=8, default="en")
    marketing_opt_in = models.BooleanField(default=False)
    anonymized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "email"])]

    def __str__(self) -> str:
        return self.email


class Booking(TenantScopedModel, ProtectedModel):
    """A customer order."""

    id_prefix = "bkg"

    booking_number = models.CharField(max_length=32, db_index=True)
    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="bookings")
    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.PROTECT, related_name="bookings"
    )
    channel = models.CharField(max_length=12, choices=CHANNEL_CHOICES, default="ONLINE")
    device = models.ForeignKey(
        "tenancy.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="bookings"
    )
    sold_by = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="sales"
    )
    partner = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    visit_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=20, choices=BOOKING_STATUS_CHOICES, default="DRAFT", db_index=True
    )
    language = models.CharField(max_length=8, default="en")

    currency = models.CharField(max_length=3, default="THB")
    #: Every monetary field is integer minor units.
    gross_minor = models.IntegerField(default=0)
    discount_minor = models.IntegerField(default=0)
    service_charge_minor = models.IntegerField(default=0)
    tax_minor = models.IntegerField(default=0)
    rounding_adjustment_minor = models.IntegerField(default=0)
    total_minor = models.IntegerField(default=0)
    #: Portion settled by stored value (gift card, loyalty points) rather than paid.
    settlement_minor = models.IntegerField(default=0)
    amount_paid_minor = models.IntegerField(default=0)

    #: Frozen configuration. Read this, never today's settings, when rendering a
    #: receipt or reconciling an old order.
    charge_snapshot = models.JSONField(default=dict, blank=True)
    #: Transaction currency vs base currency, with the exact rate used (settings §20).
    exchange_rate_snapshot = models.JSONField(default=dict, blank=True)

    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    late_confirmation = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=64, blank=True)
    #: The checkout cart that created this booking's holds, so finalize can find
    #: exactly this booking's holds and never consume another cart's (R10.10).
    cart_ref = models.CharField(max_length=40, blank=True, db_index=True)
    notes = models.TextField(blank=True)

    objects = ProtectedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "booking_number"], name="uniq_booking_number_per_tenant"
            ),
            # An idempotency key may be reused across tenants but not within one.
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_booking_idempotency_key",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status", "visit_date"]),
            models.Index(fields=["venue", "visit_date"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.booking_number

    @property
    def is_confirmed(self) -> bool:
        return self.status == "CONFIRMED"

    def cancel(self, *, when=None, reason: str = "") -> None:
        """What DELETE actually does to a booking (R46.2)."""
        from django.utils import timezone

        self.status = "CANCELLED"
        self.cancelled_at = when or timezone.now()
        if reason:
            self.notes = f"{self.notes}\ncancelled: {reason}".strip()
        self.save(update_fields=["status", "cancelled_at", "notes", "updated_at"])


class BookingItem(TenantScopedModel, ProtectedModel):
    """One priced line of a booking.

    The resolved unit price, tax, discount and net are stored here at confirmation
    and never recomputed (R5.3). ``price_rule`` records *which* rule applied, for
    audit and reconciliation (R5.2).
    """

    id_prefix = "bit"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    ticket_type = models.ForeignKey("catalog.TicketType", on_delete=models.PROTECT, related_name="+")
    segment = models.ForeignKey(
        "catalog.CustomerSegment", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    session = models.ForeignKey(
        "inventory.Session", null=True, blank=True, on_delete=models.PROTECT, related_name="booking_items"
    )

    quantity = models.PositiveIntegerField()
    unit_price_minor = models.IntegerField()
    gross_minor = models.IntegerField()
    discount_minor = models.IntegerField(default=0)
    tax_minor = models.IntegerField(default=0)
    net_minor = models.IntegerField(default=0)
    currency = models.CharField(max_length=3, default="THB")

    price_rule = models.ForeignKey(
        "pricing.PriceRule", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Which promotions applied, in what order, with what computed value (R13.9).
    applied_promotions = models.JSONField(default=list, blank=True)

    objects = ProtectedManager()

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="item_quantity_positive"),
        ]


# PDPA consent models live in a separate module for readability but must be imported
# here so Django's app registry and migrations discover them.
from .consent_models import (  # noqa: E402,F401
    ConsentRecord,
    PrivacyNotice,
    VerificationChallenge,
)
