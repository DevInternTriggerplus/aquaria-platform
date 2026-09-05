"""The report catalog: what reports exist, and what each one needs.

Every report is a declaration, not a screen. The navigation, the filter bar, the
table columns, the export header and the permission check are all derived from
these records, so adding a report is a configuration change and a metric
function — never a new page component, a new route or a new permission entry.

That is the same reason the platform models venues as configuration: this module
has to serve an aquarium, a theatre, a gym and a tour bus operator without a
code change (reports spec, opening section). A venue with no seating simply has
no seat inventory to report, and the catalog marks that report as requiring the
seating capability rather than hiding it behind an `if venue_type ==` branch.

Two details are load-bearing:

* **Only relevant filters.** The spec is explicit that a screen must not show
  every filter it *could* (§34, §35). Each report lists the filters that change
  its answer, and the UI renders exactly those.
* **The permission is per report, not per section.** Most reports need
  ``Reports.VIEW``, but the tax-invoice report needs ``Tax Invoices.VIEW`` and
  the dashboards have pages of their own, so granting someone "reports" does not
  quietly grant them finance documents (R42.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Section = Literal["ANALYTICS", "OPERATIONS", "FINANCE"]

#: Filters the platform knows how to apply. A report names only the ones that
#: change its result (§34).
Filter = Literal[
    "date_range",
    "date_basis",      # order date vs visit date — a real source of disagreement
    "venue",
    "channel",
    "product",
    "ticket_type",
    "segment",
    "pricing_group",   # Thai / International, and whatever a tenant configures
    "promotion",
    "partner",
    "payment_method",
    "staff",
    "counter",
    "device",
    "show",
    "session",
    "booking_status",
    "payment_status",
    "scan_result",
    "group_by",        # hourly / daily / weekly / monthly
    "compare_with",
    "currency",
]


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a report result.

    ``kind`` drives both formatting and export: ``money`` is integer minor units
    the client must not divide itself, ``percent`` is basis points, ``pii`` is
    masked unless the principal holds ``VIEW_PII`` (§40).
    """

    key: str
    label: str
    kind: Literal["text", "money", "number", "percent", "date", "time", "datetime", "status", "pii"] = "text"
    align: Literal["left", "right"] = "left"
    #: Hidden from a principal without ``VIEW_COST`` (R41.8).
    cost: bool = False


@dataclass(frozen=True, slots=True)
class Report:
    key: str
    title: str
    section: Section
    summary: str
    #: Permission page required to view. Export additionally requires ``EXPORT``.
    page: str = "Reports"
    filters: tuple[str, ...] = ()
    columns: tuple[Column, ...] = ()
    #: ``True`` where the report is a dashboard composed of panels rather than a
    #: single table, so the UI knows not to render a grid.
    dashboard: bool = False
    #: Capability the venue must have for this report to mean anything. A venue
    #: with no reserved seating has nothing to say about seat occupancy.
    requires: tuple[str, ...] = ()
    #: Report this one drills into, so the UI can offer a consistent next step.
    drill_to: str | None = None
    default_group_by: str = "daily"


_MONEY = "money"


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

_ANALYTICS: tuple[Report, ...] = (
    Report(
        key="executive_overview",
        title="Executive Overview",
        section="ANALYTICS",
        summary="Business health at a glance: sales, visitors, capacity and what looks wrong.",
        page="Dashboard",
        dashboard=True,
        filters=("date_range", "venue", "compare_with", "currency"),
        drill_to="revenue",
    ),
    Report(
        key="revenue",
        title="Revenue",
        section="ANALYTICS",
        summary="Gross and net sales over time, with discount and refund alongside.",
        filters=("date_range", "date_basis", "venue", "channel", "product", "group_by",
                 "compare_with", "currency"),
        columns=(
            Column("bucket", "Period", "text"),
            Column("bookings", "Bookings", "number", "right"),
            Column("tickets", "Tickets", "number", "right"),
            Column("gross_minor", "Gross sales", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("refund_minor", "Refund", _MONEY, "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("atv_minor", "Avg transaction", _MONEY, "right"),
        ),
        drill_to="sales",
    ),
    Report(
        key="visitors",
        title="Visitors",
        section="ANALYTICS",
        summary="Who visited, by segment and pricing group. No personal data.",
        filters=("date_range", "venue", "channel", "segment", "pricing_group", "group_by"),
        columns=(
            Column("label", "Group", "text"),
            Column("visitors", "Visitors", "number", "right"),
            Column("share_bp", "Share", "percent", "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("per_visitor_minor", "Revenue / visitor", _MONEY, "right"),
        ),
    ),
    Report(
        key="channels",
        title="Sales Channels",
        section="ANALYTICS",
        summary="Where sales come from, and what each channel is worth.",
        filters=("date_range", "date_basis", "venue", "product", "currency"),
        columns=(
            Column("label", "Channel", "text"),
            Column("transactions", "Transactions", "number", "right"),
            Column("tickets", "Tickets", "number", "right"),
            Column("gross_minor", "Gross sales", _MONEY, "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("share_bp", "% of total", "percent", "right"),
            Column("atv_minor", "Avg transaction", _MONEY, "right"),
        ),
        drill_to="sales",
    ),
    Report(
        key="products",
        title="Products",
        section="ANALYTICS",
        summary="What sells, at what price, and each line's contribution.",
        filters=("date_range", "date_basis", "venue", "channel", "product", "ticket_type", "segment"),
        columns=(
            Column("product", "Product", "text"),
            Column("ticket_type", "Ticket type", "text"),
            Column("segment", "Segment", "text"),
            Column("quantity", "Quantity", "number", "right"),
            Column("gross_minor", "Gross revenue", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("net_minor", "Net revenue", _MONEY, "right"),
            Column("asp_minor", "Avg selling price", _MONEY, "right"),
            Column("share_bp", "Contribution", "percent", "right"),
        ),
        drill_to="sales",
    ),
    Report(
        key="capacity",
        title="Capacity",
        section="ANALYTICS",
        summary="Reserved against capacity, and how much of it actually arrived.",
        filters=("date_range", "venue", "show", "session"),
        columns=(
            Column("label", "Session", "text"),
            Column("date", "Date", "date"),
            Column("start_time", "Time", "time"),
            Column("capacity", "Capacity", "number", "right"),
            Column("reserved", "Reserved", "number", "right"),
            Column("checked_in", "Checked in", "number", "right"),
            Column("remaining", "Remaining", "number", "right"),
            Column("utilization_bp", "Utilization", "percent", "right"),
            Column("state", "Status", "status"),
        ),
    ),
    Report(
        key="peak_time",
        title="Peak Time",
        section="ANALYTICS",
        summary="Day-of-week against hour, so quiet and busy periods are obvious.",
        filters=("date_range", "venue", "group_by"),
        dashboard=True,
    ),
    Report(
        key="advance_booking",
        title="Advance Booking",
        section="ANALYTICS",
        summary="How far ahead guests book, and whether early bookings stick.",
        filters=("date_range", "venue", "channel"),
        columns=(
            Column("label", "Lead time", "text"),
            Column("bookings", "Bookings", "number", "right"),
            Column("visitors", "Visitors", "number", "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("cancelled", "Cancelled", "number", "right"),
            Column("cancel_rate_bp", "Cancellation rate", "percent", "right"),
        ),
    ),
    Report(
        key="promotions",
        title="Promotions",
        section="ANALYTICS",
        summary="Usage, discount given and budget consumed. Not attributed uplift.",
        filters=("date_range", "venue", "channel", "promotion"),
        columns=(
            Column("promotion", "Promotion", "text"),
            Column("redemptions", "Usage", "number", "right"),
            Column("gross_minor", "Gross sales", _MONEY, "right"),
            Column("discount_minor", "Discount given", _MONEY, "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("avg_discount_minor", "Avg discount", _MONEY, "right"),
            Column("budget_minor", "Budget", _MONEY, "right"),
            Column("budget_used_minor", "Budget used", _MONEY, "right"),
            Column("budget_remaining_minor", "Remaining", _MONEY, "right"),
        ),
    ),
    Report(
        key="partners",
        title="Partners",
        section="ANALYTICS",
        summary="Agent, hotel and OTA production, with commission and no-shows.",
        filters=("date_range", "venue", "partner"),
        columns=(
            Column("partner", "Partner", "text"),
            Column("bookings", "Bookings", "number", "right"),
            Column("tickets", "Tickets", "number", "right"),
            Column("gross_minor", "Gross sales", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("commission_minor", "Commission", _MONEY, "right", cost=True),
            Column("cancelled", "Cancelled", "number", "right"),
            Column("no_show", "No-show", "number", "right"),
        ),
    ),
    Report(
        key="exceptions",
        title="Exceptions",
        section="ANALYTICS",
        summary="Everything the platform thinks is worth a second look.",
        filters=("date_range", "venue"),
        columns=(
            Column("severity", "Severity", "status"),
            Column("title", "Finding", "text"),
            Column("detail", "Detail", "text"),
            Column("metric", "Measure", "text", "right"),
            Column("created_at", "Raised", "datetime"),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #

_OPERATIONS: tuple[Report, ...] = (
    Report(
        key="today_overview",
        title="Today Overview",
        section="OPERATIONS",
        summary="What is happening right now and what needs attention.",
        page="Operations Dashboard",
        dashboard=True,
        filters=("venue",),
        drill_to="arrivals",
    ),
    Report(
        key="arrivals",
        title="Expected Arrivals",
        section="OPERATIONS",
        summary="Who is due, who has arrived, who is late and who never came.",
        filters=("date_range", "venue", "session", "booking_status"),
        columns=(
            Column("visit_time", "Visit time", "time"),
            Column("booking_number", "Booking", "text"),
            Column("customer", "Customer", "pii"),
            Column("party_size", "Party", "number", "right"),
            Column("ticket_type", "Ticket type", "text"),
            Column("session", "Session", "text"),
            Column("state", "Status", "status"),
        ),
        drill_to="bookings",
    ),
    Report(
        key="bookings",
        title="Bookings",
        section="OPERATIONS",
        summary="Orders with payment and check-in state, and the actions allowed on them.",
        filters=("date_range", "date_basis", "venue", "channel", "booking_status",
                 "payment_status", "product"),
        columns=(
            Column("booking_number", "Booking", "text"),
            Column("created_at", "Booked", "datetime"),
            Column("visit_date", "Visit date", "date"),
            Column("customer", "Customer", "pii"),
            Column("tickets", "Tickets", "number", "right"),
            Column("net_minor", "Paid", _MONEY, "right"),
            Column("status", "Booking status", "status"),
            Column("checkin_state", "Check-in", "status"),
            Column("channel", "Channel", "text"),
        ),
    ),
    Report(
        key="admissions",
        title="Admissions",
        section="OPERATIONS",
        summary="Every scan and its decision, admitted or refused.",
        filters=("date_range", "venue", "device", "scan_result"),
        columns=(
            Column("at_local", "Scanned", "datetime"),
            Column("booking_number", "Booking", "text"),
            Column("ticket_number", "Ticket", "text"),
            Column("access_point", "Access point", "text"),
            Column("device", "Device", "text"),
            Column("decision", "Result", "status"),
            Column("reason", "Reason", "text"),
        ),
    ),
    Report(
        key="counter_sales",
        title="Counter Sales",
        section="OPERATIONS",
        summary="Over-the-counter production by counter, cashier and shift.",
        filters=("date_range", "venue", "counter", "staff", "payment_method"),
        columns=(
            Column("label", "Group", "text"),
            Column("transactions", "Transactions", "number", "right"),
            Column("tickets", "Tickets", "number", "right"),
            Column("gross_minor", "Gross sales", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("net_minor", "Net sales", _MONEY, "right"),
            Column("refund_minor", "Refund", _MONEY, "right"),
            Column("void_minor", "Void", _MONEY, "right"),
        ),
    ),
    Report(
        key="shifts",
        title="Shifts",
        section="OPERATIONS",
        summary="Cash reconciliation per shift, and any variance.",
        filters=("date_range", "venue", "counter", "staff"),
        columns=(
            Column("staff", "Staff", "text"),
            Column("counter_code", "Counter", "text"),
            Column("opened_at", "Opened", "date"),
            Column("closed_at", "Closed", "date"),
            Column("opening_float_minor", "Opening cash", _MONEY, "right"),
            Column("expected_minor", "Expected cash", _MONEY, "right"),
            Column("counted_minor", "Counted cash", _MONEY, "right"),
            Column("variance_minor", "Variance", _MONEY, "right"),
            Column("state", "Status", "status"),
        ),
    ),
    Report(
        key="refund_void",
        title="Refund & Void",
        section="OPERATIONS",
        summary="Money reversed, by whom, why, and whether the ticket had been used.",
        filters=("date_range", "venue", "staff", "payment_method"),
        columns=(
            Column("kind", "Type", "status"),
            Column("reference", "Reference", "text"),
            Column("booking_number", "Booking", "text"),
            Column("original_minor", "Original", _MONEY, "right"),
            Column("amount_minor", "Amount", _MONEY, "right"),
            Column("reason", "Reason", "text"),
            Column("method", "Method", "text"),
            Column("actor", "Staff", "text"),
            Column("approver", "Approver", "text"),
            Column("ticket_used", "Ticket used", "status"),
            Column("created_at", "When", "date"),
        ),
    ),
    Report(
        key="shows",
        title="Shows",
        section="OPERATIONS",
        summary="Today's timetable with reservations, attendance and status.",
        filters=("date_range", "venue", "show"),
        requires=("shows",),
        columns=(
            Column("show", "Show", "text"),
            Column("location", "Location", "text"),
            Column("start_time", "Time", "time"),
            Column("capacity", "Capacity", "number", "right"),
            Column("reserved", "Reserved", "number", "right"),
            Column("remaining", "Remaining", "number", "right"),
            Column("attendance", "Attendance", "number", "right"),
            Column("state", "Status", "status"),
        ),
    ),
    Report(
        key="seats",
        title="Seats",
        section="OPERATIONS",
        summary="Seat inventory per session: sold, held, blocked, occupancy.",
        filters=("date_range", "venue", "session"),
        requires=("seating",),
        columns=(
            Column("session", "Session", "text"),
            Column("total", "Total seats", "number", "right"),
            Column("available", "Available", "number", "right"),
            Column("held", "Held", "number", "right"),
            Column("sold", "Sold", "number", "right"),
            Column("blocked", "Blocked", "number", "right"),
            Column("occupancy_bp", "Occupancy", "percent", "right"),
        ),
    ),
    Report(
        key="devices",
        title="Devices",
        section="OPERATIONS",
        summary="Kiosk, POS, gate and printer health.",
        page="Devices",
        filters=("venue", "device"),
        columns=(
            Column("name", "Device", "text"),
            Column("kind", "Kind", "text"),
            Column("venue", "Venue", "text"),
            Column("state", "Status", "status"),
            Column("last_seen_at", "Last heartbeat", "date"),
            Column("last_transaction_at", "Last transaction", "date"),
            Column("last_error", "Last error", "text"),
            Column("app_version", "App version", "text"),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Finance
# --------------------------------------------------------------------------- #

_FINANCE: tuple[Report, ...] = (
    Report(
        key="sales",
        title="Sales",
        section="FINANCE",
        summary="Transaction-level sales, the ledger behind every summary figure.",
        filters=("date_range", "date_basis", "venue", "channel", "product", "ticket_type",
                 "segment", "partner", "payment_method", "booking_status", "currency"),
        columns=(
            Column("booking_number", "Booking", "text"),
            Column("created_at", "Order date", "date"),
            Column("visit_date", "Visit date", "date"),
            Column("channel", "Channel", "text"),
            Column("tickets", "Tickets", "number", "right"),
            Column("gross_minor", "Gross", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("service_charge_minor", "Service charge", _MONEY, "right"),
            Column("tax_minor", "VAT", _MONEY, "right"),
            Column("net_minor", "Net", _MONEY, "right"),
            Column("transaction_currency", "Txn currency", "text"),
            Column("exchange_rate_text", "Rate used", "text"),
            Column("status", "Status", "status"),
        ),
    ),
    Report(
        key="payments",
        title="Payments",
        section="FINANCE",
        summary="Method mix with success and failure rates.",
        filters=("date_range", "venue", "channel", "payment_method", "payment_status"),
        columns=(
            Column("label", "Method", "text"),
            Column("transactions", "Transactions", "number", "right"),
            Column("amount_minor", "Amount", _MONEY, "right"),
            Column("success_bp", "Success rate", "percent", "right"),
            Column("failure_bp", "Failure rate", "percent", "right"),
            Column("refund_minor", "Refund", _MONEY, "right"),
            Column("share_bp", "% of sales", "percent", "right"),
        ),
    ),
    Report(
        key="reconciliation",
        title="Payment Reconciliation",
        section="FINANCE",
        summary="Platform payments against the provider record, and the differences.",
        filters=("date_range", "venue", "payment_method", "payment_status"),
        columns=(
            Column("created_at", "Transaction date", "date"),
            Column("booking_number", "Order", "text"),
            Column("provider_ref", "Payment reference", "text"),
            Column("method", "Method", "text"),
            Column("provider", "Provider", "text"),
            Column("currency", "Currency", "text"),
            Column("amount_minor", "Amount", _MONEY, "right"),
            Column("state", "Status", "status"),
        ),
    ),
    Report(
        key="tax",
        title="Tax",
        section="FINANCE",
        summary="Taxable base, VAT and service charge, inclusive or exclusive.",
        filters=("date_range", "venue", "group_by"),
        columns=(
            Column("bucket", "Period", "text"),
            Column("gross_minor", "Gross sales", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("service_charge_minor", "Service charge", _MONEY, "right"),
            Column("taxable_minor", "Taxable amount", _MONEY, "right"),
            Column("tax_minor", "VAT", _MONEY, "right"),
            Column("refund_minor", "Refund adjustment", _MONEY, "right"),
            Column("net_minor", "Net", _MONEY, "right"),
        ),
    ),
    Report(
        key="tax_invoices",
        title="Tax Invoices",
        section="FINANCE",
        summary="Issued invoices and credit notes, in sequence.",
        page="Tax Invoices",
        filters=("date_range", "venue"),
        columns=(
            Column("number", "Invoice number", "text"),
            Column("issued_at", "Date", "date"),
            Column("booking_number", "Order", "text"),
            Column("customer", "Customer / company", "pii"),
            Column("tax_id", "Tax ID", "pii"),
            Column("tax_base_minor", "Before VAT", _MONEY, "right"),
            Column("tax_minor", "VAT", _MONEY, "right"),
            Column("total_minor", "Total", _MONEY, "right"),
            Column("status", "Status", "status"),
        ),
    ),
    Report(
        key="discounts",
        title="Discounts",
        section="FINANCE",
        summary="Every discount, separated by where it came from.",
        filters=("date_range", "venue", "channel", "promotion", "staff"),
        columns=(
            Column("source", "Discount type", "status"),
            Column("booking_number", "Order", "text"),
            Column("original_minor", "Original price", _MONEY, "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("net_minor", "Net price", _MONEY, "right"),
            Column("reference", "Promotion / reason", "text"),
            Column("actor", "Staff", "text"),
            Column("created_at", "When", "date"),
        ),
    ),
    Report(
        key="manual_discounts",
        title="Manual Discount Control",
        section="FINANCE",
        summary="Staff-applied discounts, with anything above threshold flagged.",
        filters=("date_range", "venue", "staff"),
        columns=(
            Column("actor", "Staff", "text"),
            Column("booking_number", "Order", "text"),
            Column("rate_bp", "Discount %", "percent", "right"),
            Column("discount_minor", "Discount", _MONEY, "right"),
            Column("reason", "Reason", "text"),
            Column("approver", "Approver", "text"),
            Column("created_at", "When", "date"),
            Column("flagged", "Above threshold", "status"),
        ),
    ),
    Report(
        key="complimentary",
        title="Complimentary Tickets",
        section="FINANCE",
        summary="Tickets given away, their value, and who authorised them.",
        filters=("date_range", "venue", "staff"),
        columns=(
            Column("ticket_type", "Ticket", "text"),
            Column("quantity", "Quantity", "number", "right"),
            Column("value_minor", "Normal value", _MONEY, "right"),
            Column("reason", "Reason", "text"),
            Column("actor", "Staff", "text"),
            Column("approver", "Approver", "text"),
            Column("recipient", "Customer / partner", "pii"),
            Column("visit_date", "Visit date", "date"),
            Column("checkin_state", "Check-in", "status"),
        ),
    ),
    Report(
        key="exchange_rates",
        title="Exchange Rates",
        section="FINANCE",
        summary="Configured rates and the periods they applied to.",
        page="Exchange Rates",
        filters=("date_range",),
        columns=(
            Column("pair", "Pair", "text"),
            Column("direction", "Direction", "text"),
            Column("effective_from", "Effective from", "date"),
            Column("effective_until", "Effective until", "date"),
            Column("status", "Status", "status"),
            Column("bookings", "Transactions", "number", "right"),
        ),
    ),
)


REPORTS: tuple[Report, ...] = _ANALYTICS + _OPERATIONS + _FINANCE
REPORTS_BY_KEY: dict[str, Report] = {report.key: report for report in REPORTS}

SECTION_LABELS: dict[str, str] = {
    "ANALYTICS": "Analytics",
    "OPERATIONS": "Operations",
    "FINANCE": "Finance",
}

#: Lead-time bands for the advance-booking report (§8). Upper bound is exclusive;
#: ``None`` means open-ended.
ADVANCE_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("Same day", 0, 1),
    ("1-3 days", 1, 4),
    ("4-7 days", 4, 8),
    ("8-30 days", 8, 31),
    ("30+ days", 31, None),
)

#: Grouping options for time series (§4).
GROUP_BY_OPTIONS: tuple[str, ...] = ("hourly", "daily", "weekly", "monthly")

#: Quick date ranges offered in the filter bar (§2).
DATE_PRESETS: tuple[str, ...] = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_year",
    "custom",
)

#: Comparison bases (§2).
COMPARE_OPTIONS: tuple[str, ...] = ("previous_period", "same_period_last_year", "none")


def reports_for_section(section: str) -> tuple[Report, ...]:
    return tuple(report for report in REPORTS if report.section == section)


def catalog() -> list[dict]:
    """The catalog as plain data, for the navigation and the filter bar."""
    sections: list[dict] = []
    for section, label in SECTION_LABELS.items():
        sections.append(
            {
                "key": section,
                "label": label,
                "reports": [
                    {
                        "key": report.key,
                        "title": report.title,
                        "summary": report.summary,
                        "page": report.page,
                        "dashboard": report.dashboard,
                        "filters": list(report.filters),
                        "requires": list(report.requires),
                        "drill_to": report.drill_to,
                        "columns": [
                            {
                                "key": column.key,
                                "label": column.label,
                                "kind": column.kind,
                                "align": column.align,
                                "cost": column.cost,
                            }
                            for column in report.columns
                        ],
                    }
                    for report in reports_for_section(section)
                ],
            }
        )
    return sections


__all__ = [
    "ADVANCE_BANDS",
    "COMPARE_OPTIONS",
    "Column",
    "DATE_PRESETS",
    "GROUP_BY_OPTIONS",
    "REPORTS",
    "REPORTS_BY_KEY",
    "SECTION_LABELS",
    "Report",
    "catalog",
    "reports_for_section",
]
