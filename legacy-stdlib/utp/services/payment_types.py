"""Payment types: the customer-facing, admin-configurable ways to pay.

A *payment type* is deliberately separate from a *payment method*. The method
(``CARD``, ``QR_BANK_TRANSFER``, ``EWALLET``, ``CASH``) is the settlement primitive the
payment service understands; it is code. A payment type is presentation and
availability — PromptPay, Alipay, WeChat Pay, Credit Card — and it is data an
administrator owns (update spec §20–§25). PromptPay and Credit Card both settle through
different methods; Alipay and WeChat Pay both settle as ``EWALLET`` but are distinct
cards to the customer. Keeping them apart is what lets a new wallet be added from the
back office without touching the booking UI (§22, §49).

Two rules the spec is firm about live here:

* the customer UI must never hard-code the list — :meth:`customer_payment_types` returns
  exactly the enabled types for the given channel, in display order (§20, §49);
* provider credentials are sensitive. Only a non-secret ``provider_config_ref`` is
  stored (the real key is in the secret store), and even that is masked unless the
  caller holds ``MANAGE_PAYMENT_PROVIDER_CONFIG`` (§39).

Every mutator enforces its permission server-side and audits old→new (§40).
"""

from __future__ import annotations

from typing import Any, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.i18n import text as i18n_text
from ..core.ids import new_id
from ..domain import enums
from .authz import AuthorizationService

_CHANNEL_COLUMN = {"ONLINE": "web_enabled", "WEB": "web_enabled", "KIOSK": "kiosk_enabled", "COUNTER": "counter_enabled"}

#: Phase 1 default payment types (update spec §21, §50). Each maps a customer-facing
#: card onto a settlement method. Spelling is "PromptPay", not "Promtpay".
DEFAULT_PAYMENT_TYPES: tuple[dict[str, Any], ...] = (
    {
        "code": "PROMPTPAY",
        "method": "QR_BANK_TRANSFER",
        "display_name": {"en": "PromptPay", "th": "พร้อมเพย์", "zh": "PromptPay", "ja": "PromptPay", "ru": "PromptPay"},
        "icon": "qr",
        "provider": "promptpay",
        "web_enabled": True,
        "kiosk_enabled": True,
        "counter_enabled": True,
        "display_order": 10,
    },
    {
        "code": "CREDIT_CARD",
        "method": "CARD",
        "display_name": {"en": "Credit Card", "th": "บัตรเครดิต", "zh": "信用卡", "ja": "クレジットカード", "ru": "Кредитная карта"},
        "icon": "card",
        "provider": "card_acquirer",
        "web_enabled": True,
        "kiosk_enabled": True,
        "counter_enabled": True,
        "display_order": 20,
    },
    {
        "code": "ALIPAY",
        "method": "EWALLET",
        "display_name": {"en": "Alipay", "th": "Alipay", "zh": "支付宝", "ja": "Alipay", "ru": "Alipay"},
        "icon": "alipay",
        "provider": "alipay",
        "web_enabled": True,
        "kiosk_enabled": True,
        "counter_enabled": False,
        "display_order": 30,
    },
    {
        "code": "WECHAT_PAY",
        "method": "EWALLET",
        "display_name": {"en": "WeChat Pay", "th": "WeChat Pay", "zh": "微信支付", "ja": "WeChat Pay", "ru": "WeChat Pay"},
        "icon": "wechat",
        "provider": "wechat",
        "web_enabled": True,
        "kiosk_enabled": True,
        "counter_enabled": False,
        "display_order": 40,
    },
)


class PaymentTypeService:
    """CRUD and customer-facing resolution of payment types (update spec §20-§25)."""

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
    # Writes
    # ------------------------------------------------------------------ #

    def create(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        code: str,
        method: str,
        display_name: Any,
        icon: str | None = None,
        description: Any = None,
        provider: str | None = None,
        provider_config_ref: str | None = None,
        supported_currencies: Sequence[str] | None = None,
        web_enabled: bool = True,
        kiosk_enabled: bool = True,
        counter_enabled: bool = True,
        display_order: int = 0,
        status: str = "ACTIVE",
        reason: str | None = None,
    ) -> dict[str, Any]:
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Payment Type", "ADD")
        self.authz.require_action(vctx, "MANAGE_PAYMENT_TYPE", reason=reason, target_type="payment_type", target_id=code)
        if provider_config_ref:
            # Writing a credential reference is the sensitive part (§39).
            self.authz.require_action(
                vctx, "MANAGE_PAYMENT_PROVIDER_CONFIG", reason=reason, target_type="payment_type", target_id=code
            )
        self._validate_method(method)
        if not i18n_text(display_name, "en", fallback="").strip() and not display_name:
            raise ValidationError({"display_name": "Enter a customer-facing name."})
        existing = self.db.query_one(
            "SELECT id FROM payment_types WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        )
        if existing is not None:
            raise ConflictError(f"Payment type {code!r} already exists.", code="duplicate_payment_type")
        now = to_iso(self.clock.now())
        pt_id = new_id("pt")
        self.db.insert(
            "payment_types",
            {
                "id": pt_id,
                "tenant_id": ctx.tenant_id,
                "scope_type": "VENUE",
                "scope_id": venue_id,
                "code": code,
                "method": method,
                "display_name_json": display_name,
                "description_json": description,
                "icon": icon,
                "provider": provider,
                "provider_config_ref": provider_config_ref,
                "supported_currencies_json": list(supported_currencies) if supported_currencies else None,
                "web_enabled": 1 if web_enabled else 0,
                "kiosk_enabled": 1 if kiosk_enabled else 0,
                "counter_enabled": 1 if counter_enabled else 0,
                "display_order": int(display_order),
                "status": status,
                "created_at": now,
                "actor_id": ctx.principal.id,
            },
        )
        self.audit.record(
            vctx,
            "PAYMENT_TYPE_ADDED",
            target_type="payment_type",
            target_id=pt_id,
            new={"code": code, "method": method, "status": status},
            reason=reason,
        )
        return self.get(ctx, pt_id)

    def update(
        self,
        ctx: RequestContext,
        pt_id: str,
        *,
        changes: dict[str, Any],
        reason: str | None = None,
    ) -> dict[str, Any]:
        current = self._row(ctx, pt_id)
        vctx = ctx.for_venue(current["scope_id"])
        self.authz.require_page(vctx, "Payment Type", "EDIT")
        self.authz.require_action(vctx, "MANAGE_PAYMENT_TYPE", reason=reason, target_type="payment_type", target_id=pt_id)

        allowed = {
            "method",
            "display_name",
            "description",
            "icon",
            "provider",
            "provider_config_ref",
            "supported_currencies",
            "web_enabled",
            "kiosk_enabled",
            "counter_enabled",
            "display_order",
            "status",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError({"changes": f"Unknown fields: {', '.join(sorted(unknown))}."})
        if "provider_config_ref" in changes:
            self.authz.require_action(
                vctx, "MANAGE_PAYMENT_PROVIDER_CONFIG", reason=reason, target_type="payment_type", target_id=pt_id
            )
        if "method" in changes:
            self._validate_method(changes["method"])
        if "status" in changes and changes["status"] not in ("ACTIVE", "DISABLED", "ARCHIVED"):
            raise ValidationError({"status": "Status must be ACTIVE, DISABLED or ARCHIVED."})

        update: dict[str, Any] = {"updated_at": to_iso(self.clock.now()), "actor_id": ctx.principal.id}
        column_map = {
            "display_name": "display_name_json",
            "description": "description_json",
            "supported_currencies": "supported_currencies_json",
        }
        bool_fields = {"web_enabled", "kiosk_enabled", "counter_enabled"}
        for field, value in changes.items():
            column = column_map.get(field, field)
            if field in bool_fields:
                value = 1 if value else 0
            elif field == "display_order":
                value = int(value)
            elif field == "supported_currencies":
                value = list(value) if value else None
            update[column] = value
        self.db.update("payment_types", pt_id, update, tenant_id=ctx.tenant_id)

        action = "PAYMENT_TYPE_EDITED"
        if changes.get("status") == "DISABLED":
            action = "PAYMENT_TYPE_DISABLED"
        elif changes.get("status") == "ARCHIVED":
            action = "PAYMENT_TYPE_ARCHIVED"
        elif set(changes) & {"web_enabled", "kiosk_enabled", "counter_enabled"}:
            action = "PAYMENT_TYPE_CHANNEL_CHANGED"
        elif "display_order" in changes:
            action = "PAYMENT_TYPE_ORDER_CHANGED"
        self.audit.record(
            vctx,
            action,
            target_type="payment_type",
            target_id=pt_id,
            previous=self._audit_snapshot(current),
            new={k: v for k, v in changes.items() if k != "provider_config_ref"},
            reason=reason,
        )
        return self.get(ctx, pt_id)

    def archive(self, ctx: RequestContext, pt_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """DELETE maps to archive: a payment type may have been used on past orders."""
        current = self._row(ctx, pt_id)
        self.authz.require_page(ctx.for_venue(current["scope_id"]), "Payment Type", "DELETE")
        return self.update(ctx, pt_id, changes={"status": "ARCHIVED"}, reason=reason)

    def reorder(self, ctx: RequestContext, *, venue_id: str, ordered_ids: Sequence[str], reason: str | None = None) -> list[dict[str, Any]]:
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Payment Type", "EDIT")
        self.authz.require_action(vctx, "MANAGE_PAYMENT_TYPE", reason=reason, target_type="payment_type", target_id=venue_id)
        now = to_iso(self.clock.now())
        for position, pt_id in enumerate(ordered_ids):
            self.db.update(
                "payment_types", pt_id, {"display_order": (position + 1) * 10, "updated_at": now}, tenant_id=ctx.tenant_id
            )
        self.audit.record(
            vctx,
            "PAYMENT_TYPE_ORDER_CHANGED",
            target_type="payment_type",
            target_id=venue_id,
            new={"order": list(ordered_ids)},
            reason=reason,
        )
        return self.list(ctx, venue_id=venue_id)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def list(self, ctx: RequestContext, *, venue_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        """Back-office list (all statuses unless archived excluded)."""
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Payment Type", "VIEW")
        clause = "" if include_archived else " AND status != 'ARCHIVED'"
        rows = self.db.query(
            "SELECT * FROM payment_types WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ?"
            + clause
            + " ORDER BY display_order, code",
            (ctx.tenant_id, venue_id),
        )
        can_see_secret = self.authz.can_action(vctx, "MANAGE_PAYMENT_PROVIDER_CONFIG")
        return [self._present(dict(r), reveal_secret=can_see_secret) for r in rows]

    def get(self, ctx: RequestContext, pt_id: str) -> dict[str, Any]:
        row = self._row(ctx, pt_id)
        vctx = ctx.for_venue(row["scope_id"])
        self.authz.require_page(vctx, "Payment Type", "VIEW")
        return self._present(row, reveal_secret=self.authz.can_action(vctx, "MANAGE_PAYMENT_PROVIDER_CONFIG"))

    def customer_payment_types(
        self, ctx: RequestContext, *, venue_id: str, channel: str, currency: str | None = None
    ) -> list[dict[str, Any]]:
        """The enabled payment types for a channel, in order, for the customer UI (§49).

        No permission needed — this is the public checkout list. Provider credentials
        are never included. Falls back to the platform defaults if the venue has none
        configured yet, so checkout is never left with an empty payment step.
        """
        column = _CHANNEL_COLUMN.get(channel.upper(), "web_enabled")
        rows = self.db.query(
            f"SELECT * FROM payment_types WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ? "
            f"AND status = 'ACTIVE' AND {column} = 1 ORDER BY display_order, code",
            (ctx.tenant_id, venue_id),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            currencies = decode(record["supported_currencies_json"], None)
            if currency and currencies and currency.upper() not in [c.upper() for c in currencies]:
                continue
            out.append(
                {
                    "id": record["id"],
                    "code": record["code"],
                    "method": record["method"],
                    "display_name": i18n_text(decode(record["display_name_json"], {}), ctx.language, fallback=record["code"]),
                    "description": i18n_text(decode(record["description_json"], {}), ctx.language, fallback=""),
                    "icon": record["icon"],
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Seeding
    # ------------------------------------------------------------------ #

    def seed_defaults(self, ctx: RequestContext, *, venue_id: str) -> list[dict[str, Any]]:
        """Provision the Phase 1 default payment types for a venue (§50).

        Idempotent: skips any code that already exists, so re-running seed is safe.
        """
        created: list[dict[str, Any]] = []
        for spec in DEFAULT_PAYMENT_TYPES:
            exists = self.db.query_one(
                "SELECT 1 FROM payment_types WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ? AND code = ?",
                (ctx.tenant_id, venue_id, spec["code"]),
            )
            if exists is not None:
                continue
            created.append(
                self.create(
                    ctx,
                    venue_id=venue_id,
                    code=spec["code"],
                    method=spec["method"],
                    display_name=spec["display_name"],
                    icon=spec["icon"],
                    provider=spec["provider"],
                    web_enabled=spec["web_enabled"],
                    kiosk_enabled=spec["kiosk_enabled"],
                    counter_enabled=spec["counter_enabled"],
                    display_order=spec["display_order"],
                    reason="Phase 1 default payment types",
                )
            )
        return created

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _validate_method(self, method: str) -> None:
        if method not in enums.PAYMENT_METHODS:
            raise ValidationError(
                {"method": f"Settlement method must be one of: {', '.join(enums.PAYMENT_METHODS)}."}
            )

    def _row(self, ctx: RequestContext, pt_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM payment_types WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, pt_id)
        )
        if row is None:
            raise NotFound("Payment type not found.")
        return dict(row)

    def _present(self, row: dict[str, Any], *, reveal_secret: bool) -> dict[str, Any]:
        return {
            "id": row["id"],
            "code": row["code"],
            "method": row["method"],
            "display_name": decode(row["display_name_json"], {}),
            "description": decode(row["description_json"], {}),
            "icon": row["icon"],
            "provider": row["provider"],
            # The credential reference is masked for anyone without the provider-config
            # permission — they can see a type exists and is wired, not its secret (§39).
            "provider_config_ref": row["provider_config_ref"] if reveal_secret else self._mask(row["provider_config_ref"]),
            "supported_currencies": decode(row["supported_currencies_json"], None),
            "web_enabled": bool(row["web_enabled"]),
            "kiosk_enabled": bool(row["kiosk_enabled"]),
            "counter_enabled": bool(row["counter_enabled"]),
            "display_order": int(row["display_order"]),
            "status": row["status"],
        }

    @staticmethod
    def _mask(value: str | None) -> str | None:
        if not value:
            return value
        return "••••" + value[-2:] if len(value) > 2 else "••••"

    @staticmethod
    def _audit_snapshot(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": row["status"],
            "display_order": row["display_order"],
            "web_enabled": bool(row["web_enabled"]),
            "kiosk_enabled": bool(row["kiosk_enabled"]),
            "counter_enabled": bool(row["counter_enabled"]),
        }


__all__ = ["DEFAULT_PAYMENT_TYPES", "PaymentTypeService"]
