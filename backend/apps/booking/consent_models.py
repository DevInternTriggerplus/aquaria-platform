"""PDPA consent: versioned notice, immutable consent records.

Two rules from R12 shape this:

* **Nothing is submitted before consent.** The required item must be accepted before
  any personal data is persisted (R12.2). The confirm flow calls
  ``check_required`` before it writes a customer row.
* **Consent records are immutable evidence.** A record captures the exact notice and
  consent-text version shown, the language, the per-item decisions, timestamps in
  UTC and venue-local, and the source — and is never edited or deleted (R12.10,
  R12.12). Withdrawal is a new record, not a mutation.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import ProtectedManager, ProtectedModel, TenantScopedModel, SoftDeleteNotAllowed

#: Consent items. The required one gates the booking; the rest are freely optional
#: and declining them never blocks or degrades the purchase (R12.4, R12.6).
CONSENT_ITEMS = [
    ("BOOKING_SERVICE", True, "Booking and service delivery"),
    ("MARKETING", False, "Marketing and promotional communication"),
    ("ANALYTICS", False, "Analytics, personalization and service improvement"),
    ("PARTNER_SHARING", False, "Sharing with partners or affiliates"),
]

REQUIRED_ITEMS = frozenset(code for code, required, _ in CONSENT_ITEMS if required)


class PrivacyNotice(TenantScopedModel, ProtectedModel):
    """A published, immutable version of the privacy notice and consent text (R12.12).

    A change never mutates a published version; it creates a new one. A returning
    customer performing a new consent-requiring action re-consents against the newest
    version (R12.13).
    """

    id_prefix = "pnt"

    version = models.CharField(max_length=20, db_index=True)
    language = models.CharField(max_length=8, default="en")
    controller_name = models.CharField(max_length=200)
    controller_contact = models.CharField(max_length=200)
    dpo_contact = models.CharField(max_length=200)
    #: Full structured notice: purposes, lawful bases, retention, recipients, rights,
    #: cross-border transfer. Rendered to the customer by the dialog (R12.3).
    body = models.JSONField(default=dict)
    notice_url = models.URLField(blank=True)
    published_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(default=True)

    objects = ProtectedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "version", "language"], name="uniq_notice_version_lang"
            ),
        ]
        ordering = ["-published_at"]

    def save(self, *args, **kwargs):
        # A published version is frozen; only the is_current flag may move, via the
        # service, and even that is a targeted update rather than a rewrite.
        if not self._state.adding and set(kwargs.get("update_fields") or []) - {
            "is_current",
            "updated_at",
        }:
            raise SoftDeleteNotAllowed("A published privacy notice version is immutable.")
        return super().save(*args, **kwargs)


class ConsentRecord(TenantScopedModel, ProtectedModel):
    """Immutable proof of the consent a customer gave (R12.10)."""

    id_prefix = "con"

    #: Hashed contact when no customer row exists yet (consent precedes persistence).
    customer = models.ForeignKey(
        "booking.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="consent_records"
    )
    contact_hash = models.CharField(max_length=64, db_index=True)
    booking = models.ForeignKey(
        "booking.Booking", null=True, blank=True, on_delete=models.PROTECT, related_name="consent_records"
    )
    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="+")

    notice = models.ForeignKey(PrivacyNotice, on_delete=models.PROTECT, related_name="records")
    notice_version = models.CharField(max_length=20)
    language = models.CharField(max_length=8, default="en")

    #: {"BOOKING_SERVICE": true, "MARKETING": false, ...}
    items = models.JSONField(default=dict)

    channel = models.CharField(max_length=12, default="ONLINE")
    device = models.CharField(max_length=40, blank=True)
    staff_actor = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    captured_at_utc = models.DateTimeField(db_index=True)
    captured_at_local = models.CharField(max_length=40, blank=True)

    #: A withdrawal is a fresh record that supersedes an earlier one (R12.14).
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    is_withdrawal = models.BooleanField(default=False)

    objects = ProtectedManager()

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "contact_hash", "captured_at_utc"]),
        ]
        ordering = ["-captured_at_utc"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise SoftDeleteNotAllowed("Consent records are immutable evidence.")
        return super().save(*args, **kwargs)

    def granted(self, code: str) -> bool:
        return bool(self.items.get(code))


class VerificationChallenge(TenantScopedModel):
    """A one-time code proving ownership of a booking's email (R16.2, R16.11).

    Single-use and short-lived. Never reveals whether a booking exists: the code is
    only issued when the number/email pair matches, and the caller always gets the
    same "if that booking exists…" response (R16.3).
    """

    id_prefix = "vch"

    booking = models.ForeignKey(
        "booking.Booking", on_delete=models.CASCADE, related_name="verification_challenges"
    )
    purpose = models.CharField(max_length=30, default="MANAGE_BOOKING")
    contact_hash = models.CharField(max_length=64, db_index=True)
    #: A hash of the code — never the code itself.
    code_hash = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "booking", "contact_hash"])]
        ordering = ["-issued_at"]

    @property
    def is_live(self) -> bool:
        from django.utils import timezone

        return self.consumed_at is None and self.expires_at > timezone.now()
