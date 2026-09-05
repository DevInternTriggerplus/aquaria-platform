"""What the platform thinks is worth a second look.

Every finding here answers three questions, because an alert that does not is
noise: what is unusual, how unusual, and what to do about it. Each carries a
drill-down target so the reader can go and look rather than guess.

Severity is used sparingly and deliberately (§15):

* ``CRITICAL`` — something is broken *now* and guests are affected. A gate
  offline during opening hours is critical; a high refund rate is not, however
  large, because nobody is standing at a closed door because of it.
* ``WARNING`` — a number is outside its normal band and a person should decide.
* ``INFO`` — worth knowing, no action implied.

Red is reserved for ``CRITICAL``. The spec is explicit about not painting minor
warnings in alarming red, and the platform's own accessibility rule means
severity is always carried by a text label too, never by colour alone (R68.4).

Thresholds are configuration, not constants: a venue that refunds 8% of orders
by design should not be told daily that it refunds too much.
"""

from __future__ import annotations

from typing import Any

from .metrics import Metrics, Scope, _share_bp

#: Config keys and their platform defaults. Basis points where a rate, minor
#: units where an amount.
THRESHOLD_DEFAULTS: dict[str, int] = {
    "reporting.threshold.refund_rate_bp": 500,            # 5% of net revenue
    "reporting.threshold.void_rate_bp": 300,              # 3% of transactions
    "reporting.threshold.manual_discount_bp": 2_000,      # a single 20% discount
    "reporting.threshold.manual_discount_share_bp": 500,  # 5% of sales discounted by hand
    "reporting.threshold.complimentary_share_bp": 300,    # 3% of tickets given away
    "reporting.threshold.payment_failure_bp": 1_000,      # 10% of attempts failing
    "reporting.threshold.capacity_near_full_bp": 9_000,   # 90% reserved
    "reporting.threshold.promotion_budget_bp": 8_000,     # 80% of budget consumed
    "reporting.threshold.device_offline_minutes": 15,
}

CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"


def _finding(
    *,
    key: str,
    severity: str,
    title: str,
    detail: str,
    metric: str,
    action: str,
    drill_to: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "severity": severity,
        "title": title,
        "detail": detail,
        "metric": metric,
        "action": action,
        "drill_to": drill_to,
        "filters": filters or {},
    }


class ExceptionEngine:
    """Evaluates the exception rules for a scope."""

    def __init__(self, db: Any, metrics: Metrics, config: Any) -> None:
        self.db = db
        self.metrics = metrics
        self.config = config

    def threshold(self, ctx: Any, key: str) -> int:
        value = self.config.get(ctx, key)
        if value is None:
            return THRESHOLD_DEFAULTS[key]
        return int(value)

    def evaluate(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        """Run every rule and return the findings, worst first."""
        findings: list[dict[str, Any]] = []
        findings.extend(self._money_rules(ctx, scope))
        findings.extend(self._discount_rules(ctx, scope))
        findings.extend(self._payment_rules(ctx, scope))
        findings.extend(self._capacity_rules(ctx, scope))
        findings.extend(self._promotion_rules(ctx, scope))
        findings.extend(self._device_rules(ctx, scope))
        findings.extend(self._recorded_exceptions(scope))
        order = {CRITICAL: 0, WARNING: 1, INFO: 2}
        findings.sort(key=lambda f: (order.get(f["severity"], 3), f["title"]))
        return findings

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #

    def _money_rules(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        totals = self.metrics.totals(scope)
        out: list[dict[str, Any]] = []

        refund_bp = _share_bp(totals["refund_minor"], totals["net_minor"] + totals["refund_minor"])
        limit = self.threshold(ctx, "reporting.threshold.refund_rate_bp")
        if refund_bp > limit:
            out.append(
                _finding(
                    key="refund_rate",
                    # Money already returned. Serious, but nothing is broken right
                    # now, so this is not painted red.
                    severity=WARNING,
                    title="Refund rate above normal",
                    detail=(
                        f"{_pct(refund_bp)} of takings were refunded, against a "
                        f"threshold of {_pct(limit)}."
                    ),
                    metric=_pct(refund_bp),
                    action="Review the refunds and who approved them.",
                    drill_to="refund_void",
                )
            )

        transactions = totals["bookings"]
        void_bp = _share_bp(totals["voided"], transactions)
        void_limit = self.threshold(ctx, "reporting.threshold.void_rate_bp")
        if void_bp > void_limit and totals["voided"] > 0:
            out.append(
                _finding(
                    key="void_rate",
                    severity=WARNING,
                    title="Void rate above normal",
                    detail=(
                        f"{totals['voided']} of {transactions} transactions were voided "
                        f"({_pct(void_bp)}), against a threshold of {_pct(void_limit)}."
                    ),
                    metric=_pct(void_bp),
                    action="Check whether voids are being used to correct mistakes or to conceal them.",
                    drill_to="refund_void",
                )
            )
        return out

    def _discount_rules(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        where, params = scope.booking_where()
        row = self.db.query_one(
            f"""
            SELECT COALESCE(SUM(b.discount_minor), 0) AS discount_minor,
                   COALESCE(SUM(b.gross_minor), 0) AS gross_minor,
                   COALESCE(SUM(CASE WHEN b.staff_actor_id IS NOT NULL
                                     THEN b.discount_minor ELSE 0 END), 0) AS staff_discount_minor
            FROM bookings b {where}
            """,
            params,
        )
        gross = int(row["gross_minor"] or 0)
        staff_discount = int(row["staff_discount_minor"] or 0)
        share_bp = _share_bp(staff_discount, gross)
        limit = self.threshold(ctx, "reporting.threshold.manual_discount_share_bp")
        if share_bp > limit:
            out.append(
                _finding(
                    key="manual_discount_share",
                    severity=WARNING,
                    title="Staff-applied discount above normal",
                    detail=(
                        f"{_pct(share_bp)} of gross sales was discounted by staff, "
                        f"against a threshold of {_pct(limit)}."
                    ),
                    metric=_pct(share_bp),
                    action="Review manual discounts by staff member.",
                    drill_to="manual_discounts",
                )
            )

        # Complimentary issuance: a real cost, and a common route for leakage.
        clause, venue_params = scope.venue_clause("b")
        comp = self.db.query_one(
            f"""
            SELECT COUNT(t.id) AS comp_tickets
            FROM tickets t
            JOIN bookings b ON b.id = t.booking_id AND b.tenant_id = t.tenant_id
            JOIN payments pm ON pm.booking_id = b.id AND pm.tenant_id = b.tenant_id
            WHERE t.tenant_id = ?{clause} AND t.visit_date BETWEEN ? AND ?
              AND pm.method = 'COMPLIMENTARY'
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        tickets = self.metrics.ticket_counts(scope)["issued"]
        comp_tickets = int(comp["comp_tickets"] or 0)
        comp_bp = _share_bp(comp_tickets, tickets)
        comp_limit = self.threshold(ctx, "reporting.threshold.complimentary_share_bp")
        if comp_tickets and comp_bp > comp_limit:
            out.append(
                _finding(
                    key="complimentary_share",
                    severity=WARNING,
                    title="Complimentary ticket usage above normal",
                    detail=(
                        f"{comp_tickets} of {tickets} tickets were complimentary "
                        f"({_pct(comp_bp)}), against a threshold of {_pct(comp_limit)}."
                    ),
                    metric=f"{comp_tickets} tickets",
                    action="Check the authorising reference on each complimentary issue.",
                    drill_to="complimentary",
                )
            )
        return out

    def _payment_rules(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        methods = self.metrics.payments_by_method(scope)
        attempts = sum(entry["transactions"] for entry in methods)
        failures = sum(
            round(entry["transactions"] * entry["failure_bp"] / 10_000) for entry in methods
        )
        failure_bp = _share_bp(failures, attempts)
        limit = self.threshold(ctx, "reporting.threshold.payment_failure_bp")
        if attempts and failure_bp > limit:
            return [
                _finding(
                    key="payment_failures",
                    # Guests are being turned away from checkout right now.
                    severity=CRITICAL,
                    title="Payment failures elevated",
                    detail=(
                        f"{failures} of {attempts} payment attempts failed "
                        f"({_pct(failure_bp)}), against a threshold of {_pct(limit)}."
                    ),
                    metric=_pct(failure_bp),
                    action="Check the provider status and the payment methods on offer.",
                    drill_to="payments",
                )
            ]
        return []

    def _capacity_rules(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        limit = self.threshold(ctx, "reporting.threshold.capacity_near_full_bp")
        out: list[dict[str, Any]] = []
        for row in self.metrics.capacity_rows(scope):
            if not row["capacity"] or row["state"] in ("Cancelled", "Completed"):
                continue
            if row["utilization_bp"] >= 10_000:
                out.append(
                    _finding(
                        key=f"capacity_full:{row['session_id']}",
                        severity=INFO,
                        title=f"{row['label']} {row['start_time']} is full",
                        detail=f"{row['reserved']} of {row['capacity']} places reserved.",
                        metric="100%",
                        action="No action needed unless capacity can be released.",
                        drill_to="capacity",
                    )
                )
            elif row["utilization_bp"] >= limit:
                out.append(
                    _finding(
                        key=f"capacity_near:{row['session_id']}",
                        severity=WARNING,
                        title=f"{row['label']} {row['start_time']} is nearly full",
                        detail=(
                            f"{row['reserved']} of {row['capacity']} places reserved "
                            f"({_pct(row['utilization_bp'])})."
                        ),
                        metric=_pct(row["utilization_bp"]),
                        action="Consider releasing held allocation or opening a session.",
                        drill_to="capacity",
                    )
                )
        return out[:5]      # a wall of capacity notices is noise, not signal

    def _promotion_rules(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        limit = self.threshold(ctx, "reporting.threshold.promotion_budget_bp")
        out: list[dict[str, Any]] = []
        for row in self.metrics.promotions(scope):
            if not row["budget_minor"]:
                continue
            if row["budget_used_bp"] >= 10_000:
                out.append(
                    _finding(
                        key=f"promotion_exhausted:{row['promotion']}",
                        severity=INFO,
                        title=f"{row['promotion']} has used its whole budget",
                        detail="The promotion has stopped applying.",
                        metric="100%",
                        action="Extend the budget or let the campaign end.",
                        drill_to="promotions",
                    )
                )
            elif row["budget_used_bp"] >= limit:
                out.append(
                    _finding(
                        key=f"promotion_budget:{row['promotion']}",
                        severity=WARNING,
                        title=f"{row['promotion']} is near its budget limit",
                        detail=f"{_pct(row['budget_used_bp'])} of the budget is consumed.",
                        metric=_pct(row["budget_used_bp"]),
                        action="Decide whether to extend the budget before it stops applying.",
                        drill_to="promotions",
                    )
                )
        return out

    def _device_rules(self, ctx: Any, scope: Scope) -> list[dict[str, Any]]:
        """A gate or kiosk that has stopped reporting is the one true emergency."""
        minutes = self.threshold(ctx, "reporting.threshold.device_offline_minutes")
        clause, venue_params = scope.venue_clause("d")
        rows = self.db.query(
            f"""
            SELECT d.code, d.name, d.kind, d.status, d.last_seen_at, d.health_json
            FROM devices d
            WHERE d.tenant_id = ?{clause} AND d.status = 'ACTIVE'
            """,
            [scope.tenant_id, *venue_params],
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            stale = _minutes_since(row["last_seen_at"])
            if stale is None:
                continue
            if stale > minutes:
                out.append(
                    _finding(
                        key=f"device_offline:{row['code']}",
                        severity=CRITICAL,
                        title=f"{row['name'] or row['code']} is offline",
                        detail=(
                            f"No heartbeat for {int(stale)} minutes "
                            f"(threshold {minutes})."
                        ),
                        metric=f"{int(stale)} min",
                        action="Check power, network and the device itself.",
                        drill_to="devices",
                    )
                )
        return out

    def _recorded_exceptions(self, scope: Scope) -> list[dict[str, Any]]:
        """Exceptions other services already raised — reconciliation, sync conflicts."""
        clause, venue_params = scope.venue_clause("e")
        rows = self.db.query(
            f"""
            SELECT e.kind, e.severity, e.detail_json, e.created_at, e.state
            FROM exceptions_log e
            WHERE e.tenant_id = ?{clause} AND e.state = 'OPEN'
              AND date(e.created_at) BETWEEN ? AND ?
            ORDER BY e.created_at DESC LIMIT 20
            """,
            [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
        )
        severity_map = {"CRITICAL": CRITICAL, "HIGH": CRITICAL, "WARNING": WARNING, "LOW": INFO}
        return [
            _finding(
                key=f"logged:{row['kind']}:{row['created_at']}",
                severity=severity_map.get((row["severity"] or "").upper(), WARNING),
                title=_humanise(row["kind"]),
                detail="Raised automatically and still open.",
                metric=row["created_at"][:16].replace("T", " "),
                action="Open the exception report for the full record.",
                drill_to="exceptions",
            )
            for row in rows
        ]


def _pct(basis_points: int) -> str:
    """Basis points as a readable percentage, without spurious precision."""
    whole, remainder = divmod(abs(int(basis_points)), 100)
    sign = "-" if basis_points < 0 else ""
    if remainder == 0:
        return f"{sign}{whole}%"
    return f"{sign}{whole}.{remainder // 10}%"


def _humanise(kind: str | None) -> str:
    return (kind or "").replace("_", " ").capitalize() or "Exception"


def _minutes_since(instant_text: str | None) -> float | None:
    if not instant_text:
        return None
    import datetime as _dt

    from ..core.clock import parse_instant

    try:
        moment = parse_instant(instant_text)
    except (ValueError, TypeError):
        return None
    delta = _dt.datetime.now(_dt.timezone.utc) - moment
    return delta.total_seconds() / 60.0


__all__ = ["CRITICAL", "ExceptionEngine", "INFO", "THRESHOLD_DEFAULTS", "WARNING"]
