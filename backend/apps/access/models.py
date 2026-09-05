"""Gate validation: scan events and their decisions.

Every scan attempt is recorded, admitted or rejected, with ticket, booking,
decision, reason, timestamp, access point, device and operator (R32.5). The table
is append-only: a scan record is evidence and is never edited or removed.

Offline operation is expected. When a device syncs queued scans, a single-entry
ticket that was admitted at two different access points is *flagged*, never
silently discarded, and surfaces in an exception report (R32.8).
"""

from __future__ import annotations

from django.db import models

from apps.core.models import ProtectedManager, ProtectedModel, TenantScopedModel

#: The closed set of gate outcomes (R32.2). Exactly one is returned per scan.
DECISION_CHOICES = [
    ("ADMIT", "Admit"),
    ("ADMIT_WITH_CHECK", "Admit — proof required"),
    ("REJECT_ALREADY_USED", "Reject — already used"),
    ("REJECT_WRONG_DATE", "Reject — wrong date"),
    ("REJECT_WRONG_SESSION", "Reject — wrong session"),
    ("REJECT_WRONG_VENUE", "Reject — wrong venue or gate"),
    ("REJECT_CANCELLED", "Reject — cancelled"),
    ("REJECT_REFUNDED", "Reject — refunded"),
    ("REJECT_VOIDED", "Reject — voided"),
    ("REJECT_BLOCKED", "Reject — blocked"),
    ("REJECT_NOT_YET_VALID", "Reject — not yet valid"),
    ("REJECT_EXPIRED", "Reject — expired"),
    ("REJECT_UNKNOWN_CODE", "Reject — unknown code"),
    ("REJECT_DEVICE", "Reject — unregistered or deactivated device"),
]

ADMIT_DECISIONS = frozenset({"ADMIT", "ADMIT_WITH_CHECK"})


class ScanEvent(TenantScopedModel, ProtectedModel):
    """One scan attempt at an access point."""

    id_prefix = "scn"

    ticket = models.ForeignKey(
        "ticketing.Ticket", null=True, blank=True, on_delete=models.PROTECT, related_name="scan_events"
    )
    booking = models.ForeignKey(
        "booking.Booking", null=True, blank=True, on_delete=models.PROTECT, related_name="scan_events"
    )
    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="scan_events")
    access_point = models.ForeignKey(
        "tenancy.AccessPoint", null=True, blank=True, on_delete=models.PROTECT, related_name="scan_events"
    )
    device = models.ForeignKey(
        "tenancy.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="scan_events"
    )
    operator = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="scan_events"
    )

    decision = models.CharField(max_length=30, choices=DECISION_CHOICES, db_index=True)
    reason = models.CharField(max_length=200, blank=True)
    #: Present for a rejected scan we could not resolve to a ticket. Truncated and
    #: never used to reconstruct a payload.
    scanned_ref = models.CharField(max_length=80, blank=True)

    scanned_at = models.DateTimeField(db_index=True)
    #: Venue-local rendering of ``scanned_at``, for operators and day reports.
    scanned_at_local = models.CharField(max_length=40, blank=True)

    #: True when captured offline and synced later (R32.6).
    captured_offline = models.BooleanField(default=False)
    synced_at = models.DateTimeField(null=True, blank=True)

    #: Supervisor override of a rejection (R32.9). Requires a reason.
    override_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="overrides"
    )
    override_reason = models.CharField(max_length=200, blank=True)

    correlation_id = models.CharField(max_length=40, blank=True)

    objects = ProtectedManager()

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "decision", "scanned_at"]),
            models.Index(fields=["ticket", "scanned_at"]),
            models.Index(fields=["venue", "scanned_at"]),
        ]
        ordering = ["-scanned_at"]

    def __str__(self) -> str:
        return f"{self.decision} @ {self.scanned_at:%Y-%m-%d %H:%M:%S}"

    @property
    def admitted(self) -> bool:
        return self.decision in ADMIT_DECISIONS

    def save(self, *args, **kwargs):
        if not self._state.adding:
            from apps.core.models import SoftDeleteNotAllowed

            raise SoftDeleteNotAllowed("Scan events are append-only evidence.")
        return super().save(*args, **kwargs)


class OfflineScanConflict(TenantScopedModel, ProtectedModel):
    """A single-entry ticket admitted at two access points (R32.8).

    Recorded rather than resolved automatically: both scans are real events, and a
    human needs to decide what happened. Surfaces in the exception report.
    """

    id_prefix = "ofc"

    ticket = models.ForeignKey(
        "ticketing.Ticket", on_delete=models.PROTECT, related_name="offline_conflicts"
    )
    first_scan = models.ForeignKey(ScanEvent, on_delete=models.PROTECT, related_name="+")
    second_scan = models.ForeignKey(ScanEvent, on_delete=models.PROTECT, related_name="+")
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)

    objects = ProtectedManager()

    class Meta:
        ordering = ["-detected_at"]
