"""Minimal, audited admin writes for the seating configuration tables.

The full seating domain (the layout designer, seat maps, reservation flow — R47–R62)
is not implemented as a service yet. But the *configuration* tables (seat types,
seat zones, seat layouts) exist, and the Settings spec requires Add/Edit/Delete on
those pages. Rather than block those pages, this module provides small, safe writers
that:

* enforce the page's ADD/EDIT/DELETE permission (``authz.require_page``) exactly like
  every other settings write, so authorization is real and not a hidden button;
* write an audit entry for every change (R54);
* honour the "never physically delete something with history" invariant — a seat type
  in use, or a layout with sold/held seats, is deactivated/archived, not removed.

These are deliberately thin. When the seating service lands it should absorb this
logic; the record-CRUD registry will then point at the service instead and nothing
else changes.
"""

from __future__ import annotations

from typing import Any

from ..core.context import RequestContext
from ..core.db import decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.ids import new_id
from ..core.clock import to_iso


def _now(platform: Any) -> str:
    return to_iso(platform.clock.now())


def _require(platform: Any, ctx: RequestContext, page: str, verb: str) -> None:
    platform.authz.require_page(ctx, page, verb)


def _name_map(payload: dict[str, Any], key: str = "name") -> dict[str, str]:
    value = payload.get(key)
    if isinstance(value, dict):
        return {k: str(v) for k, v in value.items() if str(v).strip()}
    text = str(value or "").strip()
    return {"en": text} if text else {}


# --------------------------------------------------------------------------- #
# Seat Types (standalone table, tenant-scoped)
# --------------------------------------------------------------------------- #

def seat_type_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> dict[str, Any]:
    _require(platform, ctx, "Seat Type", "ADD")
    code = str(p.get("code") or "").strip()
    name = str(p.get("name") or "").strip()
    if not code or not name:
        raise ValidationError({"code": "Code and name are required."})
    if platform.db.query_one(
        "SELECT 1 FROM seat_types WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
    ):
        raise ConflictError(f"Seat type {code!r} already exists.")
    seat_type_id = new_id("sty")
    platform.db.insert(
        "seat_types",
        {
            "id": seat_type_id,
            "tenant_id": ctx.tenant_id,
            "code": code,
            "name": name,
            "display_name_json": _name_map(p, "display_name") or {"en": name},
            "colour": str(p.get("colour") or "#1F7A8C"),
            "shape": str(p.get("shape") or "ROUNDED_SQUARE"),
            "sellable": 1 if p.get("sellable", True) else 0,
            "accessible": 1 if p.get("accessible") else 0,
            "display_priority": int(p.get("display_priority") or 0),
            "status": "ACTIVE",
        },
    )
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_type", target_id=seat_type_id,
                          new={"code": code, "name": name})
    return _seat_type(platform, ctx, seat_type_id)


def seat_type_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> dict[str, Any]:
    _require(platform, ctx, "Seat Type", "EDIT")
    before = _seat_type(platform, ctx, record_id)
    changes: dict[str, Any] = {}
    if p.get("name"):
        changes["name"] = str(p["name"]).strip()
    if "display_name" in p and p["display_name"] not in ("", None):
        changes["display_name_json"] = _name_map(p, "display_name")
    for col in ("colour", "shape", "status"):
        if p.get(col):
            changes[col] = str(p[col])
    for boolcol in ("sellable", "accessible"):
        if boolcol in p:
            changes[boolcol] = 1 if p[boolcol] else 0
    if "display_priority" in p and p["display_priority"] not in ("", None):
        changes["display_priority"] = int(p["display_priority"])
    if not changes:
        raise ValidationError({"changes": "No editable fields were supplied."})
    platform.db.update("seat_types", record_id, changes, tenant_id=ctx.tenant_id)
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_type", target_id=record_id,
                          previous={"name": before.get("name"), "status": before.get("status")}, new=changes)
    return _seat_type(platform, ctx, record_id)


def seat_type_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> dict[str, Any]:
    _require(platform, ctx, "Seat Type", "DELETE")
    before = _seat_type(platform, ctx, record_id)
    # A seat type referenced by any seat (historical or live) is deactivated, not
    # removed, so existing layouts and sold tickets stay intact.
    in_use = platform.db.query_one(
        "SELECT 1 FROM seats WHERE tenant_id = ? AND seat_type_id = ? LIMIT 1", (ctx.tenant_id, record_id)
    )
    if in_use:
        platform.db.update("seat_types", record_id, {"status": "INACTIVE"}, tenant_id=ctx.tenant_id)
        platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_type", target_id=record_id,
                              previous={"status": before.get("status")}, new={"status": "INACTIVE", "performed": "DEACTIVATE"},
                              reason=reason)
        return {"requested": "DELETE", "performed": "DEACTIVATE", "status": "INACTIVE",
                "reason": "This seat type is used by existing seats and was deactivated."}
    platform.db.execute("DELETE FROM seat_types WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, record_id))
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_type", target_id=record_id,
                          previous={"code": before.get("code")}, new={"performed": "DELETE"}, reason=reason)
    return {"requested": "DELETE", "performed": "DELETE", "status": "DELETED"}


def _seat_type(platform: Any, ctx: RequestContext, seat_type_id: str) -> dict[str, Any]:
    row = platform.db.query_one(
        "SELECT id, code, name, colour, shape, sellable, accessible, status FROM seat_types "
        "WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, seat_type_id))
    if row is None:
        raise NotFound(details={"entity": "seat_type"})
    return dict(row)


# --------------------------------------------------------------------------- #
# Seat Layouts (a layout owns versioned canvases; create makes an initial DRAFT)
# --------------------------------------------------------------------------- #

def seat_layout_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> dict[str, Any]:
    _require(platform, ctx, "Seat Layout", "ADD")
    code = str(p.get("code") or "").strip()
    name = str(p.get("name") or "").strip()
    if not code or not name:
        raise ValidationError({"code": "Code and name are required."})
    if platform.db.query_one(
        "SELECT 1 FROM seat_layouts WHERE tenant_id = ? AND venue_id = ? AND code = ?",
        (ctx.tenant_id, venue_id, code)
    ):
        raise ConflictError(f"Seat layout {code!r} already exists at this venue.")
    layout_id = new_id("slo")
    version_id = new_id("slv")
    now = _now(platform)
    with platform.db.transaction():
        platform.db.insert("seat_layouts", {
            "id": layout_id, "tenant_id": ctx.tenant_id, "venue_id": venue_id,
            "code": code, "name": name, "is_template": 1 if p.get("is_template") else 0,
            "status": "ACTIVE", "created_at": now,
        })
        # Every layout starts with one editable DRAFT version — the designer fills it in.
        platform.db.insert("seat_layout_versions", {
            "id": version_id, "tenant_id": ctx.tenant_id, "layout_id": layout_id,
            "version_no": 1, "state": "DRAFT", "canvas_json": {}, "created_at": now,
            "created_by": ctx.principal.id,
        })
        platform.audit.record(ctx.for_venue(venue_id), "CONFIG_CHANGE", target_type="seat_layout",
                              target_id=layout_id, new={"code": code, "name": name})
    return _seat_layout(platform, ctx, layout_id)


def seat_layout_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> dict[str, Any]:
    # DELETE on a layout is always Archive — a layout may be referenced by a session
    # with sold or reserved seats (Fix.md §7). Never physically destroy it.
    _require(platform, ctx, "Seat Layout", "DELETE")
    before = _seat_layout(platform, ctx, record_id)
    platform.db.update("seat_layouts", record_id, {"status": "ARCHIVED", "archived_at": _now(platform)},
                       tenant_id=ctx.tenant_id)
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_layout", target_id=record_id,
                          previous={"status": before.get("status")}, new={"status": "ARCHIVED", "performed": "ARCHIVE"},
                          reason=reason)
    return {"requested": "DELETE", "performed": "ARCHIVE", "status": "ARCHIVED"}


def _seat_layout(platform: Any, ctx: RequestContext, layout_id: str) -> dict[str, Any]:
    row = platform.db.query_one(
        "SELECT id, code, name, is_template, status, created_at FROM seat_layouts "
        "WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, layout_id))
    if row is None:
        raise NotFound(details={"entity": "seat_layout"})
    return dict(row)


# --------------------------------------------------------------------------- #
# Seat Zones (belong to a layout's current version)
# --------------------------------------------------------------------------- #

def seat_zone_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> dict[str, Any]:
    _require(platform, ctx, "Seat Zone", "ADD")
    code = str(p.get("code") or "").strip()
    if not code:
        raise ValidationError({"code": "Code is required."})
    layout_id = str(p.get("layout_id") or "").strip()
    if not layout_id:
        raise ValidationError({"layout_id": "Choose the layout this zone belongs to."})
    # Resolve the layout's latest version (a zone must attach to a real version row).
    version = platform.db.query_one(
        "SELECT id FROM seat_layout_versions WHERE tenant_id = ? AND layout_id = ? "
        "ORDER BY version_no DESC LIMIT 1", (ctx.tenant_id, layout_id))
    if version is None:
        raise ValidationError({"layout_id": "That layout has no editable version."})
    if platform.db.query_one(
        "SELECT 1 FROM seat_zones WHERE tenant_id = ? AND layout_version_id = ? AND code = ?",
        (ctx.tenant_id, version["id"], code)
    ):
        raise ConflictError(f"Zone {code!r} already exists in this layout.")
    zone_id = new_id("szn")
    platform.db.insert("seat_zones", {
        "id": zone_id, "tenant_id": ctx.tenant_id, "layout_version_id": version["id"],
        "code": code, "name_json": _name_map(p) or {"en": code},
        "colour": str(p.get("colour") or "#2E8BA6"),
        "zone_kind": str(p.get("zone_kind") or "ASSIGNED"),
        "capacity": int(p["capacity"]) if p.get("capacity") not in ("", None) else None,
        "display_order": int(p.get("display_order") or 0),
    })
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_zone", target_id=zone_id,
                          new={"code": code, "layout_id": layout_id})
    return _seat_zone(platform, ctx, zone_id)


def seat_zone_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> dict[str, Any]:
    _require(platform, ctx, "Seat Zone", "EDIT")
    before = _seat_zone(platform, ctx, record_id)
    changes: dict[str, Any] = {}
    if "name" in p and p["name"] not in ("", None):
        changes["name_json"] = _name_map(p)
    for col in ("colour", "zone_kind"):
        if p.get(col):
            changes[col] = str(p[col])
    if "capacity" in p and p["capacity"] not in ("", None):
        changes["capacity"] = int(p["capacity"])
    if "display_order" in p and p["display_order"] not in ("", None):
        changes["display_order"] = int(p["display_order"])
    if not changes:
        raise ValidationError({"changes": "No editable fields were supplied."})
    platform.db.update("seat_zones", record_id, changes, tenant_id=ctx.tenant_id)
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_zone", target_id=record_id,
                          previous={"code": before.get("code")}, new=changes)
    return _seat_zone(platform, ctx, record_id)


def seat_zone_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> dict[str, Any]:
    _require(platform, ctx, "Seat Zone", "DELETE")
    before = _seat_zone(platform, ctx, record_id)
    in_use = platform.db.query_one(
        "SELECT 1 FROM seats WHERE tenant_id = ? AND zone_id = ? LIMIT 1", (ctx.tenant_id, record_id))
    if in_use:
        raise ConflictError("This zone contains seats. Remove or reassign them before deleting the zone.")
    platform.db.execute("DELETE FROM seat_zones WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, record_id))
    platform.audit.record(ctx, "CONFIG_CHANGE", target_type="seat_zone", target_id=record_id,
                          previous={"code": before.get("code")}, new={"performed": "DELETE"}, reason=reason)
    return {"requested": "DELETE", "performed": "DELETE", "status": "DELETED"}


def _seat_zone(platform: Any, ctx: RequestContext, zone_id: str) -> dict[str, Any]:
    row = platform.db.query_one(
        "SELECT id, code, name_json, colour, zone_kind, capacity FROM seat_zones "
        "WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, zone_id))
    if row is None:
        raise NotFound(details={"entity": "seat_zone"})
    out = dict(row)
    out["name"] = decode(out.pop("name_json"), {})
    return out


def seat_layout_options(platform: Any, ctx: RequestContext, venue_id: str) -> list[dict[str, str]]:
    return [
        {"value": r["id"], "label": r["name"] or r["code"]}
        for r in platform.db.query(
            "SELECT id, code, name FROM seat_layouts WHERE tenant_id = ? AND venue_id = ? "
            "AND status = 'ACTIVE' ORDER BY code", (ctx.tenant_id, venue_id))
    ]
