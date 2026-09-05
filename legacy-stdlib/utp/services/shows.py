"""Show schedule: daily timetable, recurrence, date overrides, publish, customer views.

A Show is an ``Experience`` with ``kind='SHOW'``; a ShowSession is a ``sessions`` row
with ``kind='SHOW'`` (R18.2). Sharing storage with product sessions is what makes
R25.8 true by construction — a show reservation contends for capacity through the
identical mechanism as a timed-entry ticket.

Three layers resolve for any date, and ``sessions.source`` records which produced
each row so the back office can always tell them apart (R29.6):

1. pattern-derived occurrences (R21),
2. date-specific overrides that add / replace / suppress for one date (R22),
3. one-off additions made directly for that date (R20.1).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Sequence

from ..core.audit import AuditLog
from ..core.clock import (
    Clock,
    combine_local,
    end_time_from_duration,
    minutes_between,
    operating_date,
    parse_date,
    to_iso,
    weekday_code,
)
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConfirmationRequired, ConflictError, ValidationError
from ..core.i18n import text as i18n_text
from ..core.ids import new_correlation_id, new_id
from .authz import AuthorizationService
from .calendar_rules import CalendarService
from .catalog import CatalogService
from .inventory import InventoryService

#: R21.2
RECURRENCE_KINDS: tuple[str, ...] = (
    "EVERY_DAY",
    "SELECTED_WEEKDAYS",
    "WEEKDAYS_ONLY",
    "WEEKENDS_ONLY",
    "DATE_RANGE",
)
#: R22.4
OVERRIDE_MODES: tuple[str, ...] = ("ADD", "REPLACE", "SUPPRESS")
#: R23.3
CONFLICT_STRATEGIES: tuple[str, ...] = ("SKIP", "REPLACE", "ADD_ALONGSIDE")

_WEEKEND = ("SAT", "SUN")
_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI")

#: R26.7 / R68.4 — two independent cues per live state, never colour alone.
LIVE_STATE_PRESENTATION: dict[str, dict[str, str]] = {
    "HAPPENING_NOW": {"label": "Happening now", "icon": "play-circle", "colour": "#0E7C86", "emphasis": "strong"},
    "STARTING_SOON": {"label": "Starting soon", "icon": "clock", "colour": "#B25E09", "emphasis": "strong"},
    "UPCOMING": {"label": "Upcoming", "icon": "calendar", "colour": "#0B3C5D", "emphasis": "normal"},
    "FINISHED": {"label": "Finished", "icon": "check", "colour": "#8A8F98", "emphasis": "muted"},
    "CANCELLED": {"label": "Cancelled", "icon": "x-octagon", "colour": "#8A2B2B", "emphasis": "struck"},
}


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """Half-open interval overlap on wall-clock times."""
    return start_a < end_b and start_b < end_a


def _recurrence_matches(recurrence: dict[str, Any], date: str) -> bool:
    kind = recurrence.get("kind")
    code = weekday_code(date)
    if kind == "EVERY_DAY" or kind == "DATE_RANGE":
        return True
    if kind == "SELECTED_WEEKDAYS":
        return code in (recurrence.get("weekdays") or [])
    if kind == "WEEKDAYS_ONLY":
        return code in _WEEKDAYS
    if kind == "WEEKENDS_ONLY":
        return code in _WEEKEND
    return False


def _minus_minutes(time_text: str, minutes: int) -> str:
    hour, _, minute = time_text.partition(":")
    total = (int(hour) * 60 + int(minute) - int(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


@dataclass(slots=True)
class SessionConflict:
    """A time or location clash the administrator must acknowledge."""

    kind: str
    session_id: str
    show_name: str
    start_time: str
    end_time: str
    area_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "show_name": self.show_name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "area_id": self.area_id,
        }


class ShowService:
    """Show scheduling and customer-facing timetables."""

    notifications: Any = None  #: injected by Platform
    customers: Any = None

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
        *,
        inventory: InventoryService,
        catalog: CatalogService,
        calendar: CalendarService,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config
        self.inventory = inventory
        self.catalog = catalog
        self.calendar = calendar

    def _venue(self, ctx: RequestContext, venue_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "venues", venue_id, entity="venue")
        record["name"] = decode(record.pop("name_json"), {})
        record["operating_hours"] = decode(record.pop("operating_hours_json"), {})
        return record

    # ------------------------------------------------------------------ #
    # Individual sessions (R20)
    # ------------------------------------------------------------------ #

    def create_show_session(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        experience_id: str,
        date: str,
        start_time: str,
        duration_minutes: int | None = None,
        end_time: str | None = None,
        area_id: str | None = None,
        capacity: int | None = None,
        reservation_mode: str | None = None,
        check_in_required: bool | None = None,
        product_id: str | None = None,
        publication_state: str | None = None,
        customer_visible: bool = True,
        waiting_list_enabled: bool = False,
        booking_cutoff_minutes: int | None = None,
        notes: str | None = None,
        confirm_conflicts: bool = False,
        source: str = "MANUAL",
        pattern_id: str | None = None,
        override_id: str | None = None,
        require_permission: bool = True,
    ) -> dict[str, Any]:
        """Schedule one occurrence, checking for clashes (R20.3, R20.5, R20.6, R20.7)."""
        if require_permission:
            self.authz.require_page(ctx, "Show Schedule", "ADD")
            self.authz.require_venue(ctx, venue_id)
        show = self.catalog.get_experience(ctx, experience_id)
        if show["kind"] != "SHOW":
            raise ValidationError({"experience_id": "That experience is not a show."})
        if show["venue_id"] != venue_id:
            raise ValidationError({"experience_id": "That show belongs to a different venue."})
        duration = duration_minutes or show.get("default_duration_minutes") or 30
        resolved_end = end_time or end_time_from_duration(start_time, int(duration))
        resolved_area = area_id or show.get("area_id")
        mode = reservation_mode or show.get("reservation_mode") or "NONE"

        conflicts = self.detect_conflicts(
            ctx,
            venue_id=venue_id,
            date=date,
            start_time=start_time,
            end_time=resolved_end,
            experience_id=experience_id,
            area_id=resolved_area,
        )
        if conflicts and not confirm_conflicts:
            raise ConfirmationRequired(
                "This session overlaps with another. Confirm to schedule it anyway.",
                code="schedule_conflict",
                details={"conflicts": [c.as_dict() for c in conflicts], "requires_confirmation": True},
            )
        default_publication = (
            "DRAFT"
            if self.config.get_bool(ctx, "schedule.publish_required", venue_id=venue_id)
            else "PUBLISHED"
        )
        eligibility = show.get("eligibility") or {}
        session = self.inventory.create_session(
            ctx,
            venue_id=venue_id,
            date=date,
            start_time=start_time,
            end_time=resolved_end,
            kind="SHOW",
            experience_id=experience_id,
            product_id=product_id,
            area_id=resolved_area,
            capacity=capacity,
            reservation_mode=mode,
            booking_required=mode == "REQUIRED",
            check_in_required=bool(
                eligibility.get("check_in_required") if check_in_required is None else check_in_required
            ),
            waiting_list_enabled=waiting_list_enabled,
            customer_visible=customer_visible,
            publication_state=publication_state or default_publication,
            status="SCHEDULED",
            booking_cutoff_minutes=booking_cutoff_minutes,
            notes=notes,
            source=source,
            pattern_id=pattern_id,
            override_id=override_id,
            require_permission=False,
        )
        self.audit.record(
            ctx.for_venue(venue_id),
            "CONFIG_CHANGE",
            target_type="show_session",
            target_id=session["id"],
            new={
                "show": show["code"],
                "date": date,
                "start_time": start_time,
                "area_id": resolved_area,
                "reservation_mode": mode,
                "capacity": capacity,
                "source": source,
                "conflicts_confirmed": bool(conflicts),
            },
        )
        return session

    def quick_add(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        experience_id: str,
        date: str,
        start_time: str,
        area_id: str | None = None,
        duration_minutes: int | None = None,
        reservation_mode: str | None = None,
        confirm_conflicts: bool = False,
    ) -> dict[str, Any]:
        """R29.3 — show, date, time, location, duration, reservation mode; rest defaulted."""
        return self.create_show_session(
            ctx,
            venue_id=venue_id,
            experience_id=experience_id,
            date=date,
            start_time=start_time,
            area_id=area_id,
            duration_minutes=duration_minutes,
            reservation_mode=reservation_mode,
            confirm_conflicts=confirm_conflicts,
        )

    def detect_conflicts(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        start_time: str,
        end_time: str,
        experience_id: str | None = None,
        area_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> list[SessionConflict]:
        """Same-show overlaps (R20.5) and same-location overlaps (R20.6)."""
        rows = self.db.query(
            "SELECT * FROM sessions WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = 'SHOW' "
            "AND status <> 'CANCELLED'",
            (ctx.tenant_id, venue_id, date),
        )
        conflicts: list[SessionConflict] = []
        for row in rows:
            if exclude_session_id and row["id"] == exclude_session_id:
                continue
            if not _overlaps(start_time, end_time, row["start_time"], row["end_time"]):
                continue
            same_show = bool(experience_id and row["experience_id"] == experience_id)
            same_area = bool(area_id and row["area_id"] == area_id)
            if not same_show and not same_area:
                continue
            show_name = ""
            if row["experience_id"]:
                experience = self.catalog.get_experience(ctx, row["experience_id"])
                show_name = i18n_text(experience["name"], ctx.language, fallback=experience["code"])
            conflicts.append(
                SessionConflict(
                    kind="SAME_SHOW_OVERLAP" if same_show else "LOCATION_OVERLAP",
                    session_id=row["id"],
                    show_name=show_name,
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    area_id=row["area_id"],
                )
            )
        return conflicts

    # ------------------------------------------------------------------ #
    # Recurring patterns (R21)
    # ------------------------------------------------------------------ #

    def create_pattern(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        experience_id: str,
        start_time: str,
        duration_minutes: int,
        valid_from: str,
        recurrence: dict[str, Any],
        valid_until: str | None = None,
        area_id: str | None = None,
        capacity: int | None = None,
        reservation_mode: str | None = None,
        check_in_required: bool = False,
        product_id: str | None = None,
        publication_state: str | None = None,
        materialize: bool = True,
    ) -> dict[str, Any]:
        """Define a repeating pattern and materialize its occurrences (R21.1 - R21.3)."""
        self.authz.require_page(ctx, "Show Schedule", "ADD")
        self.authz.require_venue(ctx, venue_id)
        if recurrence.get("kind") not in RECURRENCE_KINDS:
            raise ValidationError({"recurrence.kind": f"Choose one of: {', '.join(RECURRENCE_KINDS)}."})
        if recurrence["kind"] == "SELECTED_WEEKDAYS" and not recurrence.get("weekdays"):
            raise ValidationError({"recurrence.weekdays": "Select at least one weekday."})
        parse_date(valid_from)
        if valid_until and parse_date(valid_until) < parse_date(valid_from):
            raise ValidationError({"valid_until": "The end date must not be before the start date."})
        pattern_id = new_id("pat")
        default_publication = (
            "DRAFT"
            if self.config.get_bool(ctx, "schedule.publish_required", venue_id=venue_id)
            else "PUBLISHED"
        )
        self.db.insert(
            "session_patterns",
            {
                "id": pattern_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "experience_id": experience_id,
                "product_id": product_id,
                "area_id": area_id,
                "kind": "SHOW",
                "start_time": start_time,
                "duration_minutes": int(duration_minutes),
                "capacity": capacity,
                "reservation_mode": reservation_mode or "NONE",
                "check_in_required": 1 if check_in_required else 0,
                "recurrence_json": recurrence,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "publication_state": publication_state or default_publication,
                "status": "ACTIVE",
                "created_at": to_iso(self.clock.now()),
                "created_by": ctx.principal.id,
            },
        )
        self.audit.record(
            ctx.for_venue(venue_id),
            "CONFIG_CHANGE",
            target_type="session_pattern",
            target_id=pattern_id,
            new={
                "experience_id": experience_id,
                "start_time": start_time,
                "recurrence": recurrence,
                "valid_from": valid_from,
                "valid_until": valid_until,
            },
        )
        result: dict[str, Any] = {"pattern_id": pattern_id}
        if materialize:
            result.update(self.materialize_pattern(ctx, pattern_id))
        return result

    def get_pattern(self, ctx: RequestContext, pattern_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "session_patterns", pattern_id, entity="session_pattern")
        record["recurrence"] = decode(record.pop("recurrence_json"), {})
        return record

    def materialize_pattern(
        self, ctx: RequestContext, pattern_id: str, *, until: str | None = None
    ) -> dict[str, Any]:
        """Create implied sessions, reporting which dates were skipped and why (R21.4)."""
        pattern = self.get_pattern(ctx, pattern_id)
        venue = self._venue(ctx, pattern["venue_id"])
        horizon = self.config.get_int(ctx, "schedule.materialization_horizon_days", venue_id=venue["id"])
        today = operating_date(self.clock.now(), venue["timezone"], int(venue.get("day_boundary_hour") or 0))
        window_start = max(parse_date(pattern["valid_from"]), today)
        window_end = parse_date(until) if until else today + _dt.timedelta(days=horizon)
        if pattern["valid_until"]:
            window_end = min(window_end, parse_date(pattern["valid_until"]))
        existing = {
            row["date"]
            for row in self.db.query(
                "SELECT date FROM sessions WHERE tenant_id = ? AND pattern_id = ?",
                (ctx.tenant_id, pattern_id),
            )
        }
        created: list[str] = []
        skipped: list[dict[str, str]] = []
        current = window_start
        while current <= window_end:
            date_text = current.isoformat()
            current += _dt.timedelta(days=1)
            if date_text in existing or not _recurrence_matches(pattern["recurrence"], date_text):
                continue
            reason = self._non_operating_reason(ctx, venue, date_text)
            if reason is not None:
                skipped.append({"date": date_text, "reason": reason})
                continue
            session = self.create_show_session(
                ctx,
                venue_id=pattern["venue_id"],
                experience_id=pattern["experience_id"],
                date=date_text,
                start_time=pattern["start_time"],
                duration_minutes=int(pattern["duration_minutes"]),
                area_id=pattern["area_id"],
                capacity=pattern["capacity"],
                reservation_mode=pattern["reservation_mode"],
                check_in_required=bool(pattern["check_in_required"]),
                product_id=pattern["product_id"],
                publication_state=pattern["publication_state"],
                confirm_conflicts=True,
                source="PATTERN",
                pattern_id=pattern_id,
                require_permission=False,
            )
            created.append(session["id"])
        self.db.update(
            "session_patterns",
            pattern_id,
            {"materialized_until": window_end.isoformat()},
            tenant_id=ctx.tenant_id,
        )
        return {
            "pattern_id": pattern_id,
            "created": len(created),
            "session_ids": created,
            "skipped": skipped,
            "materialized_until": window_end.isoformat(),
        }

    def _non_operating_reason(self, ctx: RequestContext, venue: dict[str, Any], date: str) -> str | None:
        markings = {
            row["kind"]
            for row in self.db.query(
                "SELECT kind FROM operating_calendar WHERE tenant_id = ? AND venue_id = ? AND date = ?",
                (ctx.tenant_id, venue["id"], date),
            )
        }
        if "CLOSED" in markings:
            return "venue_closed"
        if "BLACKOUT" in markings:
            return "blackout_date"
        weekdays = self.config.get(ctx, "booking.available_weekdays", venue_id=venue["id"]) or []
        if weekdays and weekday_code(date) not in weekdays:
            return "non_operating_weekday"
        return None

    def extend_materialization_horizon(
        self, ctx: RequestContext, *, venue_id: str | None = None
    ) -> dict[str, Any]:
        """Roll the horizon forward as time advances (R21.7)."""
        sql = "SELECT id FROM session_patterns WHERE tenant_id = ? AND status = 'ACTIVE' AND ended_at IS NULL"
        params: list[Any] = [ctx.tenant_id]
        if venue_id:
            sql += " AND venue_id = ?"
            params.append(venue_id)
        created = 0
        patterns = 0
        for row in self.db.query(sql, params):
            created += self.materialize_pattern(ctx, row["id"])["created"]
            patterns += 1
        return {"patterns": patterns, "sessions_created": created}

    def edit_pattern(
        self,
        ctx: RequestContext,
        pattern_id: str,
        changes: dict[str, Any],
        *,
        scope: str = "FUTURE",
        range_from: str | None = None,
        range_to: str | None = None,
        confirmed: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Edit a pattern. Past occurrences are never touched and overrides survive (R21.5, R21.6)."""
        if scope not in ("FUTURE", "RANGE"):
            raise ValidationError({"scope": "Scope must be FUTURE or RANGE."})
        self.authz.require_page(
            ctx, "Show Schedule", "EDIT", target_type="session_pattern", target_id=pattern_id
        )
        pattern = self.get_pattern(ctx, pattern_id)
        venue = self._venue(ctx, pattern["venue_id"])
        self.authz.require_venue(ctx, venue["id"])
        today = operating_date(self.clock.now(), venue["timezone"]).isoformat()
        lower = today if scope == "FUTURE" else (range_from or today)
        upper = range_to if scope == "RANGE" else None

        affected = self._pattern_occurrences(ctx, pattern_id, date_from=lower, date_to=upper)
        reserved = [s for s in affected if s["reservation_count"] > 0]
        overridden = set(self._override_dates(ctx, venue["id"], lower, upper))
        if (reserved or overridden) and not confirmed:
            raise ConfirmationRequired(
                "Some occurrences already have reservations or date-specific overrides.",
                code="pattern_edit_affects_reservations",
                details={
                    "reserved_occurrences": [
                        {
                            "session_id": s["id"],
                            "date": s["date"],
                            "start_time": s["start_time"],
                            "reservation_count": s["reservation_count"],
                        }
                        for s in reserved
                    ],
                    "total_reservations": sum(s["reservation_count"] for s in reserved),
                    "dates_with_overrides": sorted(overridden),
                    "requires_confirmation": True,
                },
            )
        allowed = {
            "start_time",
            "duration_minutes",
            "capacity",
            "reservation_mode",
            "check_in_required",
            "area_id",
            "valid_until",
            "recurrence",
        }
        payload = {k: v for k, v in changes.items() if k in allowed}
        if "recurrence" in payload:
            payload["recurrence_json"] = payload.pop("recurrence")
        if not payload:
            raise ValidationError({"changes": "No editable fields were supplied."})
        correlation = new_correlation_id()
        updated = 0
        with self.db.transaction():
            self.db.update("session_patterns", pattern_id, payload, tenant_id=ctx.tenant_id)
            for session in affected:
                if session["date"] in overridden:
                    continue  # never silently discard an override
                session_payload: dict[str, Any] = {}
                if "start_time" in changes:
                    duration = int(changes.get("duration_minutes", pattern["duration_minutes"]))
                    session_payload["start_time"] = changes["start_time"]
                    session_payload["end_time"] = end_time_from_duration(changes["start_time"], duration)
                elif "duration_minutes" in changes:
                    session_payload["end_time"] = end_time_from_duration(
                        session["start_time"], int(changes["duration_minutes"])
                    )
                for field in ("capacity", "reservation_mode", "area_id"):
                    if field in changes:
                        session_payload[field] = changes[field]
                if session_payload:
                    self.db.update("sessions", session["id"], session_payload, tenant_id=ctx.tenant_id)
                    updated += 1
            self.audit.record(
                ctx.for_venue(venue["id"]),
                "BULK_SCHEDULE_UPDATE",
                target_type="session_pattern",
                target_id=pattern_id,
                previous={k: pattern.get(k) for k in payload},
                new={"changes": changes, "scope": scope, "occurrences_updated": updated},
                reason=reason,
                severity="WARNING",
                extra_correlation=correlation,
            )
        return {
            "pattern_id": pattern_id,
            "scope": scope,
            "occurrences_updated": updated,
            "overrides_preserved": sorted(overridden),
            "correlation_id": correlation,
        }

    def end_pattern_early(
        self, ctx: RequestContext, pattern_id: str, *, effective_from: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Stop a pattern without deleting occurrences already reserved (R21.8)."""
        self.authz.require_page(
            ctx, "Show Schedule", "EDIT", target_type="session_pattern", target_id=pattern_id
        )
        pattern = self.get_pattern(ctx, pattern_id)
        future = self._pattern_occurrences(ctx, pattern_id, date_from=effective_from)
        removable = [s for s in future if s["reservation_count"] == 0]
        retained = [s for s in future if s["reservation_count"] > 0]
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "session_patterns",
                pattern_id,
                {"status": "ENDED", "ended_at": now, "valid_until": effective_from},
                tenant_id=ctx.tenant_id,
            )
            for session in removable:
                self.inventory.set_status(
                    ctx, session["id"], "CANCELLED", reason=reason or "Pattern ended", require_permission=False
                )
            self.audit.record(
                ctx.for_venue(pattern["venue_id"]),
                "BULK_SCHEDULE_UPDATE",
                target_type="session_pattern",
                target_id=pattern_id,
                new={
                    "ended_at": now,
                    "effective_from": effective_from,
                    "cancelled": len(removable),
                    "retained_with_reservations": len(retained),
                },
                reason=reason,
                severity="WARNING",
            )
        return {
            "pattern_id": pattern_id,
            "cancelled": len(removable),
            "retained_with_reservations": [
                {"session_id": s["id"], "date": s["date"], "reservation_count": s["reservation_count"]}
                for s in retained
            ],
        }

    def _pattern_occurrences(
        self,
        ctx: RequestContext,
        pattern_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = ["SELECT * FROM sessions WHERE tenant_id = ? AND pattern_id = ? AND status <> 'CANCELLED'"]
        params: list[Any] = [ctx.tenant_id, pattern_id]
        if date_from:
            sql.append("AND date >= ?")
            params.append(date_from)
        if date_to:
            sql.append("AND date <= ?")
            params.append(date_to)
        sql.append("ORDER BY date, start_time")
        out = []
        for row in self.db.query(" ".join(sql), params):
            record = dict(row)
            record["reservation_count"] = self.inventory.confirmed_reservation_count(ctx, row["id"])
            out.append(record)
        return out

    # ------------------------------------------------------------------ #
    # Date overrides (R22)
    # ------------------------------------------------------------------ #

    def create_override(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        mode: str,
        sessions: Sequence[dict[str, Any]] = (),
        suppress_session_ids: Sequence[str] = (),
        experience_id: str | None = None,
        reason: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Change one date without touching the pattern (R22.1, R22.2, R22.4)."""
        if mode not in OVERRIDE_MODES:
            raise ValidationError({"mode": f"Mode must be one of: {', '.join(OVERRIDE_MODES)}."})
        self.authz.require_page(ctx, "Show Schedule", "ADD")
        self.authz.require_venue(ctx, venue_id)
        parse_date(date)
        existing = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM sessions WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = 'SHOW' "
                "AND status <> 'CANCELLED'",
                (ctx.tenant_id, venue_id, date),
            )
        ]
        to_remove: list[dict[str, Any]] = []
        if mode == "REPLACE":
            to_remove = existing
        elif mode == "SUPPRESS":
            wanted = set(suppress_session_ids)
            to_remove = [r for r in existing if r["id"] in wanted]
        reserved = [
            {**s, "reservation_count": self.inventory.confirmed_reservation_count(ctx, s["id"])}
            for s in to_remove
        ]
        reserved = [s for s in reserved if s["reservation_count"] > 0]
        if reserved and not confirmed:
            raise ConfirmationRequired(
                "Some sessions on this date hold confirmed reservations.",
                code="override_affects_reservations",
                details={
                    "affected_sessions": [
                        {
                            "session_id": s["id"],
                            "start_time": s["start_time"],
                            "reservation_count": s["reservation_count"],
                        }
                        for s in reserved
                    ],
                    "total_reservations": sum(s["reservation_count"] for s in reserved),
                    "requires_confirmation": True,
                },
            )
        override_id = new_id("ovr")
        correlation = new_correlation_id()
        created: list[str] = []
        with self.db.transaction():
            self.db.insert(
                "schedule_overrides",
                {
                    "id": override_id,
                    "tenant_id": ctx.tenant_id,
                    "venue_id": venue_id,
                    "experience_id": experience_id,
                    "date": date,
                    "mode": mode,
                    "payload_json": {
                        "sessions": list(sessions),
                        "suppress_session_ids": list(suppress_session_ids),
                    },
                    "actor_id": ctx.principal.id,
                    "created_at": to_iso(self.clock.now()),
                },
            )
            for session in to_remove:
                self.inventory.set_status(
                    ctx,
                    session["id"],
                    "CANCELLED",
                    reason=reason or f"Replaced by date override for {date}",
                    require_permission=False,
                )
            for spec in sessions:
                new_session = self.create_show_session(
                    ctx,
                    venue_id=venue_id,
                    experience_id=spec["experience_id"],
                    date=date,
                    start_time=spec["start_time"],
                    duration_minutes=spec.get("duration_minutes"),
                    area_id=spec.get("area_id"),
                    capacity=spec.get("capacity"),
                    reservation_mode=spec.get("reservation_mode"),
                    confirm_conflicts=True,
                    source="OVERRIDE",
                    override_id=override_id,
                    require_permission=False,
                )
                created.append(new_session["id"])
            self.audit.record(
                ctx.for_venue(venue_id),
                "OVERRIDE_CREATE",
                target_type="schedule_override",
                target_id=override_id,
                new={"date": date, "mode": mode, "created": len(created), "cancelled": len(to_remove)},
                reason=reason,
                severity="WARNING",
                extra_correlation=correlation,
            )
        return {
            "override_id": override_id,
            "date": date,
            "mode": mode,
            "sessions_created": created,
            "sessions_cancelled": [s["id"] for s in to_remove],
        }

    def remove_override(
        self, ctx: RequestContext, override_id: str, *, confirmed: bool = False, reason: str | None = None
    ) -> dict[str, Any]:
        """Restore the pattern-derived schedule for that date (R22.3, R22.5)."""
        self.authz.require_page(
            ctx, "Show Schedule", "DELETE", target_type="schedule_override", target_id=override_id
        )
        override = self.authz.load_scoped(ctx, "schedule_overrides", override_id, entity="schedule_override")
        if override["removed_at"]:
            raise ConflictError("That override has already been removed.")
        created = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM sessions WHERE tenant_id = ? AND override_id = ? AND status <> 'CANCELLED'",
                (ctx.tenant_id, override_id),
            )
        ]
        reserved = []
        for row in created:
            count = self.inventory.confirmed_reservation_count(ctx, row["id"])
            if count:
                reserved.append(
                    {"session_id": row["id"], "start_time": row["start_time"], "reservation_count": count}
                )
        if reserved and not confirmed:
            raise ConfirmationRequired(
                "Removing this override would remove sessions that hold reservations.",
                code="override_removal_affects_reservations",
                details={
                    "affected_sessions": reserved,
                    "total_reservations": sum(r["reservation_count"] for r in reserved),
                    "requires_confirmation": True,
                },
            )
        now = to_iso(self.clock.now())
        with self.db.transaction():
            for row in created:
                self.inventory.set_status(
                    ctx, row["id"], "CANCELLED", reason=reason or "Override removed", require_permission=False
                )
            self.db.update(
                "schedule_overrides",
                override_id,
                {"removed_at": now, "removed_by": ctx.principal.id},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(override["venue_id"]),
                "OVERRIDE_REMOVE",
                target_type="schedule_override",
                target_id=override_id,
                previous={"date": override["date"], "mode": override["mode"]},
                new={"removed": True, "sessions_cancelled": len(created)},
                reason=reason,
                severity="WARNING",
            )
        restored = 0
        for pattern_row in self.db.query(
            "SELECT id FROM session_patterns WHERE tenant_id = ? AND venue_id = ? AND status = 'ACTIVE'",
            (ctx.tenant_id, override["venue_id"]),
        ):
            restored += self.materialize_pattern(ctx, pattern_row["id"])["created"]
        return {
            "override_id": override_id,
            "date": override["date"],
            "sessions_cancelled": len(created),
            "pattern_sessions_restored": restored,
        }

    def _override_dates(
        self, ctx: RequestContext, venue_id: str, date_from: str, date_to: str | None
    ) -> list[str]:
        sql = [
            "SELECT DISTINCT date FROM schedule_overrides WHERE tenant_id = ? AND venue_id = ? "
            "AND removed_at IS NULL AND date >= ?"
        ]
        params: list[Any] = [ctx.tenant_id, venue_id, date_from]
        if date_to:
            sql.append("AND date <= ?")
            params.append(date_to)
        return sorted(row["date"] for row in self.db.query(" ".join(sql), params))

    def dates_with_overrides(
        self, ctx: RequestContext, *, venue_id: str, date_from: str, date_to: str
    ) -> list[str]:
        """R22.3 — the back office must show which dates carry overrides."""
        return self._override_dates(ctx, venue_id, date_from, date_to)

    # ------------------------------------------------------------------ #
    # Copy / bulk tools (R23)
    # ------------------------------------------------------------------ #

    def preview_copy(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        source_date: str,
        target_dates: Sequence[str],
        conflict_strategy: str = "SKIP",
    ) -> dict[str, Any]:
        """Preview before applying (R23.2), excluding non-operating dates (R23.4)."""
        if conflict_strategy not in CONFLICT_STRATEGIES:
            raise ValidationError(
                {"conflict_strategy": f"Choose one of: {', '.join(CONFLICT_STRATEGIES)}."}
            )
        venue = self._venue(ctx, venue_id)
        source = self.db.query(
            "SELECT * FROM sessions WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = 'SHOW' "
            "AND status <> 'CANCELLED' ORDER BY start_time",
            (ctx.tenant_id, venue_id, source_date),
        )
        will_create: list[dict[str, Any]] = []
        will_replace: list[dict[str, Any]] = []
        will_skip: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        for target in target_dates:
            reason = self._non_operating_reason(ctx, venue, target)
            if reason is not None:
                excluded.append({"date": target, "reason": reason})
                continue
            for row in source:
                conflicts = self.detect_conflicts(
                    ctx,
                    venue_id=venue_id,
                    date=target,
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    experience_id=row["experience_id"],
                    area_id=row["area_id"],
                )
                entry = {
                    "date": target,
                    "experience_id": row["experience_id"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "area_id": row["area_id"],
                    "capacity": row["capacity"],
                    "reservation_mode": row["reservation_mode"],
                    "conflicts": [c.as_dict() for c in conflicts],
                }
                if not conflicts:
                    will_create.append(entry)
                elif conflict_strategy == "SKIP":
                    will_skip.append(entry)
                elif conflict_strategy == "REPLACE":
                    entry["reservations_on_conflicts"] = sum(
                        self.inventory.confirmed_reservation_count(ctx, c["session_id"])
                        for c in entry["conflicts"]
                    )
                    will_replace.append(entry)
                else:
                    will_create.append(entry)
        return {
            "source_date": source_date,
            "source_session_count": len(source),
            "conflict_strategy": conflict_strategy,
            "will_create": will_create,
            "will_replace": will_replace,
            "will_skip": will_skip,
            "excluded_dates": excluded,
            "counts": {
                "create": len(will_create),
                "replace": len(will_replace),
                "skip": len(will_skip),
                "excluded": len(excluded),
            },
        }

    def copy_schedule(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        source_date: str,
        target_dates: Sequence[str],
        conflict_strategy: str = "SKIP",
        confirmed: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a previewed copy (R23.1, R23.5 - R23.7)."""
        self.authz.require_page(ctx, "Show Schedule", "ADD")
        self.authz.require_action(ctx.for_venue(venue_id), "BULK_UPDATE_SCHEDULE")
        preview = self.preview_copy(
            ctx,
            venue_id=venue_id,
            source_date=source_date,
            target_dates=target_dates,
            conflict_strategy=conflict_strategy,
        )
        if not confirmed:
            raise ConfirmationRequired(
                "Review what this copy will create, change and skip.",
                code="bulk_preview",
                details={"preview": preview, "requires_confirmation": True},
            )
        correlation = new_correlation_id()
        created = replaced = failed = 0
        skipped = len(preview["will_skip"])
        with self.db.transaction():
            for entry in preview["will_replace"]:
                for conflict in entry["conflicts"]:
                    self.inventory.set_status(
                        ctx,
                        conflict["session_id"],
                        "CANCELLED",
                        reason=reason or f"Replaced by copy from {source_date}",
                        require_permission=False,
                    )
                    replaced += 1
            for entry in preview["will_create"] + preview["will_replace"]:
                # Idempotent, so a retried bulk operation cannot duplicate (R23.7).
                duplicate = self.db.query_one(
                    "SELECT id FROM sessions WHERE tenant_id = ? AND venue_id = ? AND date = ? "
                    "AND kind = 'SHOW' AND experience_id = ? AND start_time = ? AND status <> 'CANCELLED'",
                    (ctx.tenant_id, venue_id, entry["date"], entry["experience_id"], entry["start_time"]),
                )
                if duplicate is not None:
                    skipped += 1
                    continue
                try:
                    self.create_show_session(
                        ctx,
                        venue_id=venue_id,
                        experience_id=entry["experience_id"],
                        date=entry["date"],
                        start_time=entry["start_time"],
                        end_time=entry["end_time"],
                        area_id=entry["area_id"],
                        capacity=entry["capacity"],
                        reservation_mode=entry["reservation_mode"],
                        confirm_conflicts=True,
                        require_permission=False,
                    )
                    created += 1
                except (ValidationError, ConflictError):
                    failed += 1
            self.audit.record(
                ctx.for_venue(venue_id),
                "BULK_SCHEDULE_UPDATE",
                target_type="show_schedule",
                target_id=f"{venue_id}:{source_date}",
                new={
                    "source_date": source_date,
                    "target_dates": list(target_dates),
                    "conflict_strategy": conflict_strategy,
                    "created": created,
                    "replaced": replaced,
                    "skipped": skipped,
                    "failed": failed,
                    "excluded_dates": preview["excluded_dates"],
                },
                reason=reason,
                severity="WARNING",
                extra_correlation=correlation,
            )
        return {
            "created": created,
            "replaced": replaced,
            "skipped": skipped,
            "failed": failed,
            "excluded_dates": preview["excluded_dates"],
            "correlation_id": correlation,
        }

    def copy_yesterday(
        self, ctx: RequestContext, *, venue_id: str, target_date: str, **kwargs: Any
    ) -> dict[str, Any]:
        previous = (parse_date(target_date) - _dt.timedelta(days=1)).isoformat()
        return self.copy_schedule(
            ctx, venue_id=venue_id, source_date=previous, target_dates=[target_date], **kwargs
        )

    def copy_weekday_to_weekday(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        source_date: str,
        target_weekday: str,
        weeks: int = 4,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if target_weekday not in _WEEKDAYS + _WEEKEND:
            raise ValidationError({"target_weekday": "Use a weekday code such as MON or SAT."})
        targets: list[str] = []
        cursor = parse_date(source_date) + _dt.timedelta(days=1)
        while len(targets) < weeks:
            if weekday_code(cursor.isoformat()) == target_weekday:
                targets.append(cursor.isoformat())
            cursor += _dt.timedelta(days=1)
        return self.copy_schedule(
            ctx, venue_id=venue_id, source_date=source_date, target_dates=targets, **kwargs
        )

    def copy_to_week(
        self, ctx: RequestContext, *, venue_id: str, source_date: str, week_start: str, **kwargs: Any
    ) -> dict[str, Any]:
        start = parse_date(week_start)
        targets = [(start + _dt.timedelta(days=i)).isoformat() for i in range(7)]
        return self.copy_schedule(
            ctx, venue_id=venue_id, source_date=source_date, target_dates=targets, **kwargs
        )

    def duplicate_session(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        date: str | None = None,
        start_time: str | None = None,
    ) -> dict[str, Any]:
        """Duplicate a single show session (R23.1)."""
        session = self.inventory.get_session(ctx, session_id)
        self.authz.require_page(ctx, "Show Schedule", "ADD")
        venue = self._venue(ctx, session["venue_id"])
        duration = self._duration_minutes(session, venue)
        return self.create_show_session(
            ctx,
            venue_id=session["venue_id"],
            experience_id=session["experience_id"],
            date=date or session["date"],
            start_time=start_time or session["start_time"],
            duration_minutes=duration,
            area_id=session["area_id"],
            capacity=session["capacity"],
            reservation_mode=session["reservation_mode"],
            confirm_conflicts=True,
            require_permission=False,
        )

    def _duration_minutes(self, session: dict[str, Any], venue: dict[str, Any]) -> int:
        starts = combine_local(session["date"], session["start_time"], venue["timezone"])
        ends = combine_local(session["date"], session["end_time"], venue["timezone"])
        if session["end_time"] < session["start_time"]:
            ends = ends + _dt.timedelta(days=1)
        return int(minutes_between(starts, ends)) or 30

    def bulk_create(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        specs: Sequence[dict[str, Any]],
        confirmed: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Create many sessions in one correlated, resumable operation (R23.1, R23.6, R23.7)."""
        self.authz.require_page(ctx, "Show Schedule", "ADD")
        self.authz.require_action(ctx.for_venue(venue_id), "BULK_UPDATE_SCHEDULE")
        if not confirmed:
            raise ConfirmationRequired(
                f"This will create up to {len(specs)} sessions.",
                code="bulk_preview",
                details={"session_count": len(specs), "requires_confirmation": True},
            )
        correlation = new_correlation_id()
        created = skipped = failed = 0
        for spec in specs:
            duplicate = self.db.query_one(
                "SELECT id FROM sessions WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = 'SHOW' "
                "AND experience_id = ? AND start_time = ? AND status <> 'CANCELLED'",
                (ctx.tenant_id, venue_id, spec["date"], spec["experience_id"], spec["start_time"]),
            )
            if duplicate is not None:
                skipped += 1
                continue
            try:
                self.create_show_session(
                    ctx,
                    venue_id=venue_id,
                    experience_id=spec["experience_id"],
                    date=spec["date"],
                    start_time=spec["start_time"],
                    duration_minutes=spec.get("duration_minutes"),
                    area_id=spec.get("area_id"),
                    capacity=spec.get("capacity"),
                    reservation_mode=spec.get("reservation_mode"),
                    confirm_conflicts=True,
                    require_permission=False,
                )
                created += 1
            except (ValidationError, ConflictError, ConfirmationRequired):
                failed += 1
        self.audit.record(
            ctx.for_venue(venue_id),
            "BULK_SCHEDULE_UPDATE",
            target_type="show_schedule",
            target_id=venue_id,
            new={"requested": len(specs), "created": created, "skipped": skipped, "failed": failed},
            reason=reason,
            severity="WARNING",
            extra_correlation=correlation,
        )
        return {
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "correlation_id": correlation,
        }

    # ------------------------------------------------------------------ #
    # Publish workflow (R31)
    # ------------------------------------------------------------------ #

    def publish(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date_from: str,
        date_to: str,
        experience_id: str | None = None,
    ) -> dict[str, Any]:
        """Make Draft sessions customer-visible (R31.3, R31.5)."""
        self.authz.require_action(
            ctx.for_venue(venue_id), "PUBLISH_SHOW_SCHEDULE", target_type="show_schedule", target_id=venue_id
        )
        sql = [
            "SELECT id, date, start_time, customer_visible, status FROM sessions "
            "WHERE tenant_id = ? AND venue_id = ? AND kind = 'SHOW' AND publication_state = 'DRAFT' "
            "AND date BETWEEN ? AND ?"
        ]
        params: list[Any] = [ctx.tenant_id, venue_id, date_from, date_to]
        if experience_id:
            sql.append("AND experience_id = ?")
            params.append(experience_id)
        rows = self.db.query(" ".join(sql), params)
        became_visible: list[dict[str, Any]] = []
        with self.db.transaction():
            for row in rows:
                self.db.update(
                    "sessions", row["id"], {"publication_state": "PUBLISHED"}, tenant_id=ctx.tenant_id
                )
                if bool(row["customer_visible"]) and row["status"] != "HIDDEN":
                    became_visible.append(
                        {"session_id": row["id"], "date": row["date"], "start_time": row["start_time"]}
                    )
            self.db.execute(
                "UPDATE session_patterns SET publication_state = 'PUBLISHED' WHERE tenant_id = ? "
                "AND venue_id = ? AND publication_state = 'DRAFT'",
                (ctx.tenant_id, venue_id),
            )
            self.audit.record(
                ctx.for_venue(venue_id),
                "SCHEDULE_PUBLISH",
                target_type="show_schedule",
                target_id=f"{venue_id}:{date_from}:{date_to}",
                new={
                    "date_from": date_from,
                    "date_to": date_to,
                    "published": len(rows),
                    "became_customer_visible": len(became_visible),
                },
                severity="WARNING",
            )
        return {
            "published": len(rows),
            "became_visible": became_visible,
            "date_from": date_from,
            "date_to": date_to,
        }

    def unpublish(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date_from: str,
        date_to: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Revert to Draft only where no reservations exist (R31.6)."""
        self.authz.require_action(
            ctx.for_venue(venue_id), "PUBLISH_SHOW_SCHEDULE", target_type="show_schedule", target_id=venue_id
        )
        rows = self.db.query(
            "SELECT id, date, start_time FROM sessions WHERE tenant_id = ? AND venue_id = ? "
            "AND kind = 'SHOW' AND publication_state = 'PUBLISHED' AND date BETWEEN ? AND ?",
            (ctx.tenant_id, venue_id, date_from, date_to),
        )
        blocked: list[dict[str, Any]] = []
        revertible: list[str] = []
        for row in rows:
            count = self.inventory.confirmed_reservation_count(ctx, row["id"])
            if count:
                blocked.append(
                    {
                        "session_id": row["id"],
                        "date": row["date"],
                        "start_time": row["start_time"],
                        "reservation_count": count,
                    }
                )
            else:
                revertible.append(row["id"])
        if blocked:
            raise ConflictError(
                "Some sessions have reservations. Hide or cancel them through the schedule "
                "change workflow instead of unpublishing.",
                code="unpublish_blocked_by_reservations",
                details={"blocked_sessions": blocked},
            )
        with self.db.transaction():
            for session_id in revertible:
                self.db.update(
                    "sessions", session_id, {"publication_state": "DRAFT"}, tenant_id=ctx.tenant_id
                )
            self.audit.record(
                ctx.for_venue(venue_id),
                "SCHEDULE_UNPUBLISH",
                target_type="show_schedule",
                target_id=f"{venue_id}:{date_from}:{date_to}",
                new={"unpublished": len(revertible)},
                reason=reason,
                severity="WARNING",
            )
        return {"unpublished": len(revertible)}

    def archive(
        self, ctx: RequestContext, *, venue_id: str, date_from: str, date_to: str
    ) -> dict[str, Any]:
        """Archive a past schedule set (R31.1, R31.7)."""
        self.authz.require_action(ctx.for_venue(venue_id), "PUBLISH_SHOW_SCHEDULE")
        cursor = self.db.execute(
            "UPDATE sessions SET publication_state = 'ARCHIVED' WHERE tenant_id = ? AND venue_id = ? "
            "AND kind = 'SHOW' AND date BETWEEN ? AND ? AND status IN ('COMPLETED','CANCELLED')",
            (ctx.tenant_id, venue_id, date_from, date_to),
        )
        self.audit.record(
            ctx.for_venue(venue_id),
            "SCHEDULE_ARCHIVE",
            target_type="show_schedule",
            target_id=f"{venue_id}:{date_from}:{date_to}",
            new={"archived": cursor.rowcount},
        )
        return {"archived": cursor.rowcount}

    # ------------------------------------------------------------------ #
    # Schedule changes and affected customers (R30)
    # ------------------------------------------------------------------ #

    def affected_reservations(self, ctx: RequestContext, session_id: str) -> dict[str, Any]:
        """Who is affected, and how many — presented before committing (R30.2)."""
        rows = self.db.query(
            """
            SELECT DISTINCT b.id AS booking_id, b.booking_number, b.customer_id, b.language, b.venue_id
            FROM tickets t
            JOIN bookings b ON b.id = t.booking_id AND b.tenant_id = t.tenant_id
            WHERE t.tenant_id = ? AND t.session_id = ?
              AND t.state IN ('ISSUED','VALID','PARTIALLY_USED') AND b.status = 'CONFIRMED'
            """,
            (ctx.tenant_id, session_id),
        )
        return {
            "session_id": session_id,
            "reservation_count": self.inventory.confirmed_reservation_count(ctx, session_id),
            "booking_count": len(rows),
            "bookings": [dict(r) for r in rows],
        }

    def change_session(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        new_start_time: str | None = None,
        new_area_id: str | None = None,
        new_duration_minutes: int | None = None,
        reason: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Retime and/or relocate, notifying everyone affected (R30.1 - R30.3, R30.8)."""
        session = self.inventory.get_session(ctx, session_id)
        venue = self._venue(ctx, session["venue_id"])
        scoped = ctx.for_venue(venue["id"])
        self.authz.require_page(scoped, "Show Schedule", "EDIT", target_type="session", target_id=session_id)
        if new_area_id and new_area_id != session["area_id"]:
            self.authz.require_action(
                scoped, "CHANGE_SHOW_LOCATION", target_type="session", target_id=session_id, reason=reason
            )
        affected = self.affected_reservations(ctx, session_id)
        if affected["booking_count"] and not confirmed:
            raise ConfirmationRequired(
                f"{affected['booking_count']} booking(s) hold reservations for this session.",
                code="change_affects_reservations",
                details={
                    "reservation_count": affected["reservation_count"],
                    "booking_count": affected["booking_count"],
                    "requires_confirmation": True,
                },
            )
        previous = {
            "start_time": session["start_time"],
            "end_time": session["end_time"],
            "area_id": session["area_id"],
        }
        payload: dict[str, Any] = {}
        if new_start_time:
            duration = new_duration_minutes or self._duration_minutes(session, venue)
            payload["start_time"] = new_start_time
            payload["end_time"] = end_time_from_duration(new_start_time, duration)
        elif new_duration_minutes:
            payload["end_time"] = end_time_from_duration(session["start_time"], new_duration_minutes)
        if new_area_id:
            payload["area_id"] = new_area_id
        if not payload:
            raise ValidationError({"changes": "Provide a new time and/or a new location."})
        conflicts = self.detect_conflicts(
            ctx,
            venue_id=venue["id"],
            date=session["date"],
            start_time=payload.get("start_time", session["start_time"]),
            end_time=payload.get("end_time", session["end_time"]),
            experience_id=session["experience_id"],
            area_id=payload.get("area_id", session["area_id"]),
            exclude_session_id=session_id,
        )
        if conflicts and not confirmed:
            raise ConfirmationRequired(
                "The new time or location clashes with another session.",
                code="schedule_conflict",
                details={"conflicts": [c.as_dict() for c in conflicts], "requires_confirmation": True},
            )
        correlation = new_correlation_id()
        with self.db.transaction():
            self.db.update("sessions", session_id, payload, tenant_id=ctx.tenant_id)
            self.audit.record(
                scoped,
                "SHOW_LOCATION_CHANGE" if new_area_id else "SHOW_RETIME",
                target_type="session",
                target_id=session_id,
                previous=previous,
                new={**payload, "affected_reservations": affected["reservation_count"]},
                reason=reason,
                severity="WARNING",
                venue_timezone=venue["timezone"],
                extra_correlation=correlation,
            )
        notified = self._notify_affected(
            ctx,
            event="SHOW_SCHEDULE_CHANGED",
            session=session,
            venue=venue,
            affected=affected,
            variables={
                "previous_start_time": previous["start_time"],
                "new_start_time": payload.get("start_time", session["start_time"]),
                "previous_location": previous["area_id"] or "",
                "new_location": payload.get("area_id", session["area_id"]) or "",
                "change_reason": reason or "",
            },
        )
        return {
            "session_id": session_id,
            "previous": previous,
            "new": payload,
            "affected_bookings": affected["booking_count"],
            "notified": notified,
            "correlation_id": correlation,
        }

    def delay_session(
        self, ctx: RequestContext, session_id: str, *, new_start_time: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Mark Delayed and surface it on customer timetables (R24.5)."""
        session = self.inventory.get_session(ctx, session_id)
        venue = self._venue(ctx, session["venue_id"])
        scoped = ctx.for_venue(venue["id"])
        self.authz.require_page(scoped, "Show Schedule", "EDIT", target_type="session", target_id=session_id)
        result = self.inventory.set_status(
            scoped, session_id, "DELAYED", reason=reason, delayed_start_time=new_start_time
        )
        affected = self.affected_reservations(ctx, session_id)
        self._notify_affected(
            ctx,
            event="SHOW_SCHEDULE_CHANGED",
            session=session,
            venue=venue,
            affected=affected,
            variables={
                "previous_start_time": session["start_time"],
                "new_start_time": new_start_time,
                "change_reason": reason or "",
            },
        )
        return {**result, "affected_bookings": affected["booking_count"]}

    def cancel_session(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        reason: str,
        confirmed: bool = False,
        remedy: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a show session and apply the configured remedy (R24.6, R30.5, R30.7)."""
        session = self.inventory.get_session(ctx, session_id)
        venue = self._venue(ctx, session["venue_id"])
        scoped = ctx.for_venue(venue["id"])
        self.authz.require_action(
            scoped, "CANCEL_SHOW", target_type="session", target_id=session_id, reason=reason
        )
        affected = self.affected_reservations(ctx, session_id)
        configured_remedy = remedy or self.config.get(
            ctx, "show.cancellation_remedy", venue_id=venue["id"], default="AUTOMATIC_RELEASE"
        )
        if affected["booking_count"] and not confirmed:
            raise ConfirmationRequired(
                f"{affected['booking_count']} booking(s) hold reservations for this session.",
                code="cancel_affects_reservations",
                details={
                    "reservation_count": affected["reservation_count"],
                    "booking_count": affected["booking_count"],
                    "requires_confirmation": True,
                    "remedy": configured_remedy,
                },
            )
        self.inventory.set_status(scoped, session_id, "CANCELLED", reason=reason, require_permission=False)
        released = 0
        if configured_remedy in ("AUTOMATIC_RELEASE", "REBOOK_OFFER"):
            released = int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM tickets WHERE tenant_id = ? AND session_id = ? "
                    "AND state IN ('ISSUED','VALID','PARTIALLY_USED')",
                    (ctx.tenant_id, session_id),
                    default=0,
                )
            )
        alternatives: list[dict[str, Any]] = []
        if configured_remedy == "REBOOK_OFFER":
            alternatives = [
                {
                    "session_id": row["id"],
                    "date": row["date"],
                    "start_time": row["start_time"],
                    "remaining": row["remaining"],
                }
                for row in self.inventory.list_sessions(
                    ctx,
                    venue_id=venue["id"],
                    date=session["date"],
                    kind="SHOW",
                    experience_id=session["experience_id"],
                    statuses=["SCHEDULED", "AVAILABLE", "LIMITED"],
                )
                if row["id"] != session_id
            ]
        notified = self._notify_affected(
            ctx,
            event="SHOW_CANCELLED",
            session=session,
            venue=venue,
            affected=affected,
            variables={
                "show_start_time": session["start_time"],
                "change_reason": reason,
                "remedy": configured_remedy,
            },
        )
        return {
            "session_id": session_id,
            "status": "CANCELLED",
            "remedy": configured_remedy,
            "affected_bookings": affected["booking_count"],
            "reservations_released": released,
            "alternative_sessions": alternatives,
            "notified": notified,
        }

    def hide_session(
        self, ctx: RequestContext, session_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """R24.7 — hidden from customers, still managed in the back office."""
        session = self.inventory.get_session(ctx, session_id)
        scoped = ctx.for_venue(session["venue_id"])
        self.authz.require_page(scoped, "Show Schedule", "EDIT", target_type="session", target_id=session_id)
        return self.inventory.set_status(
            scoped, session_id, "HIDDEN", reason=reason, require_permission=False
        )

    def _notify_affected(
        self,
        ctx: RequestContext,
        *,
        event: str,
        session: dict[str, Any],
        venue: dict[str, Any],
        affected: dict[str, Any],
        variables: dict[str, Any],
    ) -> int:
        """R30.3 / R30.4 — email is the mandatory baseline channel."""
        if self.notifications is None or self.customers is None:
            return 0
        show_name = ""
        if session.get("experience_id"):
            experience = self.catalog.get_experience(ctx, session["experience_id"])
            show_name = i18n_text(experience["name"], ctx.language, fallback=experience["code"])
        sent = 0
        for booking in affected["bookings"]:
            if not booking.get("customer_id"):
                continue
            recipient = self.customers.contact_email(ctx, booking["customer_id"])
            if not recipient:
                continue
            result = self.notifications.enqueue(
                ctx,
                event_type=event,
                booking_id=booking["booking_id"],
                recipient=recipient,
                language=booking["language"],
                venue_id=venue["id"],
                extra_variables={"show_name": show_name, **variables},
                force_resend=True,
            )
            if result.get("queued"):
                sent += 1
        return sent

    # ------------------------------------------------------------------ #
    # Back office views (R20.2, R29)
    # ------------------------------------------------------------------ #

    def daily_timetable(
        self, ctx: RequestContext, *, venue_id: str, date: str, language: str | None = None
    ) -> dict[str, Any]:
        """All sessions for one date, chronologically (R20.2)."""
        self.authz.require_page(ctx, "Show Schedule", "VIEW")
        lang = language or ctx.language
        rows = []
        for session in self.inventory.list_sessions(ctx, venue_id=venue_id, date=date, kind="SHOW"):
            experience = (
                self.catalog.get_experience(ctx, session["experience_id"])
                if session["experience_id"]
                else None
            )
            rows.append(
                {
                    "session_id": session["id"],
                    "show_name": i18n_text(experience["name"], lang, fallback=experience["code"])
                    if experience
                    else "",
                    "start_time": session["start_time"],
                    "end_time": session["end_time"],
                    "delayed_start_time": session["delayed_start_time"],
                    "area_id": session["area_id"],
                    "reservation_mode": session["reservation_mode"],
                    "capacity": session["capacity"],
                    "confirmed": session["confirmed"],
                    "remaining": session["remaining"],
                    "reservation_count": self.inventory.confirmed_reservation_count(ctx, session["id"]),
                    "status": session["status"],
                    "publication_state": session["publication_state"],
                    "customer_visible": bool(session["customer_visible"]),
                    "source": session["source"],
                    "origin": {
                        "PATTERN": "recurring_pattern",
                        "OVERRIDE": "date_override",
                        "MANUAL": "one_off",
                        "DAY_AUTO": "day_capacity",
                    }.get(session["source"], session["source"]),
                }
            )
        return {
            "venue_id": venue_id,
            "date": date,
            "has_override": date
            in self.dates_with_overrides(ctx, venue_id=venue_id, date_from=date, date_to=date),
            "sessions": rows,
        }

    def back_office_calendar(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date_from: str,
        date_to: str,
        view: str = "WEEK",
        experience_id: str | None = None,
        area_id: str | None = None,
        status: str | None = None,
        reservation_mode: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Day/Week/Month/List views with filters and reservation counts (R29.1 - R29.8)."""
        self.authz.require_page(ctx, "Show Schedule", "VIEW")
        if view not in ("DAY", "WEEK", "MONTH", "LIST"):
            raise ValidationError({"view": "View must be DAY, WEEK, MONTH or LIST."})
        lang = language or ctx.language
        by_day: dict[str, list[dict[str, Any]]] = {}
        for session in self.inventory.list_sessions(
            ctx,
            venue_id=venue_id,
            date_from=date_from,
            date_to=date_to,
            kind="SHOW",
            experience_id=experience_id,
        ):
            if area_id and session["area_id"] != area_id:
                continue
            if status and session["status"] != status:
                continue
            if reservation_mode and session["reservation_mode"] != reservation_mode:
                continue
            experience = (
                self.catalog.get_experience(ctx, session["experience_id"])
                if session["experience_id"]
                else None
            )
            by_day.setdefault(session["date"], []).append(
                {
                    "session_id": session["id"],
                    "show_name": i18n_text(experience["name"], lang, fallback=experience["code"])
                    if experience
                    else "",
                    "start_time": session["start_time"],
                    "end_time": session["end_time"],
                    "area_id": session["area_id"],
                    "capacity": session["capacity"],
                    "remaining": session["remaining"],
                    "reservation_count": self.inventory.confirmed_reservation_count(ctx, session["id"]),
                    "status": session["status"],
                    "publication_state": session["publication_state"],
                    "source": session["source"],
                }
            )
        effective = self.authz.effective_permissions(ctx.for_venue(venue_id))
        return {
            "view": view,
            "venue_id": venue_id,
            "date_from": date_from,
            "date_to": date_to,
            "dates_with_overrides": self.dates_with_overrides(
                ctx, venue_id=venue_id, date_from=date_from, date_to=date_to
            ),
            "days": [
                {"date": day, "sessions": sorted(items, key=lambda s: s["start_time"])}
                for day, items in sorted(by_day.items())
            ],
            # R29.8 — the UI hides what the principal cannot do; the API rejects it anyway.
            "permissions": {
                "can_add": effective.has_page("Show Schedule", "ADD"),
                "can_edit": effective.has_page("Show Schedule", "EDIT"),
                "can_delete": effective.has_page("Show Schedule", "DELETE"),
                "can_publish": effective.has_action("PUBLISH_SHOW_SCHEDULE"),
                "can_cancel_show": effective.has_action("CANCEL_SHOW"),
                "can_change_location": effective.has_action("CHANGE_SHOW_LOCATION"),
                "can_bulk_update": effective.has_action("BULK_UPDATE_SCHEDULE"),
                "can_export": effective.has_action("EXPORT_SHOW_SCHEDULE"),
                "drag_and_drop_enabled": effective.has_page("Show Schedule", "EDIT"),
            },
        }

    def drag_and_drop(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        new_date: str | None = None,
        new_start_time: str | None = None,
        confirmed: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Drag-and-drop reschedule, held to the same standard as any edit (R29.4)."""
        session = self.inventory.get_session(ctx, session_id)
        venue = self._venue(ctx, session["venue_id"])
        scoped = ctx.for_venue(venue["id"])
        self.authz.require_page(scoped, "Show Schedule", "EDIT", target_type="session", target_id=session_id)
        if not new_date or new_date == session["date"]:
            return self.change_session(
                ctx, session_id, new_start_time=new_start_time, confirmed=confirmed, reason=reason
            )
        affected = self.affected_reservations(ctx, session_id)
        if not confirmed:
            raise ConfirmationRequired(
                "Confirm moving this session to a different date.",
                code="drag_confirm",
                details={
                    "from": {"date": session["date"], "start_time": session["start_time"]},
                    "to": {"date": new_date, "start_time": new_start_time or session["start_time"]},
                    "affected_bookings": affected["booking_count"],
                    "requires_confirmation": True,
                },
            )
        conflicts = self.detect_conflicts(
            ctx,
            venue_id=venue["id"],
            date=new_date,
            start_time=new_start_time or session["start_time"],
            end_time=session["end_time"],
            experience_id=session["experience_id"],
            area_id=session["area_id"],
            exclude_session_id=session_id,
        )
        if conflicts:
            raise ConflictError(
                "That slot clashes with another session.",
                details={"conflicts": [c.as_dict() for c in conflicts]},
            )
        previous = {"date": session["date"], "start_time": session["start_time"]}
        payload: dict[str, Any] = {"date": new_date}
        if new_start_time:
            payload["start_time"] = new_start_time
            payload["end_time"] = end_time_from_duration(
                new_start_time, self._duration_minutes(session, venue)
            )
        with self.db.transaction():
            self.db.update("sessions", session_id, payload, tenant_id=ctx.tenant_id)
            self.audit.record(
                scoped,
                "SHOW_RETIME",
                target_type="session",
                target_id=session_id,
                previous=previous,
                new={**payload, "method": "drag_and_drop"},
                reason=reason,
                severity="WARNING",
                venue_timezone=venue["timezone"],
            )
        return {"session_id": session_id, "previous": previous, "new": payload}

    def delete_session(self, ctx: RequestContext, session_id: str, *, reason: str) -> dict[str, Any]:
        """DELETE on a show session cancels rather than erases (R24.8, R46.2)."""
        session = self.inventory.get_session(ctx, session_id)
        scoped = ctx.for_venue(session["venue_id"])
        self.authz.require_page(scoped, "Show Schedule", "DELETE", target_type="session", target_id=session_id)
        count = self.inventory.confirmed_reservation_count(ctx, session_id)
        result = self.cancel_session(ctx, session_id, reason=reason, confirmed=True)
        return {
            "requested": "DELETE",
            "performed": "CANCEL",
            "explanation": "Show sessions with reservations or check-ins are retained for reporting.",
            "reservation_count": count,
            **result,
        }

    def export_schedule(
        self, ctx: RequestContext, *, venue_id: str, date_from: str, date_to: str
    ) -> dict[str, Any]:
        """R41.2 / R71.9 — export requires its own permission and is audited."""
        self.authz.require_action(ctx.for_venue(venue_id), "EXPORT_SHOW_SCHEDULE")
        rows = self.inventory.list_sessions(
            ctx, venue_id=venue_id, date_from=date_from, date_to=date_to, kind="SHOW"
        )
        self.audit.record(
            ctx.for_venue(venue_id),
            "EXPORT",
            target_type="show_schedule",
            target_id=venue_id,
            new={"date_from": date_from, "date_to": date_to, "row_count": len(rows)},
            severity="WARNING",
        )
        return {
            "filters": {"venue_id": venue_id, "date_from": date_from, "date_to": date_to},
            "generated_at": to_iso(self.clock.now()),
            "row_count": len(rows),
            "rows": [
                {
                    "date": r["date"],
                    "start_time": r["start_time"],
                    "end_time": r["end_time"],
                    "status": r["status"],
                    "capacity": r["capacity"],
                    "confirmed": r["confirmed"],
                    "reservation_mode": r["reservation_mode"],
                }
                for r in rows
            ],
        }

    # ------------------------------------------------------------------ #
    # Customer-facing timetable (R26)
    # ------------------------------------------------------------------ #

    def customer_timetable(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        language: str | None = None,
        category: str | None = None,
        filter_key: str = "ALL",
        view: str | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """The guest-facing timetable for one date (R26)."""
        lang = language or ctx.language
        venue = self._venue(ctx, venue_id)
        tz = venue["timezone"]
        today = operating_date(
            self.clock.now(), tz, int(venue.get("day_boundary_hour") or 0)
        ).isoformat()
        if preview:
            # R31.4 / R26.14 — Draft schedules are visible only in authorized preview.
            self.authz.require_page(ctx, "Show Schedule", "VIEW")
        if category is None and filter_key.startswith("CATEGORY:"):
            category = filter_key.split(":", 1)[1]

        entries: list[dict[str, Any]] = []
        categories: set[str] = set()
        for session in self.inventory.list_sessions(
            ctx,
            venue_id=venue_id,
            date=date,
            kind="SHOW",
            published_only=not preview,
            customer_visible_only=not preview,
        ):
            if session["status"] == "HIDDEN" and not preview:
                continue
            if not session["experience_id"]:
                continue
            experience = self.catalog.get_experience(ctx, session["experience_id"])
            if experience.get("category"):
                categories.add(experience["category"])
            if category and experience.get("category") != category:
                continue
            entry = self._timetable_entry(
                ctx, session=session, experience=experience, venue=venue, language=lang, today=today
            )
            if filter_key == "AVAILABLE_TO_BOOK" and not entry["can_reserve"]:
                continue
            if filter_key == "INCLUDED_WITH_TICKET" and entry["reservation_mode"] != "NONE":
                continue
            entries.append(entry)

        is_today = date == today
        if is_today:
            # R26.8 — upcoming first, finished subordinated.
            order = {"HAPPENING_NOW": 0, "STARTING_SOON": 1, "UPCOMING": 2, "CANCELLED": 3, "FINISHED": 4}
            entries.sort(key=lambda e: (order.get(e["live_state"], 2), e["start_time"]))
        else:
            entries.sort(key=lambda e: e["start_time"])

        empty_reason = None
        suggested_date = None
        if not entries:
            # R26.11 — say why, and suggest the nearest date with sessions.
            evaluation = self.calendar.evaluate_date(
                ctx, venue=venue, date=date, channel=ctx.channel, include_availability=False
            )
            empty_reason = (
                "The venue is closed on this date."
                if evaluation.state in ("CLOSED", "BLACKOUT")
                else "No shows are scheduled for this date."
            )
            suggested_date = self._next_date_with_sessions(ctx, venue_id=venue_id, after=date)

        return {
            "venue_id": venue_id,
            "date": date,
            "is_today": is_today,
            "timezone": tz,
            "timezone_label": f"All times shown in {tz} (venue local time)",  # R26.12
            "default_view": view or "TIMELINE",  # R26.1, R26.4
            "available_views": ["TIMELINE", "CARD", "DAILY", "WEEKLY"],
            "quick_dates": self._quick_dates(today, date),  # R26.5
            "filters": [
                {"key": "ALL", "label": "All"},
                {"key": "AVAILABLE_TO_BOOK", "label": "Available to book"},
                {"key": "INCLUDED_WITH_TICKET", "label": "Included with your ticket"},
                *[
                    {"key": f"CATEGORY:{c}", "label": c.replace("_", " ").title()}
                    for c in sorted(categories)
                ],
            ],
            "active_filter": filter_key,
            "sessions": entries,
            "empty_reason": empty_reason,
            "suggested_date": suggested_date,
            "next_session": next(
                (e for e in entries if e["live_state"] in ("STARTING_SOON", "UPCOMING")), None
            )
            if is_today
            else None,
            "accessibility": {
                "semantic_headings": True,
                "non_colour_status_cues": True,
                "keyboard_navigable": True,
                "screen_reader_labels": True,
            },
            "preview": preview,
        }

    def _timetable_entry(
        self,
        ctx: RequestContext,
        *,
        session: dict[str, Any],
        experience: dict[str, Any],
        venue: dict[str, Any],
        language: str,
        today: str,
    ) -> dict[str, Any]:
        tz = venue["timezone"]
        now = self.clock.now()
        effective_start = session["delayed_start_time"] or session["start_time"]
        starts_at = combine_local(session["date"], effective_start, tz)
        ends_at = combine_local(session["date"], session["end_time"], tz)
        if session["end_time"] < effective_start:
            ends_at = ends_at + _dt.timedelta(days=1)
        minutes_to_start = minutes_between(now, starts_at)
        duration = max(int(minutes_between(starts_at, ends_at)), 0)

        if session["status"] == "CANCELLED":
            live_state = "CANCELLED"
        elif session["date"] != today:
            live_state = "UPCOMING"
        elif now > ends_at:
            live_state = "FINISHED"
        elif starts_at <= now <= ends_at:
            live_state = "HAPPENING_NOW"
        elif 0 < minutes_to_start <= 30:
            live_state = "STARTING_SOON"
        else:
            live_state = "UPCOMING"

        mode = session["reservation_mode"]
        cutoff_passed = self.inventory.cutoff_passed(ctx, session, timezone=tz)
        remaining = session["remaining"]
        can_reserve = bool(
            mode in ("OPTIONAL", "REQUIRED")
            and session["status"] not in ("CANCELLED", "COMPLETED", "FULL", "HIDDEN")
            and not cutoff_passed  # R25.10
            and (remaining is None or remaining > 0)
            and live_state not in ("FINISHED", "CANCELLED")
        )
        area = None
        if session["area_id"]:
            area = self.db.query_one(
                "SELECT code, name_json, image_url, icon, floor, map_ref FROM areas "
                "WHERE id = ? AND tenant_id = ?",
                (session["area_id"], ctx.tenant_id),
            )
        # R26.9 — a countdown only when it is unambiguous and useful.
        show_countdown = live_state == "STARTING_SOON" and session["status"] not in ("DELAYED", "CANCELLED")
        # R19.4 — flag a location that differs from the show's usual one.
        differs = bool(
            session["area_id"] and experience.get("area_id") and session["area_id"] != experience["area_id"]
        )
        presentation = LIVE_STATE_PRESENTATION.get(live_state, LIVE_STATE_PRESENTATION["UPCOMING"])
        show_name = i18n_text(experience["name"], language, fallback=experience["code"])
        return {
            "session_id": session["id"],
            "show_id": experience["id"],
            "show_name": show_name,
            "category": experience.get("category"),
            "icon": experience.get("icon"),
            "thumbnail": experience.get("cover_image_url"),
            "start_time": effective_start,
            "scheduled_start_time": session["start_time"],
            "end_time": session["end_time"],
            "duration_minutes": duration,
            "location_display_name": (
                i18n_text(decode(area["name_json"], {}), language) if area else None
            ),
            "location_image": area["image_url"] if area else None,
            "location_floor": area["floor"] if area else None,
            "location_icon": area["icon"] if area else None,
            "view_on_map": bool(area and area["map_ref"]),
            "location_differs_from_usual": differs,
            "reservation_mode": mode,
            "reservation_required": mode == "REQUIRED",
            "can_reserve": can_reserve,
            "booking_closed": cutoff_passed,
            "capacity": session["capacity"],
            "remaining": remaining,
            "status": session["status"],
            "live_state": live_state,
            "presentation": presentation,
            "show_countdown": show_countdown,
            "minutes_to_start": int(minutes_to_start) if show_countdown else None,
            "delayed": session["status"] == "DELAYED",
            "accessible_label": f"{show_name} at {effective_start}, {presentation['label']}",
            "detail_action": {"label": "View Details", "session_id": session["id"]},
        }

    def _quick_dates(self, today: str, selected: str) -> list[dict[str, Any]]:
        """Horizontal date strip anchored on Today, plus a calendar button (R26.5)."""
        base = parse_date(today)
        out = []
        for offset in range(-1, 6):
            day = (base + _dt.timedelta(days=offset)).isoformat()
            out.append(
                {
                    "date": day,
                    "is_today": day == today,
                    "is_selected": day == selected,
                    "label": "Today" if day == today else parse_date(day).strftime("%a %d"),
                    "is_past": day < today,
                }
            )
        return out

    def _next_date_with_sessions(
        self, ctx: RequestContext, *, venue_id: str, after: str, search_days: int = 30
    ) -> str | None:
        start = parse_date(after)
        for offset in range(1, search_days + 1):
            candidate = (start + _dt.timedelta(days=offset)).isoformat()
            found = self.db.query_one(
                "SELECT 1 FROM sessions WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = 'SHOW' "
                "AND publication_state = 'PUBLISHED' AND customer_visible = 1 AND status <> 'CANCELLED' "
                "LIMIT 1",
                (ctx.tenant_id, venue_id, candidate),
            )
            if found is not None:
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # Show detail, eligibility, reservations, visit plan (R25, R27, R28)
    # ------------------------------------------------------------------ #

    def show_detail(
        self, ctx: RequestContext, session_id: str, *, language: str | None = None
    ) -> dict[str, Any]:
        """Full detail plus contextual actions (R27.1, R27.2)."""
        lang = language or ctx.language
        session = self.inventory.get_session(ctx, session_id)
        venue = self._venue(ctx, session["venue_id"])
        experience = self.catalog.get_experience(ctx, session["experience_id"])
        today = operating_date(self.clock.now(), venue["timezone"]).isoformat()
        entry = self._timetable_entry(
            ctx, session=session, experience=experience, venue=venue, language=lang, today=today
        )
        later = [
            s
            for s in self.inventory.list_sessions(
                ctx,
                venue_id=venue["id"],
                date=session["date"],
                kind="SHOW",
                experience_id=session["experience_id"],
                published_only=True,
                customer_visible_only=True,
            )
            if s["start_time"] > session["start_time"] and s["status"] != "CANCELLED"
        ]
        eligibility = experience.get("eligibility") or {}
        arrival = int(eligibility.get("recommended_arrival_minutes", 10))
        return {
            **entry,
            "description": i18n_text(experience["description"], lang) or None,
            "important_information": i18n_text(experience["instructions"], lang) or None,
            "images": experience.get("images") or [],
            "recommended_arrival_minutes": arrival,
            "recommended_arrival_time": _minus_minutes(entry["start_time"], arrival),
            "audience": experience.get("audience"),
            "languages": experience.get("languages") or [],
            "eligibility_mode": eligibility.get("mode", "INCLUDED_WITH_ADMISSION"),
            "ticket_requirement": eligibility.get("requirement_text"),
            "actions": {
                "book_this_show": entry["can_reserve"],
                "add_to_my_visit": True,
                "view_location": bool(session["area_id"]),
                "view_next_show": later[0]["id"] if later else None,
                "back_to_timetable": {"venue_id": venue["id"], "date": session["date"]},
            },
        }

    def check_eligibility(
        self, ctx: RequestContext, *, session_id: str, booking_id: str | None = None
    ) -> dict[str, Any]:
        """Can this guest attend? If not, what would qualify them? (R25.5, R25.6)"""
        session = self.inventory.get_session(ctx, session_id)
        experience = self.catalog.get_experience(ctx, session["experience_id"])
        eligibility = experience.get("eligibility") or {}
        mode = eligibility.get("mode", "INCLUDED_WITH_ADMISSION")
        if mode in ("INCLUDED_WITH_ADMISSION", "COMPLIMENTARY"):
            return {"eligible": True, "mode": mode}
        if booking_id is None:
            return {
                "eligible": False,
                "mode": mode,
                "requirement": eligibility.get("requirement_text")
                or "This show needs a qualifying ticket or reservation.",
                "qualifying_path": eligibility.get("qualifying_path"),
            }
        held_types = {
            row["ticket_type_id"]
            for row in self.db.query(
                "SELECT DISTINCT ticket_type_id FROM tickets WHERE tenant_id = ? AND booking_id = ? "
                "AND state IN ('ISSUED','VALID','PARTIALLY_USED')",
                (ctx.tenant_id, booking_id),
            )
        }
        required = set(eligibility.get("ticket_type_ids") or [])
        if mode in ("REQUIRES_TICKET_TYPE", "REQUIRES_ADDON", "PACKAGE_ONLY") and required:
            if held_types & required:
                return {"eligible": True, "mode": mode}
            return {
                "eligible": False,
                "mode": mode,
                "requirement": eligibility.get("requirement_text")
                or "Your ticket does not include this show.",
                "qualifying_path": eligibility.get("qualifying_path"),
            }
        return {"eligible": True, "mode": mode}

    def add_to_visit_plan(
        self, ctx: RequestContext, *, booking_id: str, session_id: str
    ) -> dict[str, Any]:
        """Add an itinerary note. Consumes no capacity unless it is a reservation (R27.3, R27.4)."""
        session = self.inventory.get_session(ctx, session_id)
        self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        reserved = self.db.query_one(
            "SELECT 1 FROM tickets WHERE tenant_id = ? AND booking_id = ? AND session_id = ? "
            "AND state IN ('ISSUED','VALID','PARTIALLY_USED')",
            (ctx.tenant_id, booking_id, session_id),
        )
        kind = "RESERVATION" if reserved is not None else "ITINERARY"
        existing = self.db.query_one(
            "SELECT id FROM visit_plan_entries WHERE tenant_id = ? AND booking_id = ? AND session_id = ?",
            (ctx.tenant_id, booking_id, session_id),
        )
        if existing is not None:
            self.db.update(
                "visit_plan_entries", existing["id"], {"kind": kind, "removed_at": None},
                tenant_id=ctx.tenant_id,
            )
            entry_id = existing["id"]
        else:
            entry_id = new_id("vpe")
            self.db.insert(
                "visit_plan_entries",
                {
                    "id": entry_id,
                    "tenant_id": ctx.tenant_id,
                    "booking_id": booking_id,
                    "session_id": session_id,
                    "kind": kind,
                    "created_at": to_iso(self.clock.now()),
                },
            )
        return {
            "entry_id": entry_id,
            "kind": kind,
            "is_reservation": kind == "RESERVATION",
            "consumes_capacity": False,
            "session_id": session_id,
        }

    def visit_plan(
        self, ctx: RequestContext, *, booking_id: str, language: str | None = None
    ) -> dict[str, Any]:
        """Chronological plan alongside the booking's entry time (R27.5, R27.7)."""
        lang = language or ctx.language
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        venue = self._venue(ctx, booking["venue_id"])
        rows = self.db.query(
            "SELECT * FROM visit_plan_entries WHERE tenant_id = ? AND booking_id = ? AND removed_at IS NULL",
            (ctx.tenant_id, booking_id),
        )
        today = operating_date(self.clock.now(), venue["timezone"]).isoformat()
        items: list[dict[str, Any]] = []
        for row in rows:
            session = self.inventory.get_session(ctx, row["session_id"])
            experience = (
                self.catalog.get_experience(ctx, session["experience_id"])
                if session["experience_id"]
                else None
            )
            if experience is None:
                continue
            entry = self._timetable_entry(
                ctx, session=session, experience=experience, venue=venue, language=lang, today=today
            )
            items.append(
                {
                    **entry,
                    "entry_id": row["id"],
                    "kind": row["kind"],
                    "is_reservation": row["kind"] == "RESERVATION",
                    # R27.7 — changes are reflected when the plan is next viewed.
                    "changed": session["status"] in ("DELAYED", "CANCELLED"),
                }
            )
        items.sort(key=lambda i: i["start_time"])
        return {
            "booking_id": booking_id,
            "booking_number": booking["booking_number"],
            "visit_date": booking["visit_date"],
            "entry_time": items[0]["start_time"] if items else None,
            "entries": items,
            "legend": {
                "RESERVATION": "Confirmed reservation",
                "ITINERARY": "Itinerary note only, no reservation held",
            },
        }

    def remove_from_visit_plan(self, ctx: RequestContext, *, entry_id: str) -> dict[str, Any]:
        entry = self.authz.load_scoped(ctx, "visit_plan_entries", entry_id, entity="visit_plan_entry")
        self.db.update(
            "visit_plan_entries", entry_id, {"removed_at": to_iso(self.clock.now())}, tenant_id=ctx.tenant_id
        )
        return {"entry_id": entry_id, "removed": True, "session_id": entry["session_id"]}

    def recommended_sessions(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        booking_id: str | None = None,
        limit: int = 3,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recommendations for the confirmation page and reminder email (R28.1, R28.5, R28.6)."""
        timetable = self.customer_timetable(ctx, venue_id=venue_id, date=date, language=language)
        scored: list[tuple[int, dict[str, Any]]] = []
        for entry in timetable["sessions"]:
            if entry["live_state"] in ("FINISHED", "CANCELLED"):
                continue
            experience = self.catalog.get_experience(ctx, entry["show_id"])
            eligibility = self.check_eligibility(ctx, session_id=entry["session_id"], booking_id=booking_id)
            item = dict(entry)
            # R28.6 — never advertise a show without stating the extra requirement.
            item["eligible"] = eligibility["eligible"]
            item["additional_requirement"] = (
                None if eligibility["eligible"] else eligibility.get("requirement")
            )
            score = int(experience.get("display_priority") or 0)
            if eligibility["eligible"]:
                score += 100
            scored.append((score, item))
        scored.sort(key=lambda s: (-s[0], s[1]["start_time"]))
        return [item for _, item in scored[:limit]]

    def timetable_link(self, ctx: RequestContext, *, venue_code: str) -> dict[str, Any]:
        """R28.3 / R28.4 — a live link resolved at view time, not a frozen snapshot."""
        return {
            "url": f"https://book.example/{venue_code}/shows",
            "label": "View Full Show Schedule",
            "resolves_at_view_time": True,
            "snapshot_embedded": False,
        }


__all__ = [
    "CONFLICT_STRATEGIES",
    "LIVE_STATE_PRESENTATION",
    "OVERRIDE_MODES",
    "RECURRENCE_KINDS",
    "SessionConflict",
    "ShowService",
]
