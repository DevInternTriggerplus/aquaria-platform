"""Permission registry.

This is the single source of truth for what can be granted. Three properties
matter and are asserted by the test suite rather than assumed:

* **Every verb is independent.** ``ADD`` never implies ``EDIT``; ``EDIT`` never
  implies ``DELETE``; none of them imply ``VIEW`` (R40.4). The registry stores no
  implication edges at all, so there is nowhere for an implication to hide.
* **DELETE is authority to remove from active use, not to erase history.** Each
  page declares ``delete_semantics``, which is what the confirmation dialog must
  state before the user commits (R46.2, R67.6). For protected record classes the
  service layer executes Cancel/Void/Archive/Deactivate instead of a physical
  delete, and the data layer refuses the delete regardless (R46.6).
* **New permissions default to denied.** Grants are stored as rows; absence is
  denial. Adding a page therefore cannot widen any existing role (R40.11,
  R44.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Verb = Literal["VIEW", "ADD", "EDIT", "DELETE"]

ALL_VERBS: tuple[Verb, ...] = ("VIEW", "ADD", "EDIT", "DELETE")
VIEW_ONLY: tuple[Verb, ...] = ("VIEW",)

ACTION_PREFIX = "ACTION:"


@dataclass(frozen=True, slots=True)
class Page:
    """A back office page/module in the permission matrix."""

    key: str
    label: str
    group: str
    verbs: tuple[Verb, ...] = ALL_VERBS
    #: What DELETE actually performs for this page's records (R46.2).
    delete_semantics: str | None = None
    #: True when records of this page may never be physically deleted (R46.1).
    protected: bool = False
    description: str = ""

    def permission_keys(self) -> tuple[str, ...]:
        return tuple(f"{self.key}.{verb}" for verb in self.verbs)


@dataclass(frozen=True, slots=True)
class ActionPermission:
    """A sensitive operation gated separately from CRUD (R41.3)."""

    key: str
    label: str
    group: str
    requires_reason: bool = False
    requires_approval: bool = False
    revenue_affecting: bool = False
    description: str = ""

    @property
    def permission_key(self) -> str:
        return f"{ACTION_PREFIX}{self.key}"


# --------------------------------------------------------------------------- #
# Pages (R40.2, R62.1)
# --------------------------------------------------------------------------- #

PAGES: tuple[Page, ...] = (
    Page("Dashboard", "Dashboard", "Insights", VIEW_ONLY, description="Executive KPIs and drill-down."),
    Page(
        "Bookings",
        "Bookings",
        "Commerce",
        delete_semantics="CANCEL",
        protected=True,
        description="Customer orders. DELETE cancels the booking; the record is retained.",
    ),
    Page(
        "Counter Sales",
        "Counter Sales",
        "Commerce",
        delete_semantics="VOID",
        protected=True,
        description="Point-of-sale transactions. DELETE voids the sale.",
    ),
    Page(
        "Tickets",
        "Tickets",
        "Commerce",
        delete_semantics="VOID",
        protected=True,
        description="Issued admission artefacts. DELETE voids the ticket.",
    ),
    Page(
        "Customers",
        "Customers",
        "Commerce",
        delete_semantics="ANONYMIZE",
        description="Customer records. DELETE anonymizes, retaining legally required data (R12.22).",
    ),
    Page("Products", "Products", "Catalog", delete_semantics="DEACTIVATE"),
    Page("Ticket Types", "Ticket Types", "Catalog", delete_semantics="DEACTIVATE"),
    Page("Customer Segments", "Customer Segments", "Catalog", delete_semantics="DEACTIVATE"),
    Page("Experiences", "Experiences", "Catalog", delete_semantics="DEACTIVATE"),
    Page("Pricing", "Pricing", "Catalog", delete_semantics="DEACTIVATE"),
    Page(
        "Promotions",
        "Promotions",
        "Catalog",
        delete_semantics="ARCHIVE",
        description="DELETE archives; a promotion with redemptions is never removed (R13.10).",
    ),
    # --- advanced promotion engine (add_features §58) --- #
    Page(
        "Coupon Codes",
        "Coupon Codes",
        "Promotions",
        delete_semantics="ARCHIVE",
        description="Promotion / coupon codes. Codes with redemptions are archived, not deleted.",
    ),
    Page(
        "Cash Coupons",
        "Cash Coupons",
        "Promotions",
        delete_semantics="ARCHIVE",
        protected=True,
        description="Cash coupons / gift vouchers. Stored-value records are retained for accounting.",
    ),
    Page(
        "Member Rewards",
        "Member Rewards",
        "Promotions",
        delete_semantics="ARCHIVE",
        description="Loyalty point rewards and redemption rules.",
    ),
    Page(
        "Partner Benefits",
        "Partner Benefits",
        "Promotions",
        delete_semantics="DEACTIVATE",
        description="Partner-specific pricing, discounts and complimentary allowances.",
    ),
    Page("Time Slots", "Time Slots", "Operations", delete_semantics="CANCEL"),
    Page("Capacity", "Capacity", "Operations", delete_semantics="DEACTIVATE"),
    Page("Shows", "Shows", "Operations", delete_semantics="DEACTIVATE"),
    Page(
        "Show Schedule",
        "Show Schedule",
        "Operations",
        delete_semantics="CANCEL",
        description="Show sessions. DELETE cancels; sessions with reservations are never removed (R24.8).",
    ),
    Page("Venues", "Venues", "Configuration", delete_semantics="DEACTIVATE"),
    Page("Areas", "Areas & Locations", "Configuration", delete_semantics="DEACTIVATE"),
    Page("Access Points", "Access Points", "Configuration", delete_semantics="DEACTIVATE"),
    Page("Partners", "Partners", "Commerce", delete_semantics="DEACTIVATE"),
    Page("Kiosks", "Kiosks", "Devices", delete_semantics="DEACTIVATE"),
    Page("Devices", "Devices", "Devices", delete_semantics="DEACTIVATE"),
    Page("Email Templates", "Email Templates", "Communications", delete_semantics="ARCHIVE"),
    Page(
        "Tax Invoices",
        "Tax Invoices",
        "Finance",
        delete_semantics="CREDIT_NOTE",
        protected=True,
        description="DELETE issues a credit note referencing the original (R72.5).",
    ),
    Page("Reports", "Reports", "Insights", VIEW_ONLY),
    Page("Staff", "Staff", "Administration", delete_semantics="DEACTIVATE", description="R38.6, R38.7."),
    Page("Roles", "Roles", "Administration", delete_semantics="DELETE"),
    Page("Settings", "Settings", "Administration", delete_semantics="ARCHIVE"),
    Page("Audit Logs", "Audit Logs", "Administration", VIEW_ONLY, protected=True),
    # --- business / venue settings (add_features §30) --- #
    # VAT, Service Charge, Time Zone and Ticket Validity are singleton
    # configurations: they are viewed and edited, never added or deleted, so they
    # carry VIEW+EDIT only. Currency and Exchange Rates are collections and get
    # full CRUD. Physical delete never applies to any of them — history is kept.
    Page(
        "VAT Settings",
        "VAT Settings",
        "Tax & Charges",
        ("VIEW", "EDIT"),
        description="Effective-dated VAT rate and inclusive/exclusive mode.",
    ),
    Page(
        "Service Charge Settings",
        "Service Charge Settings",
        "Tax & Charges",
        ("VIEW", "EDIT"),
        description="Effective-dated service charge rate and mode.",
    ),
    Page(
        "Time Zone Settings",
        "Time Zone Settings",
        "Business",
        ("VIEW", "EDIT"),
        description="Venue IANA time zone governing all business-local time.",
    ),
    Page(
        "Ticket Validity Settings",
        "Ticket Validity Settings",
        "Ticket & Access",
        ("VIEW", "EDIT"),
        description="Default QR/ticket validity policy for the venue.",
    ),
    Page(
        "Currency Settings",
        "Currency Settings",
        "Currency",
        delete_semantics="DEACTIVATE",
        description="Base currency and supported display currencies.",
    ),
    Page(
        "Exchange Rates",
        "Exchange Rates",
        "Currency",
        delete_semantics="ARCHIVE",
        protected=True,
        description="Manual currency conversion rates. Ended rates are retained (settings spec §32).",
    ),
    Page(
        "Payment Type",
        "Payment Type",
        "Tax & Charges",
        delete_semantics="ARCHIVE",
        description="Customer-facing payment methods, per-channel availability and order (update spec §39).",
    ),
    # --- seating (R62.1) --- #
    Page("Seat Layout", "Seat Layout", "Seating", delete_semantics="ARCHIVE"),
    Page("Seat Type", "Seat Type", "Seating", delete_semantics="DEACTIVATE"),
    Page("Seat Zone", "Seat Zone", "Seating", delete_semantics="DEACTIVATE"),
    Page(
        "Seat Reservation",
        "Seat Reservation",
        "Seating",
        delete_semantics="RELEASE",
        protected=True,
    ),
)

PAGES_BY_KEY: dict[str, Page] = {p.key: p for p in PAGES}

#: Pages whose records may never be physically deleted (R46.1).
PROTECTED_PAGES: frozenset[str] = frozenset(p.key for p in PAGES if p.protected)


# --------------------------------------------------------------------------- #
# Action permissions (R41.1, R41.2, R62.2)
# --------------------------------------------------------------------------- #

ACTIONS: tuple[ActionPermission, ...] = (
    # --- general sensitive operations (R41.1) --- #
    ActionPermission("APPROVE", "Approve", "Authorization", description="Second-authorization approver (R41.5)."),
    ActionPermission("REFUND", "Refund", "Money", requires_reason=True, revenue_affecting=True),
    ActionPermission("VOID", "Void", "Money", requires_reason=True, revenue_affecting=True),
    ActionPermission("REPRINT", "Reprint", "Operations"),
    ActionPermission("EXPORT", "Export data", "Insights", description="Required for any download (R41.7)."),
    ActionPermission(
        "APPLY_MANUAL_DISCOUNT",
        "Apply manual discount",
        "Money",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission(
        "ISSUE_COMPLIMENTARY",
        "Issue complimentary ticket",
        "Money",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission("RESCHEDULE", "Reschedule booking", "Operations"),
    ActionPermission("CANCEL_BOOKING", "Cancel booking", "Operations", requires_reason=True),
    ActionPermission("ISSUE_TAX_INVOICE", "Issue tax invoice", "Finance"),
    ActionPermission("CLOSE_SHIFT", "Close cashier shift", "Money"),
    ActionPermission("VIEW_COST", "View cost and margin", "Insights"),
    ActionPermission("VIEW_PII", "View unmasked personal data", "Privacy"),
    ActionPermission("MANAGE_PERMISSION", "Manage roles and permissions", "Administration"),
    # --- advanced promotion engine (add_features §58) --- #
    ActionPermission("PUBLISH_PROMOTION", "Publish a promotion", "Promotions"),
    ActionPermission("PAUSE_PROMOTION", "Pause or resume a promotion", "Promotions"),
    ActionPermission(
        "OVERRIDE_PROMOTION",
        "Override a promotion rule",
        "Promotions",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission(
        "MANAGE_PROMOTION_BUDGET", "Manage promotion budget", "Promotions", requires_reason=True
    ),
    ActionPermission(
        "MANAGE_ACCOUNTING_TREATMENT",
        "Change coupon accounting treatment",
        "Promotions",
        requires_reason=True,
        revenue_affecting=True,
        description="Change how a coupon is booked: discount, stored value or liability (§16).",
    ),
    ActionPermission(
        "APPLY_PARTNER_DISCOUNT",
        "Apply a partner discount",
        "Money",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission(
        "APPLY_COMPLIMENTARY",
        "Apply a complimentary benefit",
        "Money",
        requires_reason=True,
        revenue_affecting=True,
    ),
    # --- business / venue settings (add_features §31) --- #
    # Changing any of these affects financial or access behaviour, so each is a
    # sensitive action gated independently of the page's EDIT verb and every change
    # is audited (settings spec §32).
    ActionPermission(
        "MANAGE_TAX_SETTINGS", "Manage VAT settings", "Tax & Charges", requires_reason=True, revenue_affecting=True
    ),
    ActionPermission(
        "MANAGE_SERVICE_CHARGE", "Manage service charge", "Tax & Charges", requires_reason=True, revenue_affecting=True
    ),
    ActionPermission("MANAGE_TIMEZONE", "Manage venue time zone", "Business", requires_reason=True),
    ActionPermission("MANAGE_TICKET_VALIDITY", "Manage ticket validity", "Ticket & Access", requires_reason=True),
    ActionPermission("MANAGE_CURRENCY", "Manage currencies", "Currency", requires_reason=True),
    ActionPermission(
        "MANAGE_EXCHANGE_RATE", "Manage exchange rates", "Currency", requires_reason=True, revenue_affecting=True
    ),
    # Payment types (update spec §39). Managing the list is separate from seeing or
    # editing the sensitive provider credentials, which are gated by their own action
    # so a marketing user can reorder cards without touching gateway secrets.
    ActionPermission("MANAGE_PAYMENT_TYPE", "Manage payment types", "Tax & Charges", requires_reason=True),
    ActionPermission(
        "MANAGE_PAYMENT_PROVIDER_CONFIG",
        "Manage payment provider credentials",
        "Tax & Charges",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission(
        "OVERRIDE_ACCESS",
        "Override gate rejection",
        "Access",
        requires_reason=True,
        revenue_affecting=True,
    ),
    # --- show & schedule (R41.2) --- #
    ActionPermission("PUBLISH_SHOW_SCHEDULE", "Publish show schedule", "Schedule"),
    ActionPermission("CANCEL_SHOW", "Cancel show session", "Schedule", requires_reason=True),
    ActionPermission("CHANGE_SHOW_LOCATION", "Change show location", "Schedule", requires_reason=True),
    ActionPermission(
        "OVERRIDE_CAPACITY",
        "Override capacity",
        "Schedule",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission("BULK_UPDATE_SCHEDULE", "Bulk update schedule", "Schedule"),
    ActionPermission("EXPORT_SHOW_SCHEDULE", "Export show schedule", "Schedule"),
    # --- seating (R62.2) --- #
    ActionPermission("PUBLISH_SEAT_LAYOUT", "Publish seat layout", "Seating"),
    ActionPermission("DUPLICATE_SEAT_LAYOUT", "Duplicate seat layout", "Seating"),
    ActionPermission("BLOCK_SEAT", "Block seat", "Seating", requires_reason=True),
    ActionPermission("UNBLOCK_SEAT", "Unblock seat", "Seating"),
    ActionPermission("CHANGE_CUSTOMER_SEAT", "Change customer seat", "Seating", requires_reason=True),
    ActionPermission(
        "OVERRIDE_SEAT_PRICE",
        "Override seat price",
        "Seating",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission(
        "OVERRIDE_SEAT_ELIGIBILITY",
        "Override seat eligibility",
        "Seating",
        requires_reason=True,
        revenue_affecting=True,
    ),
    ActionPermission("RELEASE_SEAT_HOLD", "Release seat hold", "Seating", requires_reason=True),
)

ACTIONS_BY_KEY: dict[str, ActionPermission] = {a.key: a for a in ACTIONS}

#: Overrides and discretionary money actions that warrant threshold alerting and
#: periodic review, per the residual-risk analysis in the requirements (section D.2).
OVERRIDE_ACTIONS: frozenset[str] = frozenset(
    a.key for a in ACTIONS if a.revenue_affecting
)


# --------------------------------------------------------------------------- #
# Key helpers
# --------------------------------------------------------------------------- #


def page_key(page: str, verb: str) -> str:
    """Build a page permission key, validating both parts."""
    definition = PAGES_BY_KEY.get(page)
    if definition is None:
        raise ValueError(f"unknown page: {page!r}")
    if verb not in definition.verbs:
        raise ValueError(f"page {page!r} does not define verb {verb!r}")
    return f"{page}.{verb}"


def action_key(action: str) -> str:
    """Build an action permission key, validating the action."""
    if action not in ACTIONS_BY_KEY:
        raise ValueError(f"unknown action permission: {action!r}")
    return f"{ACTION_PREFIX}{action}"


def is_action_key(key: str) -> bool:
    return key.startswith(ACTION_PREFIX)


def all_permission_keys() -> tuple[str, ...]:
    keys: list[str] = []
    for page in PAGES:
        keys.extend(page.permission_keys())
    keys.extend(a.permission_key for a in ACTIONS)
    return tuple(keys)


ALL_PERMISSION_KEYS: tuple[str, ...] = all_permission_keys()
ALL_PERMISSION_KEY_SET: frozenset[str] = frozenset(ALL_PERMISSION_KEYS)


def validate_permission_key(key: str) -> str:
    """Reject unknown keys so a typo cannot silently create a dead grant."""
    if key not in ALL_PERMISSION_KEY_SET:
        raise ValueError(f"unknown permission key: {key!r}")
    return key


def matrix_skeleton() -> list[dict[str, object]]:
    """Row-per-page structure for the permission matrix UI (R40.3)."""
    return [
        {
            "page": page.key,
            "label": page.label,
            "group": page.group,
            "verbs": {verb: (verb in page.verbs) for verb in ALL_VERBS},
            "delete_semantics": page.delete_semantics,
            "protected": page.protected,
            "description": page.description,
        }
        for page in PAGES
    ]


def action_groups() -> dict[str, list[dict[str, object]]]:
    """Action permissions grouped for the matrix UI's action section (R40.3)."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for action in ACTIONS:
        grouped.setdefault(action.group, []).append(
            {
                "key": action.key,
                "permission_key": action.permission_key,
                "label": action.label,
                "requires_reason": action.requires_reason,
                "requires_approval": action.requires_approval,
                "revenue_affecting": action.revenue_affecting,
                "description": action.description,
            }
        )
    return grouped


def combination_warnings(granted: set[str]) -> list[str]:
    """Operationally odd combinations that must warn but still be stored (R40.5).

    The platform deliberately does *not* repair these. It stores and enforces
    exactly what was configured, and tells the administrator what they have done.
    """
    warnings: list[str] = []
    for page in PAGES:
        if "VIEW" not in page.verbs:
            continue
        has_view = f"{page.key}.VIEW" in granted
        mutating = [v for v in ("ADD", "EDIT", "DELETE") if v in page.verbs and f"{page.key}.{v}" in granted]
        if mutating and not has_view:
            warnings.append(
                f"{page.label}: {', '.join(mutating)} granted without VIEW. "
                "Records reached only by viewing will not be usable, and the API will "
                "enforce this literally."
            )
    if action_key("MANAGE_PERMISSION") in granted and "Roles.VIEW" not in granted:
        warnings.append(
            "MANAGE_PERMISSION granted without Roles.VIEW. Role changes will be rejected "
            "in the UI because roles cannot be listed."
        )
    if action_key("REFUND") in granted and "Bookings.VIEW" not in granted:
        warnings.append("REFUND granted without Bookings.VIEW. Refunds cannot be initiated from the UI.")
    return warnings


# --------------------------------------------------------------------------- #
# Default role templates (R39.1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """A starting point for a tenant role.

    Templates seed a tenant's roles once. Later platform changes to a template
    never mutate a tenant role derived from it (R39.6) — the tenant's rows are
    independent from the moment they are created.
    """

    code: str
    name: str
    authority_level: int
    permissions: tuple[str, ...] = ()
    all_pages: bool = False
    all_actions: bool = False
    description: str = ""
    mfa_required: bool = False
    grants: dict[str, tuple[Verb, ...]] = field(default_factory=dict)

    def resolve(self) -> tuple[str, ...]:
        keys: set[str] = set()
        if self.all_pages:
            for page in PAGES:
                keys.update(page.permission_keys())
        if self.all_actions:
            keys.update(a.permission_key for a in ACTIONS)
        for page_name, verbs in self.grants.items():
            definition = PAGES_BY_KEY[page_name]
            for verb in verbs:
                if verb in definition.verbs:
                    keys.add(f"{page_name}.{verb}")
        keys.update(self.permissions)
        return tuple(sorted(keys))


_RO = ("VIEW",)
_RW = ("VIEW", "ADD", "EDIT")
_FULL = ("VIEW", "ADD", "EDIT", "DELETE")

ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        code="PLATFORM_SUPER_ADMIN",
        name="Platform Super Admin",
        authority_level=100,
        all_pages=True,
        all_actions=True,
        mfa_required=True,
        description="Full platform authority. MFA mandatory (R73.2). Last holder cannot be removed (R44.5).",
    ),
    RoleTemplate(
        code="ORGANIZATION_ADMIN",
        name="Organization Admin",
        authority_level=90,
        all_pages=True,
        all_actions=True,
        mfa_required=True,
        description="All venues within one organization (R43.5).",
    ),
    RoleTemplate(
        code="VENUE_MANAGER",
        name="Venue Manager",
        authority_level=70,
        grants={
            "Dashboard": _RO,
            "Bookings": _RW,
            "Counter Sales": _RW,
            "Tickets": _RW,
            "Customers": _RO,
            "Products": _RW,
            "Ticket Types": _RW,
            "Customer Segments": _RO,
            "Experiences": _RW,
            "Pricing": _RW,
            "Promotions": _RW,
            "Coupon Codes": _RW,
            "Cash Coupons": _RW,
            "Member Rewards": _RW,
            "Partner Benefits": _RW,
            "Time Slots": _FULL,
            "Capacity": _RW,
            "Shows": _RW,
            "Show Schedule": _FULL,
            "Areas": _RW,
            "Access Points": _RO,
            "Partners": _RO,
            "Kiosks": _RO,
            "Devices": _RO,
            "Email Templates": _RO,
            "Tax Invoices": _RO,
            "Reports": _RO,
            "Staff": _RO,
            "Seat Layout": _RW,
            "Seat Type": _RO,
            "Seat Zone": _RW,
            "Seat Reservation": _RW,
            "Audit Logs": _RO,
            "Settings": _RO,
            "VAT Settings": _RW,
            "Service Charge Settings": _RW,
            "Time Zone Settings": _RW,
            "Ticket Validity Settings": _RW,
            "Currency Settings": _FULL,
            "Exchange Rates": _FULL,
            "Payment Type": _FULL,
        },
        permissions=(
            f"{ACTION_PREFIX}APPROVE",
            f"{ACTION_PREFIX}REFUND",
            f"{ACTION_PREFIX}VOID",
            f"{ACTION_PREFIX}REPRINT",
            f"{ACTION_PREFIX}EXPORT",
            f"{ACTION_PREFIX}APPLY_MANUAL_DISCOUNT",
            f"{ACTION_PREFIX}ISSUE_COMPLIMENTARY",
            f"{ACTION_PREFIX}RESCHEDULE",
            f"{ACTION_PREFIX}CANCEL_BOOKING",
            f"{ACTION_PREFIX}ISSUE_TAX_INVOICE",
            f"{ACTION_PREFIX}CLOSE_SHIFT",
            f"{ACTION_PREFIX}VIEW_COST",
            f"{ACTION_PREFIX}VIEW_PII",
            f"{ACTION_PREFIX}OVERRIDE_ACCESS",
            f"{ACTION_PREFIX}OVERRIDE_CAPACITY",
            f"{ACTION_PREFIX}PUBLISH_SHOW_SCHEDULE",
            f"{ACTION_PREFIX}CANCEL_SHOW",
            f"{ACTION_PREFIX}CHANGE_SHOW_LOCATION",
            f"{ACTION_PREFIX}BULK_UPDATE_SCHEDULE",
            f"{ACTION_PREFIX}EXPORT_SHOW_SCHEDULE",
            f"{ACTION_PREFIX}PUBLISH_SEAT_LAYOUT",
            f"{ACTION_PREFIX}BLOCK_SEAT",
            f"{ACTION_PREFIX}UNBLOCK_SEAT",
            f"{ACTION_PREFIX}CHANGE_CUSTOMER_SEAT",
            f"{ACTION_PREFIX}RELEASE_SEAT_HOLD",
            f"{ACTION_PREFIX}MANAGE_TAX_SETTINGS",
            f"{ACTION_PREFIX}MANAGE_SERVICE_CHARGE",
            f"{ACTION_PREFIX}MANAGE_TIMEZONE",
            f"{ACTION_PREFIX}MANAGE_TICKET_VALIDITY",
            f"{ACTION_PREFIX}MANAGE_CURRENCY",
            f"{ACTION_PREFIX}MANAGE_EXCHANGE_RATE",
            f"{ACTION_PREFIX}MANAGE_PAYMENT_TYPE",
            f"{ACTION_PREFIX}MANAGE_PAYMENT_PROVIDER_CONFIG",
            f"{ACTION_PREFIX}PUBLISH_PROMOTION",
            f"{ACTION_PREFIX}PAUSE_PROMOTION",
            f"{ACTION_PREFIX}OVERRIDE_PROMOTION",
            f"{ACTION_PREFIX}MANAGE_PROMOTION_BUDGET",
            f"{ACTION_PREFIX}MANAGE_ACCOUNTING_TREATMENT",
            f"{ACTION_PREFIX}APPLY_PARTNER_DISCOUNT",
            f"{ACTION_PREFIX}APPLY_COMPLIMENTARY",
        ),
        description="Full operational authority at assigned venues only.",
    ),
    RoleTemplate(
        code="SUPERVISOR",
        name="Supervisor",
        authority_level=60,
        grants={
            "Dashboard": _RO,
            "Bookings": _RW,
            "Counter Sales": _RW,
            "Tickets": _RW,
            "Customers": _RO,
            "Time Slots": _RO,
            "Capacity": _RO,
            "Show Schedule": _RW,
            "Shows": _RO,
            "Reports": _RO,
            "Seat Reservation": _RW,
            "Seat Layout": _RO,
        },
        permissions=(
            f"{ACTION_PREFIX}APPROVE",
            f"{ACTION_PREFIX}REFUND",
            f"{ACTION_PREFIX}VOID",
            f"{ACTION_PREFIX}REPRINT",
            f"{ACTION_PREFIX}APPLY_MANUAL_DISCOUNT",
            f"{ACTION_PREFIX}RESCHEDULE",
            f"{ACTION_PREFIX}CANCEL_BOOKING",
            f"{ACTION_PREFIX}CLOSE_SHIFT",
            f"{ACTION_PREFIX}VIEW_PII",
            f"{ACTION_PREFIX}CHANGE_CUSTOMER_SEAT",
            f"{ACTION_PREFIX}BLOCK_SEAT",
            f"{ACTION_PREFIX}UNBLOCK_SEAT",
        ),
        description="Refund and void authority at own venue only (R41.6).",
    ),
    RoleTemplate(
        code="COUNTER_CASHIER",
        name="Counter / Cashier",
        authority_level=30,
        grants={
            "Bookings": _RW,
            "Counter Sales": _RW,
            "Tickets": ("VIEW", "ADD"),
            "Customers": ("VIEW", "ADD"),
            "Time Slots": _RO,
        },
        permissions=(f"{ACTION_PREFIX}REPRINT", f"{ACTION_PREFIX}ISSUE_TAX_INVOICE"),
        description="Sells and edits, but cannot refund or void (R41.4).",
    ),
    RoleTemplate(
        code="CUSTOMER_SERVICE",
        name="Customer Service",
        authority_level=40,
        grants={
            "Bookings": _RW,
            "Tickets": _RO,
            "Customers": _RW,
            "Time Slots": _RO,
            "Show Schedule": _RO,
            "Seat Reservation": _RO,
        },
        permissions=(
            f"{ACTION_PREFIX}RESCHEDULE",
            f"{ACTION_PREFIX}REPRINT",
            f"{ACTION_PREFIX}VIEW_PII",
        ),
        description="Can reschedule and resend but not refund.",
    ),
    RoleTemplate(
        code="GATE_STAFF",
        name="Gate Staff",
        authority_level=30,
        grants={"Tickets": _RO, "Bookings": _RO},
        description="Scan and admit at assigned access points (R43.6). No PII by default (R32.11).",
    ),
    RoleTemplate(
        code="ACCOUNTING",
        name="Accounting",
        authority_level=50,
        grants={
            "Dashboard": _RO,
            "Bookings": _RO,
            "Counter Sales": _RO,
            "Tickets": _RO,
            "Tax Invoices": _RW,
            "Reports": _RO,
            "Audit Logs": _RO,
            "VAT Settings": _RW,
            "Service Charge Settings": _RW,
            "Currency Settings": _RW,
            "Exchange Rates": _FULL,
            "Payment Type": _RW,
            "Cash Coupons": _FULL,
        },
        permissions=(
            f"{ACTION_PREFIX}EXPORT",
            f"{ACTION_PREFIX}ISSUE_TAX_INVOICE",
            f"{ACTION_PREFIX}VIEW_COST",
            f"{ACTION_PREFIX}REFUND",
            f"{ACTION_PREFIX}MANAGE_TAX_SETTINGS",
            f"{ACTION_PREFIX}MANAGE_SERVICE_CHARGE",
            f"{ACTION_PREFIX}MANAGE_CURRENCY",
            f"{ACTION_PREFIX}MANAGE_EXCHANGE_RATE",
            f"{ACTION_PREFIX}MANAGE_PAYMENT_TYPE",
            f"{ACTION_PREFIX}MANAGE_PAYMENT_PROVIDER_CONFIG",
            f"{ACTION_PREFIX}MANAGE_ACCOUNTING_TREATMENT",
            f"{ACTION_PREFIX}MANAGE_PROMOTION_BUDGET",
        ),
        description="Finance authority: tax, currency, exchange-rate and coupon accounting.",
    ),
    RoleTemplate(
        code="MARKETING",
        name="Marketing",
        authority_level=40,
        grants={
            "Dashboard": _RO,
            "Promotions": _FULL,
            "Coupon Codes": _FULL,
            "Member Rewards": _FULL,
            "Partner Benefits": _RW,
            "Cash Coupons": _RO,
            "Email Templates": _RW,
            "Products": _RO,
            "Ticket Types": _RO,
            "Pricing": _RO,
            "Shows": _RO,
            "Show Schedule": _RO,
            "Reports": _RO,
        },
        permissions=(
            f"{ACTION_PREFIX}EXPORT",
            f"{ACTION_PREFIX}PUBLISH_PROMOTION",
            f"{ACTION_PREFIX}PAUSE_PROMOTION",
            f"{ACTION_PREFIX}MANAGE_PROMOTION_BUDGET",
        ),
        description="Owns promotions and message content, not money operations.",
    ),
    RoleTemplate(
        code="REPORT_VIEWER",
        name="Report Viewer",
        authority_level=20,
        grants={"Dashboard": _RO, "Reports": _RO},
        description="Read-only insight access. No PII, no cost.",
    ),
    RoleTemplate(
        code="TECHNICAL_SUPPORT",
        name="Technical Support",
        authority_level=50,
        grants={
            "Dashboard": _RO,
            "Devices": _FULL,
            "Kiosks": _FULL,
            "Access Points": _RW,
            "Bookings": _RO,
            "Tickets": _RO,
            "Audit Logs": _RO,
            "Settings": _RO,
        },
        permissions=(f"{ACTION_PREFIX}REPRINT",),
        description="Device fleet and diagnostics. No money authority.",
    ),
)

ROLE_TEMPLATES_BY_CODE: dict[str, RoleTemplate] = {t.code: t for t in ROLE_TEMPLATES}

#: Roles whose assignment changes require MANAGE_PERMISSION plus, where
#: configured, a second approval (R44.10).
HIGH_AUTHORITY_ROLE_CODES: frozenset[str] = frozenset({"PLATFORM_SUPER_ADMIN", "ORGANIZATION_ADMIN"})

SUPER_ADMIN_CODE = "PLATFORM_SUPER_ADMIN"


__all__ = [
    "ACTION_PREFIX",
    "ACTIONS",
    "ACTIONS_BY_KEY",
    "ALL_PERMISSION_KEYS",
    "ALL_PERMISSION_KEY_SET",
    "ALL_VERBS",
    "ActionPermission",
    "HIGH_AUTHORITY_ROLE_CODES",
    "OVERRIDE_ACTIONS",
    "PAGES",
    "PAGES_BY_KEY",
    "PROTECTED_PAGES",
    "Page",
    "ROLE_TEMPLATES",
    "ROLE_TEMPLATES_BY_CODE",
    "RoleTemplate",
    "SUPER_ADMIN_CODE",
    "VIEW_ONLY",
    "Verb",
    "action_groups",
    "action_key",
    "all_permission_keys",
    "combination_warnings",
    "is_action_key",
    "matrix_skeleton",
    "page_key",
    "validate_permission_key",
]
