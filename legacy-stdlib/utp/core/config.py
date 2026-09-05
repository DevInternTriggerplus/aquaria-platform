"""Configuration resolution.

Business behaviour lives in tenant-scoped configuration records, never in
compiled constants (R1.4). This module is the single reader/writer.

Resolution order is fixed by R1.7 — Session → Product → Experience → Venue →
Organization → Tenant → platform default — and implemented as one indexed query
that returns *all* candidate rows, then picks the nearest scope in Python. That
is deliberate: it means a caller can also ask *where* a value came from, which
the back office needs in order to show an operator whether they are looking at a
venue override or an inherited tenant default.

Changes are versioned rather than overwritten: the previous row is stamped
``superseded_at`` and a new row is inserted with ``version + 1`` (R1.8). Nothing
needs a restart because resolution always reads the live table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clock import Clock, to_iso
from .context import RequestContext
from .db import Database, decode, encode
from .ids import new_id

#: Scope names ordered from most specific to least specific (R1.7).
SCOPE_ORDER: tuple[str, ...] = (
    "SESSION",
    "PRODUCT",
    "EXPERIENCE",
    "VENUE",
    "ORGANIZATION",
    "TENANT",
)

_SCOPE_RANK: dict[str, int] = {name: index for index, name in enumerate(SCOPE_ORDER)}


#: Platform defaults — the last resort in the resolution chain. These are the
#: only hard-coded values in the platform and every one of them is overridable at
#: any scope, which is what keeps R1.4 true.
PLATFORM_DEFAULTS: dict[str, Any] = {
    # --- capacity & holds (R10) ---
    "hold.duration_minutes": 10,
    "hold.warning_threshold_minutes": 2,
    "hold.reclaim_interval_seconds": 30,
    "hold.max_per_source_per_hour": 40,
    "seat_hold.duration_minutes": 10,
    "seat_hold.warning_threshold_minutes": 2,
    "availability.cache_max_staleness_seconds": 15,
    # --- calendar & booking rules (R6, R7) ---
    "booking.max_days_in_advance": 90,
    "booking.min_lead_time_minutes": 0,
    "booking.same_day_enabled": True,
    "booking.cutoff_time": None,
    "booking.available_weekdays": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
    "booking.max_per_booking": 20,
    "calendar.limited_availability_threshold": {"mode": "PERCENT", "value": 20},
    "calendar.horizon_days": 120,
    # --- sessions & schedule (R8, R21) ---
    "session.booking_cutoff_minutes": 0,
    "session.grace_minutes": 15,
    "schedule.materialization_horizon_days": 90,
    "schedule.publish_required": True,
    # --- pricing & tax (R5) ---
    "pricing.currency_fallback_enabled": False,
    "tax.model": "INCLUSIVE",
    "tax.rate_bp": 700,
    "rounding.mode": "NONE",
    # --- promotions (R13) ---
    "promotion.resolution_preference": "BEST_FOR_CUSTOMER",
    "promotion.max_stacked": 3,
    # --- consent & privacy (R12) ---
    "consent.minor_age_threshold": 20,
    "consent.withdrawal_effective_days": 7,
    "consent.retention_years": 10,
    "privacy.dsar_response_days": 30,
    "privacy.breach_report_hours": 72,
    "retention.customer_pii_days": 1825,
    "retention.audit_days": 1825,
    "retention.financial_days": 3650,
    # --- notifications (R36, R37) ---
    "notification.reminder_offsets_hours": [24],
    "notification.max_retries": 5,
    "notification.retry_base_seconds": 30,
    "notification.combine_confirmation_and_ticket": True,
    "notification.channels": ["EMAIL"],
    # How the e-ticket's QR reaches the guest: CID attaches it to the message (the
    # only mode every client renders — Gmail strips data: URLs), LINK uses a signed
    # expiring URL, DATA_URL inlines it. Configuration, not a code path.
    "notification.qr_delivery": "CID",
    #: Origin for LINK mode and for the "view in browser" copy.
    "notification.public_base_url": "",
    # --- reporting exception thresholds ------------------------------------ #
    # A venue that refunds 8% of orders by design should not be told daily that it
    # refunds too much, so every threshold is configuration. Basis points for a
    # rate, minutes for staleness.
    "reporting.threshold.refund_rate_bp": 500,
    "reporting.threshold.void_rate_bp": 300,
    "reporting.threshold.manual_discount_bp": 2000,
    "reporting.threshold.manual_discount_share_bp": 500,
    "reporting.threshold.complimentary_share_bp": 300,
    "reporting.threshold.payment_failure_bp": 1000,
    "reporting.threshold.capacity_near_full_bp": 9000,
    "reporting.threshold.promotion_budget_bp": 8000,
    "reporting.threshold.device_offline_minutes": 15,
    # --- manage booking (R16) ---
    "manage_booking.verification_ttl_minutes": 15,
    "manage_booking.max_attempts_per_hour": 5,
    # Wrong codes tolerated before the outstanding challenges are burned and the
    # caller is throttled. Low enough to stop guessing a 6-character code, high
    # enough that a customer mistyping one digit is not locked out (R16.3).
    "manage_booking.max_verification_attempts": 5,
    "manage_booking.reschedule_enabled": True,
    "manage_booking.cancel_enabled": True,
    # --- refunds (R17) ---
    "refund.policy": {
        "tiers": [
            {"min_hours_before": 48, "refund_percent_bp": 10000, "fee_minor": 0},
            {"min_hours_before": 24, "refund_percent_bp": 5000, "fee_minor": 0},
            {"min_hours_before": 0, "refund_percent_bp": 0, "fee_minor": 0},
        ],
        "restore_capacity": True,
    },
    # --- gate & devices (R32) ---
    "access.reentry_window_minutes": 0,
    "access.offline_cache_max_age_minutes": 720,
    "access.decision_target_ms": 500,
    "kiosk.idle_timeout_seconds": 90,
    "kiosk.idle_warning_seconds": 20,
    # --- presentation (R63, R65) ---
    "ui.mobile_breakpoint_px": 768,
    "ui.touch_target_min_px": 44,
    "ui.kiosk_touch_target_min_px": 72,
    "ui.seat_min_tappable_px": 32,
    "ui.seat_search_min_seats": 150,
    "ui.minimap_min_seats": 300,
    "ui.theme": {
        "primary": "#0E7C86",
        "primary_deep": "#0B3C5D",
        "accent": "#57C5B6",
        "surface": "#FFFFFF",
        "surface_alt": "#F2F9FB",
    },
    # --- seating (R55, R56) ---
    "seat.max_per_booking": 10,
    "seat.best_available_enabled": True,
    "seat.avoid_single_gaps": True,
    "seat.interaction_mode": "POPOVER",
    "seat.realtime_update_target_seconds": 5,
    # --- operations ---
    "shift.variance_tolerance_minor": 2000,
    "reporting.timezone_source": "VENUE",
    "cart.single_venue_only": True,
}


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """A resolved configuration value plus its provenance."""

    key: str
    value: Any
    scope_type: str
    scope_id: str | None
    version: int

    @property
    def is_platform_default(self) -> bool:
        return self.scope_type == "PLATFORM"


class ConfigStore:
    """Read/write access to tenant-scoped configuration."""

    def __init__(self, db: Database, clock: Clock, audit: Any = None) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit

    # --------------------------------- writes -------------------------------- #

    def set(
        self,
        ctx: RequestContext,
        key: str,
        value: Any,
        *,
        scope_type: str = "TENANT",
        scope_id: str | None = None,
        audit_action: str = "CONFIG_CHANGE",
    ) -> ResolvedConfig:
        """Set a configuration value at a scope, versioning the previous one."""
        if scope_type not in _SCOPE_RANK:
            raise ValueError(f"unknown config scope: {scope_type}")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            current = self.db.query_one(
                "SELECT * FROM config_values "
                "WHERE tenant_id = ? AND key = ? AND scope_type = ? "
                "AND IFNULL(scope_id, '') = IFNULL(?, '') AND superseded_at IS NULL",
                (ctx.tenant_id, key, scope_type, scope_id),
            )
            version = 1
            previous_value = None
            if current is not None:
                version = int(current["version"]) + 1
                previous_value = decode(current["value_json"])
                self.db.update("config_values", current["id"], {"superseded_at": now})
            row_id = new_id("cfg")
            self.db.insert(
                "config_values",
                {
                    "id": row_id,
                    "tenant_id": ctx.tenant_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "key": key,
                    "value_json": encode({"v": value}),
                    "version": version,
                    "actor_id": ctx.principal.id,
                    "created_at": now,
                },
            )
            if self.audit is not None:
                self.audit.record(
                    ctx,
                    audit_action,
                    target_type="config",
                    target_id=f"{scope_type}:{scope_id or '-'}:{key}",
                    previous={"value": previous_value} if current is not None else None,
                    new={"value": value, "version": version},
                )
        return ResolvedConfig(key=key, value=value, scope_type=scope_type, scope_id=scope_id, version=version)

    def set_many(
        self,
        ctx: RequestContext,
        values: dict[str, Any],
        *,
        scope_type: str = "TENANT",
        scope_id: str | None = None,
    ) -> int:
        for key, value in values.items():
            self.set(ctx, key, value, scope_type=scope_type, scope_id=scope_id)
        return len(values)

    # --------------------------------- reads --------------------------------- #

    def resolve(
        self,
        ctx: RequestContext,
        key: str,
        *,
        session_id: str | None = None,
        product_id: str | None = None,
        experience_id: str | None = None,
        venue_id: str | None = None,
        organization_id: str | None = None,
        default: Any = None,
        use_platform_default: bool = True,
    ) -> ResolvedConfig:
        """Resolve ``key`` by nearest scope (R1.7)."""
        candidates = {
            "SESSION": session_id,
            "PRODUCT": product_id,
            "EXPERIENCE": experience_id,
            "VENUE": venue_id if venue_id is not None else ctx.venue_id,
            "ORGANIZATION": organization_id if organization_id is not None else ctx.organization_id,
            "TENANT": None,
        }
        rows = self.db.query(
            "SELECT scope_type, scope_id, value_json, version FROM config_values "
            "WHERE tenant_id = ? AND key = ? AND superseded_at IS NULL",
            (ctx.tenant_id, key),
        )
        best: tuple[int, Any] | None = None
        for row in rows:
            scope_type = row["scope_type"]
            rank = _SCOPE_RANK.get(scope_type)
            if rank is None:
                continue
            expected = candidates.get(scope_type, "__missing__")
            if scope_type == "TENANT":
                if row["scope_id"] not in (None, ""):
                    continue
            elif expected is None or expected == "__missing__" or row["scope_id"] != expected:
                continue
            if best is None or rank < best[0]:
                best = (rank, row)
        if best is not None:
            row = best[1]
            payload = decode(row["value_json"], {}) or {}
            return ResolvedConfig(
                key=key,
                value=payload.get("v"),
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                version=int(row["version"]),
            )
        if default is not None:
            return ResolvedConfig(key=key, value=default, scope_type="PLATFORM", scope_id=None, version=0)
        if use_platform_default and key in PLATFORM_DEFAULTS:
            return ResolvedConfig(
                key=key, value=PLATFORM_DEFAULTS[key], scope_type="PLATFORM", scope_id=None, version=0
            )
        return ResolvedConfig(key=key, value=default, scope_type="PLATFORM", scope_id=None, version=0)

    def get(self, ctx: RequestContext, key: str, **kwargs: Any) -> Any:
        """Convenience accessor returning just the value."""
        return self.resolve(ctx, key, **kwargs).value

    def get_int(self, ctx: RequestContext, key: str, **kwargs: Any) -> int:
        value = self.get(ctx, key, **kwargs)
        return int(value) if value is not None else 0

    def get_bool(self, ctx: RequestContext, key: str, **kwargs: Any) -> bool:
        value = self.get(ctx, key, **kwargs)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def history(self, ctx: RequestContext, key: str) -> list[dict[str, Any]]:
        """Full version history for one key across all scopes (R1.8)."""
        rows = self.db.query(
            "SELECT scope_type, scope_id, value_json, version, actor_id, created_at, superseded_at "
            "FROM config_values WHERE tenant_id = ? AND key = ? "
            "ORDER BY created_at DESC, version DESC",
            (ctx.tenant_id, key),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["value"] = (decode(item.pop("value_json"), {}) or {}).get("v")
            out.append(item)
        return out


__all__ = ["PLATFORM_DEFAULTS", "SCOPE_ORDER", "ConfigStore", "ResolvedConfig"]
