"""Capacity: sessions, holds and the guarantee that the last ticket sells once.

Capacity is authoritative and enforced under concurrency (R10.5). The mechanism is
deliberately boring: a single row per session carries the counters, and every
decrement happens inside a transaction that has taken a row lock via
``select_for_update``. Two simultaneous requests for the last unit therefore
serialize, one wins, and the other gets a distinct "just sold out" outcome
(R10.6) — never an oversell.

``Session`` covers both product sessions (timed entry) and show sessions, split by
``kind``, so all capacity flows through one code path rather than two that can
drift apart.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db import models, transaction

from apps.core.errors import JustSoldOut
from apps.core.models import ProtectedModel, TenantScopedModel

SESSION_STATUS_CHOICES = [
    ("SCHEDULED", "Scheduled"),
    ("AVAILABLE", "Available"),
    ("LIMITED", "Limited availability"),
    ("FULL", "Full"),
    ("DELAYED", "Delayed"),
    ("CANCELLED", "Cancelled"),
    ("COMPLETED", "Completed"),
    ("HIDDEN", "Hidden"),
]


class Session(TenantScopedModel):
    """A capacity-bearing, time-bound instance of a product or show (R8.2)."""

    id_prefix = "ses"

    KIND_CHOICES = [("PRODUCT", "Product session"), ("SHOW", "Show session")]
    RESERVATION_MODE_CHOICES = [
        ("NONE", "No reservation required"),
        ("OPTIONAL", "Reservation optional"),
        ("REQUIRED", "Reservation required"),
    ]

    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="sessions")
    area = models.ForeignKey(
        "tenancy.Area", null=True, blank=True, on_delete=models.PROTECT, related_name="sessions"
    )
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default="PRODUCT")
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="sessions"
    )
    experience = models.ForeignKey(
        "catalog.Experience", null=True, blank=True, on_delete=models.PROTECT, related_name="sessions"
    )

    #: Operating date in the venue's timezone, not UTC.
    session_date = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField(null=True, blank=True)

    #: Null capacity means uncapped. Aquaria's general admission is uncapped, so a
    #: quote there legitimately takes no hold.
    capacity = models.PositiveIntegerField(null=True, blank=True)
    confirmed_count = models.PositiveIntegerField(default=0)
    held_count = models.PositiveIntegerField(default=0)

    booking_cutoff_minutes = models.PositiveIntegerField(default=0)
    grace_minutes = models.PositiveSmallIntegerField(default=0)
    reservation_mode = models.CharField(
        max_length=10, choices=RESERVATION_MODE_CHOICES, default="REQUIRED"
    )
    check_in_required = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=SESSION_STATUS_CHOICES, default="SCHEDULED")
    publication_state = models.CharField(
        max_length=10,
        choices=[("DRAFT", "Draft"), ("PUBLISHED", "Published"), ("ARCHIVED", "Archived")],
        default="DRAFT",
    )
    customer_visible = models.BooleanField(default=True)
    delayed_start_time = models.TimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    #: Set when materialized from a recurring pattern, so overrides are visible.
    origin = models.CharField(
        max_length=12,
        choices=[
            ("ONE_OFF", "One-off addition"),
            ("PATTERN", "From a recurring pattern"),
            ("OVERRIDE", "Date-specific override"),
        ],
        default="ONE_OFF",
    )

    class Meta:
        ordering = ["session_date", "start_time"]
        indexes = [
            models.Index(fields=["venue", "session_date", "kind"]),
            models.Index(fields=["product", "session_date"]),
        ]
        constraints = [
            # Confirmed consumption can never exceed capacity. Enforced by the
            # database so no service, import or admin tool can bypass it (R9.3).
            models.CheckConstraint(
                condition=models.Q(capacity__isnull=True)
                | models.Q(confirmed_count__lte=models.F("capacity")),
                name="confirmed_never_exceeds_capacity",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.session_date} {self.start_time} ({self.kind})"

    @property
    def is_uncapped(self) -> bool:
        return self.capacity is None

    @property
    def remaining(self) -> int | None:
        """Capacity minus confirmed minus active holds; never negative (R8.4)."""
        if self.capacity is None:
            return None
        return max(self.capacity - self.confirmed_count - self.held_count, 0)

    def clean(self) -> None:
        if self.kind == "PRODUCT" and not self.product_id:
            raise ValidationError({"product": "A product session needs a product."})
        if self.kind == "SHOW" and not self.experience_id:
            raise ValidationError({"experience": "A show session needs an experience."})


class Hold(TenantScopedModel):
    """A temporary, expiring reservation of capacity during checkout (R10.1).

    Scoped to tenant, session and channel: one channel's hold can never be
    consumed by another channel's confirmation (R10.10).
    """

    id_prefix = "hld"

    STATE_CHOICES = [
        ("HELD", "Held"),
        ("CONFIRMED", "Confirmed"),
        ("EXPIRED", "Expired"),
        ("RELEASED", "Released"),
    ]

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="holds")
    cart_ref = models.CharField(max_length=40, db_index=True)
    channel = models.CharField(max_length=12, default="ONLINE")
    quantity = models.PositiveIntegerField()
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default="HELD")
    expires_at = models.DateTimeField(db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["session", "state"]),
            models.Index(fields=["state", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} on {self.session_id} [{self.state}]"

    @property
    def is_live(self) -> bool:
        from django.utils import timezone

        return self.state == "HELD" and self.expires_at > timezone.now()


@transaction.atomic
def acquire_hold(
    *,
    session: Session,
    quantity: int,
    cart_ref: str,
    channel: str,
    ttl_seconds: int = 600,
) -> Hold | None:
    """Reserve ``quantity`` on ``session``, or raise :class:`JustSoldOut`.

    Returns ``None`` for uncapped inventory, where a hold would be meaningless.
    The row lock is what makes this safe: concurrent callers queue on it, so the
    availability check and the increment cannot interleave.
    """
    from django.utils import timezone

    if quantity <= 0:
        raise ValueError("quantity must be positive")

    locked = Session.objects.select_for_update().get(pk=session.pk)
    if locked.capacity is None:
        return None

    available = locked.capacity - locked.confirmed_count - locked.held_count
    if available < quantity:
        raise JustSoldOut(
            details={"session_id": locked.id, "remaining": max(available, 0)},
            log_detail=f"hold refused: want {quantity}, available {available}",
        )

    locked.held_count = locked.held_count + quantity
    if locked.capacity - locked.confirmed_count - locked.held_count <= 0:
        locked.status = "FULL"
    locked.save(update_fields=["held_count", "status", "updated_at"])

    return Hold.objects.create(
        tenant_id=locked.tenant_id,
        session=locked,
        cart_ref=cart_ref,
        channel=channel,
        quantity=quantity,
        state="HELD",
        expires_at=timezone.now() + dt.timedelta(seconds=ttl_seconds),
    )


@transaction.atomic
def confirm_hold(hold: Hold, *, allow_late: bool = True) -> bool:
    """Turn a hold into confirmed consumption. Returns True if it was a late confirm.

    R10.9: if the hold lapsed but equivalent inventory is still free, re-acquire it
    and record that late confirmation happened. R10.8: if it lapsed and the
    inventory has gone, refuse — the caller then routes the payment to
    reconciliation rather than confirming a booking that cannot be honoured.
    """
    from django.utils import timezone

    locked_hold = Hold.objects.select_for_update().get(pk=hold.pk)
    locked = Session.objects.select_for_update().get(pk=locked_hold.session_id)

    if locked_hold.state == "CONFIRMED":
        return False

    lapsed = locked_hold.expires_at <= timezone.now()
    if lapsed:
        if not allow_late:
            raise JustSoldOut(details={"session_id": locked.id}, log_detail="hold lapsed")
        # The held units were already reclaimed, so take capacity afresh.
        available = locked.capacity - locked.confirmed_count - locked.held_count
        if locked.capacity is not None and available < locked_hold.quantity:
            raise JustSoldOut(
                details={"session_id": locked.id, "remaining": max(available, 0)},
                log_detail="late confirmation refused: inventory gone",
            )
    else:
        locked.held_count = max(locked.held_count - locked_hold.quantity, 0)

    locked.confirmed_count = locked.confirmed_count + locked_hold.quantity
    if locked.capacity is not None and locked.capacity - locked.confirmed_count <= 0:
        locked.status = "FULL"
    locked.save(update_fields=["held_count", "confirmed_count", "status", "updated_at"])

    locked_hold.state = "CONFIRMED"
    locked_hold.confirmed_at = timezone.now()
    locked_hold.save(update_fields=["state", "confirmed_at", "updated_at"])
    return lapsed


@transaction.atomic
def release_hold(hold: Hold, *, reason: str = "released") -> None:
    """Return held units to available inventory (R10.3)."""
    from django.utils import timezone

    locked_hold = Hold.objects.select_for_update().get(pk=hold.pk)
    if locked_hold.state != "HELD":
        return
    locked = Session.objects.select_for_update().get(pk=locked_hold.session_id)
    locked.held_count = max(locked.held_count - locked_hold.quantity, 0)
    if locked.status == "FULL" and (locked.capacity or 0) - locked.confirmed_count - locked.held_count > 0:
        locked.status = "AVAILABLE"
    locked.save(update_fields=["held_count", "status", "updated_at"])

    locked_hold.state = "RELEASED"
    locked_hold.released_at = timezone.now()
    locked_hold.save(update_fields=["state", "released_at", "updated_at"])


def reclaim_expired_holds() -> int:
    """Return expired holds' units to inventory (R10.4).

    Run by a scheduled job. Kept idempotent so a retry cannot double-credit.
    """
    from django.utils import timezone

    now = timezone.now()
    expired = Hold.objects.filter(state="HELD", expires_at__lte=now)
    count = 0
    for hold in expired.iterator():
        with transaction.atomic():
            locked_hold = Hold.objects.select_for_update().get(pk=hold.pk)
            if locked_hold.state != "HELD":
                continue
            locked = Session.objects.select_for_update().get(pk=locked_hold.session_id)
            locked.held_count = max(locked.held_count - locked_hold.quantity, 0)
            if (
                locked.status == "FULL"
                and (locked.capacity or 0) - locked.confirmed_count - locked.held_count > 0
            ):
                locked.status = "AVAILABLE"
            locked.save(update_fields=["held_count", "status", "updated_at"])
            locked_hold.state = "EXPIRED"
            locked_hold.released_at = now
            locked_hold.save(update_fields=["state", "released_at", "updated_at"])
            count += 1
    return count
