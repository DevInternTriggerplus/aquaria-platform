"""Audit log.

Append-only by construction: the table carries BEFORE UPDATE and BEFORE DELETE
triggers that abort (R45.3). This module is the only writer.

Two design points worth stating:

* **Redaction is mandatory, not advisory.** ``_redact`` strips anything whose key
  looks like a secret, a card number, a password or unmasked contact data before
  the payload is stored (R45.9). It is applied to every previous/new value, so a
  careless caller cannot leak a credential into the audit trail.
* **Correlation over granularity.** A bulk schedule update writes one correlated
  audit event for the operation plus per-target detail under the same
  ``correlation_id`` (R23.6, R45.7), so a reviewer can see both the intent and
  the effect.
"""

from __future__ import annotations

from typing import Any

from .clock import Clock, local_iso, to_iso
from .context import RequestContext
from .db import Database, decode
from .ids import new_id

#: Keys whose values are never written to the audit payload.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "credential",
        "credential_hash",
        "secret",
        "secret_hash",
        "api_key",
        "api_key_hash",
        "token",
        "token_hash",
        "qr_token",
        "qr_signature",
        "card_number",
        "pan",
        "cvv",
        "cvc",
        "invite_token_hash",
        "code_hash",
        "signature",
        "email",
        "phone",
        "full_name",
    }
)

_REDACTED = "[redacted]"


def _redact(value: Any) -> Any:
    """Remove secrets and unmasked personal data from an audit payload."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                out[key] = _REDACTED
            else:
                out[key] = _redact(inner)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


#: Actions that must always be audited. Listed explicitly so the test suite can
#: assert coverage of R45.2 rather than trusting that callers remembered.
AUDITED_ACTIONS: tuple[str, ...] = (
    # staff & permissions
    "STAFF_ADD",
    "STAFF_EDIT",
    "STAFF_DEACTIVATE",
    "STAFF_REACTIVATE",
    "STAFF_SUSPEND",
    "STAFF_INVITE",
    "ROLE_ADD",
    "ROLE_EDIT",
    "ROLE_DELETE",
    "ROLE_ASSIGN",
    "ROLE_REMOVE",
    "PERMISSION_CHANGE",
    "LOGIN",
    "LOGIN_FAILED",
    "LOGOUT",
    "CREDENTIAL_RESET",
    "MFA_ENROL",
    "MFA_RESET",
    "SESSION_TERMINATED",
    "AUTHORIZATION_DENIED",
    "CROSS_TENANT_ATTEMPT",
    # money
    "BOOKING_CONFIRMED",
    "BOOKING_CANCELLED",
    "BOOKING_RESCHEDULED",
    "REFUND",
    "VOID",
    "MANUAL_DISCOUNT",
    "COMPLIMENTARY_ISSUE",
    "REPRINT",
    "EXPORT",
    "TAX_INVOICE_ISSUE",
    "SHIFT_OPEN",
    "SHIFT_CLOSE",
    "SHIFT_VARIANCE_APPROVED",
    "APPROVAL_GRANTED",
    # payment types (update spec §40)
    "PAYMENT_TYPE_ADDED",
    "PAYMENT_TYPE_EDITED",
    "PAYMENT_TYPE_DISABLED",
    "PAYMENT_TYPE_ARCHIVED",
    "PAYMENT_TYPE_ORDER_CHANGED",
    "PAYMENT_TYPE_CHANNEL_CHANGED",
    # access
    "OVERRIDE_ACCESS",
    "TICKET_BLOCK",
    "TICKET_REISSUE",
    "TICKET_RESEND",
    "DEVICE_DEACTIVATE",
    "OFFLINE_SCAN_CONFLICT",
    # schedule
    "CAPACITY_OVERRIDE",
    "SCHEDULE_PUBLISH",
    "SCHEDULE_UNPUBLISH",
    "SCHEDULE_ARCHIVE",
    "SHOW_CANCEL",
    "SHOW_LOCATION_CHANGE",
    "SHOW_RETIME",
    "SHOW_SESSION_DELAYED",
    "BULK_SCHEDULE_UPDATE",
    "OVERRIDE_CREATE",
    "OVERRIDE_REMOVE",
    "OVERRIDE_MODIFY",
    # seating
    "SEAT_LAYOUT_CREATE",
    "SEAT_LAYOUT_EDIT",
    "SEAT_LAYOUT_PUBLISH",
    "SEAT_LAYOUT_UNPUBLISH",
    "SEAT_LAYOUT_ARCHIVE",
    "SEAT_LAYOUT_DUPLICATE",
    "SEAT_LAYOUT_VERSION_CREATE",
    "SEAT_ADD",
    "SEAT_DELETE",
    "SEAT_TYPE_CHANGE",
    "SEAT_PRICE_CATEGORY_CHANGE",
    "SEAT_PRICE_OVERRIDE",
    "SEAT_BLOCK",
    "SEAT_UNBLOCK",
    "SEAT_CHANGE_CUSTOMER",
    "SEAT_ELIGIBILITY_OVERRIDE",
    "SEAT_HOLD_RELEASED_MANUALLY",
    # loyalty / members (add_features §32-§34, §59)
    "MEMBER_ENROL",
    "POINTS_EARN",
    "POINTS_REDEEM",
    "POINTS_RESTORE",
    "POINTS_ADJUST",
    "MEMBER_CONVERSION_RATE_CHANGE",
    # rewards / free gifts (add_features §11-§12, §59)
    "REWARD_GRANT",
    "REWARD_INVENTORY_CHANGE",
    # configuration & privacy
    "CONFIG_CHANGE",
    "CONSENT_CAPTURED",
    "CONSENT_WITHDRAWN",
    "DSAR_RECEIVED",
    "DSAR_COMPLETED",
    "PII_ACCESS",
    "BREACH_RECORDED",
    "PRODUCT_DEACTIVATE",
)


class AuditLog:
    """Writer for :data:`utp.core.schema.TABLES` ``audit_events``."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def record(
        self,
        ctx: RequestContext,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        previous: Any = None,
        new: Any = None,
        reason: str | None = None,
        severity: str = "INFO",
        venue_timezone: str | None = None,
        extra_correlation: str | None = None,
    ) -> str:
        """Append one audit event and return its id."""
        now = self.clock.now()
        event_id = new_id("aud")
        self.db.insert(
            "audit_events",
            {
                "id": event_id,
                "tenant_id": ctx.tenant_id,
                "organization_id": ctx.organization_id,
                "venue_id": ctx.venue_id,
                "actor_id": ctx.principal.id,
                "actor_role": ctx.principal.kind,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "previous_json": _redact(previous) if previous is not None else None,
                "new_json": _redact(new) if new is not None else None,
                "reason": reason,
                "at_utc": to_iso(now),
                "at_local": local_iso(now, venue_timezone) if venue_timezone else to_iso(now),
                "channel": ctx.channel,
                "device_id": ctx.device_id,
                "ip_address": ctx.ip_address,
                "correlation_id": extra_correlation or ctx.correlation_id,
                "severity": severity,
            },
        )
        return event_id

    def security(
        self,
        ctx: RequestContext,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        reason: str | None = None,
        detail: Any = None,
    ) -> str:
        """Record a security-relevant event at WARNING severity (R45.8)."""
        return self.record(
            ctx,
            action,
            target_type=target_type,
            target_id=target_id,
            new=detail,
            reason=reason,
            severity="WARNING",
        )

    # --------------------------------- reads --------------------------------- #

    def search(
        self,
        ctx: RequestContext,
        *,
        actor_id: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        venue_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        correlation_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Filtered audit search, scoped by tenant and venue (R45.4, R45.5)."""
        sql = ["SELECT * FROM audit_events WHERE tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if actor_id:
            sql.append("AND actor_id = ?")
            params.append(actor_id)
        if action:
            sql.append("AND action = ?")
            params.append(action)
        if target_type:
            sql.append("AND target_type = ?")
            params.append(target_type)
        if target_id:
            sql.append("AND target_id = ?")
            params.append(target_id)
        if correlation_id:
            sql.append("AND correlation_id = ?")
            params.append(correlation_id)
        if date_from:
            sql.append("AND at_utc >= ?")
            params.append(date_from)
        if date_to:
            sql.append("AND at_utc <= ?")
            params.append(date_to)
        if venue_ids is not None:
            if not venue_ids:
                return []
            placeholders = ", ".join("?" for _ in venue_ids)
            sql.append(f"AND (venue_id IS NULL OR venue_id IN ({placeholders}))")
            params.extend(venue_ids)
        sql.append("ORDER BY at_utc DESC, id DESC LIMIT ?")
        params.append(int(limit))
        rows = self.db.query(" ".join(sql), params)
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["previous"] = decode(item.pop("previous_json", None))
            item["new"] = decode(item.pop("new_json", None))
            out.append(item)
        return out

    def count(self, ctx: RequestContext, action: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ? AND action = ?",
                (ctx.tenant_id, action),
                default=0,
            )
        )


__all__ = ["AUDITED_ACTIONS", "AuditLog"]
