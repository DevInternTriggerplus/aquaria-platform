"""Business / Venue settings: VAT, service charge, time zone, ticket validity,
base currency and exchange rates.

These settings are configuration data scoped to an organization or venue, never
hard-coded for Thailand (add_features intro). Two storage shapes are used, chosen
by what each setting actually needs:

* **VAT and service charge** are *effective-dated*: an administrator can schedule
  a rate change for a future date, and a completed order must keep the rate that
  applied when it was made. Each change is a new ``charge_settings`` row with an
  ``effective_from`` date; resolution picks the latest row not after the
  transaction date. Nothing is edited in place, so history is intact (§2, §33).

* **Time zone and ticket-validity policy** are single current values with version
  history — exactly what :class:`~utp.core.config.ConfigStore` already provides —
  so they live there under ``venue.timezone`` / ``ticket.validity_policy``.

* **Exchange rates** are their own table: many pairs, effective periods, and a
  uniqueness rule that forbids two active rows for one pair on the same date
  (§16-§22).

Every mutator enforces the matching ``MANAGE_*`` action permission and audits the
change with old and new values (§30-§32). Reads never require the action
permission — viewing a rate is not changing it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..core.audit import AuditLog
from ..core.clock import Clock, operating_date, parse_date, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, IntegrityViolation
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.ids import new_id
from ..core.money import (
    CURRENCY_DISPLAY,
    ChargeInput,
    convert_currency,
    currency_decimals,
    parse_rate,
    rate_direction_label,
)
from .authz import AuthorizationService

#: The QR/ticket validity models an administrator may select (settings spec §11).
VALIDITY_TYPES: tuple[str, ...] = (
    "END_OF_VISIT_DAY",
    "NUMBER_OF_DAYS",
    "FIXED_DURATION",
    "FIXED_RANGE",
    "SESSION_BASED",
    "MEMBERSHIP",
    "CUSTOM",
)

#: The default policy for a venue that has configured nothing: one-day validity
#: expiring at 23:59:59 venue-local on the visit date, single entry, no re-entry
#: (settings spec §10, §12, §37).
DEFAULT_VALIDITY_POLICY: dict[str, Any] = {
    "validity_type": "END_OF_VISIT_DAY",
    "number_of_days": 1,
    "duration_minutes": None,
    "entry_start_time": None,
    "entry_cutoff_time": None,
    "grace_minutes": 0,
    "reentry_allowed": False,
    "max_entries": 1,
}


class SettingsService:
    """Tax, service charge, time zone, ticket validity, currency and exchange rates."""

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
    # VAT and service charge (effective-dated)
    # ------------------------------------------------------------------ #

    def set_vat(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        enabled: bool,
        rate_bp: int,
        mode: str,
        effective_from: str,
        display_name: str = "VAT",
        tax_registration: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Configure VAT for a venue, effective from a date (settings spec §1, §34, §35)."""
        return self._set_charge(
            ctx,
            charge_kind="VAT",
            venue_id=venue_id,
            enabled=enabled,
            rate_bp=rate_bp,
            mode=mode,
            effective_from=effective_from,
            display_name=display_name,
            tax_registration=tax_registration,
            page="VAT Settings",
            action="MANAGE_TAX_SETTINGS",
            reason=reason,
        )

    def set_service_charge(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        enabled: bool,
        rate_bp: int,
        mode: str,
        effective_from: str,
        display_name: str = "Service Charge",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Configure the service charge for a venue (settings spec §3, §36)."""
        return self._set_charge(
            ctx,
            charge_kind="SERVICE_CHARGE",
            venue_id=venue_id,
            enabled=enabled,
            rate_bp=rate_bp,
            mode=mode,
            effective_from=effective_from,
            display_name=display_name,
            tax_registration=None,
            page="Service Charge Settings",
            action="MANAGE_SERVICE_CHARGE",
            reason=reason,
        )

    def _set_charge(
        self,
        ctx: RequestContext,
        *,
        charge_kind: str,
        venue_id: str,
        enabled: bool,
        rate_bp: int,
        mode: str,
        effective_from: str,
        display_name: str,
        tax_registration: str | None,
        page: str,
        action: str,
        reason: str | None,
    ) -> dict[str, Any]:
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, page, "EDIT")
        self.authz.require_action(vctx, action, reason=reason, target_type="venue", target_id=venue_id)
        if mode not in ("INCLUSIVE", "EXCLUSIVE"):
            raise ValidationError({"mode": "Choose Included or Excluded."})
        rate_bp = int(rate_bp)
        if rate_bp < 0 or rate_bp > 100_000:
            raise ValidationError({"rate_bp": "Enter a rate between 0 and 100%."})
        parse_date(effective_from)  # validate shape
        now = to_iso(self.clock.now())

        previous = self._current_charge_row(ctx, charge_kind, venue_id, effective_from)
        with self.db.transaction():
            # Supersede any row with the same effective_from at this scope so a
            # correction replaces rather than duplicates; a genuinely new effective
            # date leaves the earlier row intact for history.
            same_date = self.db.query_one(
                "SELECT id, version FROM charge_settings "
                "WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ? "
                "AND charge_kind = ? AND effective_from = ? AND superseded_at IS NULL",
                (ctx.tenant_id, venue_id, charge_kind, effective_from),
            )
            version = 1
            if same_date is not None:
                version = int(same_date["version"]) + 1
                self.db.update("charge_settings", same_date["id"], {"superseded_at": now})
            row_id = new_id("chg")
            self.db.insert(
                "charge_settings",
                {
                    "id": row_id,
                    "tenant_id": ctx.tenant_id,
                    "scope_type": "VENUE",
                    "scope_id": venue_id,
                    "charge_kind": charge_kind,
                    "enabled": 1 if enabled else 0,
                    "rate_bp": rate_bp,
                    "mode": mode,
                    "display_name": display_name,
                    "tax_registration": tax_registration,
                    "effective_from": effective_from,
                    "version": version,
                    "actor_id": ctx.principal.id,
                    "created_at": now,
                },
            )
            self.audit.record(
                vctx,
                "CONFIG_CHANGE",
                target_type="charge_settings",
                target_id=f"{charge_kind}:{venue_id}",
                previous=previous,
                new={
                    "charge_kind": charge_kind,
                    "enabled": enabled,
                    "rate_bp": rate_bp,
                    "mode": mode,
                    "effective_from": effective_from,
                    "display_name": display_name,
                },
                reason=reason,
            )
        return self.get_charge(ctx, charge_kind=charge_kind, venue_id=venue_id, on_date=effective_from)

    def _current_charge_row(
        self, ctx: RequestContext, charge_kind: str, venue_id: str, on_date: str
    ) -> dict[str, Any] | None:
        """The charge row effective on ``on_date`` — latest effective_from not after it."""
        row = self.db.query_one(
            "SELECT * FROM charge_settings "
            "WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ? "
            "AND charge_kind = ? AND effective_from <= ? AND superseded_at IS NULL "
            "ORDER BY effective_from DESC, version DESC, rowid DESC LIMIT 1",
            (ctx.tenant_id, venue_id, charge_kind, on_date),
        )
        return dict(row) if row is not None else None

    def get_charge(
        self, ctx: RequestContext, *, charge_kind: str, venue_id: str, on_date: str | None = None
    ) -> dict[str, Any]:
        """The effective charge config for a venue on a date, as a plain dict.

        Falls back to the venue's legacy ``tax_model``/``tax_rate_bp`` for VAT when
        no ``charge_settings`` row exists, so a venue provisioned before this module
        still taxes correctly. Service charge defaults to disabled.
        """
        on_date = on_date or self._venue_today(ctx, venue_id)
        row = self._current_charge_row(ctx, charge_kind, venue_id, on_date)
        if row is not None:
            return {
                "charge_kind": charge_kind,
                "enabled": bool(row["enabled"]),
                "rate_bp": int(row["rate_bp"]),
                "mode": row["mode"],
                "display_name": row["display_name"],
                "tax_registration": row["tax_registration"],
                "effective_from": row["effective_from"],
                "source": "charge_settings",
            }
        if charge_kind == "VAT":
            venue = self._venue(ctx, venue_id)
            rate = int(venue["tax_rate_bp"] or 0)
            return {
                "charge_kind": "VAT",
                "enabled": rate > 0,
                "rate_bp": rate,
                "mode": venue["tax_model"] or "INCLUSIVE",
                "display_name": "VAT",
                "tax_registration": venue.get("tax_registration"),
                "effective_from": None,
                "source": "venue_default",
            }
        return {
            "charge_kind": "SERVICE_CHARGE",
            "enabled": False,
            "rate_bp": 0,
            "mode": "EXCLUSIVE",
            "display_name": "Service Charge",
            "tax_registration": None,
            "effective_from": None,
            "source": "default",
        }

    def charge_inputs(
        self, ctx: RequestContext, *, venue_id: str, on_date: str | None = None
    ) -> tuple[ChargeInput, ChargeInput]:
        """(service_charge, vat) as :class:`ChargeInput` for the charge engine.

        This is the single call the booking path uses so that the rate stored on an
        order is exactly the effective rate on the visit/transaction date.
        """
        vat = self.get_charge(ctx, charge_kind="VAT", venue_id=venue_id, on_date=on_date)
        sc = self.get_charge(ctx, charge_kind="SERVICE_CHARGE", venue_id=venue_id, on_date=on_date)
        return (
            ChargeInput(
                enabled=sc["enabled"], rate_bp=sc["rate_bp"], mode=sc["mode"], display_name=sc["display_name"]
            ),
            ChargeInput(
                enabled=vat["enabled"], rate_bp=vat["rate_bp"], mode=vat["mode"], display_name=vat["display_name"]
            ),
        )

    def rounding_policy(
        self, ctx: RequestContext, *, venue_id: str, fallback_mode: str = "NONE"
    ) -> tuple[str, int | None]:
        """Effective rounding (mode, increment_minor) for the charge engine.

        The Rounding *settings page* (config key ``venue.rounding``) is authoritative
        when set; otherwise we fall back to the venue's legacy ``rounding_mode`` column
        so an unconfigured venue still behaves exactly as before. Returning the pair
        keeps ``compute_charges`` the single place rounding is applied (Fix.md §5).
        """
        cfg = self.config.get(ctx.for_venue(venue_id), "venue.rounding", venue_id=venue_id)
        if isinstance(cfg, dict) and cfg.get("mode"):
            return str(cfg["mode"]), (int(cfg["increment_minor"]) if cfg.get("increment_minor") else None)
        return (fallback_mode or "NONE"), None

    def charge_history(self, ctx: RequestContext, *, charge_kind: str, venue_id: str) -> list[dict[str, Any]]:
        self.authz.require_page(
            ctx.for_venue(venue_id),
            "VAT Settings" if charge_kind == "VAT" else "Service Charge Settings",
            "VIEW",
        )
        rows = self.db.query(
            "SELECT enabled, rate_bp, mode, display_name, effective_from, version, actor_id, "
            "created_at, superseded_at FROM charge_settings "
            "WHERE tenant_id = ? AND scope_type = 'VENUE' AND scope_id = ? AND charge_kind = ? "
            "ORDER BY effective_from DESC, version DESC",
            (ctx.tenant_id, venue_id, charge_kind),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Time zone
    # ------------------------------------------------------------------ #

    def set_timezone(
        self, ctx: RequestContext, *, venue_id: str, timezone: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Set the venue's IANA time zone (settings spec §8, §39).

        Rejects a bare UTC offset: the spec is explicit that an IANA identifier is
        required so daylight rules and future venues in other countries work.
        """
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Time Zone Settings", "EDIT")
        self.authz.require_action(vctx, "MANAGE_TIMEZONE", reason=reason, target_type="venue", target_id=venue_id)
        self._validate_timezone(timezone)
        venue = self._venue(ctx, venue_id)
        previous = venue["timezone"]
        with self.db.transaction():
            self.db.update("venues", venue_id, {"timezone": timezone}, tenant_id=ctx.tenant_id)
            self.audit.record(
                vctx,
                "CONFIG_CHANGE",
                target_type="venue_timezone",
                target_id=venue_id,
                previous={"timezone": previous},
                new={"timezone": timezone},
                reason=reason,
            )
        # Changing the venue's current timezone must not alter tickets already
        # issued: those carry their own validity_timezone snapshot (settings spec §14).
        return {"venue_id": venue_id, "timezone": timezone, "previous": previous}

    @staticmethod
    def _validate_timezone(timezone: str) -> None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not timezone or "/" not in timezone:
            raise ValidationError(
                {"timezone": "Choose an IANA time zone such as Asia/Bangkok, not a UTC offset."}
            )
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError) as exc:
            raise ValidationError({"timezone": f"Unknown time zone {timezone!r}."}) from exc

    # ------------------------------------------------------------------ #
    # Ticket validity policy
    # ------------------------------------------------------------------ #

    def set_validity_policy(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        policy: dict[str, Any],
        product_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Set the default ticket validity policy for a venue or a product (§12)."""
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Ticket Validity Settings", "EDIT")
        self.authz.require_action(
            vctx, "MANAGE_TICKET_VALIDITY", reason=reason, target_type="venue", target_id=venue_id
        )
        clean = self._validate_policy(policy)
        scope_type = "PRODUCT" if product_id else "VENUE"
        scope_id = product_id or venue_id
        previous = self.config.get(
            vctx, "ticket.validity_policy", venue_id=venue_id, product_id=product_id
        )
        self.config.set(
            vctx,
            "ticket.validity_policy",
            clean,
            scope_type=scope_type,
            scope_id=scope_id,
            audit_action="CONFIG_CHANGE",
        )
        self.audit.record(
            vctx,
            "CONFIG_CHANGE",
            target_type="ticket_validity",
            target_id=scope_id,
            previous={"policy": previous},
            new={"policy": clean},
            reason=reason,
        )
        return clean

    def _validate_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        merged = {**DEFAULT_VALIDITY_POLICY, **(policy or {})}
        vtype = merged.get("validity_type")
        if vtype not in VALIDITY_TYPES:
            raise ValidationError(
                {"validity_type": f"Choose one of: {', '.join(VALIDITY_TYPES)}."}
            )
        if vtype == "NUMBER_OF_DAYS" and int(merged.get("number_of_days") or 0) < 1:
            raise ValidationError({"number_of_days": "Enter at least 1 day."})
        if vtype == "FIXED_DURATION" and int(merged.get("duration_minutes") or 0) < 1:
            raise ValidationError({"duration_minutes": "Enter a duration in minutes."})
        merged["reentry_allowed"] = bool(merged.get("reentry_allowed"))
        merged["max_entries"] = int(merged.get("max_entries") or 1)
        return merged

    def validity_policy(
        self, ctx: RequestContext, *, venue_id: str, product_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve the effective validity policy (product overrides venue) (R1.7)."""
        value = self.config.get(
            ctx, "ticket.validity_policy", venue_id=venue_id, product_id=product_id
        )
        if not value:
            return dict(DEFAULT_VALIDITY_POLICY)
        return {**DEFAULT_VALIDITY_POLICY, **value}

    # ------------------------------------------------------------------ #
    # Currency
    # ------------------------------------------------------------------ #

    def set_base_currency(
        self, ctx: RequestContext, *, venue_id: str, currency: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Set the venue's base (settlement) currency, ISO 4217 (settings spec §15)."""
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, "Currency Settings", "EDIT")
        self.authz.require_action(vctx, "MANAGE_CURRENCY", reason=reason, target_type="venue", target_id=venue_id)
        code = (currency or "").upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError({"currency": "Enter a 3-letter ISO 4217 code such as THB."})
        venue = self._venue(ctx, venue_id)
        previous = venue["currency"]
        with self.db.transaction():
            self.db.update("venues", venue_id, {"currency": code}, tenant_id=ctx.tenant_id)
            self.audit.record(
                vctx,
                "CONFIG_CHANGE",
                target_type="base_currency",
                target_id=venue_id,
                previous={"currency": previous},
                new={"currency": code},
                reason=reason,
            )
        return {"venue_id": venue_id, "currency": code, "previous": previous}

    def currency_info(self, currency: str) -> dict[str, Any]:
        """Symbol, decimals and display order for a currency (settings spec §23)."""
        code = (currency or "THB").upper()
        display = CURRENCY_DISPLAY.get(code, {"symbol": code + " ", "decimals": currency_decimals(code), "symbol_first": True})
        return {
            "code": code,
            "symbol": display["symbol"],
            "decimals": int(display["decimals"]),
            "symbol_first": bool(display.get("symbol_first", True)),
        }

    # ------------------------------------------------------------------ #
    # Exchange rates
    # ------------------------------------------------------------------ #

    def set_exchange_rate(
        self,
        ctx: RequestContext,
        *,
        organization_id: str,
        from_currency: str,
        to_currency: str,
        rate: str | Decimal,
        effective_from: str,
        effective_until: str | None = None,
        venue_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Create a manual exchange rate (settings spec §16-§22, §40).

        Direction is ``1 from_currency = rate to_currency``. A second active rate for
        the same pair and effective date is refused (§22) — end the existing one or
        choose a different effective date.
        """
        scope_type = "VENUE" if venue_id else "ORGANIZATION"
        scope_id = venue_id or organization_id
        actx = ctx.for_venue(venue_id) if venue_id else ctx
        self.authz.require_page(actx, "Exchange Rates", "ADD")
        self.authz.require_action(
            actx, "MANAGE_EXCHANGE_RATE", reason=reason, target_type="exchange_rate", target_id=scope_id
        )
        from_code = (from_currency or "").upper()
        to_code = (to_currency or "").upper()
        if from_code == to_code:
            raise ValidationError({"to_currency": "The two currencies must differ."})
        for label, code in (("from_currency", from_code), ("to_currency", to_code)):
            if len(code) != 3 or not code.isalpha():
                raise ValidationError({label: "Enter a 3-letter ISO 4217 code."})
        rate_text = str(parse_rate(rate))
        parse_date(effective_from)
        if effective_until:
            parse_date(effective_until)
            if parse_date(effective_until) < parse_date(effective_from):
                raise ValidationError({"effective_until": "The end date must not be before the start."})
        now = to_iso(self.clock.now())
        row_id = new_id("fx")
        try:
            self.db.insert(
                "exchange_rates",
                {
                    "id": row_id,
                    "tenant_id": ctx.tenant_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "from_currency": from_code,
                    "to_currency": to_code,
                    "rate_text": rate_text,
                    "effective_from": effective_from,
                    "effective_until": effective_until,
                    "status": "ACTIVE",
                    "source": "MANUAL",
                    "actor_id": ctx.principal.id,
                    "created_at": now,
                },
            )
        except IntegrityViolation as exc:
            # The partial unique index refuses a second ACTIVE row for one pair on
            # the same effective date (settings spec §22).
            if "ux_exchange_rate_active" in str(exc) or "UNIQUE" in str(exc).upper():
                raise ConflictError(
                    f"An active {from_code} to {to_code} rate already exists for {effective_from}. "
                    "End it first or choose a different effective date.",
                    code="duplicate_exchange_rate",
                ) from exc
            raise
        self.audit.record(
            actx,
            "CONFIG_CHANGE",
            target_type="exchange_rate",
            target_id=row_id,
            new={
                "pair": f"{from_code}->{to_code}",
                "rate": rate_text,
                "direction": rate_direction_label(from_code, to_code, rate_text),
                "effective_from": effective_from,
                "effective_until": effective_until,
            },
            reason=reason,
        )
        return self.get_exchange_rate_row(ctx, row_id)

    def end_exchange_rate(
        self, ctx: RequestContext, *, rate_id: str, effective_until: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        """Mark a rate ENDED so it stops applying and a new one can take its place."""
        row = self.get_exchange_rate_row(ctx, rate_id)
        actx = ctx.for_venue(row["scope_id"]) if row["scope_type"] == "VENUE" else ctx
        self.authz.require_page(actx, "Exchange Rates", "EDIT")
        self.authz.require_action(
            actx, "MANAGE_EXCHANGE_RATE", reason=reason, target_type="exchange_rate", target_id=rate_id
        )
        now = to_iso(self.clock.now())
        self.db.update(
            "exchange_rates",
            rate_id,
            {
                "status": "ENDED",
                "effective_until": effective_until or (row["effective_until"] or now[:10]),
                "updated_actor_id": ctx.principal.id,
                "updated_at": now,
            },
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            actx,
            "CONFIG_CHANGE",
            target_type="exchange_rate",
            target_id=rate_id,
            previous={"status": row["status"]},
            new={"status": "ENDED", "effective_until": effective_until},
            reason=reason,
        )
        return self.get_exchange_rate_row(ctx, rate_id)

    def get_exchange_rate_row(self, ctx: RequestContext, rate_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM exchange_rates WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, rate_id)
        )
        if row is None:
            raise NotFound("Exchange rate not found.")
        out = dict(row)
        out["direction"] = rate_direction_label(out["from_currency"], out["to_currency"], out["rate_text"])
        return out

    def resolve_exchange_rate(
        self,
        ctx: RequestContext,
        *,
        from_currency: str,
        to_currency: str,
        organization_id: str,
        venue_id: str | None = None,
        on_date: str | None = None,
    ) -> dict[str, Any] | None:
        """The active rate for a pair on a date. Venue scope beats organization.

        Returns the row (with a ``Decimal`` ``rate``) or ``None`` if no rate applies,
        in which case the caller must not invent one (mirrors R5.6 for pricing).
        """
        on_date = on_date or self._venue_today(ctx, venue_id) if venue_id else (on_date or to_iso(self.clock.now())[:10])
        from_code = (from_currency or "").upper()
        to_code = (to_currency or "").upper()
        candidates: list[tuple[str, str]] = []
        if venue_id:
            candidates.append(("VENUE", venue_id))
        candidates.append(("ORGANIZATION", organization_id))
        for scope_type, scope_id in candidates:
            row = self.db.query_one(
                "SELECT * FROM exchange_rates WHERE tenant_id = ? AND scope_type = ? AND scope_id = ? "
                "AND from_currency = ? AND to_currency = ? AND status = 'ACTIVE' "
                "AND effective_from <= ? AND (effective_until IS NULL OR effective_until >= ?) "
                "ORDER BY effective_from DESC, rowid DESC LIMIT 1",
                (ctx.tenant_id, scope_type, scope_id, from_code, to_code, on_date, on_date),
            )
            if row is not None:
                out = dict(row)
                out["rate"] = parse_rate(out["rate_text"])
                out["direction"] = rate_direction_label(from_code, to_code, out["rate_text"])
                return out
        return None

    def list_exchange_rates(
        self, ctx: RequestContext, *, organization_id: str, venue_id: str | None = None, include_ended: bool = True
    ) -> list[dict[str, Any]]:
        actx = ctx.for_venue(venue_id) if venue_id else ctx
        self.authz.require_page(actx, "Exchange Rates", "VIEW")
        scope_type = "VENUE" if venue_id else "ORGANIZATION"
        scope_id = venue_id or organization_id
        clause = "" if include_ended else " AND status = 'ACTIVE'"
        rows = self.db.query(
            "SELECT * FROM exchange_rates WHERE tenant_id = ? AND scope_type = ? AND scope_id = ?"
            + clause
            + " ORDER BY from_currency, to_currency, effective_from DESC",
            (ctx.tenant_id, scope_type, scope_id),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["direction"] = rate_direction_label(item["from_currency"], item["to_currency"], item["rate_text"])
            out.append(item)
        return out

    def quote_conversion(
        self,
        ctx: RequestContext,
        *,
        amount_minor: int,
        from_currency: str,
        to_currency: str,
        organization_id: str,
        venue_id: str | None = None,
        on_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Convert an amount and return everything a transaction must record (§19, §24)."""
        if (from_currency or "").upper() == (to_currency or "").upper():
            return {
                "transaction_currency": to_currency.upper(),
                "base_currency": to_currency.upper(),
                "exchange_rate": "1",
                "original_amount_minor": int(amount_minor),
                "converted_amount_minor": int(amount_minor),
                "direction": None,
            }
        rate = self.resolve_exchange_rate(
            ctx,
            from_currency=from_currency,
            to_currency=to_currency,
            organization_id=organization_id,
            venue_id=venue_id,
            on_date=on_date,
        )
        if rate is None:
            return None
        converted = convert_currency(
            amount_minor, rate=rate["rate"], from_currency=from_currency, to_currency=to_currency
        )
        return {
            "transaction_currency": from_currency.upper(),
            "base_currency": to_currency.upper(),
            "exchange_rate": rate["rate_text"],
            "original_amount_minor": int(amount_minor),
            "converted_amount_minor": converted,
            "direction": rate["direction"],
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _venue(self, ctx: RequestContext, venue_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM venues WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, venue_id)
        )
        if row is None:
            raise NotFound("Venue not found.")
        return dict(row)

    def _venue_today(self, ctx: RequestContext, venue_id: str | None) -> str:
        if not venue_id:
            return to_iso(self.clock.now())[:10]
        venue = self._venue(ctx, venue_id)
        return operating_date(
            self.clock.now(), venue["timezone"], int(venue.get("day_boundary_hour") or 0)
        ).isoformat()


__all__ = ["DEFAULT_VALIDITY_POLICY", "VALIDITY_TYPES", "SettingsService"]
