"""Price rules.

A price is resolved from configurable rules, never from a constant. When two rules
match, the higher priority wins and, on a tie, the more specific scope (R5.2). If
nothing matches, the ticket type is simply unavailable for that request — the
platform never invents a fallback price (R5.6).
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TenantScopedModel

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class PriceRule(TenantScopedModel):
    """One priced rule for a ticket type under some conditions (R5.1)."""

    id_prefix = "prc"

    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="price_rules")
    ticket_type = models.ForeignKey(
        "catalog.TicketType", on_delete=models.PROTECT, related_name="price_rules"
    )
    #: Amount in integer minor units of ``currency``. Never a float.
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="THB")

    date_from = models.DateField(null=True, blank=True)
    date_until = models.DateField(null=True, blank=True)
    weekdays = models.JSONField(
        default=list, blank=True, help_text="Empty means every weekday."
    )
    channel = models.CharField(max_length=12, blank=True, help_text="Blank means any channel.")
    partner = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    session = models.ForeignKey(
        "inventory.Session", null=True, blank=True, on_delete=models.CASCADE, related_name="price_rules"
    )
    quantity_min = models.PositiveSmallIntegerField(null=True, blank=True)
    quantity_max = models.PositiveSmallIntegerField(null=True, blank=True)

    priority = models.IntegerField(
        default=0, help_text="Higher wins. Ties break on scope specificity."
    )
    status = models.CharField(
        max_length=12, choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")], default="ACTIVE"
    )
    label = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-priority", "-date_from"]
        indexes = [
            models.Index(fields=["ticket_type", "status", "priority"]),
            models.Index(fields=["venue", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.ticket_type_id} @ {self.amount_minor} {self.currency}"

    @property
    def specificity(self) -> int:
        """How narrow this rule is; the tie-breaker after priority (R5.2).

        Counting set conditions makes the comparison deterministic, which matters
        because two rules at equal priority must never resolve differently between
        the quote and the confirmation.
        """
        score = 0
        for value in (
            self.date_from,
            self.date_until,
            self.channel or None,
            self.partner_id,
            self.session_id,
            self.quantity_min,
            self.quantity_max,
        ):
            if value is not None:
                score += 1
        if self.weekdays:
            score += 1
        return score
