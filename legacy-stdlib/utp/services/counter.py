"""Counter POS: cashier shifts and staff-assisted sales at the physical counter.

The design deliberately does *not* re-implement selling. A counter sale is an ordinary
booking on the ``COUNTER`` channel, so it flows through the same
``booking.quote`` → ``booking.confirm`` path as the web — which means VAT, service
charge, rounding, the charge snapshot, the receipt and the e-ticket all come for free,
and there is exactly one place that computes a total (R34.1, and the money conventions).
Putting a second pricing path behind the counter is how a POS ends up disagreeing with
the website about what a family ticket costs.

What genuinely *is* new here is the **cashier shift**: opening a drawer with a float,
accumulating the cash taken during the shift, and reconciling counted cash against
expected at close (R34.8, R34.9). That lives in the pre-provisioned ``shift_sessions``
table. Everything else on this service is thin orchestration that adds the counter's
rules on top of the booking service:

* a sale must belong to an open shift when it is paid in cash, so the drawer balances;
* a **void** is a same-day reversal of an unsettled sale, kept distinct from a refund
  with its own permission (R17.1);
* a manual discount needs ``APPLY_MANUAL_DISCOUNT`` and a reason (R34.6);
* a complimentary ticket needs ``ISSUE_COMPLIMENTARY`` and is recorded distinctly from
  paid sales (R34.7).

Every method enforces its permission server-side before doing any work, exactly like the
rest of the platform — the POS UI hiding a button is a convenience, not the control.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, operating_date, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database
from ..core.errors import ConflictError, NotFound, RuleViolation, ValidationError
from ..core.ids import new_id
from .authz import AuthorizationService

#: Payment methods whose cash lands in the physical drawer and therefore must be
#: reconciled at shift close. Card/QR settle to the acquirer, not the till.
_DRAWER_METHODS: frozenset[str] = frozenset({"CASH"})


class CounterService:
    """Cashier shifts and staff-assisted counter sales (R34)."""

    #: Injected by :class:`utp.app.Platform` after construction to avoid cycles.
    booking: Any = None
    payments: Any = None
    settings: Any = None

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
    # Shift lifecycle (R34.8, R34.9)
    # ------------------------------------------------------------------ #

    def open_shift(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        counter_code: str,
        opening_float_minor: int = 0,
    ) -> dict[str, Any]:
        """Open a cashier shift for the signed-in staff member at a counter.

        Selling is a ``Counter Sales.ADD`` capability, so opening the shift that scopes
        those sales requires the same. One open shift per (staff, counter) at a time —
        a second open would make it impossible to say which drawer a sale belongs to.
        """
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Counter Sales", "ADD")
        staff_id = ctx.principal.id
        if int(opening_float_minor) < 0:
            raise ValidationError({"opening_float_minor": "The opening float cannot be negative."})
        existing = self.db.query_one(
            "SELECT id FROM shift_sessions WHERE tenant_id = ? AND venue_id = ? AND counter_code = ? "
            "AND staff_id = ? AND status = 'OPEN'",
            (ctx.tenant_id, venue_id, counter_code, staff_id),
        )
        if existing is not None:
            raise ConflictError(
                "You already have an open shift at this counter. Close it before opening another.",
                details={"shift_id": existing["id"]},
                code="shift_already_open",
            )
        shift_id = new_id("sft")
        now = to_iso(self.clock.now())
        self.db.insert(
            "shift_sessions",
            {
                "id": shift_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "counter_code": counter_code,
                "staff_id": staff_id,
                "status": "OPEN",
                "opening_float_minor": int(opening_float_minor),
                "expected_minor": int(opening_float_minor),
                "opened_at": now,
            },
        )
        self.audit.record(
            vctx,
            "SHIFT_OPEN",
            target_type="shift_session",
            target_id=shift_id,
            new={"counter_code": counter_code, "opening_float_minor": int(opening_float_minor)},
        )
        return self.get_shift(ctx, shift_id)

    def get_shift(self, ctx: RequestContext, shift_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM shift_sessions WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, shift_id)
        )
        if row is None:
            raise NotFound("Shift not found.")
        shift = dict(row)
        self.authz.require_venue(ctx, shift["venue_id"])
        return shift

    def current_shift(
        self, ctx: RequestContext, *, venue_id: str, counter_code: str
    ) -> dict[str, Any] | None:
        """The staff member's open shift at this counter, or ``None``."""
        self.authz.require_venue(ctx, venue_id)
        row = self.db.query_one(
            "SELECT * FROM shift_sessions WHERE tenant_id = ? AND venue_id = ? AND counter_code = ? "
            "AND staff_id = ? AND status = 'OPEN'",
            (ctx.tenant_id, venue_id, counter_code, ctx.principal.id),
        )
        return dict(row) if row is not None else None

    def shift_report(self, ctx: RequestContext, shift_id: str) -> dict[str, Any]:
        """Expected drawer cash and the sales that produced it (R34.8).

        ``expected_minor`` is the opening float plus every cash payment captured on this
        shift; card/QR are reported separately because they never touch the drawer.
        """
        shift = self.get_shift(ctx, shift_id)
        rows = self.db.query(
            "SELECT method, status, amount_minor, tendered_minor, change_minor "
            "FROM payments WHERE tenant_id = ? AND shift_id = ?",
            (ctx.tenant_id, shift_id),
        )
        cash_captured = 0
        noncash_captured = 0
        for row in rows:
            if row["status"] not in ("CAPTURED", "AUTHORIZED"):
                continue
            if row["method"] in _DRAWER_METHODS:
                cash_captured += int(row["amount_minor"])
            else:
                noncash_captured += int(row["amount_minor"])
        expected = int(shift["opening_float_minor"]) + cash_captured
        return {
            "shift_id": shift_id,
            "status": shift["status"],
            "counter_code": shift["counter_code"],
            "opening_float_minor": int(shift["opening_float_minor"]),
            "cash_sales_minor": cash_captured,
            "noncash_sales_minor": noncash_captured,
            "expected_minor": expected,
            "counted_minor": shift["counted_minor"],
            "variance_minor": shift["variance_minor"],
            "payment_count": len(rows),
            "opened_at": shift["opened_at"],
            "closed_at": shift["closed_at"],
        }

    def close_shift(
        self,
        ctx: RequestContext,
        *,
        shift_id: str,
        counted_minor: int,
        approver_id: str | None = None,
        approval_reason: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile counted cash against expected and close the shift (R34.8, R34.9).

        Requires ``CLOSE_SHIFT``. If the variance exceeds the configured tolerance the
        close cannot stand on the cashier's word alone: a principal holding ``APPROVE``
        must accept it with a reason, and the approval is audited (R34.9).
        """
        shift = self.get_shift(ctx, shift_id)
        vctx = ctx.for_venue(shift["venue_id"])
        self.authz.require_action(vctx, "CLOSE_SHIFT", target_type="shift_session", target_id=shift_id)
        if shift["status"] != "OPEN":
            raise ConflictError(
                "This shift is already closed.", details={"status": shift["status"]}, code="shift_not_open"
            )
        if int(counted_minor) < 0:
            raise ValidationError({"counted_minor": "The counted amount cannot be negative."})

        report = self.shift_report(ctx, shift_id)
        expected = int(report["expected_minor"])
        variance = int(counted_minor) - expected
        tolerance = self.config.get_int(ctx, "shift.variance_tolerance_minor", venue_id=shift["venue_id"])

        approved_by = None
        if abs(variance) > tolerance:
            # R34.9 — beyond tolerance, a supervisor with APPROVE must accept the close.
            self.authz.require_action(
                vctx,
                "APPROVE",
                target_type="shift_session",
                target_id=shift_id,
                reason=approval_reason,
            )
            if not (approval_reason or "").strip():
                raise ValidationError(
                    {"approval_reason": "A reason is required to accept a shift variance."}
                )
            approved_by = approver_id or ctx.principal.id

        now = to_iso(self.clock.now())
        self.db.update(
            "shift_sessions",
            shift_id,
            {
                "status": "CLOSED",
                "expected_minor": expected,
                "counted_minor": int(counted_minor),
                "variance_minor": variance,
                "closed_at": now,
                "approved_by": approved_by,
                "approval_reason": approval_reason if approved_by else None,
            },
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            vctx,
            "SHIFT_CLOSE",
            target_type="shift_session",
            target_id=shift_id,
            previous={"status": "OPEN"},
            new={
                "expected_minor": expected,
                "counted_minor": int(counted_minor),
                "variance_minor": variance,
                "approved_by": approved_by,
            },
            reason=approval_reason,
            severity="WARNING" if variance else "INFO",
        )
        return self.shift_report(ctx, shift_id)

    # ------------------------------------------------------------------ #
    # Sales (R34.1 - R34.7)
    # ------------------------------------------------------------------ #

    def quote(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        visit_date: str,
        lines: Sequence[Any],
        promotion_codes: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Price a counter cart. Thin pass-through so the POS shows the real total."""
        self.authz.require_page(ctx.for_venue(venue_id), "Counter Sales", "ADD")
        counter_ctx = self._counter_ctx(ctx)
        quote = self.booking.quote(
            counter_ctx,
            venue_id=venue_id,
            visit_date=visit_date,
            lines=lines,
            promotion_codes=promotion_codes,
        )
        quote = self.booking.start_checkout(counter_ctx, quote)
        return quote.as_dict()

    def sell(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        visit_date: str,
        lines: Sequence[Any],
        customer: dict[str, Any],
        consent_items: dict[str, bool],
        payment_method: str,
        idempotency_key: str,
        shift_id: str | None = None,
        tendered_minor: int | None = None,
        promotion_codes: Sequence[str] = (),
        expected_total_minor: int | None = None,
        reconfirmed: bool = False,
    ) -> dict[str, Any]:
        """Complete a staff-assisted counter sale.

        The sale is an ordinary ``COUNTER``-channel booking, so pricing, tax, receipt
        and ticket issuance are the booking service's job. The counter adds one rule:
        a cash sale must be tied to the seller's open shift so the drawer reconciles.
        """
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Counter Sales", "ADD")
        counter_ctx = self._counter_ctx(ctx)

        if payment_method in _DRAWER_METHODS:
            shift = self._require_open_shift(ctx, venue_id=venue_id, shift_id=shift_id)
            shift_id = shift["id"]

        quote = self.booking.quote(
            counter_ctx,
            venue_id=venue_id,
            visit_date=visit_date,
            lines=lines,
            promotion_codes=promotion_codes,
        )
        quote = self.booking.start_checkout(counter_ctx, quote)
        result = self.booking.confirm(
            counter_ctx,
            quote,
            customer=customer,
            consent_items=consent_items,
            payment_method=payment_method,
            idempotency_key=idempotency_key,
            tendered_minor=tendered_minor,
            shift_id=shift_id,
            expected_total_minor=expected_total_minor,
            reconfirmed=reconfirmed,
        )
        result["shift_id"] = shift_id
        return result

    def void(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        reason: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Void a same-day counter sale before settlement (R17.1, R34.1).

        Void is not refund: it is the reversal of a sale that has not yet settled,
        available on the day of sale and gated by its own ``VOID`` permission. After the
        operating day rolls over it must go through the refund path instead, because the
        money has settled and the reversal is a genuine return.
        """
        vctx = ctx.for_venue(self._booking_venue(ctx, booking_id))
        self.authz.require_action(vctx, "VOID", target_type="booking", target_id=booking_id, reason=reason)
        booking = self.booking.get_booking(ctx, booking_id, mask=False)
        venue = self.db.query_one(
            "SELECT timezone, day_boundary_hour FROM venues WHERE tenant_id = ? AND id = ?",
            (ctx.tenant_id, booking["venue_id"]),
        )
        today = operating_date(
            self.clock.now(), venue["timezone"], int(venue["day_boundary_hour"] or 0)
        ).isoformat()
        sold_day = (booking.get("confirmed_at") or booking.get("created_at") or "")[:10]
        if sold_day and sold_day != today:
            raise RuleViolation(
                "This sale is from a previous day and must be refunded rather than voided.",
                details={"sold_on": sold_day, "today": today},
                code="void_after_settlement",
            )
        if not confirmed:
            from ..core.errors import ConfirmationRequired

            raise ConfirmationRequired(
                "Voiding reverses this sale in full and cannot be undone.",
                details={
                    "booking_number": booking["booking_number"],
                    "amount_minor": int(booking["net_minor"]),
                    "currency": booking["currency"],
                    "irreversible": True,
                    "performed_action": "VOID",
                },
            )
        # A void is a full reversal: cancel the booking and return the money in one step.
        # The booking service enforces the used-ticket rule and the refund ceiling.
        result = self.booking.cancel(
            ctx,
            booking_id,
            reason=reason,
            confirmed=True,
            actor_is_staff=True,
            refund=True,
        )
        self.audit.record(
            vctx,
            "VOID",
            target_type="booking",
            target_id=booking_id,
            new={"amount_minor": int(booking["net_minor"]), "reason": reason},
            reason=reason,
            severity="WARNING",
        )
        result["performed_action"] = "VOID"
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _counter_ctx(self, ctx: RequestContext) -> RequestContext:
        """A copy of the staff context bound to the COUNTER channel.

        Sales made at the till are COUNTER-channel regardless of how the staff member
        signed in, which is what lets channel-specific pricing and cutoffs (R6.9) apply
        — a cashier may sell same-day after the online cutoff has passed.
        """
        counter_ctx = ctx.for_venue(ctx.venue_id)
        counter_ctx.channel = "COUNTER"
        return counter_ctx

    def _require_open_shift(
        self, ctx: RequestContext, *, venue_id: str, shift_id: str | None
    ) -> dict[str, Any]:
        if shift_id:
            shift = self.get_shift(ctx, shift_id)
            if shift["status"] != "OPEN":
                raise ConflictError("That shift is closed.", code="shift_not_open")
            if shift["staff_id"] != ctx.principal.id:
                # A cashier's cash belongs in their own drawer, not someone else's.
                raise RuleViolation("You can only sell against your own open shift.")
            return shift
        row = self.db.query_one(
            "SELECT * FROM shift_sessions WHERE tenant_id = ? AND venue_id = ? AND staff_id = ? "
            "AND status = 'OPEN' ORDER BY opened_at DESC LIMIT 1",
            (ctx.tenant_id, venue_id, ctx.principal.id),
        )
        if row is None:
            raise RuleViolation(
                "Open a cashier shift before taking cash.", code="no_open_shift"
            )
        return dict(row)

    def _booking_venue(self, ctx: RequestContext, booking_id: str) -> str:
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        return booking["venue_id"]


__all__ = ["CounterService"]
