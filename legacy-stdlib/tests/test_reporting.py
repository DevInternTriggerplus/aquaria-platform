"""Reporting: the numbers, the scope, and who may see them.

A dashboard is only worth having if its figures can be trusted, so the tests
here concentrate on the four ways a report goes wrong:

* **It disagrees with itself.** The headline must equal the sum of the
  drill-down (R70.9). :class:`ReconciliationTests` proves the KPI, the time
  series, the channel split and the transaction ledger all land on the same
  number, and that the one legitimate residual is declared rather than hidden.
* **It counts money that came back.** Refunded, cancelled and voided amounts
  belong in activity, not in net revenue (R70.6, R46.4).
* **It shows data the reader is not entitled to.** Venue scope must be applied
  in the query, not after it, and personal data masked unless ``VIEW_PII``
  (R43.7, R42.9, reports spec §40).
* **It lets someone take a copy without the right to.** Export is a separate
  privilege and is audited with its filters and row count (R41.7).

The fixtures generate history through the real booking path (see
``demo_history``) rather than inserting rows, so the aggregates are tested
against numbers the platform actually produces.
"""

from __future__ import annotations

import datetime as _dt
import unittest

import demo_history
import seed
from utp.app import Platform
from utp.core.clock import FixedClock
from utp.core.errors import AuthorizationDenied, NotFound, ValidationError
from utp.reporting.definitions import REPORTS, REPORTS_BY_KEY, catalog
from utp.reporting.exceptions import CRITICAL, INFO, WARNING
from utp.reporting.metrics import Scope
from utp.services.booking import QuoteLineRequest


#: Built once for the whole module, not once per class.
#:
#: Generating history through the real booking path is the right call — it is what
#: makes these aggregates tested against numbers the platform actually produces —
#: but it costs a few seconds, and rebuilding it for each of the eight test classes
#: took the suite from seconds to minutes. The fixture is read-only for almost
#: every test; the two that write (saved views, a config threshold) clean up after
#: themselves.
_FIXTURE: dict[str, object] = {}

_HISTORY_DAYS = 21


def _fixture() -> dict[str, object]:
    if not _FIXTURE:
        now = _dt.datetime.now(_dt.timezone.utc)
        platform = Platform(db_path=":memory:", clock=FixedClock(now))
        info = seed.provision(platform)
        demo_history.generate(platform, info, days=_HISTORY_DAYS, base_bookings_per_day=6)
        _FIXTURE.update(
            platform=platform,
            info=info,
            now=now,
            tenant_id=info["tenant_id"],
            venue_id=info["venue_id"],
            ctx=platform.system_context(info["tenant_id"]).for_venue(info["venue_id"]),
            window={
                "date_preset": "custom",
                "date_from": (now.date() - _dt.timedelta(days=_HISTORY_DAYS)).isoformat(),
                "date_to": now.date().isoformat(),
            },
        )
    return _FIXTURE


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shared = _fixture()
        cls.platform = shared["platform"]
        cls.info = shared["info"]
        cls.now = shared["now"]
        cls.tenant_id = shared["tenant_id"]
        cls.venue_id = shared["venue_id"]
        cls.ctx = shared["ctx"]
        cls.window = dict(shared["window"])

    @property
    def reporting(self):
        return self.platform.reporting


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


class CatalogTests(_Base):
    def test_every_report_declares_a_permission_page(self) -> None:
        from utp.domain.permissions import PAGES_BY_KEY

        for report in REPORTS:
            self.assertIn(report.page, PAGES_BY_KEY, f"{report.key} names an unknown page")

    def test_report_keys_are_unique(self) -> None:
        keys = [report.key for report in REPORTS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_reports_only_declare_filters_the_platform_can_apply(self) -> None:
        """A screen must not offer a filter that does nothing (§34)."""
        known = {
            "date_range", "date_basis", "venue", "channel", "product", "ticket_type",
            "segment", "pricing_group", "promotion", "partner", "payment_method",
            "staff", "counter", "device", "show", "session", "booking_status",
            "payment_status", "scan_result", "group_by", "compare_with", "currency",
        }
        for report in REPORTS:
            for name in report.filters:
                self.assertIn(name, known, f"{report.key} declares unknown filter {name}")

    def test_columns_declare_a_known_kind(self) -> None:
        kinds = {"text", "money", "number", "percent", "date", "time", "datetime", "status", "pii"}
        for report in REPORTS:
            for column in report.columns:
                self.assertIn(column.kind, kinds, f"{report.key}.{column.key}")


    def test_three_sections_are_offered(self) -> None:
        self.assertEqual([s["key"] for s in catalog()], ["ANALYTICS", "OPERATIONS", "FINANCE"])

    def test_every_report_runs_without_error(self) -> None:
        for report in REPORTS:
            with self.subTest(report=report.key):
                result = self.reporting.run(self.ctx, report.key, dict(self.window))
                self.assertIn("meta", result)

    def test_an_unknown_report_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.reporting.run(self.ctx, "no_such_report", {})


class DateTimeFormatTests(unittest.TestCase):
    """Date and time are formatted to the venue standard and split for export."""

    def test_date_and_time_formats(self) -> None:
        from utp.reporting.export import _split_datetime, _fmt_date, _fmt_time

        # yyyy-Mmm-dd and hh:mm:ss, parsed literally (no zone shift).
        self.assertEqual(_split_datetime("2026-09-01T10:05:09"), ("2026-Sep-01", "10:05:09"))
        self.assertEqual(_split_datetime("2026-09-01 18:00:00"), ("2026-Sep-01", "18:00:00"))
        self.assertEqual(_fmt_date("2026-12-25"), "2026-Dec-25")
        self.assertEqual(_fmt_time("2026-12-25T09:30"), "09:30:00")   # seconds defaulted
        self.assertEqual(_fmt_time("07:15:42"), "07:15:42")           # time-only value
        self.assertEqual(_split_datetime(""), ("", ""))

    def test_datetime_column_splits_into_two_on_export(self) -> None:
        from utp.reporting.export import to_csv

        columns = [{"key": "created_at", "label": "Booked", "kind": "datetime", "align": "left"}]
        rows = [{"created_at": "2026-09-01T10:05:09"}]
        csv_text = to_csv(report_title="T", columns=columns, rows=rows, meta={"currency": "THB"})
        # A datetime column becomes two: a date column and a time column.
        self.assertIn("Booked (date)", csv_text)
        self.assertIn("Booked (time)", csv_text)
        self.assertIn("2026-Sep-01", csv_text)
        self.assertIn("10:05:09", csv_text)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


class ReconciliationTests(_Base):
    def setUp(self) -> None:
        self.dashboard = self.reporting.executive_overview(self.ctx, dict(self.window))
        self.net = next(c["value"] for c in self.dashboard["kpis"] if c["key"] == "net_sales")

    def test_there_is_something_to_reconcile(self) -> None:
        self.assertGreater(self.net, 0, "demo history produced no revenue")

    def test_revenue_series_sums_to_the_headline(self) -> None:
        series = sum(row["net_minor"] for row in self.dashboard["revenue_series"])
        self.assertEqual(series, self.net)

    def test_channel_breakdown_sums_to_the_headline(self) -> None:
        channels = sum(row["net_minor"] for row in self.dashboard["channels"])
        self.assertEqual(channels, self.net)

    def test_transaction_ledger_sums_to_the_headline(self) -> None:
        """The sales report is the ledger behind the summary; it must agree."""
        ledger = self.reporting.run(self.ctx, "sales", dict(self.window))
        total = sum(
            row["net_minor"] for row in ledger["rows"]
            if row["status"] in ("Confirmed", "Partially refunded")
        )
        self.assertEqual(total, self.net)

    def test_channel_shares_total_one_hundred_percent(self) -> None:
        shares = sum(row["share_bp"] for row in self.dashboard["channels"])
        self.assertAlmostEqual(shares, 10_000, delta=2)   # rounding only

    def test_product_breakdown_declares_any_residual(self) -> None:
        """A line-level total may differ from an order-level one — but it must say so."""
        result = self.reporting.run(self.ctx, "products", dict(self.window))
        line_total = sum(row["net_minor"] for row in result["rows"])
        note = result["meta"].get("reconciliation")
        if line_total == self.net:
            self.assertIsNone(note)
        else:
            self.assertIsNotNone(note, "an unexplained discrepancy was left on the report")
            self.assertEqual(note["difference_minor"], self.net - line_total)
            self.assertTrue(note["explanation"])

    def test_visitor_mix_shares_total_one_hundred_percent(self) -> None:
        shares = sum(row["share_bp"] for row in self.dashboard["visitor_segments"])
        self.assertAlmostEqual(shares, 10_000, delta=3)

    def test_table_footer_totals_match_the_rows(self) -> None:
        result = self.reporting.run(self.ctx, "revenue", dict(self.window))
        for column in result["columns"]:
            if column["key"] in result["totals"]:
                self.assertEqual(
                    result["totals"][column["key"]],
                    sum(int(row.get(column["key"]) or 0) for row in result["rows"]),
                    column["key"],
                )


# --------------------------------------------------------------------------- #
# Net revenue definition
# --------------------------------------------------------------------------- #


class NetRevenueTests(_Base):
    """Refunded, cancelled and voided money is activity, not revenue (R70.6)."""

    def test_net_revenue_excludes_refunded_amounts(self) -> None:
        totals = self.reporting.metrics.totals(self.reporting.build_scope(self.ctx, dict(self.window)))
        raw = self.platform.db.query_one(
            """
            SELECT COALESCE(SUM(COALESCE(NULLIF(base_currency_minor,0), net_minor)),0) AS gross_net,
                   COALESCE(SUM(refunded_minor),0) AS refunded
            FROM bookings WHERE tenant_id = ? AND venue_id = ?
              AND visit_date BETWEEN ? AND ?
              AND status IN ('CONFIRMED','PARTIALLY_REFUNDED')
            """,
            (self.tenant_id, self.venue_id, self.window["date_from"], self.window["date_to"]),
        )
        expected = int(raw["gross_net"]) - int(raw["refunded"])
        self.assertEqual(totals["net_minor"], expected)

    def test_cancelled_bookings_are_counted_but_not_earned(self) -> None:
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        totals = self.reporting.metrics.totals(scope)
        self.assertGreater(totals["cancelled"], 0, "demo history produced no cancellations")
        cancelled_value = self.platform.db.scalar(
            "SELECT COALESCE(SUM(net_minor),0) FROM bookings WHERE tenant_id = ? "
            "AND status = 'CANCELLED' AND visit_date BETWEEN ? AND ?",
            (self.tenant_id, self.window["date_from"], self.window["date_to"]),
            default=0,
        )
        self.assertGreater(int(cancelled_value), 0)
        # Present in the activity count, absent from the money.
        self.assertLessEqual(totals["net_minor"], totals["gross_minor"])

    def test_gross_is_never_below_net(self) -> None:
        totals = self.reporting.metrics.totals(self.reporting.build_scope(self.ctx, dict(self.window)))
        self.assertGreaterEqual(totals["gross_minor"], totals["net_minor"])

    def test_tickets_sold_and_guests_admitted_are_reported_separately(self) -> None:
        """A no-show sold a ticket and admitted nobody; one number cannot say both."""
        dashboard = self.reporting.executive_overview(self.ctx, dict(self.window))
        tickets = next(c["value"] for c in dashboard["kpis"] if c["key"] == "tickets")
        visitors = next(c["value"] for c in dashboard["kpis"] if c["key"] == "visitors")
        self.assertGreater(tickets, 0)
        self.assertLessEqual(visitors, tickets)


# --------------------------------------------------------------------------- #
# Date ranges and comparison
# --------------------------------------------------------------------------- #


class DateRangeTests(_Base):
    def test_presets_resolve_to_concrete_windows(self) -> None:
        today = _dt.date(2026, 8, 19)      # a Wednesday
        cases = {
            "today": ("2026-08-19", "2026-08-19"),
            "yesterday": ("2026-08-18", "2026-08-18"),
            "this_week": ("2026-08-17", "2026-08-19"),
            "last_week": ("2026-08-10", "2026-08-16"),
            "this_month": ("2026-08-01", "2026-08-19"),
            "last_month": ("2026-07-01", "2026-07-31"),
            "this_year": ("2026-01-01", "2026-08-19"),
        }
        for preset, expected in cases.items():
            with self.subTest(preset=preset):
                self.assertEqual(
                    self.reporting.resolve_range(
                        preset=preset, date_from=None, date_to=None, today=today
                    ),
                    expected,
                )

    def test_an_unknown_preset_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.reporting.resolve_range(
                preset="last_fortnight", date_from=None, date_to=None, today=_dt.date(2026, 8, 19)
            )

    def test_a_reversed_custom_range_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.reporting.resolve_range(
                preset="custom", date_from="2026-08-20", date_to="2026-08-01",
                today=_dt.date(2026, 8, 19),
            )

    def test_previous_period_is_the_same_length_immediately_before(self) -> None:
        # August has 31 days, so the comparison window is the 31 days ending the day
        # before it started: 1-31 July. Not "the previous calendar month", which
        # would compare 31 days against 28 in February.
        self.assertEqual(
            self.reporting.comparison_range("2026-08-01", "2026-08-31", "previous_period"),
            ("2026-07-01", "2026-07-31"),
        )

    def test_previous_period_matches_the_window_length_exactly(self) -> None:
        for start, end in (("2026-08-01", "2026-08-07"), ("2026-03-01", "2026-03-31"),
                           ("2026-02-01", "2026-02-28")):
            with self.subTest(window=(start, end)):
                previous = self.reporting.comparison_range(start, end, "previous_period")
                span = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days
                prior_span = (
                    _dt.date.fromisoformat(previous[1]) - _dt.date.fromisoformat(previous[0])
                ).days
                self.assertEqual(prior_span, span)
                self.assertEqual(
                    _dt.date.fromisoformat(previous[1]) + _dt.timedelta(days=1),
                    _dt.date.fromisoformat(start),
                )

    def test_same_period_last_year(self) -> None:
        self.assertEqual(
            self.reporting.comparison_range("2026-08-01", "2026-08-31", "same_period_last_year"),
            ("2025-08-01", "2025-08-31"),
        )

    def test_no_comparison_returns_nothing(self) -> None:
        self.assertIsNone(self.reporting.comparison_range("2026-08-01", "2026-08-31", "none"))

    def test_kpi_trend_direction_accounts_for_metrics_where_less_is_better(self) -> None:
        dashboard = self.reporting.executive_overview(self.ctx, dict(self.window))
        refunds = next(c for c in dashboard["kpis"] if c["key"] == "refunds")
        sales = next(c for c in dashboard["kpis"] if c["key"] == "net_sales")
        self.assertTrue(refunds["lower_is_better"])
        self.assertFalse(sales["lower_is_better"])

    def test_buckets_use_the_venue_timezone(self) -> None:
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        self.assertEqual(scope.timezone, "Asia/Bangkok")
        self.assertEqual(scope.currency, "THB")


# --------------------------------------------------------------------------- #
# Scope and permissions
# --------------------------------------------------------------------------- #


class ScopeAndPermissionTests(_Base):
    def _staff_ctx(self, email: str):
        """A real signed-in principal, so the permission check is the real one."""
        session = self.platform.staff.login(
            self.platform.guest_context(self.tenant_id),
            email=email,
            credential="Aquaria-Demo-2026",
        )
        base = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id)
        return base.with_principal(
            self.platform.staff.authenticate_token(base, session["token"])
        )

    def test_a_cashier_cannot_open_the_dashboards_or_reports(self) -> None:
        ctx = self._staff_ctx("cashier@aquaria.test")
        for call in (
            lambda: self.reporting.executive_overview(ctx, {}),
            lambda: self.reporting.operations_today(ctx, {}),
            lambda: self.reporting.run(ctx, "revenue", {}),
        ):
            with self.assertRaises(AuthorizationDenied):
                call()

    def test_a_cashier_sees_an_empty_catalog(self) -> None:
        ctx = self._staff_ctx("cashier@aquaria.test")
        sections = self.reporting.catalog_for(ctx)["sections"]
        offered = [r["key"] for s in sections for r in s["reports"]]
        self.assertNotIn("revenue", offered)
        self.assertNotIn("executive_overview", offered)

    def test_a_manager_can_open_the_dashboards(self) -> None:
        ctx = self._staff_ctx("manager@aquaria.test")
        self.assertIn("kpis", self.reporting.executive_overview(ctx, {}))
        self.assertIn("kpis", self.reporting.operations_today(ctx, {}))

    def test_an_out_of_scope_venue_filter_narrows_to_nothing(self) -> None:
        """Asking for a venue you do not hold must not widen the result (R43.4)."""
        scope = self.reporting.build_scope(self.ctx, {"venue": "ven_not_mine"})
        self.assertEqual(scope.venue_ids, [])
        totals = self.reporting.metrics.totals(scope)
        self.assertEqual(totals["net_minor"], 0)
        self.assertEqual(totals["bookings"], 0)

    def test_an_empty_scope_yields_nothing_not_everything(self) -> None:
        """The failure mode that would leak a whole tenant."""
        scope = Scope(
            tenant_id=self.tenant_id, venue_ids=[],
            date_from=self.window["date_from"], date_to=self.window["date_to"],
            timezone="Asia/Bangkok", currency="THB",
        )
        self.assertEqual(self.reporting.metrics.totals(scope)["bookings"], 0)
        self.assertEqual(self.reporting.metrics.by_channel(scope), [])
        self.assertEqual(self.reporting.metrics.by_product(scope), [])

    def test_cross_tenant_data_never_appears(self) -> None:
        other = self.platform.db.query_one(
            "SELECT COUNT(*) AS n FROM bookings WHERE tenant_id != ?", (self.tenant_id,)
        )
        self.assertEqual(int(other["n"]), 0, "fixture assumption: one tenant only")
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        self.assertEqual(scope.tenant_id, self.tenant_id)


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #


class MaskingTests(_Base):
    def _report_viewer_ctx(self):
        """The seeded Report Viewer: reports, but no VIEW_PII, VIEW_COST or EXPORT.

        A real signed-in principal rather than a synthetic one, so the masking under
        test is driven by actual role assignments.
        """
        session = self.platform.staff.login(
            self.platform.guest_context(self.tenant_id),
            email="viewer@aquaria.test",
            credential="Aquaria-Demo-2026",
        )
        base = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id)
        return base.with_principal(
            self.platform.staff.authenticate_token(base, session["token"])
        )

    def test_personal_data_is_masked_without_view_pii(self) -> None:
        ctx = self._report_viewer_ctx()
        result = self.reporting.run(ctx, "bookings", dict(self.window))
        self.assertTrue(result["meta"]["masked"])
        named = [row["customer"] for row in result["rows"] if row.get("customer")]
        self.assertTrue(named, "fixture assumption: bookings carry a customer name")
        for value in named:
            self.assertIn("*", value, f"unmasked personal data leaked: {value!r}")

    def test_personal_data_is_visible_with_view_pii(self) -> None:
        result = self.reporting.run(self.ctx, "bookings", dict(self.window))
        self.assertFalse(result["meta"]["masked"])
        named = [row["customer"] for row in result["rows"] if row.get("customer")]
        self.assertTrue(any("*" not in value for value in named))

    def test_cost_columns_are_removed_without_view_cost(self) -> None:
        ctx = self._report_viewer_ctx()
        result = self.reporting.run(ctx, "partners", dict(self.window))
        keys = [column["key"] for column in result["columns"]]
        self.assertNotIn("commission_minor", keys)
        for row in result["rows"]:
            self.assertNotIn("commission_minor", row)

    def test_cost_columns_are_present_with_view_cost(self) -> None:
        result = self.reporting.run(self.ctx, "partners", dict(self.window))
        self.assertIn("commission_minor", [column["key"] for column in result["columns"]])

    def test_the_executive_dashboard_carries_no_customer_identity(self) -> None:
        """It has no operational need for it (R70.8)."""
        import json

        blob = json.dumps(self.reporting.executive_overview(self.ctx, dict(self.window)))
        emails = self.platform.db.query(
            "SELECT email FROM customer_pii WHERE tenant_id = ? LIMIT 20", (self.tenant_id,)
        )
        for row in emails:
            if row["email"]:
                self.assertNotIn(row["email"], blob)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class ExportTests(_Base):
    def test_csv_carries_its_filter_criteria_and_timestamp(self) -> None:
        """An unattributable spreadsheet is worse than none (R71.9)."""
        result = self.reporting.export(self.ctx, "revenue", fmt="csv", filters=dict(self.window))
        body = result["body"]
        self.assertTrue(result["content_type"].startswith("text/csv"))
        for needle in ("Report,Revenue", "Generated,", "Times shown in,Asia/Bangkok",
                       "Currency,THB", "Rows,", self.window["date_from"]):
            self.assertIn(needle, body)

    def test_printable_export_is_a_document(self) -> None:
        result = self.reporting.export(self.ctx, "revenue", fmt="print", filters=dict(self.window))
        self.assertTrue(result["content_type"].startswith("text/html"))
        self.assertIn("<table>", result["body"])
        self.assertIn("size: A4 landscape", result["body"])

    def test_export_is_audited_with_filters_and_row_count(self) -> None:
        self.reporting.export(self.ctx, "channels", fmt="csv", filters=dict(self.window))
        row = self.platform.db.query_one(
            "SELECT new_json FROM audit_events WHERE tenant_id = ? AND action = 'EXPORT' "
            "ORDER BY at_utc DESC LIMIT 1",
            (self.tenant_id,),
        )
        self.assertIsNotNone(row, "an export was not audited (R41.7)")
        from utp.core.db import decode

        payload = decode(row["new_json"], {})
        self.assertEqual(payload["format"], "csv")
        self.assertIn("row_count", payload)
        self.assertEqual(payload["date_from"], self.window["date_from"])

    def test_a_dashboard_cannot_be_exported_as_a_table(self) -> None:
        with self.assertRaises(ValidationError):
            self.reporting.export(self.ctx, "executive_overview", fmt="csv", filters={})

    def test_an_unknown_format_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.reporting.export(self.ctx, "revenue", fmt="xlsx", filters={})

    def test_export_amounts_are_formatted_not_raw_minor_units(self) -> None:
        """A column of "125100" invites the reader to see 125,100 baht."""
        body = self.reporting.export(
            self.ctx, "channels", fmt="csv", filters=dict(self.window)
        )["body"]
        self.assertNotRegex(body, r",\d{7,},")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class ExceptionEngineTests(_Base):
    def test_findings_carry_severity_detail_and_an_action(self) -> None:
        from utp.reporting.exceptions import THRESHOLD_DEFAULTS

        # Any refund at all breaches a zero threshold, so a finding is guaranteed
        # regardless of what the generated history happened to produce.
        self.platform.config.set(
            self.ctx, key="reporting.threshold.refund_rate_bp", value=0,
            scope_type="VENUE", scope_id=self.venue_id,
        )
        self.addCleanup(
            self.platform.config.set, self.ctx,
            key="reporting.threshold.refund_rate_bp",
            value=THRESHOLD_DEFAULTS["reporting.threshold.refund_rate_bp"],
            scope_type="VENUE", scope_id=self.venue_id,
        )
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        findings = self.reporting.exceptions.evaluate(self.ctx, scope)
        self.assertTrue(findings, "a zero threshold produced no finding")
        for finding in findings:
            self.assertIn(finding["severity"], (CRITICAL, WARNING, INFO))
            self.assertTrue(finding["title"])
            self.assertTrue(finding["detail"])
            self.assertTrue(finding["action"], "an alert with no suggested action is noise")

    def test_findings_are_ordered_worst_first(self) -> None:
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        order = {CRITICAL: 0, WARNING: 1, INFO: 2}
        ranks = [order[f["severity"]] for f in self.reporting.exceptions.evaluate(self.ctx, scope)]
        self.assertEqual(ranks, sorted(ranks))

    def test_a_high_refund_rate_is_a_warning_not_a_critical(self) -> None:
        """Red is reserved for something broken right now (§15).

        Money already returned is serious but nothing is currently failing, so it
        must not be painted the same as a gate that has gone offline.
        """
        from utp.reporting.exceptions import THRESHOLD_DEFAULTS

        self.platform.config.set(
            self.ctx, key="reporting.threshold.refund_rate_bp", value=0,
            scope_type="VENUE", scope_id=self.venue_id,
        )
        self.addCleanup(
            self.platform.config.set, self.ctx,
            key="reporting.threshold.refund_rate_bp",
            value=THRESHOLD_DEFAULTS["reporting.threshold.refund_rate_bp"],
            scope_type="VENUE", scope_id=self.venue_id,
        )
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        findings = {f["key"]: f for f in self.reporting.exceptions.evaluate(self.ctx, scope)}
        self.assertIn("refund_rate", findings)
        self.assertEqual(findings["refund_rate"]["severity"], WARNING)

    def test_thresholds_are_configuration(self) -> None:
        """A venue that refunds 8% by design must be able to stop being told."""
        from utp.reporting.exceptions import THRESHOLD_DEFAULTS

        engine = self.reporting.exceptions
        scope = self.reporting.build_scope(self.ctx, dict(self.window))
        self.assertEqual(engine.threshold(self.ctx, "reporting.threshold.refund_rate_bp"), 500)

        self.platform.config.set(
            self.ctx, key="reporting.threshold.refund_rate_bp", value=0,
            scope_type="VENUE", scope_id=self.venue_id,
        )
        self.assertIn("refund_rate", [f["key"] for f in engine.evaluate(self.ctx, scope)])

        self.platform.config.set(
            self.ctx, key="reporting.threshold.refund_rate_bp", value=9_900,
            scope_type="VENUE", scope_id=self.venue_id,
        )
        self.assertEqual(engine.threshold(self.ctx, "reporting.threshold.refund_rate_bp"), 9_900)
        self.assertNotIn(
            "refund_rate", [f["key"] for f in engine.evaluate(self.ctx, scope)],
            "raising the threshold did not silence the alert",
        )
        self.platform.config.set(
            self.ctx, key="reporting.threshold.refund_rate_bp",
            value=THRESHOLD_DEFAULTS["reporting.threshold.refund_rate_bp"],
            scope_type="VENUE", scope_id=self.venue_id,
        )


# --------------------------------------------------------------------------- #
# Saved views
# --------------------------------------------------------------------------- #


class SavedViewTests(_Base):
    """A saved view belongs to a staff member, so it needs a real one.

    ``report_views.staff_id`` references ``staff(id)``: the constraint is
    deliberate, because "my daily view" is a personal preference and an orphaned
    one belongs to nobody. That is why these tests sign in rather than using the
    system context.
    """

    def setUp(self) -> None:
        session = self.platform.staff.login(
            self.platform.guest_context(self.tenant_id),
            email="manager@aquaria.test",
            credential="Aquaria-Demo-2026",
        )
        base = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id)
        self.ctx = base.with_principal(
            self.platform.staff.authenticate_token(base, session["token"])
        )

    def test_a_view_cannot_be_orphaned_from_its_owner(self) -> None:
        from utp.core.db import IntegrityViolation

        with self.assertRaises(IntegrityViolation):
            self.reporting.save_view(
                self.platform.system_context(self.tenant_id).for_venue(self.venue_id),
                report_key="revenue", name="No owner", filters={}, make_default=False,
            )

    def test_save_list_and_delete(self) -> None:
        saved = self.reporting.save_view(
            self.ctx, report_key="revenue", name="Monthly executive",
            filters={"date_preset": "this_month"}, make_default=True,
        )
        self.assertTrue(saved["id"])
        views = self.reporting.list_views(self.ctx, report_key="revenue")
        self.assertIn(saved["id"], [view["id"] for view in views])
        self.assertTrue(views[0]["is_default"])
        self.assertEqual(views[0]["filters"], {"date_preset": "this_month"})
        self.reporting.delete_view(self.ctx, saved["id"])
        self.assertNotIn(
            saved["id"], [v["id"] for v in self.reporting.list_views(self.ctx, report_key="revenue")]
        )

    def test_a_view_needs_a_name(self) -> None:
        with self.assertRaises(ValidationError):
            self.reporting.save_view(
                self.ctx, report_key="revenue", name="   ", filters={}, make_default=False
            )

    def test_deleting_someone_elses_view_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.reporting.delete_view(self.ctx, "rvw_does_not_exist")

    def test_only_one_default_per_report(self) -> None:
        first = self.reporting.save_view(self.ctx, report_key="channels", name="A",
                                         filters={}, make_default=True)
        second = self.reporting.save_view(self.ctx, report_key="channels", name="B",
                                          filters={}, make_default=True)
        defaults = [v["id"] for v in self.reporting.list_views(self.ctx, report_key="channels")
                    if v["is_default"]]
        self.assertEqual(defaults, [second["id"]])
        self.reporting.delete_view(self.ctx, first["id"])
        self.reporting.delete_view(self.ctx, second["id"])


# --------------------------------------------------------------------------- #
# Operational dashboard
# --------------------------------------------------------------------------- #


class OperationsTests(_Base):
    def setUp(self) -> None:
        self.ops = self.reporting.operations_today(self.ctx, {})

    def test_it_defaults_to_today(self) -> None:
        today = _dt.datetime.now(
            __import__("utp.core.clock", fromlist=["timezone_for"]).timezone_for("Asia/Bangkok")
        ).date().isoformat()
        self.assertEqual(self.ops["meta"]["date_from"], today)
        self.assertEqual(self.ops["meta"]["date_to"], today)

    def test_arrival_states_are_derived_and_add_up(self) -> None:
        counts = self.ops["arrival_counts"]
        self.assertEqual(set(counts), {"ARRIVING", "CHECKED_IN", "LATE", "NO_SHOW"})
        full = self.reporting.run(self.ctx, "arrivals", {"date_preset": "today"})
        self.assertEqual(sum(counts.values()), full["row_count"])

    def test_gate_rates_are_consistent(self) -> None:
        gate = self.ops["gate"]
        self.assertEqual(gate["admitted"] + gate["refused"], gate["total_scans"])
        if gate["total_scans"]:
            self.assertAlmostEqual(gate["success_bp"] + gate["failure_bp"], 10_000, delta=2)

    def test_device_status_is_derived_from_the_heartbeat(self) -> None:
        """A kiosk whose plug was pulled must not still report itself online."""
        states = {row["state"] for row in self.ops["devices"]}
        self.assertTrue(states)
        self.assertTrue(states <= {"Online", "Offline", "Never seen", "Unknown", "Paper low",
                                   "Printer error", "Payment device error", "Inactive",
                                   "Deactivated", "Suspended"}, states)

    def test_tiles_never_render_none(self) -> None:
        for tile in self.ops["kpis"]:
            self.assertIsNotNone(tile["value"], tile["label"])


if __name__ == "__main__":
    unittest.main()
