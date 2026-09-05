"""Tenancy: tenants, organizations, brands, venue types, venues, areas, access points, devices.

The hierarchy is Tenant → Organization → Brand (optional) → Venue → Area/Location
(R1.3), with Areas nesting to arbitrary depth (R2.2).

The venue-type template is the mechanism behind "configuration over code": a
``VenueType`` carries a JSON template of default configuration and terminology,
and creating a venue applies that template as a *starting point* whose every
value can then be overridden at venue scope (R1.5). Adding support for a water
park or a gym is therefore a matter of adding a template record.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.audit import AuditLog
from ..core.clock import Clock, timezone_for, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.i18n import as_map, text as i18n_text
from ..core.ids import hash_secret, new_id, new_secret
from .authz import AuthorizationService

#: Tables that make a venue/area/location undeletable while they reference it (R2.5).
_AREA_REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("sessions", "area_id", "session"),
    ("access_points", "area_id", "access point"),
    ("experiences", "area_id", "experience"),
    ("areas", "parent_id", "child area"),
)

_VENUE_REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("sessions", "venue_id", "session"),
    ("bookings", "venue_id", "booking"),
    ("tickets", "venue_id", "ticket"),
    ("access_points", "venue_id", "access point"),
    ("products", "venue_id", "product"),
    ("experiences", "venue_id", "experience"),
)


class TenancyService:
    """Structural configuration of who operates where."""

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
    # Tenant / organization / brand
    # ------------------------------------------------------------------ #

    def create_tenant(
        self,
        *,
        code: str,
        name: str,
        default_language: str = "en",
        languages: Iterable[str] = ("en",),
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Provision a tenant. Platform-level operation, outside any tenant scope."""
        if self.db.query_one("SELECT 1 FROM tenants WHERE code = ?", (code,)):
            raise ConflictError(f"Tenant code {code!r} is already in use.")
        tenant_id = new_id("ten")
        self.db.insert(
            "tenants",
            {
                "id": tenant_id,
                "code": code,
                "name": name,
                "status": "ACTIVE",
                "default_language": default_language,
                "languages_json": list(languages),
                "settings_json": settings or {},
                "created_at": to_iso(self.clock.now()),
            },
        )
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
        if row is None:
            raise NotFound(details={"entity": "tenant"})
        record = dict(row)
        record["languages"] = decode(record.pop("languages_json"), ["en"])
        record["settings"] = decode(record.pop("settings_json"), {})
        return record

    def create_organization(
        self,
        ctx: RequestContext,
        *,
        code: str,
        name: str,
        legal_name: str | None = None,
        tax_id: str | None = None,
        address: str | None = None,
        country: str = "TH",
    ) -> dict[str, Any]:
        organization_id = new_id("org")
        self.db.insert(
            "organizations",
            {
                "id": organization_id,
                "tenant_id": ctx.tenant_id,
                "code": code,
                "name": name,
                "legal_name": legal_name or name,
                "tax_id": tax_id,
                "address": address,
                "country": country,
                "status": "ACTIVE",
                "created_at": to_iso(self.clock.now()),
            },
        )
        return self.authz.load_scoped(ctx, "organizations", organization_id, entity="organization")

    def create_brand(self, ctx: RequestContext, *, organization_id: str, code: str, name: str) -> dict[str, Any]:
        brand_id = new_id("brd")
        self.db.insert(
            "brands",
            {
                "id": brand_id,
                "tenant_id": ctx.tenant_id,
                "organization_id": organization_id,
                "code": code,
                "name": name,
                "status": "ACTIVE",
            },
        )
        return self.authz.load_scoped(ctx, "brands", brand_id, entity="brand")

    # ------------------------------------------------------------------ #
    # Venue types (R1.5)
    # ------------------------------------------------------------------ #

    def create_venue_type(
        self,
        ctx: RequestContext | None,
        *,
        code: str,
        name: str,
        terminology: dict[str, Any] | None = None,
        template: dict[str, Any] | None = None,
        platform_level: bool = False,
    ) -> dict[str, Any]:
        """Register a venue-type archetype.

        ``platform_level=True`` creates a shared template visible to every tenant;
        otherwise the archetype belongs to one tenant. Either way it is data, so a
        new business type needs no deployment (R1.5, design principle 1).
        """
        tenant_id = None if platform_level else (ctx.tenant_id if ctx else None)
        existing = self.db.query_one(
            "SELECT id FROM venue_types WHERE IFNULL(tenant_id,'') = IFNULL(?,'') AND code = ?",
            (tenant_id, code),
        )
        if existing is not None:
            return dict(self.db.query_one("SELECT * FROM venue_types WHERE id = ?", (existing["id"],)))
        venue_type_id = new_id("vty")
        self.db.insert(
            "venue_types",
            {
                "id": venue_type_id,
                "tenant_id": tenant_id,
                "code": code,
                "name": name,
                "terminology_json": terminology or {},
                "template_json": template or {},
                "status": "ACTIVE",
            },
        )
        return dict(self.db.query_one("SELECT * FROM venue_types WHERE id = ?", (venue_type_id,)))

    def find_venue_type(self, ctx: RequestContext, code: str) -> dict[str, Any]:
        """Tenant archetype wins over the shared platform archetype of the same code."""
        row = self.db.query_one(
            "SELECT * FROM venue_types WHERE code = ? AND (tenant_id = ? OR tenant_id IS NULL) "
            "ORDER BY tenant_id IS NULL LIMIT 1",
            (code, ctx.tenant_id),
        )
        if row is None:
            raise NotFound(details={"entity": "venue_type", "code": code})
        record = dict(row)
        record["template"] = decode(record.pop("template_json"), {})
        record["terminology"] = decode(record.pop("terminology_json"), {})
        return record

    # ------------------------------------------------------------------ #
    # Venues (R2.1)
    # ------------------------------------------------------------------ #

    def create_venue(
        self,
        ctx: RequestContext,
        *,
        organization_id: str,
        venue_type_code: str,
        code: str,
        short_code: str,
        name: Any,
        timezone: str,
        currency: str,
        brand_id: str | None = None,
        tax_model: str | None = None,
        tax_rate_bp: int | None = None,
        tax_registration: str | None = None,
        rounding_mode: str | None = None,
        day_boundary_hour: int = 0,
        address: dict[str, Any] | None = None,
        contact: dict[str, Any] | None = None,
        logo_url: str | None = None,
        operating_hours: dict[str, Any] | None = None,
        customer_visible: bool = True,
        apply_template: bool = True,
    ) -> dict[str, Any]:
        """Create a venue and seed it from its venue-type template (R1.5)."""
        self.authz.require_page(ctx, "Venues", "ADD")
        try:
            timezone_for(timezone)
        except ValueError as exc:
            raise ValidationError({"timezone": "Choose a valid timezone, for example Asia/Bangkok."}) from exc
        if self.db.query_one("SELECT 1 FROM venues WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)):
            raise ConflictError(f"Venue code {code!r} already exists.")
        venue_type = self.find_venue_type(ctx, venue_type_code)
        template = venue_type["template"] if apply_template else {}
        venue_id = new_id("ven")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.insert(
                "venues",
                {
                    "id": venue_id,
                    "tenant_id": ctx.tenant_id,
                    "organization_id": organization_id,
                    "brand_id": brand_id,
                    "venue_type_id": venue_type["id"],
                    "code": code,
                    "short_code": short_code.upper()[:4],
                    "name_json": as_map(name),
                    "timezone": timezone,
                    "currency": currency.upper(),
                    "rounding_mode": rounding_mode or template.get("rounding_mode", "NONE"),
                    "tax_model": tax_model or template.get("tax_model", "INCLUSIVE"),
                    "tax_rate_bp": int(tax_rate_bp if tax_rate_bp is not None else template.get("tax_rate_bp", 0)),
                    "tax_registration": tax_registration,
                    "day_boundary_hour": int(day_boundary_hour),
                    "address_json": address or {},
                    "contact_json": contact or {},
                    "logo_url": logo_url,
                    "operating_hours_json": operating_hours or template.get("operating_hours", {}),
                    "status": "ACTIVE",
                    "customer_visible": 1 if customer_visible else 0,
                    "created_at": now,
                },
            )
            applied: dict[str, Any] = {}
            for key, value in (template.get("config") or {}).items():
                self.config.set(
                    ctx.for_venue(venue_id),
                    key,
                    value,
                    scope_type="VENUE",
                    scope_id=venue_id,
                    audit_action="CONFIG_CHANGE",
                )
                applied[key] = value
            self.audit.record(
                ctx.for_venue(venue_id),
                "CONFIG_CHANGE",
                target_type="venue",
                target_id=venue_id,
                new={
                    "code": code,
                    "venue_type": venue_type_code,
                    "timezone": timezone,
                    "currency": currency,
                    "template_keys": sorted(applied),
                },
                venue_timezone=timezone,
            )
        return self.get_venue(ctx, venue_id)

    def get_venue(self, ctx: RequestContext, venue_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "venues", venue_id, entity="venue")
        record["name"] = decode(record.pop("name_json"), {})
        record["address"] = decode(record.pop("address_json"), {})
        record["contact"] = decode(record.pop("contact_json"), {})
        record["operating_hours"] = decode(record.pop("operating_hours_json"), {})
        return record

    def venue_by_code(self, ctx: RequestContext, code: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM venues WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
        )
        if row is None:
            raise NotFound(details={"entity": "venue", "code": code})
        return self.get_venue(ctx, row["id"])

    def venue_timezone(self, ctx: RequestContext, venue_id: str) -> str:
        tz = self.db.scalar(
            "SELECT timezone FROM venues WHERE id = ? AND tenant_id = ?", (venue_id, ctx.tenant_id)
        )
        if not tz:
            raise NotFound(details={"entity": "venue"})
        return str(tz)

    def list_venues(self, ctx: RequestContext, *, customer_visible_only: bool = False) -> list[dict[str, Any]]:
        scoped = self.authz.scoped_venue_ids(ctx)
        sql = "SELECT * FROM venues WHERE tenant_id = ? AND status = 'ACTIVE'"
        params: list[Any] = [ctx.tenant_id]
        if customer_visible_only:
            sql += " AND customer_visible = 1"
        elif scoped is not None:
            if not scoped:
                return []
            sql += f" AND id IN ({', '.join('?' for _ in scoped)})"
            params.extend(scoped)
        sql += " ORDER BY code"
        out = []
        for row in self.db.query(sql, params):
            record = dict(row)
            record["name"] = decode(record.pop("name_json"), {})
            record["address"] = decode(record.pop("address_json"), {})
            record["contact"] = decode(record.pop("contact_json"), {})
            record["operating_hours"] = decode(record.pop("operating_hours_json"), {})
            out.append(record)
        return out

    def update_venue(self, ctx: RequestContext, venue_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.authz.require_page(ctx, "Venues", "EDIT", target_type="venue", target_id=venue_id)
        self.authz.require_venue(ctx, venue_id)
        self.authz.assert_no_forbidden_fields(ctx, changes)
        before = self.get_venue(ctx, venue_id)
        allowed = {
            "short_code",
            "timezone",
            "currency",
            "rounding_mode",
            "tax_model",
            "tax_rate_bp",
            "tax_registration",
            "day_boundary_hour",
            "logo_url",
            "status",
            "customer_visible",
        }
        payload: dict[str, Any] = {k: v for k, v in changes.items() if k in allowed}
        for json_field in ("name", "address", "contact", "operating_hours"):
            if json_field in changes:
                payload[f"{json_field}_json"] = (
                    as_map(changes[json_field]) if json_field == "name" else changes[json_field]
                )
        if "timezone" in payload:
            timezone_for(payload["timezone"])
        if not payload:
            raise ValidationError({"changes": "No editable fields were supplied."})
        with self.db.transaction():
            self.db.update("venues", venue_id, payload, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx.for_venue(venue_id),
                "CONFIG_CHANGE",
                target_type="venue",
                target_id=venue_id,
                previous={k: before.get(k.replace("_json", "")) for k in payload},
                new=payload,
                venue_timezone=before["timezone"],
            )
        return self.get_venue(ctx, venue_id)

    def deactivate_venue(self, ctx: RequestContext, venue_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """DELETE on a venue always means deactivate while anything references it (R2.5)."""
        self.authz.require_page(ctx, "Venues", "DELETE", target_type="venue", target_id=venue_id)
        venue = self.get_venue(ctx, venue_id)
        references = self._references(ctx, _VENUE_REFERENCES, venue_id)
        with self.db.transaction():
            self.db.update("venues", venue_id, {"status": "INACTIVE", "customer_visible": 0}, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx.for_venue(venue_id),
                "CONFIG_CHANGE",
                target_type="venue",
                target_id=venue_id,
                previous={"status": venue["status"]},
                new={"status": "INACTIVE", "performed": "DEACTIVATE"},
                reason=reason,
                venue_timezone=venue["timezone"],
            )
        return {
            "requested": "DELETE",
            "performed": "DEACTIVATE",
            "references": references,
            "venue_id": venue_id,
        }

    # ------------------------------------------------------------------ #
    # Areas & locations (R2.2 - R2.5)
    # ------------------------------------------------------------------ #

    def create_area(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        code: str,
        name: Any,
        parent_id: str | None = None,
        kind: str = "ZONE",
        description: Any = None,
        directions: Any = None,
        image_url: str | None = None,
        icon: str | None = None,
        floor: str | None = None,
        map_ref: str | None = None,
        display_order: int = 0,
    ) -> dict[str, Any]:
        """Create an area/location. Nesting is unbounded (R2.2)."""
        self.authz.require_page(ctx, "Areas", "ADD")
        self.authz.require_venue(ctx, venue_id)
        if parent_id:
            parent = self.authz.load_scoped(ctx, "areas", parent_id, entity="area")
            if parent["venue_id"] != venue_id:
                raise ValidationError({"parent_id": "The parent area belongs to a different venue."})
        if self.db.query_one(
            "SELECT 1 FROM areas WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        ):
            raise ConflictError(f"Area code {code!r} already exists at this venue.")
        area_id = new_id("are")
        self.db.insert(
            "areas",
            {
                "id": area_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "parent_id": parent_id,
                "code": code,
                "kind": kind,
                "name_json": as_map(name),
                "description_json": as_map(description),
                "directions_json": as_map(directions),
                "image_url": image_url,
                "icon": icon,
                "floor": floor,
                "map_ref": map_ref,
                "display_order": int(display_order),
                "status": "ACTIVE",
            },
        )
        return self.get_area(ctx, area_id)

    def get_area(self, ctx: RequestContext, area_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "areas", area_id, entity="area")
        record["name"] = decode(record.pop("name_json"), {})
        record["description"] = decode(record.pop("description_json"), {})
        record["directions"] = decode(record.pop("directions_json"), {})
        return record

    def area_by_code(self, ctx: RequestContext, venue_id: str, code: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM areas WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        )
        if row is None:
            raise NotFound(details={"entity": "area", "code": code})
        return self.get_area(ctx, row["id"])

    def area_presentation(self, ctx: RequestContext, area_id: str, *, language: str | None = None) -> dict[str, Any]:
        """Customer-facing location payload (R2.4, R19.2, R19.3).

        Returns the customer display name plus whichever optional cues are
        configured, and a ``view_on_map`` action only when a map reference exists —
        never a dead button.
        """
        lang = language or ctx.language
        area = self.get_area(ctx, area_id)
        return {
            "area_id": area_id,
            "display_name": i18n_text(area["name"], lang, fallback=area["code"]),
            "description": i18n_text(area["description"], lang) or None,
            "directions": i18n_text(area["directions"], lang) or None,
            "image_url": area.get("image_url"),
            "icon": area.get("icon"),
            "floor": area.get("floor"),
            "map_ref": area.get("map_ref"),
            "view_on_map": bool(area.get("map_ref")),
        }

    def area_path(self, ctx: RequestContext, area_id: str, *, language: str | None = None) -> list[dict[str, Any]]:
        """Breadcrumb from venue root down to the area."""
        lang = language or ctx.language
        chain: list[dict[str, Any]] = []
        current: str | None = area_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            area = self.get_area(ctx, current)
            chain.append(
                {
                    "area_id": area["id"],
                    "code": area["code"],
                    "display_name": i18n_text(area["name"], lang, fallback=area["code"]),
                }
            )
            current = area.get("parent_id")
        return list(reversed(chain))

    def list_areas(self, ctx: RequestContext, venue_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM areas WHERE tenant_id = ? AND venue_id = ? AND status = 'ACTIVE' "
            "ORDER BY display_order, code",
            (ctx.tenant_id, venue_id),
        )
        out = []
        for row in rows:
            record = dict(row)
            record["name"] = decode(record.pop("name_json"), {})
            record["description"] = decode(record.pop("description_json"), {})
            record["directions"] = decode(record.pop("directions_json"), {})
            out.append(record)
        return out

    def deactivate_area(self, ctx: RequestContext, area_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """R2.5 — referenced areas are deactivated, never deleted."""
        self.authz.require_page(ctx, "Areas", "DELETE", target_type="area", target_id=area_id)
        area = self.get_area(ctx, area_id)
        self.authz.require_venue(ctx, area["venue_id"])
        references = self._references(ctx, _AREA_REFERENCES, area_id)
        with self.db.transaction():
            self.db.update("areas", area_id, {"status": "INACTIVE"}, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx.for_venue(area["venue_id"]),
                "CONFIG_CHANGE",
                target_type="area",
                target_id=area_id,
                previous={"status": area["status"]},
                new={"status": "INACTIVE", "performed": "DEACTIVATE", "references": references},
                reason=reason,
            )
        return {"requested": "DELETE", "performed": "DEACTIVATE", "references": references, "area_id": area_id}

    # ------------------------------------------------------------------ #
    # Access points & devices (R2.6, R32.12, R73.12)
    # ------------------------------------------------------------------ #

    def create_access_point(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        code: str,
        name: Any,
        area_id: str | None = None,
        kind: str = "GATE",
        direction: str = "IN",
    ) -> dict[str, Any]:
        self.authz.require_page(ctx, "Access Points", "ADD")
        self.authz.require_venue(ctx, venue_id)
        if self.db.query_one(
            "SELECT 1 FROM access_points WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        ):
            raise ConflictError(f"Access point {code!r} already exists at this venue.")
        access_point_id = new_id("apt")
        self.db.insert(
            "access_points",
            {
                "id": access_point_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "area_id": area_id,
                "code": code,
                "name_json": as_map(name),
                "kind": kind,
                "direction": direction,
                "status": "ACTIVE",
            },
        )
        record = self.authz.load_scoped(ctx, "access_points", access_point_id, entity="access_point")
        record["name"] = decode(record.pop("name_json"), {})
        return record

    def list_access_points(self, ctx: RequestContext, venue_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM access_points WHERE tenant_id = ? AND venue_id = ? ORDER BY code",
            (ctx.tenant_id, venue_id),
        )
        out = []
        for row in rows:
            record = dict(row)
            record["name"] = decode(record.pop("name_json"), {})
            out.append(record)
        return out

    def register_device(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        code: str,
        name: str,
        kind: str,
        channel: str,
        access_point_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a kiosk/POS/scanner and return its one-time secret.

        Devices are individually identified and individually revocable, which is
        what makes R32.12 and R73.12 enforceable: a lost scanner is deactivated
        without touching any other device.
        """
        page = "Kiosks" if kind == "KIOSK" else "Devices"
        self.authz.require_page(ctx, page, "ADD")
        self.authz.require_venue(ctx, venue_id)
        if self.db.query_one("SELECT 1 FROM devices WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)):
            raise ConflictError(f"Device code {code!r} is already registered.")
        device_id = new_id("dev")
        secret = new_secret(32)
        self.db.insert(
            "devices",
            {
                "id": device_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "access_point_id": access_point_id,
                "code": code,
                "name": name,
                "kind": kind,
                "channel": channel,
                "secret_hash": hash_secret(secret),
                "status": "ACTIVE",
                "config_json": config or {},
                "created_at": to_iso(self.clock.now()),
            },
        )
        record = self.authz.load_scoped(ctx, "devices", device_id, entity="device")
        record.pop("secret_hash", None)
        record["config"] = decode(record.pop("config_json"), {})
        record["secret"] = secret
        return record

    def deactivate_device(
        self, ctx: RequestContext, device_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Remote deactivation. Also erases any offline cache issued to the device (R32.6 residual risk)."""
        device = self.authz.load_scoped(ctx, "devices", device_id, entity="device")
        page = "Kiosks" if device["kind"] == "KIOSK" else "Devices"
        self.authz.require_page(ctx, page, "EDIT", target_type="device", target_id=device_id)
        self.authz.require_venue(ctx, device["venue_id"])
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update("devices", device_id, {"status": "INACTIVE"}, tenant_id=ctx.tenant_id)
            self.db.execute(
                "UPDATE offline_caches SET erased_at = ?, payload_json = '{}' "
                "WHERE tenant_id = ? AND device_id = ? AND erased_at IS NULL",
                (now, ctx.tenant_id, device_id),
            )
            self.audit.record(
                ctx.for_venue(device["venue_id"]),
                "DEVICE_DEACTIVATE",
                target_type="device",
                target_id=device_id,
                previous={"status": device["status"]},
                new={"status": "INACTIVE", "offline_cache_erased": True},
                reason=reason,
                severity="WARNING",
            )
        return {"device_id": device_id, "status": "INACTIVE"}

    def record_device_health(
        self, ctx: RequestContext, device_id: str, health: dict[str, Any]
    ) -> dict[str, Any]:
        """Device heartbeat, used by the operational dashboard (R33.12, R71.1)."""
        device = self.authz.load_scoped(ctx, "devices", device_id, entity="device")
        now = to_iso(self.clock.now())
        self.db.update(
            "devices", device_id, {"last_seen_at": now, "health_json": health}, tenant_id=ctx.tenant_id
        )
        return {"device_id": device_id, "last_seen_at": now, "venue_id": device["venue_id"]}

    def authenticate_device(self, ctx: RequestContext, code: str, secret: str) -> dict[str, Any]:
        """Verify a device credential. Deactivated devices are rejected (R32.12)."""
        from ..core.ids import verify_secret

        row = self.db.query_one(
            "SELECT * FROM devices WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
        )
        if row is None or row["status"] != "ACTIVE" or not verify_secret(secret, row["secret_hash"]):
            self.audit.security(
                ctx, "AUTHORIZATION_DENIED", target_type="device", target_id=code, reason="device_auth_failed"
            )
            raise NotFound(details={"entity": "device"})
        record = dict(row)
        record.pop("secret_hash", None)
        record["config"] = decode(record.pop("config_json"), {})
        return record

    # ------------------------------------------------------------------ #

    def _references(
        self, ctx: RequestContext, sources: tuple[tuple[str, str, str], ...], value: str
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table, column, label in sources:
            count = int(
                self.db.scalar(
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ? AND {column} = ?",
                    (ctx.tenant_id, value),
                    default=0,
                )
            )
            if count:
                counts[label] = count
        return counts


__all__ = ["TenancyService"]
