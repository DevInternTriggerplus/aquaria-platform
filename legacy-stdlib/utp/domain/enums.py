"""Domain enumerations.

These are the platform's rule *primitives*. An admission model is not a code
path: it is a named bundle of primitive settings (validity window, entry count,
re-entry allowance, session requirement, seat requirement, capacity consumption,
transferability). New admission models can therefore be added as configuration
whenever their behaviour is expressible by those primitives (R3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #

CHANNELS: tuple[str, ...] = ("ONLINE", "KIOSK", "COUNTER", "PARTNER", "STAFF", "API")

# --------------------------------------------------------------------------- #
# Admission models (R3.1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AdmissionModel:
    """A named bundle of rule primitives.

    ``session_requirement`` / ``seat_requirement`` are defaults; a product may
    narrow or widen them (R3.3). ``entry_allowance`` of ``None`` means unlimited
    within the validity window.
    """

    code: str
    label: str
    validity: str  # FIXED_DATE | OPEN_DATE | DATE_RANGE | SESSION | SUBSCRIPTION
    entry_allowance: int | None = 1
    reentry_allowed: bool = False
    session_requirement: str = "NOT_USED"  # NOT_USED | OPTIONAL | REQUIRED
    seat_requirement: str = "NOT_USED"
    consumes_capacity: bool = True
    transferable: bool = False
    is_package: bool = False
    is_recurring: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "validity": self.validity,
            "entry_allowance": self.entry_allowance,
            "reentry_allowed": self.reentry_allowed,
            "session_requirement": self.session_requirement,
            "seat_requirement": self.seat_requirement,
            "consumes_capacity": self.consumes_capacity,
            "transferable": self.transferable,
            "is_package": self.is_package,
            "is_recurring": self.is_recurring,
        }


#: Platform-supplied admission models. Every one of these is data, and tenants may
#: add more through ``ConfigStore`` key ``catalog.admission_models``.
ADMISSION_MODELS: dict[str, AdmissionModel] = {
    m.code: m
    for m in (
        AdmissionModel("GENERAL_ADMISSION", "General Admission", "FIXED_DATE"),
        AdmissionModel("FIXED_DATE", "Fixed Date", "FIXED_DATE"),
        AdmissionModel("OPEN_DATE", "Open Date", "OPEN_DATE", consumes_capacity=False),
        AdmissionModel("TIMED_ENTRY", "Timed Entry", "SESSION", session_requirement="REQUIRED"),
        AdmissionModel("SESSION_BOOKING", "Session Booking", "SESSION", session_requirement="REQUIRED"),
        AdmissionModel(
            "RESERVED_SEAT",
            "Reserved Seat",
            "SESSION",
            session_requirement="REQUIRED",
            seat_requirement="REQUIRED",
        ),
        AdmissionModel("DAY_PASS", "Day Pass", "FIXED_DATE", entry_allowance=None, reentry_allowed=True),
        AdmissionModel("MULTI_DAY_PASS", "Multi-Day Pass", "DATE_RANGE", entry_allowance=None, reentry_allowed=True),
        AdmissionModel("SINGLE_ENTRY", "Single Entry", "FIXED_DATE", entry_allowance=1),
        AdmissionModel("MULTIPLE_ENTRY", "Multiple Entry", "DATE_RANGE", entry_allowance=None),
        AdmissionModel("RE_ENTRY", "Re-entry", "FIXED_DATE", entry_allowance=None, reentry_allowed=True),
        AdmissionModel("PACKAGE", "Package", "FIXED_DATE", is_package=True),
        AdmissionModel(
            "MEMBERSHIP",
            "Membership",
            "DATE_RANGE",
            entry_allowance=None,
            reentry_allowed=True,
            consumes_capacity=False,
        ),
        AdmissionModel(
            "SUBSCRIPTION",
            "Subscription",
            "SUBSCRIPTION",
            entry_allowance=None,
            reentry_allowed=True,
            consumes_capacity=False,
            is_recurring=True,
        ),
        AdmissionModel("CLASS", "Class", "SESSION", session_requirement="REQUIRED"),
        AdmissionModel(
            "RESOURCE_BOOKING",
            "Resource Booking",
            "SESSION",
            session_requirement="REQUIRED",
            seat_requirement="OPTIONAL",
        ),
        AdmissionModel("GROUP_TICKET", "Group Ticket", "FIXED_DATE"),
        AdmissionModel("COMPLIMENTARY", "Complimentary Ticket", "FIXED_DATE", transferable=True),
    )
}

SESSION_REQUIREMENTS: tuple[str, ...] = ("NOT_USED", "OPTIONAL", "REQUIRED")
SEAT_REQUIREMENTS: tuple[str, ...] = ("NOT_USED", "OPTIONAL", "REQUIRED")

# --------------------------------------------------------------------------- #
# Lifecycle states
# --------------------------------------------------------------------------- #

#: R8.3 / R24.2 — identical status vocabulary for product sessions and show
#: sessions, because they are the same capacity-bearing concept.
SESSION_STATUSES: tuple[str, ...] = (
    "SCHEDULED",
    "AVAILABLE",
    "LIMITED",
    "FULL",
    "DELAYED",
    "CANCELLED",
    "COMPLETED",
    "HIDDEN",
)

PUBLICATION_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")

#: R15.5
TICKET_STATES: tuple[str, ...] = (
    "ISSUED",
    "VALID",
    "USED",
    "PARTIALLY_USED",
    "EXPIRED",
    "CANCELLED",
    "VOIDED",
    "REFUNDED",
    "TRANSFERRED",
    "BLOCKED",
)

BOOKING_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "PENDING",
    "AWAITING_PAYMENT",
    "CONFIRMED",
    "PARTIALLY_REFUNDED",
    "REFUNDED",
    "CANCELLED",
    "VOIDED",
    "RECONCILIATION",
)

HOLD_STATES: tuple[str, ...] = ("ACTIVE", "CONFIRMED", "EXPIRED", "RELEASED")

#: R59.1
SEAT_STATUSES: tuple[str, ...] = (
    "AVAILABLE",
    "HELD",
    "RESERVED",
    "SOLD",
    "BLOCKED",
    "MAINTENANCE",
    "STAFF_ONLY",
    "COMPLIMENTARY",
    "ACCESSIBLE",
    "UNAVAILABLE",
)

STAFF_STATUSES: tuple[str, ...] = ("INVITED", "ACTIVE", "SUSPENDED", "INACTIVE")

#: R25.1
RESERVATION_MODES: tuple[str, ...] = ("NONE", "OPTIONAL", "REQUIRED")

#: R25.5
SHOW_ELIGIBILITY_MODES: tuple[str, ...] = (
    "INCLUDED_WITH_ADMISSION",
    "REQUIRES_TICKET_TYPE",
    "REQUIRES_ADDON",
    "REQUIRES_RESERVATION",
    "REQUIRES_PAYMENT",
    "PACKAGE_ONLY",
    "MEMBERS_ONLY",
    "COMPLIMENTARY",
)

#: R7.1
CALENDAR_STATES: tuple[str, ...] = (
    "AVAILABLE",
    "LIMITED",
    "SOLD_OUT",
    "CLOSED",
    "BLACKOUT",
    "SELECTED",
    "TODAY",
    "NOT_YET_ON_SALE",
    "PAST",
)

#: R32.2 — the complete, closed set of gate outcomes.
ACCESS_DECISIONS: tuple[str, ...] = (
    "ADMIT",
    "ADMIT_WITH_CHECK",
    "REJECT_ALREADY_USED",
    "REJECT_WRONG_DATE",
    "REJECT_WRONG_SESSION",
    "REJECT_WRONG_VENUE_OR_GATE",
    "REJECT_CANCELLED",
    "REJECT_REFUNDED",
    "REJECT_VOIDED",
    "REJECT_BLOCKED",
    "REJECT_NOT_YET_VALID",
    "REJECT_EXPIRED",
    "REJECT_UNKNOWN_CODE",
)

ADMIT_DECISIONS: frozenset[str] = frozenset({"ADMIT", "ADMIT_WITH_CHECK"})

#: R36.1
NOTIFICATION_EVENTS: tuple[str, ...] = (
    "BOOKING_CONFIRMATION",
    "PAYMENT_CONFIRMATION",
    "ETICKET_DELIVERY",
    "BOOKING_REMINDER",
    "BOOKING_RESCHEDULED",
    "BOOKING_CANCELLED",
    "REFUND_COMPLETED",
    "TAX_INVOICE_AVAILABLE",
    "SHOW_SCHEDULE_CHANGED",
    "SHOW_CANCELLED",
    "WAITING_LIST_OFFER",
    "CONSENT_WITHDRAWAL_CONFIRMATION",
    "DSAR_ACKNOWLEDGEMENT",
    "SEAT_CHANGED",
)

MARKETING_EVENTS: frozenset[str] = frozenset()  # transactional-only baseline (R36.11)

#: R13.1
PROMOTION_MECHANICS: tuple[str, ...] = (
    "PERCENT_DISCOUNT",
    "FIXED_DISCOUNT",
    "SPECIAL_PRICE",
    "BUY_X_GET_Y",
    "SECOND_ITEM_DISCOUNT",
    "FREE_GIFT",
    "FAMILY_PACKAGE",
    "GROUP_PACKAGE",
    "CHILD_FREE",
    "SENIOR_PROMOTION",
    "MEMBER_PROMOTION",
    "COUPON_CODE",
    "VOUCHER",
    "BANK_PROMOTION",
    "PAYMENT_METHOD_PROMOTION",
    "PARTNER_PROMOTION",
    "EARLY_BIRD",
    "LAST_MINUTE",
    "WEEKDAY",
    "WEEKEND",
    "SEASONAL",
    "CAMPAIGN",
    "BUNDLE",
    "QUANTITY_DISCOUNT",
)

#: R60 — where seat selection sits relative to payment.
SEAT_FLOW_MODELS: tuple[str, ...] = ("FLOW_A", "FLOW_B", "FLOW_C")

#: R49.1
LAYOUT_ELEMENT_CATEGORIES: tuple[str, ...] = (
    "SEATING",
    "VENUE",
    "NAVIGATION",
    "STRUCTURAL",
    "FACILITY",
    "TRANSPORTATION",
)

#: R50.4
SEAT_SHAPES: tuple[str, ...] = (
    "SQUARE",
    "ROUNDED_SQUARE",
    "CIRCLE",
    "THEATRE_CHAIR",
    "SOFA",
    "DOUBLE_SOFA",
    "BENCH",
    "CUSTOM_ICON",
)

ZONE_KINDS: tuple[str, ...] = ("ASSIGNED", "GA", "STANDING")

#: R59.2
SEAT_BLOCK_REASONS: tuple[str, ...] = (
    "EQUIPMENT",
    "CAMERA",
    "VIP_GUEST",
    "MAINTENANCE",
    "SAFETY",
    "STAFF_USE",
)

PAYMENT_METHODS: tuple[str, ...] = (
    "CARD",
    "QR_BANK_TRANSFER",
    "EWALLET",
    "CASH",
    "PARTNER_INVOICE",
    "COMPLIMENTARY",
    # A stored-value instrument (gift card / previously-sold cash coupon) settling
    # the bill. It is the customer's own value, so it settles without a gateway and
    # is permitted in any channel (add_features §16, §68).
    "STORED_VALUE",
)

STAFF_ONLY_PAYMENT_METHODS: frozenset[str] = frozenset({"CASH", "COMPLIMENTARY"})

#: Methods settled internally without an external gateway authorization.
IMMEDIATE_SETTLE_METHODS: frozenset[str] = frozenset({"CASH", "COMPLIMENTARY", "STORED_VALUE"})


def admission_model(code: str) -> AdmissionModel:
    """Look up an admission model, raising a clear error for unknown codes."""
    try:
        return ADMISSION_MODELS[code]
    except KeyError as exc:  # pragma: no cover - configuration guard
        raise ValueError(f"unknown admission model: {code}") from exc


__all__ = [
    "ACCESS_DECISIONS",
    "ADMISSION_MODELS",
    "ADMIT_DECISIONS",
    "AdmissionModel",
    "BOOKING_STATUSES",
    "CALENDAR_STATES",
    "CHANNELS",
    "HOLD_STATES",
    "IMMEDIATE_SETTLE_METHODS",
    "LAYOUT_ELEMENT_CATEGORIES",
    "NOTIFICATION_EVENTS",
    "PAYMENT_METHODS",
    "PROMOTION_MECHANICS",
    "PUBLICATION_STATES",
    "RESERVATION_MODES",
    "SEAT_BLOCK_REASONS",
    "SEAT_FLOW_MODELS",
    "SEAT_REQUIREMENTS",
    "SEAT_SHAPES",
    "SEAT_STATUSES",
    "SESSION_REQUIREMENTS",
    "SESSION_STATUSES",
    "SHOW_ELIGIBILITY_MODES",
    "STAFF_ONLY_PAYMENT_METHODS",
    "STAFF_STATUSES",
    "TICKET_STATES",
    "ZONE_KINDS",
    "admission_model",
]
