"""Aggregation over the transactional record. No figure is invented here.

Three rules govern every query in this module, and the tests hold them:

1. **Money is read, never recomputed.** Every amount comes from a column that
   was written when the transaction completed — ``gross_minor``,
   ``discount_minor``, ``tax_minor``, ``net_minor`` and the
   ``charge_snapshot_json`` beside them. A report that recalculates VAT from
   today's rate would disagree with the receipt the guest is holding
   (add_features §33). For a foreign-currency order the base-currency figure is
   ``base_currency_minor``, converted with the rate stored on the order, so a
   later rate change cannot move last month's revenue (§33).

2. **Gross activity and net revenue are different questions.** Cancelled,
   voided, refunded and complimentary items stay visible in activity counts and
   are excluded from net revenue (R46.4, R70.6). ``_NET_STATUSES`` is the single
   place that distinction lives.

3. **Scope is applied in SQL, not after.** Out-of-scope venues never contribute
   to a figure the principal can see (R43.7), so the venue filter is part of the
   WHERE clause rather than a post-filter on results that a SUM has already
   passed through.

Aggregation buckets are computed in the **venue's** timezone. A day boundary in
UTC would split a Bangkok evening across two reporting days and make every daily
total subtly wrong (R1.9).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

from ..core.clock import local, parse_instant, timezone_for
from ..core.db import decode
from ..core.i18n import text as i18n_text
from ..core.money import allocate
from ..domain import enums
from .definitions import ADVANCE_BANDS

#: Booking statuses that contribute to **net** revenue. Everything else is
#: retained in gross activity reporting but excluded from the net figure.
_NET_STATUSES: tuple[str, ...] = ("CONFIRMED", "PARTIALLY_REFUNDED")

#: Statuses that represent a real commercial event, and so belong in activity
#: counts even when the money was later reversed.
_ACTIVITY_STATUSES: tuple[str, ...] = (
    "CONFIRMED",
    "PARTIALLY_REFUNDED",
    "REFUNDED",
    "CANCELLED",
    "VOIDED",
)

#: Payment methods that are not a sale: a complimentary ticket has a value but no
#: revenue, and stored value settles a bill without being new income.
_NON_REVENUE_METHODS: tuple[str, ...] = ("COMPLIMENTARY",)

#: Net revenue for one booking, in the organization's base currency.
#:
#: Two corrections are folded in here rather than left to each caller:
#:
#: * ``base_currency_minor`` when the order was taken in another currency, so the
#:   rate stored on the order is used and a later rate change cannot move history
#:   (add_features §20). ``NULLIF(...,0)`` because a same-currency order leaves it
#:   at zero rather than null.
#: * ``- refunded_minor``, because money returned is not revenue earned. A
#:   partially refunded booking keeps its original ``net_minor`` and records the
#:   refund separately, so counting ``net_minor`` alone overstates takings by
#:   exactly the amount handed back (R70.6, R46.4).
_NET_REVENUE_SQL = (
    "(COALESCE(NULLIF(b.base_currency_minor, 0), b.net_minor) "
    "- COALESCE(b.refunded_minor, 0))"
)


# --------------------------------------------------------------------------- #
# Scope and filters
# --------------------------------------------------------------------------- #


class Scope:
    """The venue and date window a report is allowed to see.

    Built by the service from the principal's role assignments, so a metric
    function cannot accidentally query outside it — the only way to reach the
    data is through the ``where``/``params`` this produces.
    """

    __slots__ = ("tenant_id", "venue_ids", "date_from", "date_to", "timezone", "currency", "filters")

    def __init__(
        self,
        *,
        tenant_id: str,
        venue_ids: list[str],
        date_from: str,
        date_to: str,
        timezone: str = "UTC",
        currency: str = "THB",
        filters: dict[str, Any] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.venue_ids = list(venue_ids)
        self.date_from = date_from
        self.date_to = date_to
        self.timezone = timezone
        self.currency = currency
        self.filters = dict(filters or {})

    # -- SQL fragments --------------------------------------------------- #

    def venue_clause(self, alias: str = "b") -> tuple[str, list[Any]]:
        if not self.venue_ids:
            # No venue in scope must yield nothing, never everything.
            return f" AND 1 = 0", []
        placeholders = ",".join("?" for _ in self.venue_ids)
        return f" AND {alias}.venue_id IN ({placeholders})", list(self.venue_ids)

    def date_clause(self, alias: str = "b", *, basis: str | None = None) -> tuple[str, list[Any]]:
        """Restrict to the window, by visit date or by order date.

        Which one is correct depends on the question: "how much did we take in
        March" is order date, "how busy is March" is visit date. The report says
        which, and the filter bar exposes it (§34, ``date_basis``).
        """
        chosen = basis or self.filters.get("date_basis") or "visit_date"
        if chosen == "order_date":
            # created_at is a UTC instant; compare on the venue-local date.
            return (
                f" AND date({alias}.created_at) BETWEEN ? AND ?",
                [self.date_from, self.date_to],
            )
        return f" AND {alias}.visit_date BETWEEN ? AND ?", [self.date_from, self.date_to]

    def filter_clause(self, alias: str = "b") -> tuple[str, list[Any]]:
        """Apply the optional report filters that map to booking columns."""
        sql = ""
        params: list[Any] = []
        mapping = {
            "channel": f"{alias}.channel",
            "partner": f"{alias}.partner_id",
            "booking_status": f"{alias}.status",
            "venue": f"{alias}.venue_id",
            "staff": f"{alias}.staff_actor_id",
            "device": f"{alias}.device_id",
        }
        for key, column in mapping.items():
            value = self.filters.get(key)
            if not value:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            values = [v for v in values if v]
            if not values:
                continue
            sql += f" AND {column} IN ({','.join('?' for _ in values)})"
            params.extend(values)
        return sql, params

    def booking_where(self, alias: str = "b", *, statuses: Iterable[str] | None = None) -> tuple[str, list[Any]]:
        """The standard WHERE for a bookings query: tenant, scope, window, filters."""
        sql = f" WHERE {alias}.tenant_id = ?"
        params: list[Any] = [self.tenant_id]
        for fragment, values in (
            self.venue_clause(alias),
            self.date_clause(alias),
            self.filter_clause(alias),
        ):
            sql += fragment
            params.extend(values)
        statuses = tuple(statuses) if statuses is not None else _ACTIVITY_STATUSES
        if statuses:
            sql += f" AND {alias}.status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        return sql, params


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _money(row: Any, key: str) -> int:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return 0
    return int(value or 0)


def base_amount(row: Any) -> int:
    """The order's **net revenue** in the organization's base currency.

    The Python counterpart of :data:`_NET_REVENUE_SQL`, and it must stay in step
    with it: converted amount where the order was taken in another currency, less
    anything refunded, because money handed back is not revenue earned (R70.6).
    """
    converted = _money(row, "base_currency_minor")
    gross = converted if converted else _money(row, "net_minor")
    return gross - _money(row, "refunded_minor")


def _share_bp(part: int, whole: int) -> int:
    """Share in basis points. Percentages are never floats in this codebase."""
    if not whole:
        return 0
    return round(part * 10_000 / whole)


def _safe_div(numerator: int, denominator: int) -> int:
    return round(numerator / denominator) if denominator else 0


def bucket_of(instant_text: str | None, *, group_by: str, tz_name: str) -> str:
    """Label a timestamp for a time series, in the venue's timezone."""
    if not instant_text:
        return ""
    moment = local(parse_instant(instant_text), tz_name)
    if group_by == "hourly":
        return moment.strftime("%Y-%m-%d %H:00")
    if group_by == "weekly":
        monday = moment.date() - _dt.timedelta(days=moment.weekday())
        return monday.isoformat()
    if group_by == "monthly":
        return moment.strftime("%Y-%m")
    return moment.strftime("%Y-%m-%d")


def bucket_of_date(day: str, *, group_by: str) -> str:
    """Label a plain date (a visit date carries no time) for a time series."""
    if not day:
        return ""
    value = _dt.date.fromisoformat(day)
    if group_by == "weekly":
        return (value - _dt.timedelta(days=value.weekday())).isoformat()
    if group_by == "monthly":
        return value.strftime("%Y-%m")
    if group_by == "hourly":
        # A visit date has no hour; fall back to the day rather than invent one.
        return value.isoformat()
    return value.isoformat()


class Metrics:
    """Every aggregation the reports need, over one database."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # Headline figures
    # ------------------------------------------------------------------ #

    def totals(self, scope: Scope) -> dict[str, Any]:
        """The KPI set for a window: activity counts and net revenue (§3)."""
        where, params = scope.booking_where()
        # Parameter order follows the order the placeholders appear in the SQL
        # *text*, and these two groups are in the SELECT list, ahead of the WHERE.
        # Appending them after `params` silently shifts every binding along and
        # returns zero for every money column.
        row = self.db.query_one(
            f"""
            SELECT
              COUNT(*)                                              AS bookings,
              COALESCE(SUM(b.gross_minor), 0)                       AS gross_minor,
              COALESCE(SUM(b.discount_minor), 0)                    AS discount_minor,
              COALESCE(SUM(b.service_charge_minor), 0)              AS service_charge_minor,
              COALESCE(SUM(b.tax_minor), 0)                         AS tax_minor,
              COALESCE(SUM(b.refunded_minor), 0)                    AS refund_minor,
              COALESCE(SUM(CASE WHEN b.status IN ({','.join('?' for _ in _NET_STATUSES)})
                                THEN {_NET_REVENUE_SQL} ELSE 0 END), 0) AS net_minor,
              COALESCE(SUM(CASE WHEN b.status IN ({','.join('?' for _ in _NET_STATUSES)})
                                THEN 1 ELSE 0 END), 0)              AS net_bookings,
              COALESCE(SUM(CASE WHEN b.status = 'CANCELLED' THEN 1 ELSE 0 END), 0) AS cancelled,
              COALESCE(SUM(CASE WHEN b.status = 'VOIDED' THEN 1 ELSE 0 END), 0)    AS voided,
              COALESCE(SUM(CASE WHEN b.status IN ('REFUNDED','PARTIALLY_REFUNDED')
                                THEN 1 ELSE 0 END), 0)              AS refunded
            FROM bookings b
            {where}
            """,
            list(_NET_STATUSES) + list(_NET_STATUSES) + params,
        )
        result = {key: int(row[key] or 0) for key in row.keys()}

        tickets = self.ticket_counts(scope)
        result["tickets"] = tickets["issued"]
        result["visitors"] = tickets["admitted"]
        result["atv_minor"] = _safe_div(result["net_minor"], result["net_bookings"])
        result["revenue_per_visitor_minor"] = _safe_div(result["net_minor"], tickets["admitted"])
        capacity = self.capacity_totals(scope)
        result["capacity"] = capacity["capacity"]
        result["capacity_reserved"] = capacity["reserved"]
        result["capacity_utilization_bp"] = capacity["utilization_bp"]
        return result

    def ticket_counts(self, scope: Scope) -> dict[str, int]:
        """Tickets issued against tickets that actually walked in."""
        clause, venue_params = scope.venue_clause("t")
        row = self.db.query_one(
            f"""
            SELECT
              COUNT(*)                                                       AS issued,
              COALESCE(SUM(CASE WHEN t.entries_used > 0 THEN 1 ELSE 0 END), 0) AS admitted,
              COALESCE(SUM(CASE WHEN t.state = 'CANCELLED' THEN 1 ELSE 0 END), 0) AS cancelled,
              COALESCE(SUM(CASE WHEN t.state = 'REFUNDED' THEN 1 ELSE 0 END), 0)  AS refunded
            FROM tickets t
            WHERE t.tenant_id = ?{clause} AND t.visit_date BETWEEN ? AND ?
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        return {key: int(row[key] or 0) for key in row.keys()}

    # ------------------------------------------------------------------ #
    # Time series
    # ------------------------------------------------------------------ #

    def revenue_series(self, scope: Scope, *, group_by: str = "daily") -> list[dict[str, Any]]:
        """Revenue over time (§4). Bucketed in the venue's timezone."""
        where, params = scope.booking_where()
        rows = self.db.query(
            f"""
            SELECT b.visit_date, b.created_at, b.status, b.gross_minor, b.discount_minor,
                   b.net_minor, b.refunded_minor, b.base_currency_minor
            FROM bookings b
            {where}
            ORDER BY b.visit_date
            """,
            list(params),
        )
        basis = scope.filters.get("date_basis") or "visit_date"
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            if basis == "order_date":
                key = bucket_of(row["created_at"], group_by=group_by, tz_name=scope.timezone)
            else:
                key = bucket_of_date(row["visit_date"], group_by=group_by)
            entry = buckets.setdefault(
                key,
                {"bucket": key, "bookings": 0, "tickets": 0, "gross_minor": 0,
                 "discount_minor": 0, "refund_minor": 0, "net_minor": 0, "net_bookings": 0},
            )
            entry["bookings"] += 1
            entry["gross_minor"] += int(row["gross_minor"] or 0)
            entry["discount_minor"] += int(row["discount_minor"] or 0)
            entry["refund_minor"] += int(row["refunded_minor"] or 0)
            if row["status"] in _NET_STATUSES:
                entry["net_minor"] += base_amount(row)
                entry["net_bookings"] += 1

        tickets_by_bucket = self._tickets_by_bucket(scope, group_by=group_by)
        for key, entry in buckets.items():
            entry["tickets"] = tickets_by_bucket.get(key, 0)
            entry["atv_minor"] = _safe_div(entry["net_minor"], entry["net_bookings"])
        return [buckets[key] for key in sorted(buckets)]

    def _tickets_by_bucket(self, scope: Scope, *, group_by: str) -> dict[str, int]:
        clause, venue_params = scope.venue_clause("t")
        rows = self.db.query(
            f"""
            SELECT t.visit_date, COUNT(*) AS n
            FROM tickets t
            WHERE t.tenant_id = ?{clause} AND t.visit_date BETWEEN ? AND ?
            GROUP BY t.visit_date
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        totals: dict[str, int] = {}
        for row in rows:
            key = bucket_of_date(row["visit_date"], group_by=group_by)
            totals[key] = totals.get(key, 0) + int(row["n"] or 0)
        return totals

    def peak_time(self, scope: Scope, *, measure: str = "visitors") -> dict[str, Any]:
        """Day-of-week × hour heatmap (§10), from real scan or sale times."""
        clause, venue_params = scope.venue_clause("s")
        grid = [[0 for _ in range(24)] for _ in range(7)]
        if measure in ("visitors", "checkins"):
            rows = self.db.query(
                f"""
                SELECT s.at_local FROM scan_events s
                WHERE s.tenant_id = ?{clause} AND date(s.at_local) BETWEEN ? AND ?
                  AND s.decision IN ('ADMIT','ADMIT_WITH_CHECK')
                """,
                [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
            )
            for row in rows:
                stamp = row["at_local"]
                if not stamp:
                    continue
                try:
                    moment = _dt.datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                grid[moment.weekday()][moment.hour] += 1
        else:
            where, params = scope.booking_where()
            rows = self.db.query(
                f"SELECT b.created_at, b.net_minor, b.status FROM bookings b {where}", params
            )
            for row in rows:
                moment = local(parse_instant(row["created_at"]), scope.timezone)
                amount = 1 if measure == "sales" else int(row["net_minor"] or 0)
                grid[moment.weekday()][moment.hour] += amount
        peak = max((value for line in grid for value in line), default=0)
        return {
            "measure": measure,
            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "grid": grid,
            "peak": peak,
        }

    # ------------------------------------------------------------------ #
    # Breakdowns
    # ------------------------------------------------------------------ #

    def by_channel(self, scope: Scope) -> list[dict[str, Any]]:
        """Sales by channel (§5)."""
        where, params = scope.booking_where()
        rows = self.db.query(
            f"""
            SELECT b.channel AS label,
                   COUNT(*) AS transactions,
                   COALESCE(SUM(b.gross_minor), 0) AS gross_minor,
                   COALESCE(SUM(b.discount_minor), 0) AS discount_minor,
                   COALESCE(SUM(CASE WHEN b.status IN ({','.join('?' for _ in _NET_STATUSES)})
                                     THEN {_NET_REVENUE_SQL} ELSE 0 END), 0) AS net_minor,
                   COALESCE(SUM(CASE WHEN b.status IN ({','.join('?' for _ in _NET_STATUSES)})
                                     THEN 1 ELSE 0 END), 0) AS net_transactions
            FROM bookings b
            {where}
            GROUP BY b.channel
            ORDER BY net_minor DESC
            """,
            # Same ordering trap as totals(): these placeholders are in the SELECT.
            list(_NET_STATUSES) + list(_NET_STATUSES) + params,
        )
        tickets = self._tickets_by_channel(scope)
        result = [
            {
                "label": row["label"],
                "transactions": int(row["transactions"] or 0),
                "tickets": tickets.get(row["label"], 0),
                "gross_minor": int(row["gross_minor"] or 0),
                "discount_minor": int(row["discount_minor"] or 0),
                "net_minor": int(row["net_minor"] or 0),
                "atv_minor": _safe_div(int(row["net_minor"] or 0), int(row["net_transactions"] or 0)),
            }
            for row in rows
        ]
        total = sum(entry["net_minor"] for entry in result)
        for entry in result:
            entry["share_bp"] = _share_bp(entry["net_minor"], total)
        return result

    def _tickets_by_channel(self, scope: Scope) -> dict[str, int]:
        where, params = scope.booking_where()
        rows = self.db.query(
            f"""
            SELECT b.channel AS label, COUNT(t.id) AS n
            FROM bookings b JOIN tickets t ON t.booking_id = b.id AND t.tenant_id = b.tenant_id
            {where}
            GROUP BY b.channel
            """,
            params,
        )
        return {row["label"]: int(row["n"] or 0) for row in rows}

    def by_product(self, scope: Scope) -> list[dict[str, Any]]:
        """Product and ticket-type performance (§11).

        Rows are read per booking rather than aggregated in SQL so the order-level
        residual can be allocated across the lines. Without that step a product
        breakdown does not add up to the headline net-sales figure: a cart-level
        discount and the final rounding both live on the order, not on any single
        line, so the lines are short by exactly that amount. An unexplained
        discrepancy is a reason to distrust every other number on the page
        (R70.9), so it is allocated rather than annotated.
        """
        where, params = scope.booking_where(statuses=_NET_STATUSES)
        rows = self.db.query(
            f"""
            SELECT bi.booking_id, bi.product_id, bi.ticket_type_id, bi.segment_id,
                   bi.quantity, bi.gross_minor, bi.discount_minor, bi.net_minor,
                   p.name_json AS product_name, p.code AS product_code,
                   tt.name_json AS tt_name, tt.code AS tt_code,
                   cs.name_json AS seg_name, cs.code AS seg_code,
                   {_NET_REVENUE_SQL} AS booking_net
            FROM booking_items bi
            JOIN bookings b        ON b.id = bi.booking_id AND b.tenant_id = bi.tenant_id
            JOIN products p        ON p.id = bi.product_id AND p.tenant_id = bi.tenant_id
            JOIN ticket_types tt   ON tt.id = bi.ticket_type_id AND tt.tenant_id = bi.tenant_id
            JOIN customer_segments cs ON cs.id = bi.segment_id AND cs.tenant_id = bi.tenant_id
            {where} AND bi.state = 'ACTIVE'
            """,
            params,
        )

        # Group by booking so the residual can be shared out across its own lines.
        per_booking: dict[str, list[Any]] = {}
        for row in rows:
            per_booking.setdefault(row["booking_id"], []).append(row)

        language = scope.filters.get("language") or "en"
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for items in per_booking.values():
            line_nets = [int(item["net_minor"] or 0) for item in items]
            booking_net = int(items[0]["booking_net"] or 0)
            residual = booking_net - sum(line_nets)
            if residual and any(line_nets):
                # Proportional integer allocation: the same helper the charge
                # engine uses, so no satang is created or lost.
                shares = allocate(residual, line_nets)
            else:
                shares = [0] * len(items)

            for item, share in zip(items, shares):
                key = (item["product_code"], item["tt_code"], item["seg_code"])
                entry = buckets.setdefault(
                    key,
                    {
                        "product": i18n_text(
                            decode(item["product_name"], {}), language, fallback=item["product_code"]
                        ),
                        "ticket_type": i18n_text(
                            decode(item["tt_name"], {}), language, fallback=item["tt_code"]
                        ),
                        "segment": i18n_text(
                            decode(item["seg_name"], {}), language, fallback=item["seg_code"]
                        ),
                        # Codes kept so other views (the visitor mix) can group on
                        # them without re-querying or matching on display text.
                        "product_code": item["product_code"],
                        "segment_code": item["seg_code"],
                        "quantity": 0,
                        "gross_minor": 0,
                        "discount_minor": 0,
                        "net_minor": 0,
                    },
                )
                entry["quantity"] += int(item["quantity"] or 0)
                entry["gross_minor"] += int(item["gross_minor"] or 0)
                entry["discount_minor"] += int(item["discount_minor"] or 0)
                entry["net_minor"] += int(item["net_minor"] or 0) + share

        result = sorted(buckets.values(), key=lambda entry: -entry["net_minor"])
        total = sum(entry["net_minor"] for entry in result)
        for entry in result:
            entry["asp_minor"] = _safe_div(entry["net_minor"], entry["quantity"])
            entry["share_bp"] = _share_bp(entry["net_minor"], total)
        return result

    def visitor_mix(self, scope: Scope) -> dict[str, Any]:
        """Visitors by segment and by pricing group (§6, §7).

        The pricing group is derived from the product the ticket was sold under
        rather than hardcoded: Aquaria happens to model Thai-resident and
        international as two products, and another venue may model none or five.
        """
        where, params = scope.booking_where(statuses=_NET_STATUSES)
        # Tickets sold and guests admitted are counted separately and both
        # reported. They are different numbers — a no-show sold a ticket and
        # admitted nobody — and collapsing them into one "visitors" column is how
        # a dashboard ends up disagreeing with the turnstile.
        # Built from the product breakdown so the order-level allocation is applied
        # once and every view agrees with the headline figure.
        product_rows = self.by_product(scope)
        admitted = self._admitted_by_segment(scope)

        segments: dict[str, dict[str, Any]] = {}
        groups: dict[str, dict[str, Any]] = {}
        for row in product_rows:
            seg = segments.setdefault(
                row["segment"],
                {"label": row["segment"], "code": row["segment_code"],
                 "tickets": 0, "visitors": 0, "net_minor": 0},
            )
            seg["tickets"] += row["quantity"]
            seg["net_minor"] += row["net_minor"]

            group_label = self._pricing_group_label(row["product_code"])
            group = groups.setdefault(
                group_label,
                {"label": group_label, "tickets": 0, "visitors": 0, "net_minor": 0},
            )
            group["tickets"] += row["quantity"]
            group["net_minor"] += row["net_minor"]

        # Segments can report guests who actually walked in, because a ticket
        # carries its segment. Pricing groups cannot without joining tickets back
        # to products, so there the ticket count is the honest basis.
        for entry in segments.values():
            entry["visitors"] = admitted.get(entry.get("code"), 0)
        for entry in groups.values():
            entry["visitors"] = entry["tickets"]

        def finish(table: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
            total = sum(entry["tickets"] for entry in table.values())
            out = []
            for entry in sorted(table.values(), key=lambda e: -e["tickets"]):
                entry.pop("code", None)
                entry["share_bp"] = _share_bp(entry["tickets"], total)
                entry["per_visitor_minor"] = _safe_div(entry["net_minor"], entry["tickets"])
                out.append(entry)
            return out

        return {"segments": finish(segments), "pricing_groups": finish(groups)}

    def _admitted_by_segment(self, scope: Scope) -> dict[str, int]:
        """Guests who actually walked in, by segment code."""
        clause, venue_params = scope.venue_clause("t")
        rows = self.db.query(
            f"""
            SELECT cs.code AS seg_code, COUNT(*) AS n
            FROM tickets t
            JOIN customer_segments cs ON cs.id = t.segment_id AND cs.tenant_id = t.tenant_id
            WHERE t.tenant_id = ?{clause} AND t.visit_date BETWEEN ? AND ?
              AND t.entries_used > 0
            GROUP BY t.segment_id
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        return {row["seg_code"]: int(row["n"] or 0) for row in rows}

    @staticmethod
    def _pricing_group_label(product_code: str | None) -> str:
        """Map a product code to its pricing group.

        Configuration-driven in spirit: the code is inspected, not a venue type.
        A venue that does not split pricing gets a single "Standard" group.
        """
        code = (product_code or "").upper()
        if "LOCAL" in code:
            return "Thai / resident"
        if "INTL" in code:
            return "International"
        return "Standard"

    def advance_booking(self, scope: Scope) -> list[dict[str, Any]]:
        """How far ahead guests book, and whether early bookings survive (§8)."""
        where, params = scope.booking_where()
        rows = self.db.query(
            f"""
            SELECT b.visit_date, b.created_at, b.status, b.net_minor, b.base_currency_minor,
                   b.refunded_minor,
                   (SELECT COALESCE(SUM(bi.quantity), 0) FROM booking_items bi
                     WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id
                       AND bi.state = 'ACTIVE') AS visitors
            FROM bookings b
            {where}
            """,
            params,
        )
        bands = {label: {"label": label, "bookings": 0, "visitors": 0,
                         "net_minor": 0, "cancelled": 0} for label, _lo, _hi in ADVANCE_BANDS}
        for row in rows:
            try:
                visit = _dt.date.fromisoformat(row["visit_date"])
                booked = local(parse_instant(row["created_at"]), scope.timezone).date()
            except (ValueError, TypeError):
                continue
            lead = max((visit - booked).days, 0)
            for label, low, high in ADVANCE_BANDS:
                if lead >= low and (high is None or lead < high):
                    band = bands[label]
                    band["bookings"] += 1
                    band["visitors"] += int(row["visitors"] or 0)
                    if row["status"] in _NET_STATUSES:
                        band["net_minor"] += base_amount(row)
                    if row["status"] in ("CANCELLED", "VOIDED"):
                        band["cancelled"] += 1
                    break
        ordered = [bands[label] for label, _lo, _hi in ADVANCE_BANDS]
        for band in ordered:
            band["cancel_rate_bp"] = _share_bp(band["cancelled"], band["bookings"])
        return ordered

    # ------------------------------------------------------------------ #
    # Capacity
    # ------------------------------------------------------------------ #

    def capacity_rows(self, scope: Scope) -> list[dict[str, Any]]:
        """Capacity per session, with what arrived (§9)."""
        clause, venue_params = scope.venue_clause("s")
        rows = self.db.query(
            f"""
            SELECT s.id, s.date, s.start_time, s.capacity, s.confirmed, s.status, s.kind,
                   e.name_json AS experience_name, p.name_json AS product_name, p.code AS product_code
            FROM sessions s
            LEFT JOIN experiences e ON e.id = s.experience_id AND e.tenant_id = s.tenant_id
            LEFT JOIN products p ON p.id = s.product_id AND p.tenant_id = s.tenant_id
            WHERE s.tenant_id = ?{clause} AND s.date BETWEEN ? AND ?
            ORDER BY s.date, s.start_time
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        language = scope.filters.get("language") or "en"
        checkins = self._checkins_by_session(scope)
        result = []
        for row in rows:
            capacity = int(row["capacity"] or 0)
            reserved = int(row["confirmed"] or 0)
            checked_in = checkins.get(row["id"], 0)
            label = i18n_text(decode(row["experience_name"], {}), language) or i18n_text(
                decode(row["product_name"], {}), language, fallback=row["product_code"] or "Session"
            )
            result.append(
                {
                    "session_id": row["id"],
                    "label": label,
                    "date": row["date"],
                    "start_time": row["start_time"],
                    "capacity": capacity,
                    "reserved": reserved,
                    "checked_in": checked_in,
                    "remaining": max(capacity - reserved, 0) if capacity else None,
                    "utilization_bp": _share_bp(reserved, capacity),
                    "state": self._capacity_state(reserved, capacity, row["status"]),
                }
            )
        return result

    @staticmethod
    def _capacity_state(reserved: int, capacity: int, status: str | None) -> str:
        """Never colour alone: this label is the primary cue (§9, R68.4)."""
        if status in ("CANCELLED", "COMPLETED"):
            return str(status).title()
        if not capacity:
            return "Unlimited"
        if reserved >= capacity:
            return "Full"
        if reserved * 100 >= capacity * 85:
            return "Near full"
        return "Normal"

    def _checkins_by_session(self, scope: Scope) -> dict[str, int]:
        clause, venue_params = scope.venue_clause("sc")
        rows = self.db.query(
            f"""
            SELECT t.session_id AS session_id, COUNT(*) AS n
            FROM scan_events sc
            JOIN tickets t ON t.id = sc.ticket_id AND t.tenant_id = sc.tenant_id
            WHERE sc.tenant_id = ?{clause} AND date(sc.at_local) BETWEEN ? AND ?
              AND sc.decision IN ('ADMIT','ADMIT_WITH_CHECK') AND t.session_id IS NOT NULL
            GROUP BY t.session_id
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        return {row["session_id"]: int(row["n"] or 0) for row in rows}

    def capacity_totals(self, scope: Scope) -> dict[str, int]:
        rows = self.capacity_rows(scope)
        capacity = sum(row["capacity"] for row in rows)
        reserved = sum(row["reserved"] for row in rows)
        return {
            "capacity": capacity,
            "reserved": reserved,
            "checked_in": sum(row["checked_in"] for row in rows),
            "utilization_bp": _share_bp(reserved, capacity),
        }

    # ------------------------------------------------------------------ #
    # Gate
    # ------------------------------------------------------------------ #

    def gate_summary(self, scope: Scope) -> dict[str, Any]:
        """Scan outcomes and their rates (§20)."""
        clause, venue_params = scope.venue_clause("s")
        rows = self.db.query(
            f"""
            SELECT s.decision, COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN s.override_actor_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS overrides,
                   COALESCE(SUM(CASE WHEN s.conflict_flag = 1 THEN 1 ELSE 0 END), 0) AS conflicts
            FROM scan_events s
            WHERE s.tenant_id = ?{clause} AND date(s.at_local) BETWEEN ? AND ?
            GROUP BY s.decision
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        by_decision = {row["decision"]: int(row["n"] or 0) for row in rows}
        overrides = sum(int(row["overrides"] or 0) for row in rows)
        conflicts = sum(int(row["conflicts"] or 0) for row in rows)
        total = sum(by_decision.values())
        admitted = sum(by_decision.get(d, 0) for d in enums.ADMIT_DECISIONS)
        return {
            "total_scans": total,
            "admitted": admitted,
            "refused": total - admitted,
            "success_bp": _share_bp(admitted, total),
            "failure_bp": _share_bp(total - admitted, total),
            "already_used": by_decision.get("REJECT_ALREADY_USED", 0),
            "expired": by_decision.get("REJECT_EXPIRED", 0),
            "wrong_date": by_decision.get("REJECT_WRONG_DATE", 0),
            "unknown_code": by_decision.get("REJECT_UNKNOWN_CODE", 0),
            "overrides": overrides,
            "conflicts": conflicts,
            "by_decision": [
                {"label": decision, "count": count}
                for decision, count in sorted(by_decision.items(), key=lambda kv: -kv[1])
            ],
        }

    def gate_activity(self, scope: Scope, *, limit: int = 200) -> list[dict[str, Any]]:
        clause, venue_params = scope.venue_clause("s")
        extra, extra_params = "", []
        result_filter = scope.filters.get("scan_result")
        if result_filter:
            values = result_filter if isinstance(result_filter, (list, tuple)) else [result_filter]
            extra = f" AND s.decision IN ({','.join('?' for _ in values)})"
            extra_params = list(values)
        rows = self.db.query(
            f"""
            SELECT s.at_local, s.decision, s.reason, s.override_actor_id,
                   t.ticket_number, b.booking_number,
                   ap.name_json AS ap_name, ap.code AS ap_code,
                   d.name AS device_name, d.code AS device_code
            FROM scan_events s
            LEFT JOIN tickets t ON t.id = s.ticket_id AND t.tenant_id = s.tenant_id
            LEFT JOIN bookings b ON b.id = s.booking_id AND b.tenant_id = s.tenant_id
            LEFT JOIN access_points ap ON ap.id = s.access_point_id AND ap.tenant_id = s.tenant_id
            LEFT JOIN devices d ON d.id = s.device_id AND d.tenant_id = s.tenant_id
            WHERE s.tenant_id = ?{clause} AND date(s.at_local) BETWEEN ? AND ?{extra}
            ORDER BY s.at_local DESC
            LIMIT ?
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to, *extra_params, int(limit)],
        )
        language = scope.filters.get("language") or "en"
        return [
            {
                "at_local": row["at_local"],
                "booking_number": row["booking_number"] or "",
                "ticket_number": row["ticket_number"] or "",
                "access_point": i18n_text(decode(row["ap_name"], {}), language, fallback=row["ap_code"] or ""),
                "device": row["device_name"] or row["device_code"] or "",
                "decision": row["decision"],
                "reason": row["reason"] or ("Override" if row["override_actor_id"] else ""),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Payments and finance
    # ------------------------------------------------------------------ #

    def payments_by_method(self, scope: Scope) -> list[dict[str, Any]]:
        """Method mix with success and failure rates (§14)."""
        clause, venue_params = scope.venue_clause("b")
        rows = self.db.query(
            f"""
            SELECT pm.method AS label,
                   COUNT(*) AS transactions,
                   -- Collected means AUTHORIZED or CAPTURED, matching
                   -- PaymentService.captured_total, which is the ceiling on refunds
                   -- (R17.6). Counting only CAPTURED would under-report takings.
                   COALESCE(SUM(CASE WHEN pm.status IN ('AUTHORIZED','CAPTURED')
                                     THEN pm.amount_minor ELSE 0 END), 0) AS amount_minor,
                   COALESCE(SUM(CASE WHEN pm.status IN ('AUTHORIZED','CAPTURED')
                                     THEN 1 ELSE 0 END), 0) AS captured,
                   COALESCE(SUM(CASE WHEN pm.status = 'FAILED' THEN 1 ELSE 0 END), 0) AS failed
            FROM payments pm
            JOIN bookings b ON b.id = pm.booking_id AND b.tenant_id = pm.tenant_id
            WHERE pm.tenant_id = ?{clause} AND date(pm.created_at) BETWEEN ? AND ?
            GROUP BY pm.method
            ORDER BY amount_minor DESC
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        refunds = self._refunds_by_method(scope)
        result = [
            {
                "label": row["label"],
                "transactions": int(row["transactions"] or 0),
                "amount_minor": int(row["amount_minor"] or 0),
                "success_bp": _share_bp(int(row["captured"] or 0), int(row["transactions"] or 0)),
                "failure_bp": _share_bp(int(row["failed"] or 0), int(row["transactions"] or 0)),
                "refund_minor": refunds.get(row["label"], 0),
            }
            for row in rows
        ]
        total = sum(entry["amount_minor"] for entry in result)
        for entry in result:
            entry["share_bp"] = _share_bp(entry["amount_minor"], total)
        return result

    def _refunds_by_method(self, scope: Scope) -> dict[str, int]:
        clause, venue_params = scope.venue_clause("b")
        rows = self.db.query(
            f"""
            SELECT pm.method AS label, COALESCE(SUM(r.amount_minor), 0) AS refund_minor
            FROM refunds r
            JOIN bookings b ON b.id = r.booking_id AND b.tenant_id = r.tenant_id
            LEFT JOIN payments pm ON pm.id = r.payment_id AND pm.tenant_id = r.tenant_id
            WHERE r.tenant_id = ?{clause} AND date(r.created_at) BETWEEN ? AND ?
            GROUP BY pm.method
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        return {(row["label"] or "UNKNOWN"): int(row["refund_minor"] or 0) for row in rows}

    def promotions(self, scope: Scope) -> list[dict[str, Any]]:
        """Usage and discount cost per promotion (§12).

        Deliberately no "ROI" or "incremental revenue" column: the platform has
        no counterfactual, and the spec says not to claim one.
        """
        clause, venue_params = scope.venue_clause("b")
        rows = self.db.query(
            f"""
            SELECT pr.id, pr.name_json, pr.code, pr.budget_minor, pr.budget_used_minor,
                   COUNT(rd.id) AS redemptions,
                   COALESCE(SUM(rd.amount_minor), 0) AS discount_minor,
                   COALESCE(SUM(b.gross_minor), 0) AS gross_minor,
                   COALESCE(SUM(b.net_minor), 0) AS net_minor
            FROM promotion_redemptions rd
            JOIN promotions pr ON pr.id = rd.promotion_id AND pr.tenant_id = rd.tenant_id
            JOIN bookings b ON b.id = rd.booking_id AND b.tenant_id = rd.tenant_id
            WHERE rd.tenant_id = ?{clause} AND date(rd.created_at) BETWEEN ? AND ?
              AND rd.state = 'APPLIED'
            GROUP BY rd.promotion_id
            ORDER BY discount_minor DESC
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        language = scope.filters.get("language") or "en"
        result = []
        for row in rows:
            redemptions = int(row["redemptions"] or 0)
            discount = int(row["discount_minor"] or 0)
            budget = int(row["budget_minor"] or 0)
            used = int(row["budget_used_minor"] or 0)
            result.append(
                {
                    "promotion": i18n_text(decode(row["name_json"], {}), language, fallback=row["code"]),
                    "redemptions": redemptions,
                    "gross_minor": int(row["gross_minor"] or 0),
                    "discount_minor": discount,
                    "net_minor": int(row["net_minor"] or 0),
                    "avg_discount_minor": _safe_div(discount, redemptions),
                    "budget_minor": budget,
                    "budget_used_minor": used,
                    "budget_remaining_minor": max(budget - used, 0) if budget else 0,
                    "budget_used_bp": _share_bp(used, budget),
                }
            )
        return result

    def partners(self, scope: Scope) -> list[dict[str, Any]]:
        """Partner production (§13)."""
        clause, venue_params = scope.venue_clause("b")
        rows = self.db.query(
            f"""
            SELECT pa.name AS partner, pa.commission_bp,
                   COUNT(b.id) AS bookings,
                   COALESCE(SUM(b.gross_minor), 0) AS gross_minor,
                   COALESCE(SUM(b.discount_minor), 0) AS discount_minor,
                   COALESCE(SUM(CASE WHEN b.status IN ({','.join('?' for _ in _NET_STATUSES)})
                                     THEN b.net_minor ELSE 0 END), 0) AS net_minor,
                   COALESCE(SUM(CASE WHEN b.status IN ('CANCELLED','VOIDED') THEN 1 ELSE 0 END), 0) AS cancelled
            FROM bookings b
            JOIN partners pa ON pa.id = b.partner_id AND pa.tenant_id = b.tenant_id
            WHERE b.tenant_id = ?{clause} AND b.visit_date BETWEEN ? AND ?
            GROUP BY b.partner_id
            ORDER BY net_minor DESC
            """,
            list(_NET_STATUSES) + [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        tickets = self._partner_tickets(scope)
        result = []
        for row in rows:
            net = int(row["net_minor"] or 0)
            counts = tickets.get(row["partner"], {"tickets": 0, "no_show": 0})
            result.append(
                {
                    "partner": row["partner"],
                    "bookings": int(row["bookings"] or 0),
                    "tickets": counts["tickets"],
                    "gross_minor": int(row["gross_minor"] or 0),
                    "discount_minor": int(row["discount_minor"] or 0),
                    "net_minor": net,
                    "commission_minor": round(net * int(row["commission_bp"] or 0) / 10_000),
                    "cancelled": int(row["cancelled"] or 0),
                    "no_show": counts["no_show"],
                }
            )
        return result

    def _partner_tickets(self, scope: Scope) -> dict[str, dict[str, int]]:
        clause, venue_params = scope.venue_clause("b")
        rows = self.db.query(
            f"""
            SELECT pa.name AS partner, COUNT(t.id) AS tickets,
                   COALESCE(SUM(CASE WHEN t.entries_used = 0 AND t.visit_date < ?
                                     THEN 1 ELSE 0 END), 0) AS no_show
            FROM tickets t
            JOIN bookings b ON b.id = t.booking_id AND b.tenant_id = t.tenant_id
            JOIN partners pa ON pa.id = b.partner_id AND pa.tenant_id = b.tenant_id
            WHERE t.tenant_id = ?{clause} AND t.visit_date BETWEEN ? AND ?
            GROUP BY b.partner_id
            """,
            [_today_iso(scope), scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        return {
            row["partner"]: {"tickets": int(row["tickets"] or 0), "no_show": int(row["no_show"] or 0)}
            for row in rows
        }

    def tax_series(self, scope: Scope, *, group_by: str = "monthly") -> list[dict[str, Any]]:
        """Tax report (§31): taxable base, VAT and service charge."""
        series = self.revenue_series(scope, group_by=group_by)
        where, params = scope.booking_where()
        rows = self.db.query(
            f"""
            SELECT b.visit_date, b.created_at, b.service_charge_minor, b.tax_minor,
                   b.gross_minor, b.discount_minor
            FROM bookings b {where}
            """,
            params,
        )
        basis = scope.filters.get("date_basis") or "visit_date"
        extra: dict[str, dict[str, int]] = {}
        for row in rows:
            key = (
                bucket_of(row["created_at"], group_by=group_by, tz_name=scope.timezone)
                if basis == "order_date"
                else bucket_of_date(row["visit_date"], group_by=group_by)
            )
            entry = extra.setdefault(key, {"service_charge_minor": 0, "tax_minor": 0})
            entry["service_charge_minor"] += int(row["service_charge_minor"] or 0)
            entry["tax_minor"] += int(row["tax_minor"] or 0)
        for entry in series:
            addition = extra.get(entry["bucket"], {})
            entry["service_charge_minor"] = addition.get("service_charge_minor", 0)
            entry["tax_minor"] = addition.get("tax_minor", 0)
            entry["taxable_minor"] = entry["net_minor"] - entry["tax_minor"]
        return series


def _today_iso(scope: Scope) -> str:
    """Today in the venue's timezone — a no-show is only a no-show once the day is past."""
    tz = timezone_for(scope.timezone)
    return _dt.datetime.now(tz).date().isoformat()


__all__ = ["Metrics", "Scope", "base_amount", "bucket_of", "bucket_of_date"]
