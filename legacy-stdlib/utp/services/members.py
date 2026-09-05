"""Membership and loyalty points (add_features §31-§34).

A member has a live ``points_balance`` and an append-only ``point_ledger`` behind
it. The balance is the fast authoritative figure; the ledger is the audit trail
and the source of truth for reconciliation and restoration.

Two properties matter and are enforced structurally rather than by convention:

* **Points are spent exactly once.** Redemption moves the balance with a single
  conditional UPDATE floored at zero (mirroring the capacity engine), so two
  concurrent redemptions can never both succeed on the same points, and the CHECK
  constraint on ``members.points_balance`` is the belt to that braces (§69).
* **A historical redemption is never re-valued.** The conversion rate used is
  snapshotted onto the ledger row as an exact decimal string, so changing the
  current rate later cannot move what a past redemption was worth (§33).

Point-to-cash conversion
-------------------------
The conversion is configured as *minor currency units per point* (a `Decimal`
string), which keeps the arithmetic exact in integer satang. Examples from the
spec: ``1`` point = ``1`` THB → 100 satang per point → rate ``"100"``; ``100``
points = ``10`` THB → 1000 satang / 100 points → rate ``"10"``.

Points as settlement, not discount
-----------------------------------
Redeemed value is the member's own accrued value, so — like a gift card — it
settles the bill through ``cart.settlements`` and is **not** booked as a sales
discount (§16, §68). Revenue and tax are unaffected; only the amount collected by
the payment method goes down.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..core.audit import AuditLog
from ..core.clock import Clock, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.ids import hash_identifier, new_id
from ..core.money import parse_rate
from .authz import AuthorizationService

#: Default conversion when a tenant has configured none: 1 point = 1 minor unit.
DEFAULT_POINTS_RATE = "1"


class MemberService:
    """Loyalty membership, point balances, redemption and conversion."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config

    # ------------------------------------------------------------------ #
    # Enrolment and lookup
    # ------------------------------------------------------------------ #

    def enrol(
        self,
        ctx: RequestContext,
        *,
        email: str,
        tier: str = "STANDARD",
        customer_id: str | None = None,
        require_permission: bool = True,
    ) -> dict[str, Any]:
        """Create or return the member for an email (idempotent)."""
        if require_permission:
            self.authz.require_page(ctx, "Member Rewards", "ADD")
        email_norm = (email or "").strip().lower()
        if "@" not in email_norm:
            raise ValidationError({"email": "Enter a valid email address."})
        email_hash = hash_identifier(email_norm)
        existing = self.db.query_one(
            "SELECT id FROM members WHERE tenant_id = ? AND email_hash = ?",
            (ctx.tenant_id, email_hash),
        )
        if existing is not None:
            return self.get(ctx, existing["id"])
        member_id = new_id("mbr")
        now = to_iso(self.clock.now())
        self.db.insert(
            "members",
            {
                "id": member_id,
                "tenant_id": ctx.tenant_id,
                "email_hash": email_hash,
                "customer_id": customer_id,
                "tier": tier,
                "points_balance": 0,
                "status": "ACTIVE",
                "created_at": now,
            },
        )
        self.audit.record(
            ctx, "MEMBER_ENROL", target_type="member", target_id=member_id, new={"tier": tier}
        )
        return self.get(ctx, member_id)

    def get(self, ctx: RequestContext, member_id: str) -> dict[str, Any]:
        return self.authz.load_scoped(ctx, "members", member_id, entity="member")

    def find_by_email(self, ctx: RequestContext, email: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT id FROM members WHERE tenant_id = ? AND email_hash = ?",
            (ctx.tenant_id, hash_identifier((email or "").strip().lower())),
        )
        return self.get(ctx, row["id"]) if row else None

    def balance(self, ctx: RequestContext, member_id: str) -> int:
        return int(self.get(ctx, member_id)["points_balance"])

    # ------------------------------------------------------------------ #
    # Earning
    # ------------------------------------------------------------------ #

    def earn(
        self,
        ctx: RequestContext,
        *,
        member_id: str,
        points: int,
        reason: str | None = None,
        booking_id: str | None = None,
    ) -> dict[str, Any]:
        """Credit points to a member and post an EARN ledger entry."""
        points = int(points)
        if points <= 0:
            raise ValidationError({"points": "Enter a positive number of points to award."})
        member = self.get(ctx, member_id)
        now = to_iso(self.clock.now())
        with self.db.transaction(immediate=True):
            self.db.compare_and_increment(
                "members", member_id, counter="points_balance", delta=points, tenant_id=ctx.tenant_id
            )
            self._post_ledger(
                ctx, member_id=member_id, entry_type="EARN", points=points, booking_id=booking_id, reason=reason
            )
            self.audit.record(
                ctx, "POINTS_EARN", target_type="member", target_id=member_id,
                previous={"balance": int(member["points_balance"])},
                new={"balance": int(member["points_balance"]) + points, "points": points},
                reason=reason,
            )
        return self.get(ctx, member_id)

    # ------------------------------------------------------------------ #
    # Conversion
    # ------------------------------------------------------------------ #

    def conversion_rate(self, ctx: RequestContext, *, venue_id: str | None = None) -> Decimal:
        """Configured minor-units-of-cash per point (§33)."""
        configured = self.config.get(ctx, "loyalty.points_rate", venue_id=venue_id)
        return parse_rate(configured if configured is not None else DEFAULT_POINTS_RATE)

    def set_conversion_rate(
        self, ctx: RequestContext, *, rate: str, venue_id: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        """Change the point-to-cash rate (audited; historical rows keep their rate)."""
        self.authz.require_page(ctx.for_venue(venue_id) if venue_id else ctx, "Member Rewards", "EDIT")
        previous = str(self.conversion_rate(ctx, venue_id=venue_id))
        clean = str(parse_rate(rate))
        self.config.set(
            ctx if not venue_id else ctx.for_venue(venue_id),
            "loyalty.points_rate",
            clean,
            scope_type="VENUE" if venue_id else "TENANT",
            scope_id=venue_id or ctx.tenant_id,
            audit_action="CONFIG_CHANGE",
        )
        self.audit.record(
            ctx, "MEMBER_CONVERSION_RATE_CHANGE", target_type="loyalty_rate",
            target_id=venue_id or ctx.tenant_id, previous={"rate": previous}, new={"rate": clean},
            reason=reason,
        )
        return {"rate": clean, "previous": previous}

    def points_to_minor(self, points: int, rate: Decimal) -> int:
        """Cash value of ``points`` at ``rate`` (minor units per point), floored."""
        return int((Decimal(int(points)) * rate).to_integral_value(rounding="ROUND_FLOOR"))

    def minor_to_points(self, amount_minor: int, rate: Decimal) -> int:
        """Points needed to cover ``amount_minor`` at ``rate`` (rounded up)."""
        if rate <= 0:
            return 0
        return int((Decimal(int(amount_minor)) / rate).to_integral_value(rounding="ROUND_CEILING"))

    # ------------------------------------------------------------------ #
    # Redemption
    # ------------------------------------------------------------------ #

    def redeem_points(
        self,
        ctx: RequestContext,
        *,
        member_id: str,
        points: int,
        booking_id: str | None = None,
        venue_id: str | None = None,
        currency: str = "THB",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Spend ``points`` for their cash value, atomically and exactly once (§69).

        Returns the redemption including the cash ``value_minor`` and the exact rate
        snapshotted, which the caller records against the transaction (§33).
        """
        points = int(points)
        if points <= 0:
            raise ValidationError({"points": "Enter a positive number of points to redeem."})
        member = self.get(ctx, member_id)
        min_redeem = self.config.get_int(ctx, "loyalty.min_redeem_points", venue_id=venue_id)
        if min_redeem and points < min_redeem:
            raise ValidationError(
                {"points": f"Redeem at least {min_redeem} points."},
                message=f"A minimum of {min_redeem} points is required.",
            )
        rate = self.conversion_rate(ctx, venue_id=venue_id)
        value_minor = self.points_to_minor(points, rate)
        now = to_iso(self.clock.now())
        with self.db.transaction(immediate=True):
            # Floor-guarded decrement: only succeeds if the balance covers it, so two
            # concurrent redemptions cannot both spend the same points (§69).
            granted = self.db.compare_and_increment(
                "members", member_id, counter="points_balance", delta=-points, tenant_id=ctx.tenant_id
            )
            if not granted:
                raise ConflictError(
                    "Not enough points to redeem.",
                    code="insufficient_points",
                    details={"available": int(member["points_balance"]), "requested": points},
                )
            ledger_id = self._post_ledger(
                ctx, member_id=member_id, entry_type="REDEEM", points=-points, booking_id=booking_id,
                rate_text=str(rate), value_minor=value_minor, currency=currency, reason=reason,
            )
            self.audit.record(
                ctx, "POINTS_REDEEM", target_type="member", target_id=member_id,
                previous={"balance": int(member["points_balance"])},
                new={"balance": int(member["points_balance"]) - points, "points": points,
                     "value_minor": value_minor, "rate": str(rate)},
                reason=reason,
            )
        return {
            "ledger_id": ledger_id,
            "member_id": member_id,
            "points": points,
            "rate_text": str(rate),
            "value_minor": value_minor,
            "currency": currency,
        }

    def restore_for_booking(
        self, ctx: RequestContext, *, booking_id: str, reason: str = "cancelled"
    ) -> dict[str, Any]:
        """Give redeemed points back when a booking is cancelled or refunded.

        Idempotent: a REDEEM already matched by a RESTORE is not restored twice.
        """
        redeems = self.db.query(
            "SELECT * FROM point_ledger WHERE tenant_id = ? AND booking_id = ? "
            "AND entry_type = 'REDEEM' AND state = 'POSTED'",
            (ctx.tenant_id, booking_id),
        )
        restored_points = 0
        now = to_iso(self.clock.now())
        with self.db.transaction(immediate=True):
            for row in redeems:
                points_back = -int(row["points"])  # REDEEM points are negative
                if points_back <= 0:
                    continue
                self.db.compare_and_increment(
                    "members", row["member_id"], counter="points_balance", delta=points_back,
                    tenant_id=ctx.tenant_id,
                )
                self._post_ledger(
                    ctx, member_id=row["member_id"], entry_type="RESTORE", points=points_back,
                    booking_id=booking_id, rate_text=row["rate_text"], value_minor=int(row["value_minor"]),
                    currency=row["currency"], reason=reason,
                )
                self.db.update("point_ledger", row["id"], {"state": "RESTORED"}, tenant_id=ctx.tenant_id)
                restored_points += points_back
            if restored_points:
                self.audit.record(
                    ctx, "POINTS_RESTORE", target_type="booking", target_id=booking_id,
                    new={"points_restored": restored_points}, reason=reason,
                )
        return {"booking_id": booking_id, "points_restored": restored_points}

    # ------------------------------------------------------------------ #
    # Ledger
    # ------------------------------------------------------------------ #

    def ledger(self, ctx: RequestContext, member_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Member Rewards", "VIEW")
        rows = self.db.query(
            "SELECT * FROM point_ledger WHERE tenant_id = ? AND member_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (ctx.tenant_id, member_id, int(limit)),
        )
        return [dict(r) for r in rows]

    def _post_ledger(
        self,
        ctx: RequestContext,
        *,
        member_id: str,
        entry_type: str,
        points: int,
        booking_id: str | None = None,
        rate_text: str | None = None,
        value_minor: int = 0,
        currency: str | None = None,
        reason: str | None = None,
    ) -> str:
        ledger_id = new_id("pln")
        self.db.insert(
            "point_ledger",
            {
                "id": ledger_id,
                "tenant_id": ctx.tenant_id,
                "member_id": member_id,
                "entry_type": entry_type,
                "points": int(points),
                "rate_text": rate_text,
                "value_minor": int(value_minor),
                "currency": currency,
                "booking_id": booking_id,
                "reason": reason,
                "state": "POSTED",
                "created_at": to_iso(self.clock.now()),
            },
        )
        return ledger_id


__all__ = ["DEFAULT_POINTS_RATE", "MemberService"]
