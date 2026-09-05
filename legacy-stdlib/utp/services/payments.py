"""Payments: idempotency, webhooks, reconciliation, refunds.

Card data never touches this module. There is no column for a card number, CVV or
track data anywhere in the schema, and the provider interface only ever exchanges
tokens and references (R14.2, R73.11).

Two invariants do the heavy lifting:

* **One charge per attempt.** ``payments.idempotency_key`` is UNIQUE per tenant.
  A repeated submission of the same attempt returns the original payment instead
  of creating a second one (R14.3).
* **The provider is the source of truth.** A webhook is verified, deduplicated by
  ``(tenant, provider, provider_event_id)`` and processed exactly once, and any
  disagreement with the client-side return is resolved in the provider's favour
  (R14.4, R14.7).

The :class:`PaymentProvider` protocol keeps the real gateway out of the domain. The
in-process :class:`SimulatedProvider` is what the test suite drives; a production
adapter implements the same three methods.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.audit import AuditLog
from ..core.clock import Clock, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, IntegrityViolation, decode
from ..core.errors import ConflictError, NotFound, PaymentFailed, ValidationError
from ..core.ids import new_id, sign_payload, verify_signature
from ..domain import enums
from .authz import AuthorizationService

#: Provider failure code → customer-safe message key (R14.12). A raw provider
#: payload is never shown; anything unmapped falls back to the generic message.
FAILURE_MESSAGES: dict[str, str] = {
    "insufficient_funds": "That card was declined for insufficient funds. Please try another card.",
    "card_declined": "That card was declined. Please try another payment method.",
    "expired_card": "That card has expired. Please use a different card.",
    "authentication_required": "Your bank needs to verify this payment. Please try again and complete the check.",
    "processing_error": "The payment could not be processed just now. Please try again.",
    "timeout": "The payment provider did not respond in time. Please try again.",
}

RECONCILIATION_STATES: tuple[str, ...] = ("MATCHED", "ORPHANED_AUTHORIZATION", "SURPLUS", "AMOUNT_MISMATCH", "MISSING_CONFIRMATION")


class PaymentProvider(Protocol):
    """The narrow surface the platform needs from a gateway."""

    name: str

    def authorize(
        self, *, amount_minor: int, currency: str, method: str, idempotency_key: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """Request an authorization. Returns ``{status, provider_ref, failure_code?}``."""

    def refund(
        self, *, provider_ref: str, amount_minor: int, currency: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Request a refund. Returns ``{status, provider_ref, failure_code?}``."""

    def void(self, *, provider_ref: str, idempotency_key: str) -> dict[str, Any]:
        """Void an unsettled authorization."""


@dataclass
class SimulatedProvider:
    """Deterministic in-process provider used by tests and local development.

    Behaviour is driven by explicit instructions rather than randomness, so a test
    can reproduce a decline, a timeout, a duplicate webhook or a late confirmation
    exactly.
    """

    name: str = "simulated"
    webhook_secret: str = "whsec_simulated"
    #: Queue of outcomes to return from ``authorize``, e.g. ``["AUTHORIZED", ("FAILED", "card_declined")]``.
    scripted: list[Any] = field(default_factory=list)
    authorizations: dict[str, dict[str, Any]] = field(default_factory=dict)
    refunds: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def _next_outcome(self) -> tuple[str, str | None]:
        if not self.scripted:
            return "AUTHORIZED", None
        entry = self.scripted.pop(0)
        if isinstance(entry, tuple):
            return entry[0], entry[1]
        return str(entry), None

    def authorize(
        self, *, amount_minor: int, currency: str, method: str, idempotency_key: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"op": "authorize", "idempotency_key": idempotency_key, "amount_minor": amount_minor})
        if idempotency_key in self.authorizations:
            # A real gateway replays the original result for a repeated key.
            return dict(self.authorizations[idempotency_key])
        status, failure = self._next_outcome()
        result = {
            "status": status,
            "provider_ref": f"auth_{hashlib.blake2b(idempotency_key.encode(), digest_size=8).hexdigest()}",
            "failure_code": failure,
            "amount_minor": amount_minor,
            "currency": currency,
        }
        self.authorizations[idempotency_key] = result
        return dict(result)

    def refund(
        self, *, provider_ref: str, amount_minor: int, currency: str, idempotency_key: str
    ) -> dict[str, Any]:
        self.calls.append({"op": "refund", "idempotency_key": idempotency_key, "amount_minor": amount_minor})
        if idempotency_key in self.refunds:
            return dict(self.refunds[idempotency_key])
        status, failure = self._next_outcome()
        result = {
            "status": "REFUNDED" if status in ("AUTHORIZED", "REFUNDED") else "FAILED",
            "provider_ref": f"ref_{hashlib.blake2b(idempotency_key.encode(), digest_size=8).hexdigest()}",
            "failure_code": failure,
        }
        self.refunds[idempotency_key] = result
        return dict(result)

    def void(self, *, provider_ref: str, idempotency_key: str) -> dict[str, Any]:
        self.calls.append({"op": "void", "idempotency_key": idempotency_key})
        return {"status": "VOIDED", "provider_ref": provider_ref}

    # --- test helper ------------------------------------------------- #

    def sign_webhook(self, body: str) -> str:
        return sign_payload(self.webhook_secret.encode("utf-8"), body)


class PaymentService:
    """Payment attempts, webhooks, refunds and reconciliation."""

    #: Called with ``(ctx, payment)`` when a payment reaches CAPTURED. Wired by
    #: :class:`utp.app.Platform` to ``BookingService.finalize_paid_booking`` so a
    #: provider callback completes the booking and delivers the e-ticket even when the
    #: customer's browser never came back (R14.6).
    on_payment_captured: Any = None

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
        provider: PaymentProvider | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config
        self.provider: PaymentProvider = provider or SimulatedProvider()

    # ------------------------------------------------------------------ #
    # Method availability (R14.1)
    # ------------------------------------------------------------------ #

    def available_methods(self, ctx: RequestContext, *, venue_id: str, channel: str) -> list[str]:
        """Configured methods for a venue and channel.

        Cash and complimentary are staff-channel only regardless of configuration —
        a kiosk must never offer "pay cash" it cannot collect.
        """
        configured = self.config.get(
            ctx, f"payment.methods.{channel}", venue_id=venue_id, use_platform_default=False
        )
        if configured is None:
            configured = self.config.get(
                ctx, "payment.methods", venue_id=venue_id, default=["CARD", "QR_BANK_TRANSFER"]
            )
        methods = [m for m in configured if m in enums.PAYMENT_METHODS]
        if channel not in ("COUNTER", "STAFF"):
            methods = [m for m in methods if m not in enums.STAFF_ONLY_PAYMENT_METHODS]
        return methods

    def assert_method_allowed(
        self, ctx: RequestContext, *, venue_id: str, channel: str, method: str
    ) -> None:
        # Stored value is the customer's own instrument settling their own bill, so
        # it is permitted in any channel regardless of the configured method list
        # (§16, §68). Everything else must be explicitly available here.
        if method == "STORED_VALUE":
            return
        if method not in self.available_methods(ctx, venue_id=venue_id, channel=channel):
            raise ValidationError(
                {"method": "That payment method is not available here."},
                message="Please choose another payment method.",
            )

    # ------------------------------------------------------------------ #
    # Authorization (R14.3, R14.8)
    # ------------------------------------------------------------------ #

    def start_payment(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        amount_minor: int,
        currency: str,
        method: str,
        idempotency_key: str,
        venue_id: str,
        tendered_minor: int | None = None,
        shift_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create and authorize a payment attempt, exactly once per key (R14.3)."""
        if not idempotency_key:
            raise ValidationError({"idempotency_key": "An idempotency key is required."})
        if int(amount_minor) < 0:
            raise ValidationError({"amount_minor": "The amount cannot be negative."})
        self.assert_method_allowed(ctx, venue_id=venue_id, channel=ctx.channel, method=method)

        existing = self.db.query_one(
            "SELECT * FROM payments WHERE tenant_id = ? AND idempotency_key = ?",
            (ctx.tenant_id, idempotency_key),
        )
        if existing is not None:
            # Replaying an attempt returns the original outcome. This is the guarantee
            # that a double-tapped Pay button cannot double-charge.
            return self._payment_result(ctx, dict(existing), replayed=True)

        payment_id = new_id("pay")
        now = to_iso(self.clock.now())
        try:
            self.db.insert(
                "payments",
                {
                    "id": payment_id,
                    "tenant_id": ctx.tenant_id,
                    "booking_id": booking_id,
                    "method": method,
                    "provider": self.provider.name,
                    "amount_minor": int(amount_minor),
                    "tendered_minor": tendered_minor,
                    "change_minor": (int(tendered_minor) - int(amount_minor)) if tendered_minor else None,
                    "currency": currency.upper(),
                    "status": "INITIATED",
                    "idempotency_key": idempotency_key,
                    "channel": ctx.channel,
                    "device_id": ctx.device_id,
                    "actor_id": ctx.principal.id,
                    "shift_id": shift_id,
                    "created_at": now,
                },
            )
        except IntegrityViolation:
            # Lost a race on the unique key: the other request owns the charge.
            row = self.db.query_one(
                "SELECT * FROM payments WHERE tenant_id = ? AND idempotency_key = ?",
                (ctx.tenant_id, idempotency_key),
            )
            if row is None:  # pragma: no cover - defensive
                raise
            return self._payment_result(ctx, dict(row), replayed=True)

        if method in enums.IMMEDIATE_SETTLE_METHODS:
            # Settled at the counter, or by the customer's own stored value; no
            # gateway involved (§16, §68).
            self.db.update(
                "payments",
                payment_id,
                {"status": "CAPTURED", "authorized_at": now, "captured_at": now, "provider_ref": payment_id},
                tenant_id=ctx.tenant_id,
            )
            return self._payment_result(ctx, self._row(ctx, payment_id))

        outcome = self.provider.authorize(
            amount_minor=int(amount_minor),
            currency=currency.upper(),
            method=method,
            idempotency_key=idempotency_key,
            metadata={"booking_id": booking_id, "tenant_id": ctx.tenant_id, **(metadata or {})},
        )
        status = outcome.get("status")
        if status in ("AUTHORIZED", "CAPTURED", "SUCCEEDED"):
            self.db.update(
                "payments",
                payment_id,
                {
                    "status": "AUTHORIZED",
                    "provider_ref": outcome.get("provider_ref"),
                    "authorized_at": now,
                },
                tenant_id=ctx.tenant_id,
            )
        elif status == "PENDING":
            self.db.update(
                "payments",
                payment_id,
                {"status": "PENDING", "provider_ref": outcome.get("provider_ref")},
                tenant_id=ctx.tenant_id,
            )
        else:
            failure = outcome.get("failure_code") or "processing_error"
            self.db.update(
                "payments",
                payment_id,
                {
                    "status": "FAILED",
                    "provider_ref": outcome.get("provider_ref"),
                    "failure_code": failure,
                    "failed_at": now,
                },
                tenant_id=ctx.tenant_id,
            )
            # R14.8 — the cart and hold stay intact; the caller may retry with a new key.
            raise PaymentFailed(
                FAILURE_MESSAGES.get(failure, PaymentFailed.default_message),
                details={"payment_id": payment_id, "retry_allowed": True},
                log_detail=f"provider {self.provider.name} returned {failure}",
                correlation_id=ctx.correlation_id,
            )
        return self._payment_result(ctx, self._row(ctx, payment_id))

    def _row(self, ctx: RequestContext, payment_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM payments WHERE id = ? AND tenant_id = ?", (payment_id, ctx.tenant_id)
        )
        if row is None:
            raise NotFound(details={"entity": "payment"})
        return dict(row)

    def _payment_result(
        self, ctx: RequestContext, row: dict[str, Any], *, replayed: bool = False
    ) -> dict[str, Any]:
        return {
            "payment_id": row["id"],
            "booking_id": row["booking_id"],
            "status": row["status"],
            "method": row["method"],
            "provider": row["provider"],
            "provider_ref": row["provider_ref"],
            "amount_minor": int(row["amount_minor"]),
            "currency": row["currency"],
            "tendered_minor": row["tendered_minor"],
            "change_minor": row["change_minor"],
            "channel": row["channel"],
            "replayed": replayed,
            "created_at": row["created_at"],
            "authorized_at": row["authorized_at"],
        }

    def get_payment(self, ctx: RequestContext, payment_id: str) -> dict[str, Any]:
        return self._payment_result(ctx, self.authz.load_scoped(ctx, "payments", payment_id, entity="payment"))

    def list_for_booking(self, ctx: RequestContext, booking_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM payments WHERE tenant_id = ? AND booking_id = ? ORDER BY created_at",
            (ctx.tenant_id, booking_id),
        )
        return [self._payment_result(ctx, dict(r)) for r in rows]

    def captured_total(self, ctx: RequestContext, booking_id: str) -> int:
        """Net amount actually collected — the ceiling on all refunds (R17.6)."""
        return int(
            self.db.scalar(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM payments "
                "WHERE tenant_id = ? AND booking_id = ? AND status IN ('AUTHORIZED','CAPTURED')",
                (ctx.tenant_id, booking_id),
                default=0,
            )
        )

    def mark_captured(
        self, ctx: RequestContext, payment_id: str, *, notify: bool = True
    ) -> dict[str, Any]:
        """Mark a payment captured.

        ``notify=False`` is used by the booking service, which is already completing
        the booking and must not be re-entered through the capture hook.
        """
        row = self.authz.load_scoped(ctx, "payments", payment_id, entity="payment")
        if row["status"] not in ("AUTHORIZED", "PENDING"):
            return self._payment_result(ctx, row)
        self.db.update(
            "payments",
            payment_id,
            {"status": "CAPTURED", "captured_at": to_iso(self.clock.now())},
            tenant_id=ctx.tenant_id,
        )
        result = self._payment_result(ctx, self._row(ctx, payment_id))
        if notify:
            self._fire_capture_hook(ctx, result)
        return result

    def _fire_capture_hook(self, ctx: RequestContext, payment: dict[str, Any]) -> dict[str, Any] | None:
        """Hand a captured payment to whoever completes bookings (R14.6).

        A hook rather than a direct call, so this module never imports the booking
        service. The composition root wires it to
        ``BookingService.finalize_paid_booking``.

        Failures are swallowed into an operational exception on purpose: the money is
        already taken and the provider's callback must still be acknowledged, otherwise
        the gateway retries forever. The exception is what gets the booking fixed.
        """
        if self.on_payment_captured is None or not payment.get("booking_id"):
            return None
        try:
            return self.on_payment_captured(ctx, payment)
        except Exception as exc:  # noqa: BLE001 - deliberate: see docstring
            self._raise_exception(
                ctx,
                kind="PAYMENT_CAPTURED_BOOKING_INCOMPLETE",
                entity_type="booking",
                entity_id=str(payment.get("booking_id")),
                detail={
                    "payment_id": payment.get("payment_id"),
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc)[:500],
                },
                severity="ERROR",
            )
            return None

    # ------------------------------------------------------------------ #
    # Webhooks (R14.4 - R14.7)
    # ------------------------------------------------------------------ #

    def handle_webhook(
        self,
        ctx: RequestContext,
        *,
        provider_event_id: str,
        kind: str,
        body: str,
        signature: str,
        secret: str | None = None,
        idempotency_key: str | None = None,
        payment_id: str | None = None,
        amount_minor: int | None = None,
        failure_code: str | None = None,
    ) -> dict[str, Any]:
        """Verify and process a provider callback exactly once (R14.4).

        Unverified callbacks are rejected and recorded. Duplicate or out-of-order
        deliveries of the same event produce exactly one state transition, because
        the event row is inserted under a unique constraint *before* any state change.
        """
        expected_secret = secret or getattr(self.provider, "webhook_secret", "")
        signature_valid = bool(
            expected_secret and verify_signature(str(expected_secret).encode("utf-8"), body, signature)
        )
        payload_hash = hashlib.blake2b(body.encode("utf-8"), digest_size=16).hexdigest()
        now = to_iso(self.clock.now())

        if not signature_valid:
            self.audit.security(
                ctx,
                "AUTHORIZATION_DENIED",
                target_type="payment_event",
                target_id=provider_event_id,
                reason="webhook_signature_invalid",
                detail={"provider": self.provider.name, "kind": kind},
            )
            raise ValidationError(
                {"signature": "The callback signature is not valid."},
                message="This callback could not be verified.",
                code="webhook_signature_invalid",
            )

        resolved_payment = self._resolve_webhook_payment(ctx, payment_id, idempotency_key)
        event_id = new_id("pev")
        try:
            with self.db.transaction(immediate=True):
                self.db.insert(
                    "payment_events",
                    {
                        "id": event_id,
                        "tenant_id": ctx.tenant_id,
                        "payment_id": resolved_payment["id"] if resolved_payment else None,
                        "provider": self.provider.name,
                        "provider_event_id": provider_event_id,
                        "kind": kind,
                        "signature_valid": 1,
                        "payload_hash": payload_hash,
                        "received_at": now,
                    },
                )
        except IntegrityViolation:
            existing = self.db.query_one(
                "SELECT * FROM payment_events WHERE tenant_id = ? AND provider = ? AND provider_event_id = ?",
                (ctx.tenant_id, self.provider.name, provider_event_id),
            )
            return {
                "duplicate": True,
                "processed": False,
                "event_id": existing["id"] if existing else None,
                "outcome": existing["outcome"] if existing else None,
                "payment_id": existing["payment_id"] if existing else None,
            }

        if resolved_payment is None:
            # R14.9 — an authorization with no matching platform payment is an
            # exception to investigate, not something to silently drop.
            self._raise_exception(
                ctx,
                kind="PAYMENT_ORPHANED_AUTHORIZATION",
                entity_type="payment_event",
                entity_id=event_id,
                detail={"provider_event_id": provider_event_id, "kind": kind, "amount_minor": amount_minor},
            )
            self.db.update(
                "payment_events",
                event_id,
                {"processed_at": now, "outcome": "ORPHANED"},
                tenant_id=ctx.tenant_id,
            )
            return {"duplicate": False, "processed": True, "outcome": "ORPHANED", "event_id": event_id}

        outcome = self._apply_webhook(
            ctx,
            payment=resolved_payment,
            kind=kind,
            amount_minor=amount_minor,
            failure_code=failure_code,
            now=now,
        )
        self.db.update(
            "payment_events", event_id, {"processed_at": now, "outcome": outcome}, tenant_id=ctx.tenant_id
        )

        # R14.6 — this is the path that matters when the browser, kiosk session or
        # network was lost after authorization. The provider's confirmation is
        # authoritative (R14.7), so it completes the booking and delivers the e-ticket.
        # Safe to call unconditionally: finalization is idempotent, so a duplicate or
        # out-of-order delivery produces exactly one confirmed booking and one email.
        completion = None
        if outcome == "CAPTURED":
            completion = self._fire_capture_hook(
                ctx, self._payment_result(ctx, self._row(ctx, resolved_payment["id"]))
            )
        return {
            "duplicate": False,
            "processed": True,
            "outcome": outcome,
            "event_id": event_id,
            "payment_id": resolved_payment["id"],
            "booking_id": resolved_payment["booking_id"],
            "booking_completed": bool(completion and completion.get("confirmed")),
            "tickets_issued": len(completion.get("tickets", [])) if completion else 0,
            "completion": completion,
        }

    def _resolve_webhook_payment(
        self, ctx: RequestContext, payment_id: str | None, idempotency_key: str | None
    ) -> dict[str, Any] | None:
        if payment_id:
            row = self.db.query_one(
                "SELECT * FROM payments WHERE id = ? AND tenant_id = ?", (payment_id, ctx.tenant_id)
            )
            return dict(row) if row else None
        if idempotency_key:
            row = self.db.query_one(
                "SELECT * FROM payments WHERE tenant_id = ? AND idempotency_key = ?",
                (ctx.tenant_id, idempotency_key),
            )
            return dict(row) if row else None
        return None

    def _apply_webhook(
        self,
        ctx: RequestContext,
        *,
        payment: dict[str, Any],
        kind: str,
        amount_minor: int | None,
        failure_code: str | None,
        now: str,
    ) -> str:
        """Apply one verified event. The provider's view wins (R14.7)."""
        if kind in ("payment.succeeded", "payment.captured", "charge.succeeded"):
            if amount_minor is not None and int(amount_minor) != int(payment["amount_minor"]):
                self._raise_exception(
                    ctx,
                    kind="PAYMENT_AMOUNT_MISMATCH",
                    entity_type="payment",
                    entity_id=payment["id"],
                    detail={"platform_amount": int(payment["amount_minor"]), "provider_amount": int(amount_minor)},
                )
                self.db.update(
                    "payments",
                    payment["id"],
                    {"reconciliation_state": "AMOUNT_MISMATCH"},
                    tenant_id=ctx.tenant_id,
                )
                return "AMOUNT_MISMATCH"
            self.db.update(
                "payments",
                payment["id"],
                {
                    "status": "CAPTURED",
                    "authorized_at": payment["authorized_at"] or now,
                    "captured_at": now,
                    "reconciliation_state": "MATCHED",
                },
                tenant_id=ctx.tenant_id,
            )
            return "CAPTURED"
        if kind in ("payment.failed", "charge.failed"):
            self.db.update(
                "payments",
                payment["id"],
                {"status": "FAILED", "failure_code": failure_code or "processing_error", "failed_at": now},
                tenant_id=ctx.tenant_id,
            )
            return "FAILED"
        if kind in ("payment.refunded", "charge.refunded"):
            self.db.update(
                "payments", payment["id"], {"status": "REFUNDED"}, tenant_id=ctx.tenant_id
            )
            return "REFUNDED"
        if kind in ("payment.voided", "charge.voided"):
            self.db.update("payments", payment["id"], {"status": "VOIDED"}, tenant_id=ctx.tenant_id)
            return "VOIDED"
        return "IGNORED"

    def detect_duplicate_payment(self, ctx: RequestContext, booking_id: str) -> dict[str, Any] | None:
        """Flag a surplus payment for refund and notify finance (R14.5)."""
        rows = self.db.query(
            "SELECT * FROM payments WHERE tenant_id = ? AND booking_id = ? "
            "AND status IN ('AUTHORIZED','CAPTURED') ORDER BY created_at",
            (ctx.tenant_id, booking_id),
        )
        if len(rows) < 2:
            return None
        booking = self.db.query_one(
            "SELECT net_minor FROM bookings WHERE id = ? AND tenant_id = ?", (booking_id, ctx.tenant_id)
        )
        due = int(booking["net_minor"]) if booking else 0
        collected = sum(int(r["amount_minor"]) for r in rows)
        if collected <= due:
            return None
        surplus_rows = list(rows[1:])
        for row in surplus_rows:
            self.db.update(
                "payments", row["id"], {"reconciliation_state": "SURPLUS"}, tenant_id=ctx.tenant_id
            )
        exception = self._raise_exception(
            ctx,
            kind="PAYMENT_DUPLICATE",
            entity_type="booking",
            entity_id=booking_id,
            detail={
                "collected_minor": collected,
                "due_minor": due,
                "surplus_minor": collected - due,
                "surplus_payment_ids": [r["id"] for r in surplus_rows],
            },
            severity="ERROR",
        )
        return {
            "booking_id": booking_id,
            "surplus_minor": collected - due,
            "surplus_payment_ids": [r["id"] for r in surplus_rows],
            "exception_id": exception,
        }

    # ------------------------------------------------------------------ #
    # Refunds and voids (R14.11, R17.7)
    # ------------------------------------------------------------------ #

    def execute_refund(
        self,
        ctx: RequestContext,
        *,
        refund_id: str,
        payment_id: str,
        amount_minor: int,
        currency: str,
    ) -> dict[str, Any]:
        """Ask the provider to refund. A failure stays retryable (R17.7)."""
        payment = self.authz.load_scoped(ctx, "payments", payment_id, entity="payment")
        attempts = int(
            self.db.scalar(
                "SELECT attempts FROM refunds WHERE id = ? AND tenant_id = ?",
                (refund_id, ctx.tenant_id),
                default=0,
            )
        )
        key = f"{refund_id}:{attempts + 1}"
        outcome = self.provider.refund(
            provider_ref=payment["provider_ref"] or payment_id,
            amount_minor=int(amount_minor),
            currency=currency.upper(),
            idempotency_key=key,
        )
        now = to_iso(self.clock.now())
        if outcome.get("status") == "REFUNDED":
            self.db.update(
                "refunds",
                refund_id,
                {"status": "COMPLETED", "completed_at": now, "attempts": attempts + 1, "last_error": None},
                tenant_id=ctx.tenant_id,
            )
            return {"refund_id": refund_id, "status": "COMPLETED", "provider_ref": outcome.get("provider_ref")}
        failure = outcome.get("failure_code") or "processing_error"
        self.db.update(
            "refunds",
            refund_id,
            {"status": "FAILED_RETRYABLE", "attempts": attempts + 1, "last_error": failure},
            tenant_id=ctx.tenant_id,
        )
        self._raise_exception(
            ctx,
            kind="REFUND_FAILED",
            entity_type="refund",
            entity_id=refund_id,
            detail={"failure_code": failure, "attempts": attempts + 1},
            severity="ERROR",
        )
        return {"refund_id": refund_id, "status": "FAILED_RETRYABLE", "failure_code": failure}

    def retry_failed_refunds(self, ctx: RequestContext, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM refunds WHERE tenant_id = ? AND status = 'FAILED_RETRYABLE' "
            "ORDER BY created_at LIMIT ?",
            (ctx.tenant_id, int(limit)),
        )
        results = []
        for row in rows:
            if not row["payment_id"]:
                continue
            results.append(
                self.execute_refund(
                    ctx,
                    refund_id=row["id"],
                    payment_id=row["payment_id"],
                    amount_minor=int(row["amount_minor"]),
                    currency="THB",
                )
            )
        return results

    def void_payment(self, ctx: RequestContext, payment_id: str, *, reason: str) -> dict[str, Any]:
        payment = self.authz.load_scoped(ctx, "payments", payment_id, entity="payment")
        if payment["status"] not in ("AUTHORIZED", "PENDING", "CAPTURED"):
            raise ConflictError("That payment cannot be voided in its current state.")
        outcome = self.provider.void(
            provider_ref=payment["provider_ref"] or payment_id, idempotency_key=f"void:{payment_id}"
        )
        self.db.update(
            "payments",
            payment_id,
            {"status": "VOIDED", "reconciliation_state": "MATCHED"},
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            ctx,
            "VOID",
            target_type="payment",
            target_id=payment_id,
            previous={"status": payment["status"]},
            new={"status": "VOIDED", "provider_ref": outcome.get("provider_ref")},
            reason=reason,
            severity="WARNING",
        )
        return {"payment_id": payment_id, "status": "VOIDED"}

    # ------------------------------------------------------------------ #
    # Reconciliation (R14.9)
    # ------------------------------------------------------------------ #

    def reconcile(
        self, ctx: RequestContext, *, provider_transactions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Compare provider transactions with platform payments.

        ``provider_transactions`` items look like
        ``{"provider_ref": ..., "amount_minor": ..., "status": ...}`` — the shape a
        settlement file gives. Every disagreement becomes an operational exception
        rather than a silent correction.
        """
        by_ref = {t["provider_ref"]: t for t in provider_transactions if t.get("provider_ref")}
        exceptions: list[dict[str, Any]] = []

        rows = self.db.query(
            "SELECT * FROM payments WHERE tenant_id = ? AND status IN ('AUTHORIZED','CAPTURED','PENDING')",
            (ctx.tenant_id,),
        )
        matched = 0
        for row in rows:
            ref = row["provider_ref"]
            transaction = by_ref.pop(ref, None) if ref else None
            if transaction is None:
                if row["method"] in ("CASH", "COMPLIMENTARY"):
                    continue  # never appears in a gateway settlement file
                exceptions.append(
                    {
                        "kind": "PAYMENT_MISSING_CONFIRMATION",
                        "payment_id": row["id"],
                        "amount_minor": int(row["amount_minor"]),
                    }
                )
                self.db.update(
                    "payments",
                    row["id"],
                    {"reconciliation_state": "MISSING_CONFIRMATION"},
                    tenant_id=ctx.tenant_id,
                )
                continue
            if int(transaction.get("amount_minor", 0)) != int(row["amount_minor"]):
                exceptions.append(
                    {
                        "kind": "PAYMENT_AMOUNT_MISMATCH",
                        "payment_id": row["id"],
                        "platform_amount": int(row["amount_minor"]),
                        "provider_amount": int(transaction.get("amount_minor", 0)),
                    }
                )
                self.db.update(
                    "payments", row["id"], {"reconciliation_state": "AMOUNT_MISMATCH"}, tenant_id=ctx.tenant_id
                )
                continue
            self.db.update(
                "payments", row["id"], {"reconciliation_state": "MATCHED"}, tenant_id=ctx.tenant_id
            )
            matched += 1

        for ref, transaction in by_ref.items():
            exceptions.append(
                {
                    "kind": "PAYMENT_ORPHANED_AUTHORIZATION",
                    "provider_ref": ref,
                    "amount_minor": int(transaction.get("amount_minor", 0)),
                }
            )

        unmatched_refunds = self.db.query(
            "SELECT id, amount_minor FROM refunds WHERE tenant_id = ? AND status = 'FAILED_RETRYABLE'",
            (ctx.tenant_id,),
        )
        for row in unmatched_refunds:
            exceptions.append(
                {"kind": "REFUND_UNMATCHED", "refund_id": row["id"], "amount_minor": int(row["amount_minor"])}
            )

        for exception in exceptions:
            self._raise_exception(
                ctx,
                kind=exception["kind"],
                entity_type="payment",
                entity_id=str(exception.get("payment_id") or exception.get("refund_id") or exception.get("provider_ref")),
                detail=exception,
                severity="ERROR",
            )
        return {
            "matched": matched,
            "exception_count": len(exceptions),
            "exceptions": exceptions,
            "generated_at": to_iso(self.clock.now()),
        }

    def _raise_exception(
        self,
        ctx: RequestContext,
        *,
        kind: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any],
        severity: str = "WARNING",
    ) -> str:
        exception_id = new_id("exc")
        self.db.insert(
            "exceptions_log",
            {
                "id": exception_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": ctx.venue_id,
                "kind": kind,
                "severity": severity,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "detail_json": detail,
                "state": "OPEN",
                "created_at": to_iso(self.clock.now()),
            },
        )
        return exception_id

    def open_exceptions(self, ctx: RequestContext, *, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM exceptions_log WHERE tenant_id = ? AND state = 'OPEN'"
        params: list[Any] = [ctx.tenant_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC"
        out = []
        for row in self.db.query(sql, params):
            item = dict(row)
            item["detail"] = decode(item.pop("detail_json"), {})
            out.append(item)
        return out


__all__ = [
    "FAILURE_MESSAGES",
    "RECONCILIATION_STATES",
    "PaymentProvider",
    "PaymentService",
    "SimulatedProvider",
]
