"""Business / venue settings: VAT, service charge, ticket validity, currency, FX.

Everything here is **effective-dated and never edited in place**. Changing the VAT
rate creates a new row with a new ``effective_from``; the old row stays exactly as
it was. That is what lets a completed order from last year still reconcile after
this year's rate change (settings spec §2, §33).

Rates are basis points (7% = 700) so no float rate ever enters a calculation.
Exchange rates are exact decimal strings.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models

from apps.core.models import ProtectedModel, TenantScopedModel
from apps.core.money import EXCHANGE_RATE_PRECISION, parse_rate, rate_direction_label
from apps.tenancy.models import validate_iana_timezone

CHARGE_MODE_CHOICES = [
    ("INCLUSIVE", "Included in the displayed price"),
    ("EXCLUSIVE", "Added at checkout"),
]


class EffectiveDatedQuerySet(models.QuerySet):
    def effective_on(self, venue_id: str, on_date):
        """The row in force on ``on_date`` — the latest one that has started."""
        return (
            self.filter(venue_id=venue_id, effective_from__lte=on_date)
            .order_by("-effective_from", "-created_at")
            .first()
        )


class VatSetting(TenantScopedModel):
    """Effective-dated VAT configuration for one venue (settings spec §1)."""

    id_prefix = "vat"

    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="vat_settings")
    enabled = models.BooleanField(default=True)
    rate_bp = models.PositiveIntegerField(
        default=700,
        validators=[MaxValueValidator(10_000)],
        help_text="Basis points. 7% VAT is 700. Max 10000 (100%).",
    )
    mode = models.CharField(max_length=10, choices=CHARGE_MODE_CHOICES, default="INCLUSIVE")
    display_name = models.CharField(max_length=60, default="VAT")
    tax_registration = models.CharField(max_length=60, blank=True)
    effective_from = models.DateField(db_index=True)
    changed_by = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True)

    objects = models.Manager.from_queryset(EffectiveDatedQuerySet)()

    class Meta:
        ordering = ["-effective_from"]
        indexes = [models.Index(fields=["venue", "effective_from"])]

    def __str__(self) -> str:
        pct = Decimal(self.rate_bp) / 100
        return f"{self.display_name} {pct}% {self.mode} from {self.effective_from}"


class ServiceChargeSetting(TenantScopedModel):
    """Effective-dated service charge configuration (settings spec §3)."""

    id_prefix = "svc"

    venue = models.ForeignKey(
        "tenancy.Venue", on_delete=models.PROTECT, related_name="service_charge_settings"
    )
    enabled = models.BooleanField(default=False)
    rate_bp = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(10_000)])
    mode = models.CharField(max_length=10, choices=CHARGE_MODE_CHOICES, default="EXCLUSIVE")
    display_name = models.CharField(max_length=60, default="Service charge")
    effective_from = models.DateField(db_index=True)
    changed_by = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True)

    objects = models.Manager.from_queryset(EffectiveDatedQuerySet)()

    class Meta:
        ordering = ["-effective_from"]
        indexes = [models.Index(fields=["venue", "effective_from"])]


class TicketValidityPolicy(TenantScopedModel):
    """How long an issued ticket/QR stays valid (settings spec §10–§12).

    The platform default is End of Visit Day: valid until 23:59:59 on the visit
    date **in the venue's timezone**. The resolved window is snapshotted onto each
    ticket at issue time, so changing this policy — or the venue timezone — never
    moves an already-issued ticket (settings spec §14).
    """

    id_prefix = "tvp"

    VALIDITY_TYPE_CHOICES = [
        ("END_OF_VISIT_DAY", "End of visit day (23:59:59 venue local)"),
        ("NUMBER_OF_DAYS", "A number of days"),
        ("FIXED_DURATION", "A fixed duration in minutes"),
        ("FIXED_RANGE", "A fixed date/time range"),
        ("SESSION_BASED", "Bound to a session"),
        ("MEMBERSHIP_BASED", "Follows membership rights"),
        ("CUSTOM", "Custom access rule"),
    ]

    venue = models.ForeignKey(
        "tenancy.Venue", on_delete=models.PROTECT, related_name="validity_policies"
    )
    #: Null means the venue-wide default; otherwise this policy is for one product.
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.CASCADE, related_name="validity_policies"
    )
    validity_type = models.CharField(
        max_length=20, choices=VALIDITY_TYPE_CHOICES, default="END_OF_VISIT_DAY"
    )
    number_of_days = models.PositiveSmallIntegerField(default=1)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    entry_start_time = models.TimeField(null=True, blank=True)
    entry_cutoff_time = models.TimeField(null=True, blank=True)
    grace_minutes = models.PositiveSmallIntegerField(default=0)
    reentry_allowed = models.BooleanField(default=False)
    reentry_window_minutes = models.PositiveIntegerField(null=True, blank=True)
    max_entries = models.PositiveSmallIntegerField(
        default=1, help_text="0 means unlimited entries."
    )
    effective_from = models.DateField(db_index=True)

    objects = models.Manager.from_queryset(EffectiveDatedQuerySet)()

    class Meta:
        ordering = ["-effective_from"]

    def clean(self) -> None:
        if self.validity_type == "NUMBER_OF_DAYS" and self.number_of_days < 1:
            raise ValidationError({"number_of_days": "Enter at least 1 day."})
        if self.validity_type == "FIXED_DURATION" and not self.duration_minutes:
            raise ValidationError({"duration_minutes": "Enter a duration in minutes."})

    def as_snapshot(self) -> dict:
        """The policy frozen onto a ticket at issue time."""
        return {
            "validity_type": self.validity_type,
            "number_of_days": self.number_of_days,
            "duration_minutes": self.duration_minutes,
            "entry_start_time": self.entry_start_time.isoformat() if self.entry_start_time else None,
            "entry_cutoff_time": (
                self.entry_cutoff_time.isoformat() if self.entry_cutoff_time else None
            ),
            "grace_minutes": self.grace_minutes,
            "reentry_allowed": self.reentry_allowed,
            "max_entries": self.max_entries,
        }


class SupportedCurrency(TenantScopedModel):
    """A currency a venue will display or accept (settings spec §15, §23)."""

    id_prefix = "cur"

    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="currencies")
    code = models.CharField(max_length=3, help_text="ISO 4217, e.g. THB.")
    is_base = models.BooleanField(default=False)
    display_enabled = models.BooleanField(default=True)
    payment_enabled = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")], default="ACTIVE"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["venue", "code"], name="uniq_currency_per_venue"),
            # Exactly one base currency per venue.
            models.UniqueConstraint(
                fields=["venue"],
                condition=models.Q(is_base=True),
                name="uniq_base_currency_per_venue",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.upper()
        return super().save(*args, **kwargs)


class ExchangeRate(TenantScopedModel, ProtectedModel):
    """A manually configured conversion rate (settings spec §16–§22).

    Stored as an exact decimal, never a float. Direction is always "1 from = rate
    to", so ``1 USD = 33.10 THB`` is ``from=USD, to=THB, rate=33.100000``.

    Ended rates are retained rather than deleted, because completed orders
    reference the rate that applied at the time.
    """

    id_prefix = "fxr"

    venue = models.ForeignKey(
        "tenancy.Venue", on_delete=models.PROTECT, related_name="exchange_rates"
    )
    from_currency = models.CharField(max_length=3)
    to_currency = models.CharField(max_length=3)
    rate = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        help_text="Units of to_currency per one from_currency.",
    )
    effective_from = models.DateField(db_index=True)
    effective_until = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=12,
        choices=[("ACTIVE", "Active"), ("ENDED", "Ended")],
        default="ACTIVE",
    )
    source = models.CharField(max_length=40, default="MANUAL")
    created_by = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-effective_from"]
        constraints = [
            # No two active rows for the same pair starting on the same day, which
            # would make the applicable rate ambiguous (settings spec §22).
            models.UniqueConstraint(
                fields=["venue", "from_currency", "to_currency", "effective_from"],
                condition=models.Q(status="ACTIVE"),
                name="uniq_active_rate_per_pair_and_date",
            ),
        ]

    def save(self, *args, **kwargs):
        self.from_currency = (self.from_currency or "").upper()
        self.to_currency = (self.to_currency or "").upper()
        if self.rate is not None:
            # Normalise to the platform's fixed precision.
            self.rate = parse_rate(self.rate)
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        if self.from_currency and self.from_currency == self.to_currency:
            raise ValidationError({"to_currency": "The two currencies must differ."})
        if self.rate is not None and Decimal(self.rate) <= 0:
            raise ValidationError({"rate": "Enter a rate greater than zero."})
        if self.effective_until and self.effective_until < self.effective_from:
            raise ValidationError(
                {"effective_until": "The end date must not be before the start."}
            )

    @property
    def direction_label(self) -> str:
        return rate_direction_label(self.from_currency, self.to_currency, self.rate)

    def __str__(self) -> str:
        return self.direction_label


class VenueTimezoneChange(TenantScopedModel, ProtectedModel):
    """Audit-grade record of a venue timezone change (settings spec §32).

    Kept separate from the venue row so the history of what the zone *was* is
    recoverable — issued tickets snapshot their own zone, but operators still need
    to see when the venue's zone moved and who moved it.
    """

    id_prefix = "vtz"

    venue = models.ForeignKey(
        "tenancy.Venue", on_delete=models.PROTECT, related_name="timezone_changes"
    )
    previous_timezone = models.CharField(max_length=64, validators=[validate_iana_timezone])
    new_timezone = models.CharField(max_length=64, validators=[validate_iana_timezone])
    reason = models.TextField()
    changed_by = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
