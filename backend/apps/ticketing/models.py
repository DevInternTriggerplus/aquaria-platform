"""Issued tickets and their access rights.

A ticket carries a **frozen** validity window. ``valid_from``/``valid_until`` are
computed once at issue time from the policy and the venue's timezone, and stored
alongside the timezone and policy that produced them. Changing the venue's
timezone or the validity policy afterwards must never move an already-issued
ticket (settings spec §14), and reading the stored window is what guarantees that.

The QR payload is unguessable, tenant-scoped, signed, and carries no personal data
(R15.2).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from django.db import models

from apps.core.models import ProtectedManager, ProtectedModel, TenantScopedModel

TICKET_STATE_CHOICES = [
    ("ISSUED", "Issued"),
    ("VALID", "Valid"),
    ("USED", "Used"),
    ("PARTIALLY_USED", "Partially used"),
    ("EXPIRED", "Expired"),
    ("CANCELLED", "Cancelled"),
    ("VOIDED", "Voided"),
    ("REFUNDED", "Refunded"),
    ("TRANSFERRED", "Transferred"),
    ("BLOCKED", "Blocked"),
]


def end_of_visit_day(visit_date: dt.date, timezone_name: str) -> dt.datetime:
    """23:59:59 on ``visit_date`` in the venue's zone (settings spec §37).

    Returned as an aware datetime, so storage in UTC is exact and the boundary is
    the venue's midnight rather than the server's.
    """
    tz = ZoneInfo(timezone_name)
    return dt.datetime.combine(visit_date, dt.time(23, 59, 59), tzinfo=tz)


def start_of_visit_day(visit_date: dt.date, timezone_name: str) -> dt.datetime:
    tz = ZoneInfo(timezone_name)
    return dt.datetime.combine(visit_date, dt.time(0, 0, 0), tzinfo=tz)


class Ticket(TenantScopedModel, ProtectedModel):
    """An individually redeemable admission artefact (R15.1)."""

    id_prefix = "tck"

    ticket_number = models.CharField(max_length=40, db_index=True)
    booking = models.ForeignKey("booking.Booking", on_delete=models.PROTECT, related_name="tickets")
    booking_item = models.ForeignKey(
        "booking.BookingItem", on_delete=models.PROTECT, related_name="tickets"
    )
    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="tickets")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    ticket_type = models.ForeignKey("catalog.TicketType", on_delete=models.PROTECT, related_name="+")
    segment = models.ForeignKey(
        "catalog.CustomerSegment", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    session = models.ForeignKey(
        "inventory.Session", null=True, blank=True, on_delete=models.PROTECT, related_name="tickets"
    )

    state = models.CharField(
        max_length=16, choices=TICKET_STATE_CHOICES, default="VALID", db_index=True
    )
    visit_date = models.DateField(db_index=True)

    # --- frozen validity snapshot ----------------------------------------- #
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(db_index=True)
    validity_timezone = models.CharField(max_length=64)
    validity_type = models.CharField(max_length=20, default="END_OF_VISIT_DAY")
    validity_policy = models.JSONField(default=dict, blank=True)

    # --- entry allowance --------------------------------------------------- #
    entry_allowance = models.PositiveSmallIntegerField(
        default=1, help_text="0 means unlimited."
    )
    entries_used = models.PositiveSmallIntegerField(default=0)
    reentry_allowed = models.BooleanField(default=False)
    reentry_window_minutes = models.PositiveIntegerField(null=True, blank=True)
    first_entry_at = models.DateTimeField(null=True, blank=True)
    last_entry_at = models.DateTimeField(null=True, blank=True)

    proof_required = models.BooleanField(default=False)
    blocked_reason = models.CharField(max_length=200, blank=True)

    #: Signed, opaque, no personal data. Regenerated on reissue supersedes the old.
    qr_payload = models.CharField(max_length=255, unique=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    reissue_count = models.PositiveSmallIntegerField(default=0)

    objects = ProtectedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "ticket_number"], name="uniq_ticket_number_per_tenant"
            ),
            models.CheckConstraint(
                condition=models.Q(entry_allowance=0)
                | models.Q(entries_used__lte=models.F("entry_allowance")),
                name="entries_never_exceed_allowance",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state", "visit_date"]),
            models.Index(fields=["venue", "visit_date"]),
        ]
        ordering = ["ticket_number"]

    def __str__(self) -> str:
        return self.ticket_number

    @property
    def unlimited_entries(self) -> bool:
        return self.entry_allowance == 0

    @property
    def entries_remaining(self) -> int | None:
        if self.unlimited_entries:
            return None
        return max(self.entry_allowance - self.entries_used, 0)

    def is_expired_at(self, moment: dt.datetime) -> bool:
        """Expiry against the *stored* window, not current configuration."""
        return moment > self.valid_until

    def is_not_yet_valid_at(self, moment: dt.datetime) -> bool:
        return moment < self.valid_from
