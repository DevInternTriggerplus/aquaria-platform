"""Booking rules, the operating calendar, and the customer-facing calendar.

Three jobs:

1. **Resolve booking rules** independently at Venue, Experience, Product and
   Session scope, with channel overrides on top (R6.1, R6.9). Resolution is
   per-key nearest-scope-wins, so a venue can set a 90-day advance window while
   one product narrows only the cutoff time and inherits everything else.
2. **Own the operating calendar** — closed dates, blackout dates, holidays and
   special operating dates. A special operating date's configuration takes
   precedence over the standard weekly pattern (R6.7).
3. **Render the calendar** with exactly one state per date, communicated by at
   least two independent cues and always with a text alternative for assistive
   technology (R7.1–R7.5).

Availability is not computed here. The inventory service injects
``availability_fn`` so that remaining capacity comes from the one authoritative
source (R10.11) rather than from a second, drifting implementation.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, combine_local, local, operating_date, parse_date, to_iso, weekday_code
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import NotAvailable, RuleViolation, ValidationError
from ..core.i18n import message
from ..core.ids import new_id
from .authz import AuthorizationService

#: Calendar entry kinds. ``SPECIAL`` carries overriding configuration (R6.7).
CALENDAR_KINDS: tuple[str, ...] = ("CLOSED", "BLACKOUT", "HOLIDAY", "SPECIAL")

#: Settings a booking rule may define (R6.2). Anything absent inherits.
BOOKING_RULE_KEYS: tuple[str, ...] = (
    "max_days_in_advance",
    "min_lead_time_minutes",
    "same_day_enabled",
    "cutoff_time",
    "available_weekdays",
    "available_dates",
    "blackout_dates",
    "max_capacity",
    "capacity_per_session",
    "capacity_per_channel",
    "capacity_per_partner",
    "max_per_booking",
    "min_per_booking",
    "on_sale_from",
)

#: Presentation cues per calendar state. Every state carries at least two
#: independent cues, and never relies on colour alone (R7.2, R68.4).
STATE_PRESENTATION: dict[str, dict[str, Any]] = {
    # Distinct, saturated swatch colours so each calendar state reads at a glance.
    # Colour is only ever a reinforcement — every state also carries an icon, a text
    # label and a pattern (R7.2, R68.4).
    "AVAILABLE": {"colour": "#0E9E86", "icon": "check-circle", "pattern": "none", "selectable": True},
    "LIMITED": {"colour": "#E0A33A", "icon": "alert-triangle", "pattern": "dots", "selectable": True},
    "SOLD_OUT": {"colour": "#B0453A", "icon": "slash", "pattern": "hatch", "selectable": False},
    "CLOSED": {"colour": "#6B7A85", "icon": "lock", "pattern": "solid-muted", "selectable": False},
    "BLACKOUT": {"colour": "#6B7A85", "icon": "x-octagon", "pattern": "cross-hatch", "selectable": False},
    "NOT_YET_ON_SALE": {"colour": "#2C6B84", "icon": "clock", "pattern": "outline", "selectable": False},
    "PAST": {"colour": "#A7ACB3", "icon": "history", "pattern": "faded", "selectable": False},
    "TODAY": {"colour": "#0E9E86", "icon": "target", "pattern": "ring", "selectable": True},
    "SELECTED": {"colour": "#17384C", "icon": "check-square", "pattern": "filled", "selectable": True},
}

_NON_SELECTABLE: frozenset[str] = frozenset({"SOLD_OUT", "CLOSED", "BLACKOUT", "NOT_YET_ON_SALE", "PAST"})


@dataclass(slots=True)
class DateEvaluation:
    """One calendar date's resolved state and the reason behind it."""

    date: str
    state: str
    selectable: bool
    reason_code: str | None = None
    reason: str | None = None
    capacity: int | None = None
    remaining: int | None = None
    on_sale_from: str | None = None
    is_today: bool = False
    session_count: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_cell(self, language: str = "en") -> dict[str, Any]:
        """Render for a calendar UI: state, two-plus cues, and an a11y label."""
        presentation = STATE_PRESENTATION.get(self.state, STATE_PRESENTATION["AVAILABLE"])
        label = message(f"calendar.state.{self.state}", language, fallback=self.state.replace("_", " ").title())
        pretty_date = parse_date(self.date).strftime("%d %B %Y")
        accessible = f"{pretty_date}: {label}"
        if self.reason:
            accessible = f"{accessible}. {self.reason}"
        if self.state == "LIMITED" and self.remaining is not None:
            accessible = f"{accessible}. {self.remaining} remaining"
        return {
            "date": self.date,
            "state": self.state,
            "selectable": self.selectable,
            "is_today": self.is_today,
            "label": label,
            "colour": presentation["colour"],
            "icon": presentation["icon"],
            "pattern": presentation["pattern"],
            "accessible_label": accessible,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "remaining": self.remaining,
            "capacity": self.capacity,
            "on_sale_from": self.on_sale_from,
            "session_count": self.session_count,
        }


class CalendarService:
    """Booking rules, operating calendar, and calendar rendering."""

    #: Injected by :class:`utp.app.Platform` so availability has one source (R10.11).
    availability_fn: Callable[..., dict[str, Any]] | None = None

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config

    # ------------------------------------------------------------------ #
    # Booking rules (R6.1, R6.2, R6.9)
    # ------------------------------------------------------------------ #

    def set_booking_rules(
        self,
        ctx: RequestContext,
        *,
        scope_type: str,
        scope_id: str | None,
        settings: dict[str, Any],
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Define or replace booking rules at one scope, optionally per channel."""
        if scope_type not in ("TENANT", "VENUE", "EXPERIENCE", "PRODUCT", "SESSION"):
            raise ValidationError(
                {"scope_type": "Scope must be TENANT, VENUE, EXPERIENCE, PRODUCT or SESSION."}
            )
        unknown = sorted(set(settings) - set(BOOKING_RULE_KEYS))
        if unknown:
            raise ValidationError(
                {"settings": f"Unknown booking rule setting(s): {', '.join(unknown)}."},
                message="One or more booking rule settings are not recognised.",
            )
        self.authz.require_page(ctx, "Capacity", "EDIT")
        now = to_iso(self.clock.now())
        existing = self.db.query_one(
            "SELECT id, settings_json FROM booking_rules WHERE tenant_id = ? AND scope_type = ? "
            "AND IFNULL(scope_id,'') = IFNULL(?,'') AND IFNULL(channel,'') = IFNULL(?,'') "
            "AND status = 'ACTIVE'",
            (ctx.tenant_id, scope_type, scope_id, channel),
        )
        with self.db.transaction():
            if existing is None:
                rule_id = new_id("brl")
                self.db.insert(
                    "booking_rules",
                    {
                        "id": rule_id,
                        "tenant_id": ctx.tenant_id,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "channel": channel,
                        "settings_json": settings,
                        "status": "ACTIVE",
                        "created_at": now,
                    },
                )
                previous = None
            else:
                rule_id = existing["id"]
                previous = decode(existing["settings_json"], {})
                self.db.update("booking_rules", rule_id, {"settings_json": settings}, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx,
                "CONFIG_CHANGE",
                target_type="booking_rules",
                target_id=rule_id,
                previous={"settings": previous} if previous is not None else None,
                new={"scope_type": scope_type, "scope_id": scope_id, "channel": channel, "settings": settings},
            )
        return {"booking_rules_id": rule_id, "scope_type": scope_type, "scope_id": scope_id, "channel": channel}

    def resolve_rules(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        channel: str,
        experience_id: str | None = None,
        product_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Merge booking rules by nearest scope, channel override winning (R6.1, R6.9).

        The merge is per key, so narrowing one setting at product scope does not
        discard the venue's other settings.
        """
        chain: list[tuple[str, str | None]] = [
            ("SESSION", session_id),
            ("PRODUCT", product_id),
            ("EXPERIENCE", experience_id),
            ("VENUE", venue_id),
            ("TENANT", None),
        ]
        resolved: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for scope_type, scope_id in chain:
            if scope_type != "TENANT" and not scope_id:
                continue
            # Channel-specific first: at the same scope, a channel override wins
            # over the channel-agnostic rule (R6.9).
            for channel_filter in (channel, None):
                row = self.db.query_one(
                    "SELECT settings_json FROM booking_rules WHERE tenant_id = ? AND scope_type = ? "
                    "AND IFNULL(scope_id,'') = IFNULL(?,'') AND IFNULL(channel,'') = IFNULL(?,'') "
                    "AND status = 'ACTIVE'",
                    (ctx.tenant_id, scope_type, scope_id, channel_filter),
                )
                if row is None:
                    continue
                settings = decode(row["settings_json"], {}) or {}
                for key, value in settings.items():
                    if key not in resolved:
                        resolved[key] = value
                        provenance[key] = f"{scope_type}{'/' + channel_filter if channel_filter else ''}"
        # Platform defaults fill anything still unset.
        defaults = {
            "max_days_in_advance": self.config.get_int(ctx, "booking.max_days_in_advance", venue_id=venue_id),
            "min_lead_time_minutes": self.config.get_int(ctx, "booking.min_lead_time_minutes", venue_id=venue_id),
            "same_day_enabled": self.config.get_bool(ctx, "booking.same_day_enabled", venue_id=venue_id),
            "cutoff_time": self.config.get(ctx, "booking.cutoff_time", venue_id=venue_id),
            "available_weekdays": self.config.get(ctx, "booking.available_weekdays", venue_id=venue_id),
            "max_per_booking": self.config.get_int(ctx, "booking.max_per_booking", venue_id=venue_id),
        }
        for key, value in defaults.items():
            if key not in resolved:
                resolved[key] = value
                provenance[key] = "PLATFORM"
        resolved["_provenance"] = provenance
        return resolved

    # ------------------------------------------------------------------ #
    # Operating calendar (R6.6, R6.7)
    # ------------------------------------------------------------------ #

    def set_calendar_entry(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        kind: str,
        config: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Mark a date closed, blacked out, a holiday, or a special operating date."""
        if kind not in CALENDAR_KINDS:
            raise ValidationError({"kind": f"Kind must be one of {', '.join(CALENDAR_KINDS)}."})
        self.authz.require_page(ctx, "Capacity", "EDIT")
        self.authz.require_venue(ctx, venue_id)
        parse_date(date)
        existing = self.db.query_one(
            "SELECT id FROM operating_calendar WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = ?",
            (ctx.tenant_id, venue_id, date, kind),
        )
        now = to_iso(self.clock.now())
        if existing is None:
            entry_id = new_id("cal")
            self.db.insert(
                "operating_calendar",
                {
                    "id": entry_id,
                    "tenant_id": ctx.tenant_id,
                    "venue_id": venue_id,
                    "date": date,
                    "kind": kind,
                    "config_json": config or {},
                    "note": note,
                    "created_at": now,
                },
            )
        else:
            entry_id = existing["id"]
            self.db.update(
                "operating_calendar",
                entry_id,
                {"config_json": config or {}, "note": note},
                tenant_id=ctx.tenant_id,
            )
        self.audit.record(
            ctx.for_venue(venue_id),
            "CONFIG_CHANGE",
            target_type="operating_calendar",
            target_id=entry_id,
            new={"date": date, "kind": kind, "config": config or {}, "note": note},
        )
        return {"entry_id": entry_id, "date": date, "kind": kind}

    def remove_calendar_entry(self, ctx: RequestContext, *, venue_id: str, date: str, kind: str) -> bool:
        """Remove a calendar marking. Administrators can always manage these (R6.6)."""
        self.authz.require_page(ctx, "Capacity", "DELETE")
        self.authz.require_venue(ctx, venue_id)
        cursor = self.db.execute(
            "DELETE FROM operating_calendar WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = ?",
            (ctx.tenant_id, venue_id, date, kind),
        )
        removed = cursor.rowcount > 0
        if removed:
            self.audit.record(
                ctx.for_venue(venue_id),
                "CONFIG_CHANGE",
                target_type="operating_calendar",
                target_id=f"{venue_id}:{date}:{kind}",
                previous={"date": date, "kind": kind},
                new={"removed": True},
            )
        return removed

    def calendar_entries(
        self, ctx: RequestContext, venue_id: str, date_from: str, date_to: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Calendar markings in a range, grouped by date."""
        rows = self.db.query(
            "SELECT * FROM operating_calendar WHERE tenant_id = ? AND venue_id = ? "
            "AND date BETWEEN ? AND ? ORDER BY date",
            (ctx.tenant_id, venue_id, date_from, date_to),
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            entry = dict(row)
            entry["config"] = decode(entry.pop("config_json"), {})
            grouped.setdefault(entry["date"], []).append(entry)
        return grouped

    def special_config(self, ctx: RequestContext, venue_id: str, date: str) -> dict[str, Any]:
        """Configuration overriding the weekly pattern for a special date (R6.7)."""
        row = self.db.query_one(
            "SELECT config_json FROM operating_calendar "
            "WHERE tenant_id = ? AND venue_id = ? AND date = ? AND kind = 'SPECIAL'",
            (ctx.tenant_id, venue_id, date),
        )
        return decode(row["config_json"], {}) if row else {}

    # ------------------------------------------------------------------ #
    # Date evaluation (R6.3 - R6.5, R7)
    # ------------------------------------------------------------------ #

    def evaluate_date(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        date: str,
        channel: str,
        product_id: str | None = None,
        experience_id: str | None = None,
        session_id: str | None = None,
        selected_date: str | None = None,
        include_availability: bool = True,
    ) -> DateEvaluation:
        """Resolve exactly one state for one date (R7.1)."""
        tz = venue["timezone"]
        boundary = int(venue.get("day_boundary_hour") or 0)
        today = operating_date(self.clock.now(), tz, boundary)
        target = parse_date(date)
        rules = self.resolve_rules(
            ctx,
            venue_id=venue["id"],
            channel=channel,
            experience_id=experience_id,
            product_id=product_id,
            session_id=session_id,
        )
        is_today = target == today
        special = self.special_config(ctx, venue["id"], date)

        # --- Past ------------------------------------------------------ #
        if target < today:
            return DateEvaluation(date, "PAST", False, "past", "This date has already passed.", is_today=False)

        # --- Not yet on sale ------------------------------------------- #
        max_advance = rules.get("max_days_in_advance")
        on_sale_from = rules.get("on_sale_from")
        if max_advance is not None:
            horizon = today + _dt.timedelta(days=int(max_advance))
            if target > horizon:
                opens_on = (target - _dt.timedelta(days=int(max_advance))).isoformat()
                return DateEvaluation(
                    date,
                    "NOT_YET_ON_SALE",
                    False,
                    "beyond_advance_window",
                    f"Booking opens on {parse_date(opens_on).strftime('%d %B %Y')}.",
                    on_sale_from=opens_on,
                    detail={"max_days_in_advance": int(max_advance)},
                )
        if on_sale_from and to_iso(self.clock.now()) < str(on_sale_from):
            return DateEvaluation(
                date,
                "NOT_YET_ON_SALE",
                False,
                "on_sale_from",
                "This date is not on sale yet.",
                on_sale_from=str(on_sale_from),
            )

        # --- Closed / blackout ----------------------------------------- #
        markings = {
            row["kind"]
            for row in self.db.query(
                "SELECT kind FROM operating_calendar WHERE tenant_id = ? AND venue_id = ? AND date = ?",
                (ctx.tenant_id, venue["id"], date),
            )
        }
        if "CLOSED" in markings:
            return DateEvaluation(
                date, "CLOSED", False, "closed", "The venue is closed on this date.", is_today=is_today
            )
        if "BLACKOUT" in markings:
            return DateEvaluation(
                date,
                "BLACKOUT",
                False,
                "blackout",
                "This date is not available for booking.",
                is_today=is_today,
            )

        # --- Weekday pattern ------------------------------------------- #
        # A special operating date overrides the weekly pattern entirely (R6.7).
        weekdays = special.get("available_weekdays", rules.get("available_weekdays"))
        if weekdays and weekday_code(date) not in weekdays and not special:
            return DateEvaluation(
                date,
                "CLOSED",
                False,
                "weekday_not_operating",
                "The venue does not operate on this day of the week.",
                is_today=is_today,
            )
        allow_list = rules.get("available_dates")
        if allow_list and date not in allow_list:
            return DateEvaluation(
                date,
                "CLOSED",
                False,
                "not_in_available_dates",
                "This date is not part of the published operating calendar.",
                is_today=is_today,
            )
        deny_list = rules.get("blackout_dates") or []
        if date in deny_list:
            return DateEvaluation(
                date, "BLACKOUT", False, "blackout", "This date is not available for booking.", is_today=is_today
            )

        # --- Same-day and cutoff --------------------------------------- #
        if is_today and rules.get("same_day_enabled") is False:
            return DateEvaluation(
                date,
                "CLOSED",
                False,
                "same_day_disabled",
                "Same-day booking is not available for this product in this channel.",
                is_today=True,
            )
        cutoff_state = self._cutoff_state(ctx, venue, date, rules, is_today=is_today, special=special)
        if cutoff_state is not None:
            return cutoff_state

        # --- Availability ---------------------------------------------- #
        capacity: int | None = None
        remaining: int | None = None
        session_count: int | None = None
        if include_availability and self.availability_fn is not None:
            snapshot = self.availability_fn(
                ctx,
                venue_id=venue["id"],
                date=date,
                product_id=product_id,
                experience_id=experience_id,
                channel=channel,
            )
            capacity = snapshot.get("capacity")
            remaining = snapshot.get("remaining")
            session_count = snapshot.get("session_count")
            if capacity is not None and (remaining or 0) <= 0:
                return DateEvaluation(
                    date,
                    "SOLD_OUT",
                    False,
                    "sold_out",
                    "This date is fully booked.",
                    capacity=capacity,
                    remaining=0,
                    is_today=is_today,
                    session_count=session_count,
                )

        state = "AVAILABLE"
        reason_code = None
        reason = None
        if capacity is not None and remaining is not None and self._is_limited(ctx, venue, capacity, remaining):
            state = "LIMITED"
            reason_code = "limited"
            reason = f"Only {remaining} left for this date."
        if selected_date == date:
            state = "SELECTED"
        return DateEvaluation(
            date,
            state,
            True,
            reason_code,
            reason,
            capacity=capacity,
            remaining=remaining,
            is_today=is_today,
            session_count=session_count,
        )

    def _cutoff_state(
        self,
        ctx: RequestContext,
        venue: dict[str, Any],
        date: str,
        rules: dict[str, Any],
        *,
        is_today: bool,
        special: dict[str, Any] | None = None,
    ) -> DateEvaluation | None:
        """Last admission, daily cutoff and minimum lead time, in venue-local time.

        Last admission is a distinct concept from the configurable booking cutoff
        (update spec §4-§6): once the current venue-local time is past the venue's
        Last Admission Time, today can no longer be booked online, and the message is
        customer-friendly rather than technical (§5, §42). A special operating date's
        hours override the standard ones (R6.7).
        """
        now = self.clock.now()
        hours = (special or {}).get("operating_hours") or (venue.get("operating_hours") or {}).get("default", {})
        last_admission = hours.get("last_admission")
        if last_admission and is_today:
            last_admission_at = combine_local(date, str(last_admission), venue["timezone"])
            if now > last_admission_at:
                return DateEvaluation(
                    date,
                    "CLOSED",
                    False,
                    "last_admission_passed",
                    message("calendar.last_admission_passed", ctx.language),
                    is_today=True,
                    detail={"last_admission": last_admission, "message_short_key": "calendar.last_admission_short"},
                )
        cutoff_time = rules.get("cutoff_time")
        if cutoff_time and is_today:
            cutoff_at = combine_local(date, str(cutoff_time), venue["timezone"])
            if now > cutoff_at:
                return DateEvaluation(
                    date,
                    "CLOSED",
                    False,
                    "cutoff_passed",
                    f"Online booking for today closed at {cutoff_time}.",
                    is_today=True,
                    detail={"cutoff_time": cutoff_time},
                )
        lead = int(rules.get("min_lead_time_minutes") or 0)
        if lead:
            open_time = (venue.get("operating_hours") or {}).get("default", {}).get("open", "00:00")
            earliest_entry = combine_local(date, open_time, venue["timezone"])
            if add_minutes(now, lead) > earliest_entry:
                return DateEvaluation(
                    date,
                    "CLOSED",
                    False,
                    "lead_time",
                    f"This product must be booked at least {lead} minutes ahead.",
                    is_today=is_today,
                    detail={"min_lead_time_minutes": lead},
                )
        return None

    def _is_limited(
        self, ctx: RequestContext, venue: dict[str, Any], capacity: int, remaining: int
    ) -> bool:
        """Configurable "limited availability" threshold (R7.5)."""
        threshold = self.config.get(
            ctx, "calendar.limited_availability_threshold", venue_id=venue["id"]
        ) or {}
        mode = threshold.get("mode", "PERCENT")
        value = int(threshold.get("value", 20))
        if capacity <= 0:
            return False
        if mode == "UNITS":
            return remaining <= value
        return (remaining * 100) <= (capacity * value)

    # ------------------------------------------------------------------ #
    # Calendar rendering (R7)
    # ------------------------------------------------------------------ #

    def calendar(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        date_from: str,
        date_to: str,
        channel: str,
        product_id: str | None = None,
        experience_id: str | None = None,
        selected_date: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Render calendar cells for a date range."""
        lang = language or ctx.language
        start = parse_date(date_from)
        end = parse_date(date_to)
        if end < start:
            raise ValidationError({"date_to": "The end date must not be before the start date."})
        horizon = self.config.get_int(ctx, "calendar.horizon_days", venue_id=venue["id"])
        if (end - start).days > horizon:
            raise ValidationError(
                {"date_to": f"Request at most {horizon} days at a time."},
                message="That date range is too large.",
            )
        cells: list[dict[str, Any]] = []
        current = start
        while current <= end:
            evaluation = self.evaluate_date(
                ctx,
                venue=venue,
                date=current.isoformat(),
                channel=channel,
                product_id=product_id,
                experience_id=experience_id,
                selected_date=selected_date,
            )
            cells.append(evaluation.as_cell(lang))
            current += _dt.timedelta(days=1)
        return {
            "venue_id": venue["id"],
            "timezone": venue["timezone"],
            "channel": channel,
            "date_from": date_from,
            "date_to": date_to,
            "mobile_breakpoint_px": self.config.get_int(ctx, "ui.mobile_breakpoint_px", venue_id=venue["id"]),
            "min_touch_target_px": self.config.get_int(ctx, "ui.touch_target_min_px", venue_id=venue["id"]),
            "legend": [
                {
                    "state": state,
                    "label": message(f"calendar.state.{state}", lang, fallback=state),
                    **presentation,
                }
                for state, presentation in STATE_PRESENTATION.items()
            ],
            "cells": cells,
        }

    def next_bookable_dates(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        channel: str,
        product_id: str | None = None,
        experience_id: str | None = None,
        after: str | None = None,
        limit: int = 3,
        search_days: int = 60,
    ) -> list[str]:
        """The nearest bookable dates — used to make every rejection actionable (R6.4)."""
        tz = venue["timezone"]
        start = parse_date(after) if after else operating_date(self.clock.now(), tz, int(venue.get("day_boundary_hour") or 0))
        found: list[str] = []
        for offset in range(0, search_days + 1):
            candidate = (start + _dt.timedelta(days=offset)).isoformat()
            evaluation = self.evaluate_date(
                ctx,
                venue=venue,
                date=candidate,
                channel=channel,
                product_id=product_id,
                experience_id=experience_id,
            )
            if evaluation.selectable:
                found.append(candidate)
                if len(found) >= limit:
                    break
        return found

    def assert_bookable(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        date: str,
        channel: str,
        product_id: str | None = None,
        experience_id: str | None = None,
        session_id: str | None = None,
        include_availability: bool = True,
    ) -> DateEvaluation:
        """Gate a booking attempt, explaining any rejection and the nearest option (R6.4).

        ``include_availability=False`` checks only the *rules* — closed dates, blackout,
        advance window, cutoff, weekday pattern — and skips the sold-out test. Callers
        pass it when the customer already holds the inventory: a guest holding the last
        remaining place must not be told the date is sold out by their own hold, which is
        precisely what the hold exists to prevent (R10.1).
        """
        evaluation = self.evaluate_date(
            ctx,
            venue=venue,
            date=date,
            channel=channel,
            product_id=product_id,
            experience_id=experience_id,
            session_id=session_id,
            include_availability=include_availability,
        )
        if evaluation.selectable:
            return evaluation
        alternatives = self.next_bookable_dates(
            ctx,
            venue=venue,
            channel=channel,
            product_id=product_id,
            experience_id=experience_id,
            after=date,
        )
        details = {
            "date": date,
            "state": evaluation.state,
            "reason_code": evaluation.reason_code,
            "nearest_available_dates": alternatives,
            **evaluation.detail,
        }
        if evaluation.on_sale_from:
            details["on_sale_from"] = evaluation.on_sale_from
        error_class = NotAvailable if evaluation.state == "SOLD_OUT" else RuleViolation
        raise error_class(
            evaluation.reason or "That date cannot be booked.",
            details=details,
        )

    def selectable_dates(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        channel: str,
        product_id: str | None = None,
        experience_id: str | None = None,
        days: int = 90,
    ) -> list[str]:
        """Convenience list used by kiosk and POS quick-pick UIs."""
        tz = venue["timezone"]
        start = operating_date(self.clock.now(), tz, int(venue.get("day_boundary_hour") or 0))
        out: list[str] = []
        for offset in range(days + 1):
            candidate = (start + _dt.timedelta(days=offset)).isoformat()
            evaluation = self.evaluate_date(
                ctx,
                venue=venue,
                date=candidate,
                channel=channel,
                product_id=product_id,
                experience_id=experience_id,
            )
            if evaluation.selectable:
                out.append(candidate)
        return out


__all__ = [
    "BOOKING_RULE_KEYS",
    "CALENDAR_KINDS",
    "STATE_PRESENTATION",
    "CalendarService",
    "DateEvaluation",
]
