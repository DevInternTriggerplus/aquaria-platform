"""Catalog: customer segments, experiences, products, ticket types.

What the venue *sells* and how each thing is *admitted* is entirely data here.
Adult/Child/Senior for Aquaria Phuket are ordinary ``customer_segments`` rows,
not enumeration members (R4.2), and a Show is an ``Experience`` with
``kind='SHOW'`` rather than a separate entity type (R18.2).

The one piece of real logic is admission-model reconciliation: a product declares
its admission model, and the platform derives the rule primitives (session
requirement, seat requirement, entry allowance, re-entry, capacity consumption)
from that model unless the product explicitly narrows them. That is what allows a
new admission model to arrive as configuration (R3.2) — the primitives are the
only thing the rest of the platform reads.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.audit import AuditLog
from ..core.clock import Clock, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.i18n import as_map, text as i18n_text, untranslated_languages
from ..core.ids import new_id
from ..domain import enums
from .authz import AuthorizationService


class CatalogService:
    """Segments, experiences, products and ticket types."""

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
    # Customer segments (R4)
    # ------------------------------------------------------------------ #

    def create_segment(
        self,
        ctx: RequestContext,
        *,
        code: str,
        name: Any,
        description: Any = None,
        qualification: dict[str, Any] | None = None,
        proof_required: bool = False,
        proof: dict[str, Any] | None = None,
        display_order: int = 0,
    ) -> dict[str, Any]:
        """Create a buyer class.

        ``qualification`` holds the rules a guest must meet — age or height range,
        residency, membership, document at gate. They are surfaced to the customer
        before purchase and to gate staff at validation (R4.4, R4.5, R3.6).
        """
        self.authz.require_page(ctx, "Customer Segments", "ADD")
        if self.db.query_one(
            "SELECT 1 FROM customer_segments WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
        ):
            raise ConflictError(f"Customer segment {code!r} already exists.")
        segment_id = new_id("seg")
        self.db.insert(
            "customer_segments",
            {
                "id": segment_id,
                "tenant_id": ctx.tenant_id,
                "code": code,
                "name_json": as_map(name),
                "description_json": as_map(description),
                "qualification_json": qualification or {},
                "proof_required": 1 if proof_required else 0,
                "proof_json": proof or {},
                "display_order": int(display_order),
                "status": "ACTIVE",
            },
        )
        self.audit.record(
            ctx, "CONFIG_CHANGE", target_type="customer_segment", target_id=segment_id, new={"code": code}
        )
        return self.get_segment(ctx, segment_id)

    def get_segment(self, ctx: RequestContext, segment_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "customer_segments", segment_id, entity="customer_segment")
        record["name"] = decode(record.pop("name_json"), {})
        record["description"] = decode(record.pop("description_json"), {})
        record["qualification"] = decode(record.pop("qualification_json"), {})
        record["proof"] = decode(record.pop("proof_json"), {})
        return record

    def segment_by_code(self, ctx: RequestContext, code: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM customer_segments WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
        )
        if row is None:
            raise NotFound(details={"entity": "customer_segment", "code": code})
        return self.get_segment(ctx, row["id"])

    def list_segments(self, ctx: RequestContext, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM customer_segments WHERE tenant_id = ?"
        params: list[Any] = [ctx.tenant_id]
        if active_only:
            sql += " AND status = 'ACTIVE'"
        sql += " ORDER BY display_order, code"
        out = []
        for row in self.db.query(sql, params):
            record = dict(row)
            record["name"] = decode(record.pop("name_json"), {})
            record["description"] = decode(record.pop("description_json"), {})
            record["qualification"] = decode(record.pop("qualification_json"), {})
            record["proof"] = decode(record.pop("proof_json"), {})
            out.append(record)
        return out

    def reorder_segments(self, ctx: RequestContext, ordered_ids: Iterable[str]) -> int:
        """Display order is configuration too (R4.1)."""
        self.authz.require_page(ctx, "Customer Segments", "EDIT")
        count = 0
        with self.db.transaction():
            for index, segment_id in enumerate(ordered_ids):
                self.authz.load_scoped(ctx, "customer_segments", segment_id, entity="customer_segment")
                self.db.update(
                    "customer_segments", segment_id, {"display_order": index * 10}, tenant_id=ctx.tenant_id
                )
                count += 1
        return count

    def set_segment_status(self, ctx: RequestContext, segment_id: str, status: str) -> dict[str, Any]:
        self.authz.require_page(ctx, "Customer Segments", "EDIT", target_type="customer_segment", target_id=segment_id)
        before = self.get_segment(ctx, segment_id)
        self.db.update("customer_segments", segment_id, {"status": status}, tenant_id=ctx.tenant_id)
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="customer_segment",
            target_id=segment_id,
            previous={"status": before["status"]},
            new={"status": status},
        )
        return self.get_segment(ctx, segment_id)

    # ------------------------------------------------------------------ #
    # Experiences (R3, R18)
    # ------------------------------------------------------------------ #

    def create_experience(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        code: str,
        name: Any,
        kind: str = "EXPERIENCE",
        short_name: Any = None,
        description: Any = None,
        instructions: Any = None,
        cancellation_message: Any = None,
        area_id: str | None = None,
        meeting_point_area_id: str | None = None,
        category: str | None = None,
        audience: str | None = None,
        languages: Iterable[str] = (),
        cover_image_url: str | None = None,
        images: Iterable[str] = (),
        icon: str | None = None,
        default_duration_minutes: int | None = None,
        reservation_mode: str = "NONE",
        eligibility: dict[str, Any] | None = None,
        display_priority: int = 0,
        customer_visible: bool = True,
    ) -> dict[str, Any]:
        """Create an experience. ``kind='SHOW'`` makes it a Show (R18.2, R18.3)."""
        page = "Shows" if kind == "SHOW" else "Experiences"
        self.authz.require_page(ctx, page, "ADD")
        self.authz.require_venue(ctx, venue_id)
        if reservation_mode not in enums.RESERVATION_MODES:
            raise ValidationError({"reservation_mode": "Choose NONE, OPTIONAL or REQUIRED."})
        if self.db.query_one(
            "SELECT 1 FROM experiences WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        ):
            raise ConflictError(f"Experience code {code!r} already exists at this venue.")
        experience_id = new_id("exp")
        self.db.insert(
            "experiences",
            {
                "id": experience_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "area_id": area_id,
                "code": code,
                "kind": kind,
                "name_json": as_map(name),
                "short_name_json": as_map(short_name),
                "description_json": as_map(description),
                "instructions_json": as_map(instructions),
                "cancellation_message_json": as_map(cancellation_message),
                "category": category,
                "audience": audience,
                "languages_json": list(languages),
                "cover_image_url": cover_image_url,
                "images_json": list(images),
                "icon": icon,
                "default_duration_minutes": default_duration_minutes,
                "meeting_point_area_id": meeting_point_area_id,
                "reservation_mode": reservation_mode,
                "eligibility_json": eligibility or {},
                "display_priority": int(display_priority),
                "customer_visible": 1 if customer_visible else 0,
                "status": "ACTIVE",
                "created_at": to_iso(self.clock.now()),
            },
        )
        self.audit.record(
            ctx.for_venue(venue_id),
            "CONFIG_CHANGE",
            target_type="experience",
            target_id=experience_id,
            new={"code": code, "kind": kind, "reservation_mode": reservation_mode},
        )
        return self.get_experience(ctx, experience_id)

    def get_experience(self, ctx: RequestContext, experience_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "experiences", experience_id, entity="experience")
        for field in (
            "name",
            "short_name",
            "description",
            "instructions",
            "cancellation_message",
        ):
            record[field] = decode(record.pop(f"{field}_json"), {})
        record["languages"] = decode(record.pop("languages_json"), [])
        record["images"] = decode(record.pop("images_json"), [])
        record["eligibility"] = decode(record.pop("eligibility_json"), {})
        return record

    def experience_by_code(self, ctx: RequestContext, venue_id: str, code: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM experiences WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        )
        if row is None:
            raise NotFound(details={"entity": "experience", "code": code})
        return self.get_experience(ctx, row["id"])

    def list_experiences(
        self,
        ctx: RequestContext,
        venue_id: str,
        *,
        kind: str | None = None,
        category: str | None = None,
        customer_visible_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM experiences WHERE tenant_id = ? AND venue_id = ? AND status = 'ACTIVE'"
        params: list[Any] = [ctx.tenant_id, venue_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if customer_visible_only:
            sql += " AND customer_visible = 1"
        sql += " ORDER BY display_priority DESC, code"
        return [self.get_experience(ctx, row["id"]) for row in self.db.query(sql, params)]

    def translation_gaps(self, ctx: RequestContext, experience_id: str) -> dict[str, list[str]]:
        """Which translatable fields are missing which languages (R69.5)."""
        experience = self.get_experience(ctx, experience_id)
        languages = tuple(self._tenant_languages(ctx))
        gaps: dict[str, list[str]] = {}
        for field in ("name", "description", "instructions", "cancellation_message"):
            missing = untranslated_languages(experience.get(field), languages)
            if missing:
                gaps[field] = missing
        return gaps

    def _tenant_languages(self, ctx: RequestContext) -> list[str]:
        raw = self.db.scalar("SELECT languages_json FROM tenants WHERE id = ?", (ctx.tenant_id,))
        languages = decode(raw, ["en"]) or ["en"]
        return [str(lang) for lang in languages]

    def deactivate_experience(
        self, ctx: RequestContext, experience_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """R18.6 — a Show with any historical or future session is deactivated, not deleted."""
        experience = self.get_experience(ctx, experience_id)
        page = "Shows" if experience["kind"] == "SHOW" else "Experiences"
        self.authz.require_page(ctx, page, "DELETE", target_type="experience", target_id=experience_id)
        session_count = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM sessions WHERE tenant_id = ? AND experience_id = ?",
                (ctx.tenant_id, experience_id),
                default=0,
            )
        )
        with self.db.transaction():
            self.db.update(
                "experiences",
                experience_id,
                {"status": "INACTIVE", "customer_visible": 0},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(experience["venue_id"]),
                "CONFIG_CHANGE",
                target_type="experience",
                target_id=experience_id,
                previous={"status": experience["status"]},
                new={"status": "INACTIVE", "performed": "DEACTIVATE", "session_count": session_count},
                reason=reason,
            )
        return {
            "requested": "DELETE",
            "performed": "DEACTIVATE",
            "experience_id": experience_id,
            "session_count": session_count,
        }

    # ------------------------------------------------------------------ #
    # Products (R3.3 - R3.4)
    # ------------------------------------------------------------------ #

    def create_product(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        code: str,
        name: Any,
        admission_model: str,
        experience_id: str | None = None,
        description: Any = None,
        session_requirement: str | None = None,
        seat_requirement: str | None = None,
        seat_flow_model: str = "FLOW_A",
        capacity_controlled: bool | None = None,
        min_per_booking: int = 1,
        max_per_booking: int | None = None,
        channels: Iterable[str] = (),
        available_from: str | None = None,
        available_until: str | None = None,
        display_order: int = 0,
        customer_visible: bool = True,
    ) -> dict[str, Any]:
        """Create a product.

        Session and seat requirements default to the admission model's primitives
        but may be set explicitly per product, independently of any other product
        (R3.3).
        """
        self.authz.require_page(ctx, "Products", "ADD")
        self.authz.require_venue(ctx, venue_id)
        model = enums.admission_model(admission_model)
        session_req = session_requirement or model.session_requirement
        seat_req = seat_requirement or model.seat_requirement
        if session_req not in enums.SESSION_REQUIREMENTS:
            raise ValidationError({"session_requirement": "Choose NOT_USED, OPTIONAL or REQUIRED."})
        if seat_req not in enums.SEAT_REQUIREMENTS:
            raise ValidationError({"seat_requirement": "Choose NOT_USED, OPTIONAL or REQUIRED."})
        if seat_req != "NOT_USED" and session_req == "NOT_USED":
            raise ValidationError(
                {"session_requirement": "Seat selection needs a session to hold the seat inventory."},
                message="A seated product must use sessions.",
            )
        if seat_flow_model not in enums.SEAT_FLOW_MODELS:
            raise ValidationError({"seat_flow_model": "Choose FLOW_A, FLOW_B or FLOW_C."})
        if self.db.query_one(
            "SELECT 1 FROM products WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        ):
            raise ConflictError(f"Product code {code!r} already exists at this venue.")
        product_id = new_id("prd")
        controlled = (
            model.consumes_capacity if capacity_controlled is None else bool(capacity_controlled)
        )
        self.db.insert(
            "products",
            {
                "id": product_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "experience_id": experience_id,
                "code": code,
                "name_json": as_map(name),
                "description_json": as_map(description),
                "admission_model": admission_model,
                "session_requirement": session_req,
                "seat_requirement": seat_req,
                "seat_flow_model": seat_flow_model,
                "capacity_controlled": 1 if controlled else 0,
                "min_per_booking": int(min_per_booking),
                "max_per_booking": max_per_booking,
                "channels_json": list(channels) or list(enums.CHANNELS),
                "available_from": available_from,
                "available_until": available_until,
                "display_order": int(display_order),
                "customer_visible": 1 if customer_visible else 0,
                "status": "ACTIVE",
                "created_at": to_iso(self.clock.now()),
            },
        )
        self.audit.record(
            ctx.for_venue(venue_id),
            "CONFIG_CHANGE",
            target_type="product",
            target_id=product_id,
            new={
                "code": code,
                "admission_model": admission_model,
                "session_requirement": session_req,
                "seat_requirement": seat_req,
            },
        )
        return self.get_product(ctx, product_id)

    def get_product(self, ctx: RequestContext, product_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "products", product_id, entity="product")
        record["name"] = decode(record.pop("name_json"), {})
        record["description"] = decode(record.pop("description_json"), {})
        record["channels"] = decode(record.pop("channels_json"), [])
        record["admission"] = enums.admission_model(record["admission_model"]).as_dict()
        return record

    def product_by_code(self, ctx: RequestContext, venue_id: str, code: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM products WHERE tenant_id = ? AND venue_id = ? AND code = ?",
            (ctx.tenant_id, venue_id, code),
        )
        if row is None:
            raise NotFound(details={"entity": "product", "code": code})
        return self.get_product(ctx, row["id"])

    def list_products(
        self,
        ctx: RequestContext,
        venue_id: str,
        *,
        channel: str | None = None,
        customer_visible_only: bool = False,
        on_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sellable products, honouring scheduled availability (R3.7)."""
        sql = "SELECT * FROM products WHERE tenant_id = ? AND venue_id = ? AND status = 'ACTIVE'"
        params: list[Any] = [ctx.tenant_id, venue_id]
        if customer_visible_only:
            sql += " AND customer_visible = 1"
        if on_date:
            sql += " AND (available_from IS NULL OR available_from <= ?)"
            sql += " AND (available_until IS NULL OR available_until >= ?)"
            params.extend([on_date, on_date])
        sql += " ORDER BY display_order, code"
        products = [self.get_product(ctx, row["id"]) for row in self.db.query(sql, params)]
        if channel:
            products = [p for p in products if channel in p["channels"]]
        return products

    def add_component(
        self,
        ctx: RequestContext,
        *,
        parent_product_id: str,
        child_product_id: str,
        relation: str = "ADDON",
        quantity: int = 1,
        min_quantity: int = 0,
        max_quantity: int | None = None,
        eligibility: dict[str, Any] | None = None,
        display_order: int = 0,
    ) -> dict[str, Any]:
        """Attach a bundle item or add-on to a product (R3.4)."""
        if relation not in ("BUNDLE_ITEM", "ADDON"):
            raise ValidationError({"relation": "Relation must be BUNDLE_ITEM or ADDON."})
        parent = self.get_product(ctx, parent_product_id)
        child = self.get_product(ctx, child_product_id)
        self.authz.require_page(ctx, "Products", "EDIT", target_type="product", target_id=parent_product_id)
        if parent["venue_id"] != child["venue_id"]:
            raise ValidationError(
                {"child_product_id": "Bundle items must belong to the same venue as the parent."}
            )
        if parent_product_id == child_product_id:
            raise ValidationError({"child_product_id": "A product cannot contain itself."})
        component_id = new_id("pcm")
        self.db.insert(
            "product_components",
            {
                "id": component_id,
                "tenant_id": ctx.tenant_id,
                "parent_product_id": parent_product_id,
                "child_product_id": child_product_id,
                "relation": relation,
                "quantity": int(quantity),
                "min_quantity": int(min_quantity),
                "max_quantity": max_quantity,
                "eligibility_json": eligibility or {},
                "display_order": int(display_order),
            },
        )
        return {"component_id": component_id, "relation": relation}

    def components(
        self, ctx: RequestContext, product_id: str, *, relation: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM product_components WHERE tenant_id = ? AND parent_product_id = ?"
        params: list[Any] = [ctx.tenant_id, product_id]
        if relation:
            sql += " AND relation = ?"
            params.append(relation)
        sql += " ORDER BY display_order"
        out = []
        for row in self.db.query(sql, params):
            item = dict(row)
            item["eligibility"] = decode(item.pop("eligibility_json"), {})
            out.append(item)
        return out

    def deactivate_product(
        self,
        ctx: RequestContext,
        product_id: str,
        *,
        reason: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Deactivate a product, warning about future confirmed bookings (R3.8).

        Existing bookings keep their validity: deactivation stops new sales only.
        """
        self.authz.require_page(ctx, "Products", "DELETE", target_type="product", target_id=product_id)
        product = self.get_product(ctx, product_id)
        today = to_iso(self.clock.now())[:10]
        affected = int(
            self.db.scalar(
                """
                SELECT COUNT(DISTINCT b.id) FROM bookings b
                JOIN booking_items bi ON bi.booking_id = b.id AND bi.tenant_id = b.tenant_id
                WHERE b.tenant_id = ? AND bi.product_id = ? AND b.status = 'CONFIRMED'
                  AND (b.visit_date IS NULL OR b.visit_date >= ?)
                """,
                (ctx.tenant_id, product_id, today),
                default=0,
            )
        )
        if affected and not confirmed:
            from ..core.errors import ConfirmationRequired

            raise ConfirmationRequired(
                f"{affected} future confirmed booking(s) use this product. "
                "They remain valid, but the product will stop being sold.",
                details={"affected_bookings": affected, "product_code": product["code"]},
            )
        with self.db.transaction():
            self.db.update(
                "products",
                product_id,
                {"status": "INACTIVE", "customer_visible": 0},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(product["venue_id"]),
                "PRODUCT_DEACTIVATE",
                target_type="product",
                target_id=product_id,
                previous={"status": product["status"]},
                new={"status": "INACTIVE", "affected_future_bookings": affected},
                reason=reason,
                severity="WARNING" if affected else "INFO",
            )
        return {
            "requested": "DELETE",
            "performed": "DEACTIVATE",
            "product_id": product_id,
            "affected_future_bookings": affected,
        }

    # ------------------------------------------------------------------ #
    # Ticket types (R3.5, R3.6)
    # ------------------------------------------------------------------ #

    def create_ticket_type(
        self,
        ctx: RequestContext,
        *,
        product_id: str,
        segment_code: str,
        code: str,
        name: Any,
        admission_model: str | None = None,
        tax_treatment: str = "STANDARD",
        validity: dict[str, Any] | None = None,
        entry_allowance: int | None = None,
        reentry_window_minutes: int | None = None,
        min_quantity: int = 0,
        max_quantity: int | None = None,
        channels: Iterable[str] = (),
        eligibility: dict[str, Any] | None = None,
        seat_eligibility: dict[str, Any] | None = None,
        transferable: bool | None = None,
        consumes_capacity: bool | None = None,
        is_complimentary: bool = False,
        display_order: int = 0,
    ) -> dict[str, Any]:
        """Create a priced variant of a product for one customer segment (R3.5)."""
        product = self.get_product(ctx, product_id)
        self.authz.require_page(ctx, "Ticket Types", "ADD")
        self.authz.require_venue(ctx, product["venue_id"])
        segment = self.segment_by_code(ctx, segment_code)
        model_code = admission_model or product["admission_model"]
        model = enums.admission_model(model_code)
        if self.db.query_one(
            "SELECT 1 FROM ticket_types WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
        ):
            raise ConflictError(f"Ticket type code {code!r} already exists.")
        ticket_type_id = new_id("tkt")
        allowance = model.entry_allowance if entry_allowance is None else entry_allowance
        # ``None`` allowance means unlimited within validity; store as a large
        # sentinel-free representation by using -1 so the column stays NOT NULL.
        stored_allowance = -1 if allowance is None else int(allowance)
        self.db.insert(
            "ticket_types",
            {
                "id": ticket_type_id,
                "tenant_id": ctx.tenant_id,
                "product_id": product_id,
                "segment_id": segment["id"],
                "code": code,
                "name_json": as_map(name),
                "admission_model": model_code,
                "tax_treatment": tax_treatment,
                "validity_json": validity or {},
                "entry_allowance": stored_allowance,
                "reentry_window_minutes": reentry_window_minutes,
                "min_quantity": int(min_quantity),
                "max_quantity": max_quantity,
                "channels_json": list(channels) or product["channels"],
                "eligibility_json": eligibility or segment.get("qualification") or {},
                "seat_eligibility_json": seat_eligibility or {},
                "transferable": 1 if (model.transferable if transferable is None else transferable) else 0,
                "consumes_capacity": 1
                if (model.consumes_capacity if consumes_capacity is None else consumes_capacity)
                else 0,
                "is_complimentary": 1 if is_complimentary else 0,
                "display_order": int(display_order),
                "status": "ACTIVE",
                "created_at": to_iso(self.clock.now()),
            },
        )
        self.audit.record(
            ctx.for_venue(product["venue_id"]),
            "CONFIG_CHANGE",
            target_type="ticket_type",
            target_id=ticket_type_id,
            new={"code": code, "segment": segment_code, "admission_model": model_code},
        )
        return self.get_ticket_type(ctx, ticket_type_id)

    def get_ticket_type(self, ctx: RequestContext, ticket_type_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "ticket_types", ticket_type_id, entity="ticket_type")
        record["name"] = decode(record.pop("name_json"), {})
        record["validity"] = decode(record.pop("validity_json"), {})
        record["channels"] = decode(record.pop("channels_json"), [])
        record["eligibility"] = decode(record.pop("eligibility_json"), {})
        record["seat_eligibility"] = decode(record.pop("seat_eligibility_json"), {})
        allowance = int(record["entry_allowance"])
        record["entry_allowance"] = None if allowance < 0 else allowance
        record["unlimited_entries"] = allowance < 0
        record["admission"] = enums.admission_model(record["admission_model"]).as_dict()
        return record

    def ticket_type_by_code(self, ctx: RequestContext, code: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM ticket_types WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
        )
        if row is None:
            raise NotFound(details={"entity": "ticket_type", "code": code})
        return self.get_ticket_type(ctx, row["id"])

    def list_ticket_types(
        self,
        ctx: RequestContext,
        product_id: str,
        *,
        channel: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id FROM ticket_types WHERE tenant_id = ? AND product_id = ?"
        params: list[Any] = [ctx.tenant_id, product_id]
        if active_only:
            sql += " AND status = 'ACTIVE'"
        sql += " ORDER BY display_order, code"
        types = [self.get_ticket_type(ctx, row["id"]) for row in self.db.query(sql, params)]
        if channel:
            types = [t for t in types if channel in t["channels"]]
        return types

    def set_ticket_type_status(self, ctx: RequestContext, ticket_type_id: str, status: str) -> dict[str, Any]:
        self.authz.require_page(ctx, "Ticket Types", "EDIT", target_type="ticket_type", target_id=ticket_type_id)
        before = self.get_ticket_type(ctx, ticket_type_id)
        self.db.update("ticket_types", ticket_type_id, {"status": status}, tenant_id=ctx.tenant_id)
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="ticket_type",
            target_id=ticket_type_id,
            previous={"status": before["status"]},
            new={"status": status},
        )
        return self.get_ticket_type(ctx, ticket_type_id)

    # ------------------------------------------------------------------ #
    # Customer-facing projections
    # ------------------------------------------------------------------ #

    def eligibility_notice(
        self, ctx: RequestContext, ticket_type_id: str, *, language: str | None = None
    ) -> dict[str, Any]:
        """Conditions a guest must meet, shown before purchase and at the gate (R3.6, R4.4)."""
        lang = language or ctx.language
        ticket_type = self.get_ticket_type(ctx, ticket_type_id)
        segment = self.get_segment(ctx, ticket_type["segment_id"])
        eligibility = dict(ticket_type["eligibility"])
        conditions: list[str] = []
        if "age_min" in eligibility or "age_max" in eligibility:
            low = eligibility.get("age_min")
            high = eligibility.get("age_max")
            if low is not None and high is not None:
                conditions.append(f"Ages {low}–{high}")
            elif low is not None:
                conditions.append(f"Ages {low} and over")
            elif high is not None:
                conditions.append(f"Ages up to {high}")
        if "height_min_cm" in eligibility or "height_max_cm" in eligibility:
            low = eligibility.get("height_min_cm")
            high = eligibility.get("height_max_cm")
            if low is not None and high is not None:
                conditions.append(f"Height {low}–{high} cm")
            elif low is not None:
                conditions.append(f"Height {low} cm and above")
            elif high is not None:
                conditions.append(f"Height up to {high} cm")
        if eligibility.get("residency"):
            conditions.append(f"Proof of {eligibility['residency']} residency required")
        if eligibility.get("membership_required"):
            conditions.append("Valid membership required")
        for document in eligibility.get("documents", []) or []:
            conditions.append(f"Bring: {document}")
        proof_required = bool(segment["proof_required"]) or bool(eligibility.get("documents"))
        return {
            "ticket_type_id": ticket_type_id,
            "ticket_type_name": i18n_text(ticket_type["name"], lang, fallback=ticket_type["code"]),
            "segment_code": segment["code"],
            "segment_name": i18n_text(segment["name"], lang, fallback=segment["code"]),
            "conditions": conditions,
            "proof_required_at_entry": proof_required,
            "proof": segment.get("proof") or {},
        }


__all__ = ["CatalogService"]
