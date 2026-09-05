"""Generic create / edit / delete for the record-collection settings pages.

The read side of these pages (``GET /api/staff/settings/records``) is uniform and
lives in ``server.py``. This module supplies the *write* side in the same declarative
spirit: one registry describes, per settings page, the editable fields and the three
callables that create, edit and delete a record.

The one rule that matters: **every callable dispatches to the owning service method**
(catalog, promotions, staff, payment types, …). It never writes a table directly.
That is deliberate — those service methods already enforce the page's ADD/EDIT/DELETE
permission *and* write the audit entry (R54, settingsAndReports §47, §54). Routing the
generic endpoints through them means a new settings mutation is audited for free and
can never bypass authorization, which a direct ``db.insert`` here would.

DELETE follows the platform invariant: for records with history it deactivates or
archives rather than physically removing (§17, §51). Each service already makes that
choice, so the callable simply asks the service to delete and returns what it did.

A page absent from :data:`RECORD_CRUD` is read-only by design (Audit Logs, Permissions,
the registry view) or not yet wired; the endpoints answer 404 for it, exactly as an
unknown page would, so nothing half-works.

Each field descriptor is ``{name, label, type, required, ...}`` where ``type`` is one
of: ``text``, ``textarea``, ``number``, ``bool``, ``select`` (with ``options``),
``i18n`` (a language->text map the client renders as a small multi-language field).
The client renders a form from these descriptors, so adding a field is a one-line
change here rather than bespoke HTML per page.
"""

from __future__ import annotations

from typing import Any, Callable

from ..core.context import RequestContext
from . import seating_admin

# A create/edit callable takes (platform, ctx, venue_id, payload) and returns the
# saved record (or a result dict). A delete callable takes (platform, ctx, record_id,
# reason).
Writer = Callable[[Any, RequestContext, str, dict[str, Any]], Any]
Deleter = Callable[[Any, RequestContext, str, "str | None"], Any]


class RecordPage:
    """Declarative CRUD description for one settings record page."""

    __slots__ = (
        "page", "id_field", "fields", "create", "update", "delete",
        "delete_label", "sensitive", "full_edit",
    )

    def __init__(
        self,
        page: str,
        *,
        fields: list[dict[str, Any]],
        create: Writer | None = None,
        update: Writer | None = None,
        delete: Deleter | None = None,
        id_field: str = "id",
        delete_label: str = "Deactivate",
        sensitive: bool = False,
        full_edit: bool = False,
    ) -> None:
        self.page = page
        self.fields = fields
        self.create = create
        self.update = update
        self.delete = delete
        self.id_field = id_field
        self.delete_label = delete_label
        self.sensitive = sensitive
        # When True the edit form shows the full field set; otherwise the owning
        # service only supports a status change, so the client shows a status control.
        self.full_edit = full_edit

    def descriptor(self) -> dict[str, Any]:
        """What the client needs to draw the Add/Edit form and the row controls."""
        return {
            "page": self.page,
            "fields": self.fields,
            "can_create": self.create is not None,
            "can_update": self.update is not None,
            "can_delete": self.delete is not None,
            "delete_label": self.delete_label,
            "sensitive": self.sensitive,
            "full_edit": self.full_edit,
        }


def _str(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return "" if value is None else str(value).strip()


def _int(payload: dict[str, Any], key: str, default: int = 0) -> int:
    value = payload.get(key, default)
    if value in ("", None):
        return default
    return int(value)


def _bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _name_map(payload: dict[str, Any], key: str = "name") -> dict[str, str]:
    """Accept either a plain string or a language map for an i18n field."""
    value = payload.get(key)
    if isinstance(value, dict):
        return {k: str(v) for k, v in value.items() if str(v).strip()}
    text = _str(payload, key)
    return {"en": text} if text else {}


# --------------------------------------------------------------------------- #
# Per-page writers. Each calls the owning service (which audits + authorizes).
# --------------------------------------------------------------------------- #

# ---- Customer Segments ---------------------------------------------------- #

def _segment_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.catalog.create_segment(
        ctx,
        code=_str(p, "code"),
        name=_name_map(p),
        proof_required=_bool(p, "proof_required"),
        display_order=_int(p, "display_order"),
    )


def _segment_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    # No full field editor exists for a segment; EDIT toggles active/inactive, which
    # is the meaningful change and is audited by set_segment_status.
    return platform.catalog.set_segment_status(ctx, record_id, _str(p, "status", "ACTIVE"))


def _segment_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.catalog.set_segment_status(ctx, record_id, "INACTIVE")


# ---- Products ------------------------------------------------------------- #

def _product_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    status = _str(p, "status", "ACTIVE")
    if status != "ACTIVE":
        return platform.catalog.deactivate_product(ctx, record_id)
    # Reactivation goes straight to the table via the service's status path is not
    # exposed; catalog only offers deactivate. Treat non-deactivate edits as no-op
    # status confirmations by deactivating only when asked.
    raise _Unsupported("Products can be deactivated here; edit details in the catalog module.")


def _product_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    # confirmed=True: the operator chose Deactivate on the settings screen, which is
    # the confirmation. Existing bookings keep their validity; only new sales stop.
    return platform.catalog.deactivate_product(ctx, record_id, reason=reason, confirmed=True)


# ---- Ticket Types --------------------------------------------------------- #

def _ticket_type_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.catalog.create_ticket_type(
        ctx,
        product_id=_str(p, "product_id"),
        segment_code=_str(p, "segment_code"),
        code=_str(p, "code"),
        name=_name_map(p),
        entry_allowance=_int(p, "entry_allowance", 1),
        display_order=_int(p, "display_order"),
    )


def _ticket_type_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    return platform.catalog.set_ticket_type_status(ctx, record_id, _str(p, "status", "ACTIVE"))


def _ticket_type_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.catalog.set_ticket_type_status(ctx, record_id, "ARCHIVED")


# ---- Promotions family ---------------------------------------------------- #

def _promotion_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.promotions.create_promotion(
        ctx,
        internal_code=_str(p, "internal_code"),
        name=_name_map(p),
        mechanic=_str(p, "mechanic", "PERCENT_DISCOUNT"),
        config=p.get("config") or {"percent_bp": _int(p, "percent_bp", 1000)},
        code=_str(p, "code") or None,
        priority=_int(p, "priority"),
        usage_limit=_int(p, "usage_limit", 0) or None,
        budget_minor=_int(p, "budget_minor", 0) or None,
    )


def _promotion_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    return platform.promotions.set_status(ctx, record_id, _str(p, "status", "ACTIVE"), reason=p.get("reason"))


def _promotion_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.promotions.delete_promotion(ctx, record_id, reason=reason)


# ---- Staff ---------------------------------------------------------------- #

def _staff_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    org_id = _str(p, "organization_id") or ctx.organization_id
    return platform.staff.invite_staff(
        ctx,
        email=_str(p, "email"),
        first_name=_str(p, "first_name"),
        last_name=_str(p, "last_name"),
        organization_id=org_id,
        phone=_str(p, "phone") or None,
        employee_id=_str(p, "employee_id") or None,
        mfa_required=_bool(p, "mfa_required"),
    )


def _staff_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    if _str(p, "status"):
        return platform.staff.set_staff_status(ctx, record_id, _str(p, "status"), reason=p.get("reason"))
    changes = {
        k: p[k]
        for k in ("first_name", "last_name", "display_name", "phone", "employee_id", "mfa_required")
        if k in p and p[k] not in ("", None)
    }
    return platform.staff.update_staff(ctx, record_id, changes)


def _staff_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.staff.delete_staff(ctx, record_id, reason=reason)


# ---- Roles ---------------------------------------------------------------- #

def _role_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.staff.create_role(
        ctx,
        code=_str(p, "code"),
        name=_str(p, "name"),
        authority_level=_int(p, "authority_level", 10),
        description=_str(p, "description") or None,
    )


def _role_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    # Full permission editing lives on the Roles matrix screen; here EDIT deactivates.
    return platform.staff.deactivate_role(ctx, record_id, reason=p.get("reason"))


def _role_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.staff.delete_role(ctx, record_id, reason=reason)


# ---- Payment Type --------------------------------------------------------- #

def _payment_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.payment_types.create(
        ctx,
        venue_id=venue_id,
        code=_str(p, "code"),
        method=_str(p, "method", "CARD"),
        display_name=_name_map(p, "display_name") or _name_map(p),
        display_order=_int(p, "display_order"),
        reason=p.get("reason"),
    )


def _payment_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    changes: dict[str, Any] = {}
    for key in ("method", "display_order", "status", "web_enabled", "kiosk_enabled", "counter_enabled"):
        if key in p and p[key] not in ("", None):
            changes[key] = p[key]
    if "display_name" in p and p["display_name"] not in ("", None):
        changes["display_name"] = _name_map(p, "display_name")
    return platform.payment_types.update(ctx, record_id, changes=changes, reason=p.get("reason"))


def _payment_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.payment_types.archive(ctx, record_id, reason=reason)


# ---- Shows (Show Master = experiences kind=SHOW) -------------------------- #

def _show_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.catalog.create_experience(
        ctx,
        venue_id=venue_id,
        code=_str(p, "code"),
        name=_name_map(p),
        kind="SHOW",
        category=_str(p, "category") or None,
        default_duration_minutes=_int(p, "default_duration_minutes", 0) or None,
        reservation_mode=_str(p, "reservation_mode", "NONE") or "NONE",
        display_priority=_int(p, "display_priority"),
        customer_visible=_bool(p, "customer_visible", True),
    )


def _show_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.catalog.deactivate_experience(ctx, record_id, reason=reason)


# ---- Show Schedule (show sessions) ---------------------------------------- #

def _show_session_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.shows.create_show_session(
        ctx,
        venue_id=venue_id,
        experience_id=_str(p, "experience_id"),
        date=_str(p, "date"),
        start_time=_str(p, "start_time"),
        duration_minutes=_int(p, "duration_minutes", 0) or None,
        capacity=_int(p, "capacity", 0) or None,
        reservation_mode=_str(p, "reservation_mode") or None,
        customer_visible=_bool(p, "customer_visible", True),
        confirm_conflicts=True,
    )


def _show_session_delete(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    # Cancelling notifies affected reservations and keeps the row (R20). A session
    # with no reservations can be removed outright by the service if it chooses.
    return platform.shows.cancel_session(ctx, record_id, reason=reason or "Cancelled from Settings")


# ---- Email Templates (versioned; edit = new version) ---------------------- #

def _email_template_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.notifications.create_template(
        ctx,
        event_type=_str(p, "event_type"),
        language=_str(p, "language", "en") or "en",
        subject=_str(p, "subject"),
        body=_str(p, "body"),
        header=_str(p, "header"),
        footer=_str(p, "footer"),
        venue_id=venue_id,
    )


# ---- Cash Coupons (promotions with a settlement accounting treatment) ------ #

def _cash_coupon_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    return platform.promotions.create_promotion(
        ctx,
        internal_code=_str(p, "internal_code"),
        name=_name_map(p),
        mechanic="FIXED_DISCOUNT",
        config={"amount_minor": _int(p, "amount_minor", 0)},
        code=_str(p, "code") or None,
        accounting_treatment=_str(p, "accounting_treatment", "STORED_VALUE") or "STORED_VALUE",
        usage_limit=_int(p, "usage_limit", 0) or None,
    )


# ---- Member Rewards (promotions, gift/reward mechanic) -------------------- #

def _member_reward_create(platform: Any, ctx: RequestContext, venue_id: str, p: dict[str, Any]) -> Any:
    reward_name = _str(p, "reward_name") or _name_map(p).get("en") or _str(p, "internal_code")
    return platform.promotions.create_promotion(
        ctx,
        internal_code=_str(p, "internal_code"),
        name=_name_map(p),
        mechanic="FREE_GIFT",
        config={"reward": {"name": reward_name, "kind": "PRODUCT"},
                "reward_quantity": _int(p, "reward_quantity", 1) or 1,
                "points_required": _int(p, "points_required", 0)},
        usage_limit=_int(p, "usage_limit", 0) or None,
    )


def _promotion_status_update(platform: Any, ctx: RequestContext, record_id: str, p: dict[str, Any]) -> Any:
    return platform.promotions.set_status(ctx, record_id, _str(p, "status", "ACTIVE"), reason=p.get("reason"))


def _promotion_delete_(platform: Any, ctx: RequestContext, record_id: str, reason: str | None) -> Any:
    return platform.promotions.delete_promotion(ctx, record_id, reason=reason)


class _Unsupported(Exception):
    """Raised by a writer when the requested variant is not offered here."""


# --------------------------------------------------------------------------- #
# The registry.
# --------------------------------------------------------------------------- #

_STATUS_FIELD = {
    "name": "status",
    "label": "Status",
    "type": "select",
    "options": [
        {"value": "ACTIVE", "label": "Active"},
        {"value": "INACTIVE", "label": "Inactive"},
    ],
    "required": False,
}


RECORD_CRUD: dict[str, RecordPage] = {
    "Customer Segments": RecordPage(
        "Customer Segments",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "proof_required", "label": "Proof required at gate", "type": "bool"},
            {"name": "display_order", "label": "Display order", "type": "number"},
        ],
        create=_segment_create,
        update=_segment_update,
        delete=_segment_delete,
    ),
    "Ticket Types": RecordPage(
        "Ticket Types",
        fields=[
            {"name": "product_id", "label": "Product", "type": "select", "options_source": "products", "required": True},
            {"name": "segment_code", "label": "Customer segment", "type": "select",
             "options_source": "segments", "required": True},
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "entry_allowance", "label": "Entries allowed", "type": "number"},
            {"name": "display_order", "label": "Display order", "type": "number"},
        ],
        create=_ticket_type_create,
        update=_ticket_type_update,
        delete=_ticket_type_delete,
        delete_label="Archive",
    ),
    "Products": RecordPage(
        "Products",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
        ],
        # Creation needs an experience + admission model that the catalog module owns;
        # here we offer deactivate/delete only, which is what "manage records" needs.
        delete=_product_delete,
        delete_label="Deactivate",
    ),
    "Promotions": RecordPage(
        "Promotions",
        fields=[
            {"name": "internal_code", "label": "Internal code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "code", "label": "Public coupon code", "type": "text"},
            {"name": "percent_bp", "label": "Percent discount (basis points, 1000 = 10%)", "type": "number"},
            {"name": "priority", "label": "Priority", "type": "number"},
            {"name": "usage_limit", "label": "Usage limit (blank = unlimited)", "type": "number"},
            {"name": "budget_minor", "label": "Budget (minor units, blank = none)", "type": "number"},
        ],
        create=_promotion_create,
        update=_promotion_update,
        delete=_promotion_delete,
        delete_label="Archive",
    ),
    "Coupon Codes": RecordPage(
        "Coupon Codes",
        fields=[
            {"name": "internal_code", "label": "Internal code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "code", "label": "Public coupon code", "type": "text", "required": True},
            {"name": "percent_bp", "label": "Percent discount (basis points)", "type": "number"},
            {"name": "usage_limit", "label": "Usage limit", "type": "number"},
        ],
        create=_promotion_create,
        update=_promotion_update,
        delete=_promotion_delete,
        delete_label="Archive",
    ),
    "Staff": RecordPage(
        "Staff",
        fields=[
            {"name": "email", "label": "Email", "type": "text", "required": True},
            {"name": "first_name", "label": "First name", "type": "text", "required": True},
            {"name": "last_name", "label": "Last name", "type": "text", "required": True},
            {"name": "phone", "label": "Phone", "type": "text"},
            {"name": "employee_id", "label": "Employee ID", "type": "text"},
            {"name": "mfa_required", "label": "Require MFA", "type": "bool"},
        ],
        create=_staff_create,
        update=_staff_update,
        delete=_staff_delete,
        delete_label="Deactivate",
        sensitive=True,
    ),
    "Roles": RecordPage(
        "Roles",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "authority_level", "label": "Authority level", "type": "number", "required": True},
            {"name": "description", "label": "Description", "type": "textarea"},
        ],
        create=_role_create,
        update=_role_update,
        delete=_role_delete,
        delete_label="Deactivate",
        sensitive=True,
    ),
    "Payment Type": RecordPage(
        "Payment Type",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "display_name", "label": "Display name", "type": "i18n", "required": True},
            {"name": "method", "label": "Method", "type": "select", "options": [
                {"value": "CARD", "label": "Card"},
                {"value": "QR_BANK_TRANSFER", "label": "QR / bank transfer"},
                {"value": "EWALLET", "label": "E-wallet"},
                {"value": "CASH", "label": "Cash"},
                {"value": "STORED_VALUE", "label": "Stored value"},
            ], "required": True},
            {"name": "display_order", "label": "Display order", "type": "number"},
        ],
        create=_payment_create,
        update=_payment_update,
        delete=_payment_delete,
        delete_label="Archive",
        sensitive=True,
        full_edit=True,
    ),
    # ---- Shows & Seating (Fix.md Gap 2) ---- #
    "Shows": RecordPage(
        "Shows",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "category", "label": "Category", "type": "text"},
            {"name": "default_duration_minutes", "label": "Duration (minutes)", "type": "number"},
            {"name": "reservation_mode", "label": "Reservation", "type": "select", "options": [
                {"value": "NONE", "label": "Not required"},
                {"value": "OPTIONAL", "label": "Optional"},
                {"value": "REQUIRED", "label": "Required"}]},
            {"name": "display_priority", "label": "Display order", "type": "number"},
            {"name": "customer_visible", "label": "Visible to customers", "type": "bool"},
        ],
        create=_show_create,
        delete=_show_delete,
        delete_label="Deactivate",
    ),
    "Show Schedule": RecordPage(
        "Show Schedule",
        fields=[
            {"name": "experience_id", "label": "Show", "type": "select",
             "options_source": "shows", "required": True},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "type": "text", "required": True},
            {"name": "start_time", "label": "Start time (HH:MM)", "type": "text", "required": True},
            {"name": "duration_minutes", "label": "Duration (minutes)", "type": "number"},
            {"name": "capacity", "label": "Capacity", "type": "number"},
            {"name": "customer_visible", "label": "Visible to customers", "type": "bool"},
        ],
        create=_show_session_create,
        delete=_show_session_delete,
        delete_label="Cancel",
    ),
    "Email Templates": RecordPage(
        "Email Templates",
        fields=[
            {"name": "event_type", "label": "Event", "type": "text", "required": True},
            {"name": "language", "label": "Language", "type": "text", "required": True},
            {"name": "subject", "label": "Subject", "type": "text", "required": True},
            {"name": "header", "label": "Header", "type": "textarea"},
            {"name": "body", "label": "Body", "type": "textarea", "required": True},
            {"name": "footer", "label": "Footer", "type": "textarea"},
        ],
        # Editing a template saves a new version (the previous one is superseded but
        # kept), so create IS the edit path; there is no destructive delete.
        create=_email_template_create,
    ),
    "Cash Coupons": RecordPage(
        "Cash Coupons",
        fields=[
            {"name": "internal_code", "label": "Internal code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "code", "label": "Coupon code", "type": "text"},
            {"name": "amount_minor", "label": "Value (minor units)", "type": "number", "required": True},
            {"name": "accounting_treatment", "label": "Accounting treatment", "type": "select", "options": [
                {"value": "STORED_VALUE", "label": "Stored value"},
                {"value": "PAYMENT", "label": "Payment instrument"},
                {"value": "LIABILITY", "label": "Liability"},
                {"value": "DISCOUNT", "label": "Discount"}], "required": True},
            {"name": "usage_limit", "label": "Usage limit", "type": "number"},
        ],
        create=_cash_coupon_create,
        update=_promotion_status_update,
        delete=_promotion_delete_,
        delete_label="Disable",
    ),
    "Member Rewards": RecordPage(
        "Member Rewards",
        fields=[
            {"name": "internal_code", "label": "Internal code", "type": "text", "required": True},
            {"name": "name", "label": "Reward name", "type": "i18n", "required": True},
            {"name": "reward_name", "label": "Reward description", "type": "text"},
            {"name": "points_required", "label": "Points required", "type": "number"},
            {"name": "reward_quantity", "label": "Reward quantity", "type": "number"},
            {"name": "usage_limit", "label": "Usage limit", "type": "number"},
        ],
        create=_member_reward_create,
        update=_promotion_status_update,
        delete=_promotion_delete_,
        delete_label="Archive",
    ),
    "Seat Type": RecordPage(
        "Seat Type",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "colour", "label": "Colour", "type": "text"},
            {"name": "shape", "label": "Shape", "type": "select", "options": [
                {"value": "ROUNDED_SQUARE", "label": "Rounded square"},
                {"value": "CIRCLE", "label": "Circle"},
                {"value": "SQUARE", "label": "Square"}]},
            {"name": "sellable", "label": "Sellable", "type": "bool"},
            {"name": "accessible", "label": "Accessible", "type": "bool"},
            {"name": "display_priority", "label": "Display order", "type": "number"},
        ],
        create=seating_admin.seat_type_create,
        update=seating_admin.seat_type_update,
        delete=seating_admin.seat_type_delete,
        delete_label="Deactivate",
        full_edit=True,
    ),
    "Seat Zone": RecordPage(
        "Seat Zone",
        fields=[
            {"name": "layout_id", "label": "Layout", "type": "select",
             "options_source": "layouts", "required": True},
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "i18n", "required": True},
            {"name": "colour", "label": "Colour", "type": "text"},
            {"name": "zone_kind", "label": "Zone kind", "type": "select", "options": [
                {"value": "ASSIGNED", "label": "Assigned seating"},
                {"value": "GENERAL", "label": "General admission"},
                {"value": "STANDING", "label": "Standing"}]},
            {"name": "capacity", "label": "Capacity", "type": "number"},
            {"name": "display_order", "label": "Display order", "type": "number"},
        ],
        create=seating_admin.seat_zone_create,
        update=seating_admin.seat_zone_update,
        delete=seating_admin.seat_zone_delete,
        delete_label="Delete",
        full_edit=True,
    ),
    "Seat Layout": RecordPage(
        "Seat Layout",
        fields=[
            {"name": "code", "label": "Code", "type": "text", "required": True},
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "is_template", "label": "Is a template", "type": "bool"},
        ],
        create=seating_admin.seat_layout_create,
        delete=seating_admin.seat_layout_delete,
        delete_label="Archive",
    ),
}


def record_page(page: str) -> RecordPage | None:
    return RECORD_CRUD.get(page)
