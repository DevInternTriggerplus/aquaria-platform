"""A09 Security Logging and Monitoring Failures.

Auditing already records what happened (:mod:`utp.core.audit`). This module answers
the harder question: *which patterns should wake somebody up*. R73.14 names four —
credential stuffing, authorization probing, abnormal refund activity and abnormal
export volume — and the requirements analysis adds insider abuse of override
permissions (D.2) and partner credential compromise (D.3).

Detection reads the audit log rather than keeping separate counters. That means an
alert can always be traced to the exact events that raised it, and a detector cannot
drift out of agreement with the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from ..core.clock import Clock, add_minutes, to_iso
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.ids import new_id

Severity = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]


@dataclass(frozen=True, slots=True)
class Detector:
    """One monitored pattern."""

    key: str
    title: str
    actions: tuple[str, ...]
    threshold: int
    window_minutes: int
    severity: Severity
    #: Group events by this audit field before comparing to the threshold.
    group_by: str = "actor_id"
    description: str = ""
    requirement: str = ""


#: The monitored set. Thresholds are starting points and are overridable per tenant;
#: they should be tuned against a baseline rather than trusted as delivered.
DETECTORS: tuple[Detector, ...] = (
    Detector(
        key="credential_stuffing",
        title="Credential stuffing",
        actions=("LOGIN_FAILED",),
        threshold=20,
        window_minutes=10,
        severity="CRITICAL",
        group_by="ip_address",
        description="Many failed logins from one source across different accounts.",
        requirement="R73.14",
    ),
    Detector(
        key="account_brute_force",
        title="Account brute force",
        actions=("LOGIN_FAILED",),
        threshold=8,
        window_minutes=15,
        severity="WARNING",
        group_by="target_id",
        description="Repeated failures against a single account.",
        requirement="R73.1",
    ),
    Detector(
        key="authorization_probing",
        title="Authorization probing",
        actions=("AUTHORIZATION_DENIED",),
        threshold=15,
        window_minutes=10,
        severity="WARNING",
        group_by="actor_id",
        description="One principal repeatedly attempting actions they do not hold.",
        requirement="R45.8, R73.14",
    ),
    Detector(
        key="cross_tenant_probing",
        title="Cross-tenant access attempts",
        actions=("CROSS_TENANT_ATTEMPT",),
        threshold=1,
        window_minutes=60,
        severity="CRITICAL",
        group_by="actor_id",
        description="Any attempt to reach another tenant's data is investigated.",
        requirement="R1.2, R44.6",
    ),
    Detector(
        key="abnormal_refunds",
        title="Abnormal refund activity",
        actions=("REFUND",),
        threshold=10,
        window_minutes=60,
        severity="ERROR",
        group_by="actor_id",
        description="Unusual refund volume by one actor.",
        requirement="R73.14",
    ),
    Detector(
        key="abnormal_exports",
        title="Abnormal export volume",
        actions=("EXPORT",),
        threshold=10,
        window_minutes=60,
        severity="ERROR",
        group_by="actor_id",
        description="Bulk data egress by one actor.",
        requirement="R41.7, R73.14",
    ),
    Detector(
        key="override_abuse",
        title="Override permission abuse",
        actions=(
            "OVERRIDE_ACCESS",
            "CAPACITY_OVERRIDE",
            "SEAT_PRICE_OVERRIDE",
            "SEAT_ELIGIBILITY_OVERRIDE",
            "MANUAL_DISCOUNT",
            "COMPLIMENTARY_ISSUE",
        ),
        threshold=15,
        window_minutes=1440,
        severity="WARNING",
        group_by="actor_id",
        description="Revenue-affecting overrides concentrated on one actor.",
        requirement="Analysis D.2",
    ),
    Detector(
        key="pii_harvesting",
        title="Personal data harvesting",
        actions=("PII_ACCESS",),
        threshold=150,
        window_minutes=60,
        severity="ERROR",
        group_by="actor_id",
        description="Unusual volume of unmasked personal data reads.",
        requirement="R12.24",
    ),
    Detector(
        key="permission_escalation_attempts",
        title="Permission change activity",
        actions=("PERMISSION_CHANGE", "ROLE_ASSIGN"),
        threshold=10,
        window_minutes=60,
        severity="WARNING",
        group_by="actor_id",
        description="Rapid permission changes, which can indicate account takeover.",
        requirement="R44",
    ),
    Detector(
        key="offline_scan_conflicts",
        title="Offline scan conflicts",
        actions=("OFFLINE_SCAN_CONFLICT",),
        threshold=3,
        window_minutes=1440,
        severity="ERROR",
        group_by="venue_id",
        description="Single-entry tickets admitted at more than one access point.",
        requirement="R32.8",
    ),
)

DETECTORS_BY_KEY: dict[str, Detector] = {d.key: d for d in DETECTORS}


@dataclass(slots=True)
class Alert:
    """A fired detection."""

    detector: str
    title: str
    severity: Severity
    group_field: str
    group_value: str | None
    count: int
    threshold: int
    window_minutes: int
    first_seen: str
    last_seen: str
    correlation_ids: tuple[str, ...] = ()
    requirement: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "title": self.title,
            "severity": self.severity,
            "group_field": self.group_field,
            "group_value": self.group_value,
            "count": self.count,
            "threshold": self.threshold,
            "window_minutes": self.window_minutes,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "correlation_ids": list(self.correlation_ids),
            "requirement": self.requirement,
        }


class SecurityMonitor:
    """Threshold detection over the audit log, plus exception raising."""

    def __init__(self, db: Database, clock: Clock, *, config=None, sink: Callable[[Alert], None] | None = None) -> None:
        self.db = db
        self.clock = clock
        self.config = config
        #: Where fired alerts go in production — SNS, PagerDuty, a SIEM. Tests capture.
        self.sink = sink

    def threshold_for(self, ctx: RequestContext, detector: Detector) -> int:
        if self.config is None:
            return detector.threshold
        override = self.config.get(
            ctx, f"security.threshold.{detector.key}", use_platform_default=False
        )
        return int(override) if override is not None else detector.threshold

    def evaluate(
        self, ctx: RequestContext, *, detectors: Sequence[Detector] | None = None
    ) -> list[Alert]:
        """Run every detector and return the alerts that fired."""
        alerts: list[Alert] = []
        for detector in detectors or DETECTORS:
            alerts.extend(self._evaluate_one(ctx, detector))
        for alert in alerts:
            self._record(ctx, alert)
        return alerts

    def _evaluate_one(self, ctx: RequestContext, detector: Detector) -> list[Alert]:
        since = to_iso(add_minutes(self.clock.now(), -detector.window_minutes))
        placeholders = ", ".join("?" for _ in detector.actions)
        column = {
            "actor_id": "actor_id",
            "ip_address": "ip_address",
            "target_id": "target_id",
            "venue_id": "venue_id",
        }.get(detector.group_by, "actor_id")
        rows = self.db.query(
            f"""
            SELECT {column} AS group_value, COUNT(*) AS event_count,
                   MIN(at_utc) AS first_seen, MAX(at_utc) AS last_seen
            FROM audit_events
            WHERE tenant_id = ? AND action IN ({placeholders}) AND at_utc >= ?
            GROUP BY {column}
            """,
            (ctx.tenant_id, *detector.actions, since),
        )
        threshold = self.threshold_for(ctx, detector)
        alerts: list[Alert] = []
        for row in rows:
            count = int(row["event_count"])
            if count < threshold:
                continue
            correlations = [
                r["correlation_id"]
                for r in self.db.query(
                    f"""
                    SELECT DISTINCT correlation_id FROM audit_events
                    WHERE tenant_id = ? AND action IN ({placeholders}) AND at_utc >= ?
                      AND IFNULL({column}, '') = IFNULL(?, '')
                    LIMIT 20
                    """,
                    (ctx.tenant_id, *detector.actions, since, row["group_value"]),
                )
                if r["correlation_id"]
            ]
            alerts.append(
                Alert(
                    detector=detector.key,
                    title=detector.title,
                    severity=detector.severity,
                    group_field=detector.group_by,
                    group_value=row["group_value"],
                    count=count,
                    threshold=threshold,
                    window_minutes=detector.window_minutes,
                    first_seen=row["first_seen"],
                    last_seen=row["last_seen"],
                    correlation_ids=tuple(correlations),
                    requirement=detector.requirement,
                )
            )
        return alerts

    def _record(self, ctx: RequestContext, alert: Alert) -> str:
        """Persist as an operational exception so it appears on the ops dashboard (R71.6)."""
        exception_id = new_id("exc")
        self.db.insert(
            "exceptions_log",
            {
                "id": exception_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": alert.group_value if alert.group_field == "venue_id" else ctx.venue_id,
                "kind": f"SECURITY_{alert.detector.upper()}",
                "severity": alert.severity,
                "entity_type": "security_alert",
                "entity_id": alert.group_value or "-",
                "detail_json": alert.as_dict(),
                "state": "OPEN",
                "created_at": to_iso(self.clock.now()),
            },
        )
        if self.sink is not None:
            self.sink(alert)
        return exception_id

    def open_alerts(self, ctx: RequestContext) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM exceptions_log WHERE tenant_id = ? AND state = 'OPEN' "
            "AND kind LIKE 'SECURITY_%' ORDER BY created_at DESC",
            (ctx.tenant_id,),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = decode(item.pop("detail_json"), {})
            out.append(item)
        return out

    def acknowledge(self, ctx: RequestContext, exception_id: str, *, actor_id: str, note: str) -> dict[str, Any]:
        self.db.update(
            "exceptions_log",
            exception_id,
            {"state": "ACKNOWLEDGED", "resolved_at": to_iso(self.clock.now()), "resolved_by": actor_id},
            tenant_id=ctx.tenant_id,
        )
        return {"exception_id": exception_id, "state": "ACKNOWLEDGED", "note": note}

    # ------------------------------------------------------------------ #
    # Periodic review reports (analysis D.2)
    # ------------------------------------------------------------------ #

    def override_review(self, ctx: RequestContext, *, days: int = 30) -> dict[str, Any]:
        """Who used revenue-affecting overrides, and how often.

        Auditing alone does not deter insider abuse if nobody looks; this is the report
        that makes looking routine.
        """
        since = to_iso(add_minutes(self.clock.now(), -days * 24 * 60))
        actions = DETECTORS_BY_KEY["override_abuse"].actions
        placeholders = ", ".join("?" for _ in actions)
        rows = self.db.query(
            f"""
            SELECT actor_id, action, COUNT(*) AS event_count, MAX(at_utc) AS last_used
            FROM audit_events
            WHERE tenant_id = ? AND action IN ({placeholders}) AND at_utc >= ?
            GROUP BY actor_id, action
            ORDER BY event_count DESC
            """,
            (ctx.tenant_id, *actions, since),
        )
        by_actor: dict[str, dict[str, Any]] = {}
        for row in rows:
            actor = row["actor_id"] or "unknown"
            entry = by_actor.setdefault(actor, {"actor_id": actor, "total": 0, "by_action": {}})
            entry["total"] += int(row["event_count"])
            entry["by_action"][row["action"]] = int(row["event_count"])
            entry["last_used"] = max(entry.get("last_used", ""), row["last_used"] or "")
        missing_reason = int(
            self.db.scalar(
                f"""
                SELECT COUNT(*) FROM audit_events
                WHERE tenant_id = ? AND action IN ({placeholders}) AND at_utc >= ?
                  AND (reason IS NULL OR TRIM(reason) = '')
                """,
                (ctx.tenant_id, *actions, since),
                default=0,
            )
        )
        return {
            "window_days": days,
            "actors": sorted(by_actor.values(), key=lambda a: -a["total"]),
            "events_without_reason": missing_reason,
            "generated_at": to_iso(self.clock.now()),
        }

    def partner_anomalies(self, ctx: RequestContext, *, days: int = 7, spike_ratio: float = 3.0) -> list[dict[str, Any]]:
        """Partner volume spikes, which can indicate credential compromise (analysis D.3)."""
        since = to_iso(add_minutes(self.clock.now(), -days * 24 * 60))
        rows = self.db.query(
            """
            SELECT partner_id, substr(created_at, 1, 10) AS day, COUNT(*) AS bookings
            FROM bookings
            WHERE tenant_id = ? AND partner_id IS NOT NULL AND created_at >= ?
            GROUP BY partner_id, day ORDER BY partner_id, day
            """,
            (ctx.tenant_id, since),
        )
        by_partner: dict[str, list[int]] = {}
        for row in rows:
            by_partner.setdefault(row["partner_id"], []).append(int(row["bookings"]))
        anomalies: list[dict[str, Any]] = []
        for partner_id, counts in by_partner.items():
            if len(counts) < 3:
                continue
            *history, latest = counts
            baseline = sum(history) / len(history)
            if baseline > 0 and latest >= baseline * spike_ratio:
                anomalies.append(
                    {
                        "partner_id": partner_id,
                        "latest_day_bookings": latest,
                        "baseline_daily_bookings": round(baseline, 2),
                        "ratio": round(latest / baseline, 2),
                        "severity": "WARNING",
                    }
                )
        return anomalies


__all__ = ["DETECTORS", "DETECTORS_BY_KEY", "Alert", "Detector", "SecurityMonitor", "Severity"]
