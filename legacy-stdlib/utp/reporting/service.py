"""ReportingService: the only way into the numbers.

Everything a report needs to be trustworthy is enforced here rather than in the
metric functions, so a new report cannot forget it:

* **Permission per report.** The report's own page must be held for VIEW; export
  additionally needs ``EXPORT`` and scheduling needs ``SCHEDULE_REPORT`` (§39).
  Checked server-side every time, because a hidden menu item is a convenience,
  not a control (R42.1).
* **Venue scope in the query.** ``Scope`` is built from the principal's role
  assignments, so out-of-scope venues never reach a SUM (R43.7). A principal with
  no venue in scope gets an empty report, never the whole tenant.
* **Masking at the API boundary.** Personal data is masked unless ``VIEW_PII``
  and cost columns are dropped without ``VIEW_COST``, in the response rather than
  in the UI (R42.9, §40). The executive dashboard carries no customer identity at
  all — it has no operational need for it (R70.8).
* **Export is audited** with the filters and the row count (R41.7).

Comparison figures come from re-running the same aggregation over the shifted
window, not from a stored snapshot, so "vs previous month" always matches what
the previous month's report would say.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..core.clock import to_iso
from ..core.errors import NotFound, ValidationError
from ..core.i18n import text as i18n_text
from ..core.db import decode
from ..core.ids import new_id
from .definitions import (
    COMPARE_OPTIONS,
    DATE_PRESETS,
    GROUP_BY_OPTIONS,
    REPORTS_BY_KEY,
    catalog,
)
from . import rows as detail
from .exceptions import ExceptionEngine
from .export import to_csv, to_printable_html
from .metrics import Metrics, Scope


class ReportingService:
    """Runs reports and the two dashboards."""

    def __init__(self, db, clock, audit, authz, config) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config
        self.metrics = Metrics(db)
        self.exceptions = ExceptionEngine(db, self.metrics, config)
        #: Wired by the composition root where available.
        self.tenancy: Any = None

    # ------------------------------------------------------------------ #
    # Catalog
    # ------------------------------------------------------------------ #

    def catalog_for(self, ctx) -> dict[str, Any]:
        """The navigation, filtered to what this principal may actually open.

        A report the user cannot view is omitted rather than shown disabled: a
        menu full of locked doors tells them nothing useful, and R42.7 requires
        pages they cannot VIEW to be absent from navigation.
        """
        self.authz.require_authenticated(ctx)
        sections = []
        for section in catalog():
            visible = [
                report
                for report in section["reports"]
                if self.authz.can_page(ctx, report["page"], "VIEW")
            ]
            if visible:
                sections.append({**section, "reports": visible})
        return {
            "sections": sections,
            "date_presets": list(DATE_PRESETS),
            "group_by_options": list(GROUP_BY_OPTIONS),
            "compare_options": list(COMPARE_OPTIONS),
            "can_export": self.authz.can_action(ctx, "EXPORT"),
            "can_schedule": self.authz.can_action(ctx, "SCHEDULE_REPORT"),
            "can_view_pii": self.authz.can_action(ctx, "VIEW_PII"),
            "can_view_cost": self.authz.can_action(ctx, "VIEW_COST"),
            "venues": self._venue_options(ctx),
        }

    def _venue_options(self, ctx) -> list[dict[str, str]]:
        venue_ids = self._scoped_venues(ctx)
        if not venue_ids:
            return []
        placeholders = ",".join("?" for _ in venue_ids)
        rows = self.db.query(
            f"SELECT id, code, name_json FROM venues WHERE tenant_id = ? AND id IN ({placeholders})"
            " ORDER BY code",
            [ctx.tenant_id, *venue_ids],
        )
        return [
            {
                "id": row["id"],
                "code": row["code"],
                "name": i18n_text(decode(row["name_json"], {}), ctx.language or "en", fallback=row["code"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Scope
    # ------------------------------------------------------------------ #

    def _scoped_venues(self, ctx) -> list[str]:
        """Venues this principal may see. Empty means nothing, never everything."""
        scoped = self.authz.scoped_venue_ids(ctx)
        if scoped is None:
            # Organization-wide or platform scope: every venue in the tenant.
            rows = self.db.query("SELECT id FROM venues WHERE tenant_id = ?", (ctx.tenant_id,))
            return [row["id"] for row in rows]
        return list(scoped)

    def resolve_range(self, *, preset: str | None, date_from: str | None, date_to: str | None,
                      today: _dt.date) -> tuple[str, str]:
        """Turn a quick preset into a concrete window (§2)."""
        if preset in (None, "", "custom"):
            if not date_from or not date_to:
                # Default to this month rather than erroring: a dashboard should
                # open with something meaningful on it.
                start = today.replace(day=1)
                return start.isoformat(), today.isoformat()
            if date_from > date_to:
                raise ValidationError({"date_from": "The start date must not be after the end date."})
            return date_from, date_to
        if preset == "today":
            return today.isoformat(), today.isoformat()
        if preset == "yesterday":
            day = today - _dt.timedelta(days=1)
            return day.isoformat(), day.isoformat()
        if preset == "this_week":
            start = today - _dt.timedelta(days=today.weekday())
            return start.isoformat(), today.isoformat()
        if preset == "last_week":
            this_monday = today - _dt.timedelta(days=today.weekday())
            start = this_monday - _dt.timedelta(days=7)
            return start.isoformat(), (this_monday - _dt.timedelta(days=1)).isoformat()
        if preset == "this_month":
            return today.replace(day=1).isoformat(), today.isoformat()
        if preset == "last_month":
            first_this = today.replace(day=1)
            last_prev = first_this - _dt.timedelta(days=1)
            return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
        if preset == "this_year":
            return today.replace(month=1, day=1).isoformat(), today.isoformat()
        raise ValidationError({"date_preset": "That date range is not one of the available options."})

    @staticmethod
    def comparison_range(date_from: str, date_to: str, basis: str) -> tuple[str, str] | None:
        """The window a comparison figure is measured against (§2)."""
        if basis in (None, "", "none"):
            return None
        start = _dt.date.fromisoformat(date_from)
        end = _dt.date.fromisoformat(date_to)
        if basis == "same_period_last_year":
            try:
                return (
                    start.replace(year=start.year - 1).isoformat(),
                    end.replace(year=end.year - 1).isoformat(),
                )
            except ValueError:      # 29 February
                return (
                    (start - _dt.timedelta(days=365)).isoformat(),
                    (end - _dt.timedelta(days=365)).isoformat(),
                )
        span = (end - start).days + 1
        previous_end = start - _dt.timedelta(days=1)
        return (previous_end - _dt.timedelta(days=span - 1)).isoformat(), previous_end.isoformat()

    def build_scope(self, ctx, filters: dict[str, Any]) -> Scope:
        """Assemble the scope for a request, intersecting any venue filter with the grant."""
        permitted = self._scoped_venues(ctx)
        requested = filters.get("venue")
        if requested:
            wanted = requested if isinstance(requested, (list, tuple)) else [requested]
            # Intersection, not replacement: asking for a venue you do not hold
            # must narrow the result to nothing, not widen it (R43.4).
            permitted = [venue for venue in permitted if venue in set(wanted)]
        venue_id = permitted[0] if permitted else None
        timezone, currency = self._venue_locale(ctx, venue_id)
        today = _dt.datetime.now(_tz(timezone)).date()
        date_from, date_to = self.resolve_range(
            preset=filters.get("date_preset"),
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
            today=today,
        )
        merged = dict(filters)
        merged.setdefault("language", ctx.language or "en")
        return Scope(
            tenant_id=ctx.tenant_id,
            venue_ids=permitted,
            date_from=date_from,
            date_to=date_to,
            timezone=timezone,
            currency=currency,
            filters=merged,
        )

    def _venue_locale(self, ctx, venue_id: str | None) -> tuple[str, str]:
        if not venue_id:
            return "UTC", "THB"
        row = self.db.query_one(
            "SELECT timezone, currency FROM venues WHERE tenant_id = ? AND id = ?",
            (ctx.tenant_id, venue_id),
        )
        if row is None:
            return "UTC", "THB"
        return row["timezone"] or "UTC", row["currency"] or "THB"

    # ------------------------------------------------------------------ #
    # Dashboards
    # ------------------------------------------------------------------ #

    def executive_overview(self, ctx, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Business health in one payload (§2-§16, R70)."""
        self.authz.require_page(ctx, "Dashboard", "VIEW")
        filters = dict(filters or {})
        scope = self.build_scope(ctx, filters)
        totals = self.metrics.totals(scope)

        compare = self.comparison_range(
            scope.date_from, scope.date_to, filters.get("compare_with") or "previous_period"
        )
        previous: dict[str, Any] = {}
        if compare:
            prior = Scope(
                tenant_id=scope.tenant_id,
                venue_ids=scope.venue_ids,
                date_from=compare[0],
                date_to=compare[1],
                timezone=scope.timezone,
                currency=scope.currency,
                filters=scope.filters,
            )
            previous = self.metrics.totals(prior)

        group_by = filters.get("group_by") or self._auto_group_by(scope)
        mix = self.metrics.visitor_mix(scope)
        return {
            "meta": self._meta(ctx, scope, compare=compare, group_by=group_by),
            "kpis": self._kpis(totals, previous, scope.currency),
            "revenue_series": self.metrics.revenue_series(scope, group_by=group_by),
            "channels": self.metrics.by_channel(scope),
            "visitor_segments": mix["segments"],
            "pricing_groups": mix["pricing_groups"],
            "top_products": self.metrics.by_product(scope)[:8],
            "promotions": self.metrics.promotions(scope)[:6],
            "partners": self.metrics.partners(scope)[:6],
            "capacity": self.metrics.capacity_totals(scope),
            "peak_time": self.metrics.peak_time(scope, measure="visitors"),
            "advance_booking": self.metrics.advance_booking(scope),
            "exceptions": self.exceptions.evaluate(ctx, scope)[:6],
        }

    def _auto_group_by(self, scope: Scope) -> str:
        """Pick a sensible granularity so a year does not render 365 columns."""
        span = (_dt.date.fromisoformat(scope.date_to) - _dt.date.fromisoformat(scope.date_from)).days
        if span <= 1:
            return "hourly"
        if span <= 62:
            return "daily"
        if span <= 365:
            return "weekly"
        return "monthly"

    _KPI_SPEC: tuple[tuple[str, str, str, str], ...] = (
        ("gross_sales", "Gross Sales", "gross_minor", "money"),
        ("net_sales", "Net Sales", "net_minor", "money"),
        ("visitors", "Visitors", "visitors", "number"),
        ("tickets", "Tickets Sold", "tickets", "number"),
        ("bookings", "Bookings", "bookings", "number"),
        ("atv", "Average Transaction", "atv_minor", "money"),
        ("rpv", "Revenue per Visitor", "revenue_per_visitor_minor", "money"),
        ("capacity", "Capacity Utilization", "capacity_utilization_bp", "percent"),
        ("refunds", "Refund Amount", "refund_minor", "money"),
        ("discounts", "Discount Amount", "discount_minor", "money"),
    )

    def _kpis(self, totals: dict[str, Any], previous: dict[str, Any], currency: str) -> list[dict[str, Any]]:
        """KPI cards with their comparison (§3).

        ``lower_is_better`` matters: a 20% rise in refunds is not good news, and a
        card that paints it green because the arrow points up is worse than no
        card at all.
        """
        cards = []
        for key, label, source, kind in self._KPI_SPEC:
            current = int(totals.get(source) or 0)
            prior = int(previous.get(source) or 0) if previous else None
            change_bp = None
            if prior:
                change_bp = round((current - prior) * 10_000 / prior)
            elif previous and current:
                change_bp = 10_000        # from nothing to something
            cards.append(
                {
                    "key": key,
                    "label": label,
                    "value": current,
                    "kind": kind,
                    "currency": currency if kind == "money" else None,
                    "previous": prior,
                    "change_bp": change_bp,
                    "direction": "flat" if not change_bp else ("up" if change_bp > 0 else "down"),
                    "lower_is_better": key in ("refunds", "discounts"),
                    "drill_to": {
                        "gross_sales": "revenue", "net_sales": "revenue", "visitors": "visitors",
                        "tickets": "products", "bookings": "bookings", "atv": "revenue",
                        "rpv": "visitors", "capacity": "capacity", "refunds": "refund_void",
                        "discounts": "discounts",
                    }.get(key),
                }
            )
        return cards

    def operations_today(self, ctx, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """What is happening now and what needs attention (§17-§20, R71.1)."""
        self.authz.require_page(ctx, "Operations Dashboard", "VIEW")
        filters = dict(filters or {})
        filters.setdefault("date_preset", "today")
        scope = self.build_scope(ctx, filters)
        totals = self.metrics.totals(scope)
        tickets = self.metrics.ticket_counts(scope)
        arrivals = self._arrivals(ctx, scope)
        counts = {"ARRIVING": 0, "CHECKED_IN": 0, "LATE": 0, "NO_SHOW": 0}
        for row in arrivals:
            counts[row["state_code"]] = counts.get(row["state_code"], 0) + 1
        capacity = self.metrics.capacity_totals(scope)
        return {
            "meta": self._meta(ctx, scope, compare=None, group_by="hourly"),
            "kpis": [
                _tile("sales", "Today's Sales", totals["net_minor"], "money", scope.currency),
                _tile("visitors", "Checked In", tickets["admitted"], "number"),
                _tile("bookings", "Today's Bookings", totals["bookings"], "number"),
                _tile("expected", "Not Yet Arrived", counts["ARRIVING"] + counts["LATE"], "number"),
                _tile("tickets", "Tickets Issued", tickets["issued"], "number"),
                _tile("no_show", "No-show", counts["NO_SHOW"], "number", tone="warn"),
                _tile("cancelled", "Cancelled", totals["cancelled"], "number", tone="warn"),
                _tile("refunded", "Refunded", totals["refund_minor"], "money", scope.currency, tone="warn"),
                _tile(
                    "capacity",
                    "Capacity Remaining",
                    max(capacity["capacity"] - capacity["reserved"], 0),
                    "number",
                ),
            ],
            "arrivals": arrivals[:40],
            "arrival_counts": counts,
            "capacity_rows": self.metrics.capacity_rows(scope),
            "gate": self.metrics.gate_summary(scope),
            "gate_activity": self.metrics.gate_activity(scope, limit=25),
            "payments": self.metrics.payments_by_method(scope),
            "devices": self._devices(ctx, scope),
            "shows": self._shows(ctx, scope),
            "exceptions": self.exceptions.evaluate(ctx, scope),
        }

    # ------------------------------------------------------------------ #
    # Report runner
    # ------------------------------------------------------------------ #

    def run(self, ctx, report_key: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one report. The single entry point for every table in the module."""
        report = REPORTS_BY_KEY.get(report_key)
        if report is None:
            raise NotFound()
        self.authz.require_page(ctx, report.page, "VIEW")
        filters = dict(filters or {})
        scope = self.build_scope(ctx, filters)

        if report.dashboard:
            if report_key == "executive_overview":
                return self.executive_overview(ctx, filters)
            if report_key == "today_overview":
                return self.operations_today(ctx, filters)
            if report_key == "peak_time":
                return {
                    "meta": self._meta(ctx, scope, compare=None, group_by="daily"),
                    "peak_time": self.metrics.peak_time(
                        scope, measure=filters.get("measure") or "visitors"
                    ),
                }

        rows = self._rows_for(ctx, report_key, scope)
        columns = [
            {
                "key": column.key,
                "label": column.label,
                "kind": column.kind,
                "align": column.align,
                "cost": column.cost,
            }
            for column in report.columns
        ]
        columns, rows = self._apply_visibility(ctx, columns, rows)
        meta = self._meta(
            ctx, scope, compare=None,
            group_by=filters.get("group_by") or report.default_group_by,
        )
        reconciliation = self._reconciliation_note(report_key, scope, rows)
        if reconciliation:
            meta["reconciliation"] = reconciliation
        return {
            "report": {"key": report.key, "title": report.title, "section": report.section,
                       "summary": report.summary, "drill_to": report.drill_to},
            "meta": meta,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "totals": self._column_totals(columns, rows),
        }

    def _reconciliation_note(
        self, report_key: str, scope: Scope, rows: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Explain a legitimate gap between a line-level and an order-level total.

        A product breakdown sums *booking items*; the headline sums *orders*. Those
        differ by the order-level rounding adjustment the charge engine applies so
        the amount charged is a payable figure (R5.5). The difference is real, small
        and explainable — so it is stated, because a reader who spots an
        unexplained discrepancy is right to distrust the whole report (R70.9).
        """
        if report_key not in ("products", "visitors"):
            return None
        order_net = self.metrics.totals(scope)["net_minor"]
        line_net = sum(int(row.get("net_minor") or 0) for row in rows)
        difference = order_net - line_net
        if difference == 0:
            return None

        # Name the cause rather than leaving the reader to wonder. The usual one is
        # a partially refunded order: the platform deactivates its booking items, so
        # the revenue it retains belongs to no product line and cannot be attributed
        # to one.
        where, params = scope.booking_where(statuses=("PARTIALLY_REFUNDED",))
        unattributed = self.db.query_one(
            f"""
            SELECT COUNT(*) AS orders,
                   COALESCE(SUM(COALESCE(NULLIF(b.base_currency_minor, 0), b.net_minor)
                                - COALESCE(b.refunded_minor, 0)), 0) AS net_minor
            FROM bookings b {where}
              AND NOT EXISTS (SELECT 1 FROM booking_items bi
                               WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id
                                 AND bi.state = 'ACTIVE')
            """,
            params,
        )
        orders = int(unattributed["orders"] or 0)
        causes = []
        if orders:
            causes.append(
                f"{orders} partially refunded order(s) whose lines are no longer "
                f"active, so their retained revenue is not attributable to a product"
            )
        causes.append("order-level rounding applied so the charged amount is payable")
        return {
            "order_total_minor": order_net,
            "line_total_minor": line_net,
            "difference_minor": difference,
            "unattributed_orders": orders,
            "explanation": (
                "Line totals are per booking item; the headline is per order. "
                "They differ by " + "; and ".join(causes) + "."
            ),
        }

    def _rows_for(self, ctx, key: str, scope: Scope) -> list[dict[str, Any]]:
        group_by = scope.filters.get("group_by") or "daily"
        if key == "revenue":
            return self.metrics.revenue_series(scope, group_by=group_by)
        if key == "channels":
            return self.metrics.by_channel(scope)
        if key == "products":
            return self.metrics.by_product(scope)
        if key == "visitors":
            return self.metrics.visitor_mix(scope)["segments"]
        if key == "capacity":
            return self.metrics.capacity_rows(scope)
        if key == "advance_booking":
            return self.metrics.advance_booking(scope)
        if key == "promotions":
            return self.metrics.promotions(scope)
        if key == "partners":
            return self.metrics.partners(scope)
        if key == "payments":
            return self.metrics.payments_by_method(scope)
        if key == "tax":
            return self.metrics.tax_series(scope, group_by=group_by)
        if key == "admissions":
            return self.metrics.gate_activity(scope, limit=500)
        if key == "exceptions":
            return [
                {**finding, "created_at": self._now_local(scope)}
                for finding in self.exceptions.evaluate(ctx, scope)
            ]
        if key == "arrivals":
            return self._arrivals(ctx, scope)
        if key == "bookings":
            return self._bookings(ctx, scope)
        if key == "sales":
            return self._sales(ctx, scope)
        if key == "shifts":
            return self._shifts(ctx, scope)
        if key == "counter_sales":
            return self._counter_sales(scope)
        if key == "refund_void":
            return self._refunds(ctx, scope)
        if key == "discounts":
            return self._discounts(scope)
        if key == "manual_discounts":
            return self._manual_discounts(ctx, scope)
        if key == "complimentary":
            return self._complimentary(ctx, scope)
        if key == "reconciliation":
            return self._reconciliation(scope)
        if key == "tax_invoices":
            return self._tax_invoices(ctx, scope)
        if key == "exchange_rates":
            return self._exchange_rates(scope)
        if key == "devices":
            return self._devices(ctx, scope)
        if key == "shows":
            return self._shows(ctx, scope)
        if key == "seats":
            return self._seats(scope)
        return []

    # ------------------------------------------------------------------ #
    # Visibility: masking and cost
    # ------------------------------------------------------------------ #

    def _apply_visibility(
        self, ctx, columns: list[dict[str, Any]], rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Mask PII and drop cost columns, in the response not the UI (R42.9)."""
        may_see_pii = self.authz.can_action(ctx, "VIEW_PII")
        may_see_cost = self.authz.can_action(ctx, "VIEW_COST")

        if not may_see_cost:
            cost_keys = {column["key"] for column in columns if column.get("cost")}
            if cost_keys:
                columns = [column for column in columns if not column.get("cost")]
                rows = [{k: v for k, v in row.items() if k not in cost_keys} for row in rows]

        pii_keys = [column["key"] for column in columns if column["kind"] == "pii"]
        if pii_keys and not may_see_pii:
            rows = [
                {**row, **{key: _mask(row.get(key)) for key in pii_keys if key in row}}
                for row in rows
            ]
        elif pii_keys and rows:
            # Reading unmasked personal data is itself auditable (R12.24).
            self.audit.record(
                ctx,
                "PII_ACCESS",
                target_type="report",
                target_id="reporting",
                new={"rows": len(rows), "fields": pii_keys},
            )
        return columns, rows

    @staticmethod
    def _column_totals(columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Footer totals for money and count columns.

        Rates are deliberately not summed — adding percentages produces a number
        that means nothing.
        """
        totals: dict[str, Any] = {}
        for column in columns:
            if column["kind"] in ("money", "number"):
                values = [row.get(column["key"]) for row in rows]
                numeric = [int(value) for value in values if isinstance(value, (int, float))]
                if numeric:
                    totals[column["key"]] = sum(numeric)
        return totals

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    def export(self, ctx, report_key: str, *, fmt: str = "csv",
               filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render a report for download. Requires ``EXPORT`` and is audited (R41.7)."""
        report = REPORTS_BY_KEY.get(report_key)
        if report is None:
            raise NotFound()
        self.authz.require_page(ctx, report.page, "VIEW")
        self.authz.require_action(ctx, "EXPORT", target_type="report", target_id=report_key)
        if fmt not in ("csv", "print"):
            raise ValidationError({"format": "Choose CSV or a printable document."})
        if report.dashboard:
            raise ValidationError(
                {"format": "A dashboard cannot be exported as a table. Open one of its reports."}
            )

        result = self.run(ctx, report_key, filters)
        meta = dict(result["meta"])
        meta["row_count"] = result["row_count"]
        meta["masked"] = not self.authz.can_action(ctx, "VIEW_PII")
        meta["subtitle"] = report.summary

        self.audit.record(
            ctx,
            "EXPORT",
            target_type="report",
            target_id=report_key,
            new={
                "format": fmt,
                "row_count": result["row_count"],
                "filters": meta.get("filters"),
                "date_from": meta.get("date_from"),
                "date_to": meta.get("date_to"),
                "venues": meta.get("venue_ids"),
            },
        )
        if fmt == "csv":
            body = to_csv(report_title=report.title, columns=result["columns"],
                          rows=result["rows"], meta=meta)
            return {"content_type": "text/csv; charset=utf-8",
                    "filename": f"{report_key}-{meta['date_from']}-to-{meta['date_to']}.csv",
                    "body": body}
        body = to_printable_html(report_title=report.title, columns=result["columns"],
                                 rows=result["rows"], meta=meta, auto_print=True)
        return {"content_type": "text/html; charset=utf-8",
                "filename": f"{report_key}.html", "body": body}

    # ------------------------------------------------------------------ #
    # Saved views (§36)
    # ------------------------------------------------------------------ #

    def save_view(self, ctx, *, report_key: str, name: str,
                  filters: dict[str, Any], make_default: bool = False) -> dict[str, Any]:
        report = REPORTS_BY_KEY.get(report_key)
        if report is None:
            raise NotFound()
        self.authz.require_page(ctx, report.page, "VIEW")
        label = (name or "").strip()
        if not label:
            raise ValidationError({"name": "Give the view a name so you can find it again."})
        staff_id = self.authz.require_authenticated(ctx)
        import json

        view_id = new_id("rvw")
        with self.db.transaction():
            if make_default:
                self.db.execute(
                    "UPDATE report_views SET is_default = 0 WHERE tenant_id = ? AND staff_id = ?"
                    " AND report_key = ?",
                    (ctx.tenant_id, staff_id, report_key),
                )
            self.db.insert(
                "report_views",
                {
                    "id": view_id,
                    "tenant_id": ctx.tenant_id,
                    "staff_id": staff_id,
                    "report_key": report_key,
                    "name": label,
                    "filters_json": json.dumps(filters or {}, separators=(",", ":")),
                    "is_default": 1 if make_default else 0,
                    "created_at": to_iso(self.clock.now()),
                },
            )
        return {"id": view_id, "name": label, "report_key": report_key, "is_default": make_default}

    def list_views(self, ctx, *, report_key: str | None = None) -> list[dict[str, Any]]:
        staff_id = self.authz.require_authenticated(ctx)
        sql = "SELECT * FROM report_views WHERE tenant_id = ? AND staff_id = ?"
        params: list[Any] = [ctx.tenant_id, staff_id]
        if report_key:
            sql += " AND report_key = ?"
            params.append(report_key)
        sql += " ORDER BY is_default DESC, name"
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "report_key": row["report_key"],
                "filters": decode(row["filters_json"], {}),
                "is_default": bool(row["is_default"]),
            }
            for row in self.db.query(sql, params)
        ]

    def delete_view(self, ctx, view_id: str) -> dict[str, Any]:
        staff_id = self.authz.require_authenticated(ctx)
        row = self.db.query_one(
            "SELECT id FROM report_views WHERE tenant_id = ? AND id = ? AND staff_id = ?",
            (ctx.tenant_id, view_id, staff_id),
        )
        if row is None:
            raise NotFound()
        self.db.execute(
            "DELETE FROM report_views WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, view_id)
        )
        return {"deleted": True, "id": view_id}

    # ------------------------------------------------------------------ #
    # Meta
    # ------------------------------------------------------------------ #

    def _meta(self, ctx, scope: Scope, *, compare: tuple[str, str] | None,
              group_by: str) -> dict[str, Any]:
        """Provenance for every response: window, scope, currency, freshness (§49)."""
        return {
            "date_from": scope.date_from,
            "date_to": scope.date_to,
            "compare_from": compare[0] if compare else None,
            "compare_to": compare[1] if compare else None,
            "group_by": group_by,
            "timezone": scope.timezone,
            "currency": scope.currency,
            "venue_ids": list(scope.venue_ids),
            "venue_names": [venue["name"] for venue in self._venue_options(ctx)],
            "filters": {
                key: value
                for key, value in scope.filters.items()
                if key != "language" and value not in (None, "", [], ())
            },
            "generated_at": to_iso(self.clock.now()),
            "generated_local": self._now_local(scope),
            "masked": not self.authz.can_action(ctx, "VIEW_PII"),
        }

    def _now_local(self, scope: Scope) -> str:
        from ..core.clock import local

        return local(self.clock.now(), scope.timezone).isoformat(timespec="seconds")

    # ------------------------------------------------------------------ #
    # Detail rows — thin delegations to the row queries
    # ------------------------------------------------------------------ #

    def _arrivals(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.arrivals(self.db, scope)

    def _bookings(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.bookings(self.db, scope)

    def _sales(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.sales(self.db, scope)

    def _shifts(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.shifts(self.db, scope)

    def _counter_sales(self, scope: Scope) -> list[dict[str, Any]]:
        return detail.counter_sales(self.db, scope)

    def _refunds(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.refunds_and_voids(self.db, scope)

    def _discounts(self, scope: Scope) -> list[dict[str, Any]]:
        return detail.discounts(self.db, scope)

    def _manual_discounts(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        threshold = self.config.get(ctx, "reporting.threshold.manual_discount_bp")
        if threshold is not None:
            scope.filters["manual_discount_threshold_bp"] = int(threshold)
        return detail.manual_discounts(self.db, scope)

    def _complimentary(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.complimentary(self.db, scope)

    def _reconciliation(self, scope: Scope) -> list[dict[str, Any]]:
        return detail.reconciliation(self.db, scope)

    def _tax_invoices(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.tax_invoices(self.db, scope)

    def _exchange_rates(self, scope: Scope) -> list[dict[str, Any]]:
        return detail.exchange_rates(self.db, scope)

    def _devices(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.devices(self.db, scope)

    def _shows(self, ctx, scope: Scope) -> list[dict[str, Any]]:
        return detail.shows(self.db, scope)

    def _seats(self, scope: Scope) -> list[dict[str, Any]]:
        return detail.seats(self.db, scope)


def _tile(key: str, label: str, value: Any, kind: str, currency: str | None = None,
          *, tone: str = "normal") -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "kind": kind,
            "currency": currency, "tone": tone}


def _mask(value: Any) -> str:
    """Mask a personal field the way the spec shows: ``John D***``, ``jo***@mail.com``."""
    text = str(value or "")
    if not text:
        return ""
    if "@" in text:
        local_part, _, domain = text.partition("@")
        keep = local_part[:2]
        return f"{keep}{'*' * max(len(local_part) - 2, 3)}@{domain}"
    parts = text.split()
    if len(parts) == 1:
        return parts[0][:1] + "*" * max(len(parts[0]) - 1, 3)
    return f"{parts[0]} {parts[-1][:1]}{'*' * 3}"


def _tz(name: str):
    from ..core.clock import timezone_for

    return timezone_for(name)


__all__ = ["ReportingService"]
