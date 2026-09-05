"""Sessions, allocations, holds — the authoritative capacity mechanism.

This is the module the whole platform's integrity rests on, so the guarantees are
stated plainly:

* ``remaining = capacity - confirmed - active_holds``, never negative (R8.4).
* Every mutation of ``confirmed`` runs inside ``BEGIN IMMEDIATE``, which serializes
  writers, and is issued as a *conditional* UPDATE whose predicate includes the
  capacity limit. A losing request is detected by ``rowcount == 0`` and reported as
  a distinct "just sold out" outcome (R10.5, R10.6).
* The ``sessions`` table additionally carries ``CHECK (capacity IS NULL OR
  confirmed <= capacity)``. Even a future coding mistake cannot oversell, because
  the database refuses the row (R46.6 applied to capacity).
* Holds are scoped to tenant, session and channel. A confirmation must present a
  hold from its own channel, so one channel can never consume another's hold
  (R10.10).

Day-level capacity is modelled as a session too. A general-admission product with
a daily cap gets an automatically materialized "day session" spanning the venue's
operating hours. That keeps a single code path — and therefore a single
correctness argument — for timed entry, shows, classes and plain daily caps.

Products with no capacity limit take no holds at all. That is the explicit
resolution of ambiguity C.1 in the requirements analysis, and
:meth:`InventoryService.acquire_hold` returns ``None`` for them rather than
inventing a hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..core.audit import AuditLog
from ..core.clock import (
    Clock,
    add_minutes,
    combine_local,
    end_time_from_duration,
    minutes_between,
    parse_date,
    parse_instant,
    to_iso,
)
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database
from ..core.errors import (
    ConflictError,
    HoldExpired,
    JustSoldOut,
    NotAvailable,
    NotFound,
    RuleViolation,
    ValidationError,
)
from ..core.ids import new_id
from ..domain import enums
from .authz import AuthorizationService

#: Session states in which new holds and confirmations are refused.
_CLOSED_STATUSES: frozenset[str] = frozenset({"FULL", "CANCELLED", "COMPLETED", "HIDDEN"})

#: Marks a session that exists only to carry a product's daily capacity.
DAY_SESSION_SOURCE = "DAY_AUTO"


@dataclass(slots=True)
class Availability:
    """Availability snapshot for one session."""

    session_id: str
    capacity: int | None
    confirmed: int
    held: int
    remaining: int | None
    status: str
    unlimited: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "capacity": self.capacity,
            "confirmed": self.confirmed,
            "held": self.held,
            "remaining": self.remaining,
            "status": self.status,
            "unlimited": self.unlimited,
        }


@dataclass(slots=True)
class Hold:
    """An expiring reservation of capacity during checkout."""

    id: str
    session_id: str | None
    cart_id: str
    channel: str
    quantity: int
    state: str
    expires_at: str
    zone_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hold_id": self.id,
            "session_id": self.session_id,
            "zone_id": self.zone_id,
            "cart_id": self.cart_id,
            "channel": self.channel,
            "quantity": self.quantity,
            "state": self.state,
            "expires_at": self.expires_at,
        }


class InventoryService:
    """Sessions, channel/partner allocations and capacity holds."""

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
    # Sessions (R8)
    # ------------------------------------------------------------------ #

    def create_session(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        start_time: str,
        kind: str = "PRODUCT",
        product_id: str | None = None,
        experience_id: str | None = None,
        area_id: str | None = None,
        end_time: str | None = None,
        duration_minutes: int | None = None,
        capacity: int | None = None,
        booking_cutoff_minutes: int | None = None,
        grace_minutes: int | None = None,
        reservation_mode: str = "REQUIRED",
        booking_required: bool = True,
        check_in_required: bool = False,
        waiting_list_enabled: bool = False,
        customer_visible: bool = True,
        publication_state: str = "PUBLISHED",
        status: str = "SCHEDULED",
        seat_layout_version_id: str | None = None,
        source: str = "MANUAL",
        pattern_id: str | None = None,
        override_id: str | None = None,
        notes: str | None = None,
        require_permission: bool = True,
    ) -> dict[str, Any]:
        """Create a capacity-bearing session (R8.2) or show session (R24.1)."""
        page = "Show Schedule" if kind == "SHOW" else "Time Slots"
        if require_permission:
            self.authz.require_page(ctx, page, "ADD")
            self.authz.require_venue(ctx, venue_id)
        if not product_id and not experience_id:
            raise ValidationError(
                {"product_id": "A session must belong to a product or an experience."},
                message="Choose what this session is for.",
            )
        if reservation_mode not in enums.RESERVATION_MODES:
            raise ValidationError({"reservation_mode": "Choose NONE, OPTIONAL or REQUIRED."})
        if status not in enums.SESSION_STATUSES:
            raise ValidationError({"status": f"Status must be one of {', '.join(enums.SESSION_STATUSES)}."})
        parse_date(date)
        if end_time is None:
            minutes = duration_minutes if duration_minutes is not None else 60
            end_time = end_time_from_duration(start_time, int(minutes))
        session_id = new_id("ses")
        now = to_iso(self.clock.now())
        self.db.insert(
            "sessions",
            {
                "id": session_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id,
                "kind": kind,
                "product_id": product_id,
                "experience_id": experience_id,
                "area_id": area_id,
                "date": date,
                "start_time": start_time,
                "end_time": end_time,
                "capacity": capacity,
                "confirmed": 0,
                "status": status,
                "publication_state": publication_state,
                "booking_cutoff_minutes": booking_cutoff_minutes,
                "grace_minutes": int(
                    grace_minutes
                    if grace_minutes is not None
                    else self.config.get_int(ctx, "session.grace_minutes", venue_id=venue_id)
                ),
                "reservation_mode": reservation_mode,
                "booking_required": 1 if booking_required else 0,
                "check_in_required": 1 if check_in_required else 0,
                "waiting_list_enabled": 1 if waiting_list_enabled else 0,
                "customer_visible": 1 if customer_visible else 0,
                "seat_layout_version_id": seat_layout_version_id,
                "source": source,
                "pattern_id": pattern_id,
                "override_id": override_id,
                "notes": notes,
                "created_at": now,
            },
        )
        return self.get_session(ctx, session_id)

    def get_session(self, ctx: RequestContext, session_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "sessions", session_id, entity="session")
        availability = self._availability_from_row(ctx, record)
        record.update(availability.as_dict())
        return record

    def list_sessions(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        kind: str | None = None,
        product_id: str | None = None,
        experience_id: str | None = None,
        statuses: Iterable[str] | None = None,
        published_only: bool = False,
        customer_visible_only: bool = False,
        include_day_sessions: bool = True,
    ) -> list[dict[str, Any]]:
        sql = ["SELECT * FROM sessions WHERE tenant_id = ? AND venue_id = ?"]
        params: list[Any] = [ctx.tenant_id, venue_id]
        if date:
            sql.append("AND date = ?")
            params.append(date)
        if date_from:
            sql.append("AND date >= ?")
            params.append(date_from)
        if date_to:
            sql.append("AND date <= ?")
            params.append(date_to)
        if kind:
            sql.append("AND kind = ?")
            params.append(kind)
        if product_id:
            sql.append("AND product_id = ?")
            params.append(product_id)
        if experience_id:
            sql.append("AND experience_id = ?")
            params.append(experience_id)
        if statuses:
            codes = list(statuses)
            sql.append(f"AND status IN ({', '.join('?' for _ in codes)})")
            params.extend(codes)
        if published_only:
            sql.append("AND publication_state = 'PUBLISHED'")
        if customer_visible_only:
            sql.append("AND customer_visible = 1 AND status <> 'HIDDEN'")
        if not include_day_sessions:
            sql.append("AND source <> ?")
            params.append(DAY_SESSION_SOURCE)
        sql.append("ORDER BY date, start_time, id")
        out = []
        for row in self.db.query(" ".join(sql), params):
            record = dict(row)
            record.update(self._availability_from_row(ctx, record).as_dict())
            out.append(record)
        return out

    def set_capacity(
        self,
        ctx: RequestContext,
        session_id: str,
        capacity: int | None,
        *,
        reason: str | None = None,
        override: bool = False,
    ) -> dict[str, Any]:
        """Change a session's capacity.

        Reducing below current confirmed consumption is refused unless the actor
        holds ``OVERRIDE_CAPACITY``; even then existing bookings are never
        cancelled automatically (R8.9).
        """
        session = self.get_session(ctx, session_id)
        page = "Show Schedule" if session["kind"] == "SHOW" else "Capacity"
        self.authz.require_page(ctx, page, "EDIT", target_type="session", target_id=session_id)
        self.authz.require_venue(ctx, session["venue_id"])
        confirmed = int(session["confirmed"])
        if capacity is not None and int(capacity) < confirmed:
            if not override:
                raise ConflictError(
                    f"{confirmed} place(s) are already confirmed for this session. "
                    f"Capacity cannot be set below {confirmed}.",
                    details={"confirmed": confirmed, "requested_capacity": int(capacity)},
                )
            self.authz.require_action(
                ctx, "OVERRIDE_CAPACITY", target_type="session", target_id=session_id, reason=reason
            )
        overridden = capacity is not None and int(capacity) < confirmed
        with self.db.transaction(immediate=True):
            self.db.update(
                "sessions",
                session_id,
                {
                    "capacity": capacity,
                    # Recorded on the row so the CHECK constraint permits the state
                    # and so reporting can distinguish an overridden session.
                    "capacity_overridden": 1 if overridden else 0,
                    "updated_at": to_iso(self.clock.now()),
                },
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(session["venue_id"]),
                "CAPACITY_OVERRIDE" if overridden else "CONFIG_CHANGE",
                target_type="session",
                target_id=session_id,
                previous={"capacity": session["capacity"], "confirmed": confirmed},
                new={"capacity": capacity, "bookings_preserved": True},
                reason=reason,
                severity="WARNING" if overridden else "INFO",
            )
            self._refresh_status(ctx, session_id)
        return self.get_session(ctx, session_id)

    def set_status(
        self,
        ctx: RequestContext,
        session_id: str,
        status: str,
        *,
        reason: str | None = None,
        delayed_start_time: str | None = None,
        require_permission: bool = True,
    ) -> dict[str, Any]:
        """Move a session through its lifecycle (R8.3, R24.2–R24.7)."""
        if status not in enums.SESSION_STATUSES:
            raise ValidationError({"status": f"Status must be one of {', '.join(enums.SESSION_STATUSES)}."})
        session = self.get_session(ctx, session_id)
        page = "Show Schedule" if session["kind"] == "SHOW" else "Time Slots"
        if require_permission:
            self.authz.require_page(ctx, page, "EDIT", target_type="session", target_id=session_id)
            self.authz.require_venue(ctx, session["venue_id"])
        payload: dict[str, Any] = {"status": status, "updated_at": to_iso(self.clock.now())}
        if status == "DELAYED":
            if not delayed_start_time:
                raise ValidationError(
                    {"delayed_start_time": "Enter the new expected start time."},
                    message="A delay needs a new expected start time.",
                )
            payload["delayed_start_time"] = delayed_start_time
        if status == "CANCELLED":
            payload["cancel_reason"] = reason
        with self.db.transaction():
            self.db.update("sessions", session_id, payload, tenant_id=ctx.tenant_id)
            action = {
                "CANCELLED": "SHOW_CANCEL" if session["kind"] == "SHOW" else "CONFIG_CHANGE",
                "DELAYED": "SHOW_SESSION_DELAYED",
            }.get(status, "CONFIG_CHANGE")
            self.audit.record(
                ctx.for_venue(session["venue_id"]),
                action,
                target_type="session",
                target_id=session_id,
                previous={"status": session["status"], "delayed_start_time": session.get("delayed_start_time")},
                new=payload,
                reason=reason,
                severity="WARNING" if status in ("CANCELLED", "DELAYED") else "INFO",
            )
        return self.get_session(ctx, session_id)

    def confirmed_reservation_count(self, ctx: RequestContext, session_id: str) -> int:
        """How many guests hold a confirmed place — shown before any disruptive change."""
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM tickets WHERE tenant_id = ? AND session_id = ? "
                "AND state IN ('ISSUED','VALID','PARTIALLY_USED','USED')",
                (ctx.tenant_id, session_id),
                default=0,
            )
        )

    # ------------------------------------------------------------------ #
    # Availability (R8.4, R10.11)
    # ------------------------------------------------------------------ #

    def availability(self, ctx: RequestContext, session_id: str) -> Availability:
        row = self.db.query_one(
            "SELECT * FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, ctx.tenant_id)
        )
        if row is None:
            raise NotFound(details={"entity": "session"})
        return self._availability_from_row(ctx, dict(row))

    def _availability_from_row(self, ctx: RequestContext, row: dict[str, Any]) -> Availability:
        capacity = row["capacity"]
        confirmed = int(row["confirmed"] or 0)
        held = self._active_hold_quantity(ctx, row["id"])
        if capacity is None:
            return Availability(row["id"], None, confirmed, held, None, row["status"], True)
        remaining = max(int(capacity) - confirmed - held, 0)  # never negative (R8.4)
        return Availability(row["id"], int(capacity), confirmed, held, remaining, row["status"], False)

    def _active_hold_quantity(self, ctx: RequestContext, session_id: str, *, zone_id: str | None = None) -> int:
        sql = (
            "SELECT COALESCE(SUM(quantity), 0) FROM holds WHERE tenant_id = ? AND session_id = ? "
            "AND state = 'ACTIVE' AND expires_at > ?"
        )
        params: list[Any] = [ctx.tenant_id, session_id, to_iso(self.clock.now())]
        if zone_id is not None:
            sql += " AND zone_id = ?"
            params.append(zone_id)
        return int(self.db.scalar(sql, params, default=0))

    def availability_snapshot(
        self,
        ctx: RequestContext,
        *,
        venue_id: str,
        date: str,
        product_id: str | None = None,
        experience_id: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Day-level availability, aggregated from the authoritative sessions.

        Injected into :class:`~utp.services.calendar_rules.CalendarService` so the
        calendar and the checkout read the same numbers (R10.11).
        """
        sessions = self.list_sessions(
            ctx,
            venue_id=venue_id,
            date=date,
            product_id=product_id,
            experience_id=experience_id,
            statuses=[s for s in enums.SESSION_STATUSES if s not in ("CANCELLED", "HIDDEN")],
        )
        if not sessions:
            # No session records: either the product is unlimited or a day session
            # has not been materialized yet. Either way there is nothing to cap.
            return {"capacity": None, "remaining": None, "session_count": 0, "unlimited": True}
        if any(s["capacity"] is None for s in sessions):
            return {
                "capacity": None,
                "remaining": None,
                "session_count": len(sessions),
                "unlimited": True,
            }
        capacity = sum(int(s["capacity"]) for s in sessions)
        remaining = sum(int(s["remaining"] or 0) for s in sessions)
        return {
            "capacity": capacity,
            "remaining": remaining,
            "session_count": len(sessions),
            "unlimited": False,
            "staleness_bound_seconds": self.config.get_int(
                ctx, "availability.cache_max_staleness_seconds", venue_id=venue_id
            ),
        }

    def _refresh_status(self, ctx: RequestContext, session_id: str) -> str:
        """Derive AVAILABLE / LIMITED / FULL from live numbers (R8.5, R24.3)."""
        row = self.db.query_one(
            "SELECT * FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, ctx.tenant_id)
        )
        if row is None:
            raise NotFound(details={"entity": "session"})
        record = dict(row)
        if record["status"] in ("CANCELLED", "COMPLETED", "HIDDEN", "DELAYED"):
            return record["status"]
        availability = self._availability_from_row(ctx, record)
        if availability.unlimited:
            status = "AVAILABLE"
        elif (availability.remaining or 0) <= 0:
            status = "FULL"
        else:
            threshold = self.config.get(
                ctx, "calendar.limited_availability_threshold", venue_id=record["venue_id"]
            ) or {}
            mode = threshold.get("mode", "PERCENT")
            value = int(threshold.get("value", 20))
            capacity = availability.capacity or 0
            limited = (
                (availability.remaining or 0) <= value
                if mode == "UNITS"
                else capacity > 0 and ((availability.remaining or 0) * 100) <= capacity * value
            )
            status = "LIMITED" if limited else "AVAILABLE"
        if status != record["status"]:
            self.db.update("sessions", session_id, {"status": status}, tenant_id=ctx.tenant_id)
        return status

    # ------------------------------------------------------------------ #
    # Day sessions for products without explicit time slots
    # ------------------------------------------------------------------ #

    def ensure_day_session(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        product: dict[str, Any],
        date: str,
        channel: str,
        daily_capacity: int | None,
    ) -> dict[str, Any] | None:
        """Materialize the day-level capacity record for a session-less product.

        Returns ``None`` when the product has no capacity limit, which is how the
        platform expresses "no hold needed" for unlimited inventory (analysis C.1).
        """
        if daily_capacity is None:
            return None
        existing = self.db.query_one(
            "SELECT id FROM sessions WHERE tenant_id = ? AND venue_id = ? AND product_id = ? "
            "AND date = ? AND source = ?",
            (ctx.tenant_id, venue["id"], product["id"], date, DAY_SESSION_SOURCE),
        )
        if existing is not None:
            return self.get_session(ctx, existing["id"])
        hours = (venue.get("operating_hours") or {}).get("default", {})
        with self.db.transaction(immediate=True):
            # Re-check inside the write lock so two concurrent first-sales of the day
            # cannot create two day sessions.
            again = self.db.query_one(
                "SELECT id FROM sessions WHERE tenant_id = ? AND venue_id = ? AND product_id = ? "
                "AND date = ? AND source = ?",
                (ctx.tenant_id, venue["id"], product["id"], date, DAY_SESSION_SOURCE),
            )
            if again is not None:
                return self.get_session(ctx, again["id"])
            return self.create_session(
                ctx,
                venue_id=venue["id"],
                date=date,
                start_time=hours.get("open", "00:00"),
                end_time=hours.get("close", "23:59"),
                kind="PRODUCT",
                product_id=product["id"],
                experience_id=product.get("experience_id"),
                capacity=int(daily_capacity),
                reservation_mode="NONE",
                booking_required=False,
                customer_visible=False,
                status="AVAILABLE",
                source=DAY_SESSION_SOURCE,
                require_permission=False,
            )

    def resolve_inventory_session(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        product: dict[str, Any],
        date: str,
        channel: str,
        session_id: str | None,
        rules: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Pick the session that will carry this line's capacity.

        Honours the product's session requirement (R3.3, R8.1) and falls back to a
        day session when the product has a daily cap but no time slots.
        """
        requirement = product["session_requirement"]
        if session_id:
            session = self.get_session(ctx, session_id)
            if session["product_id"] not in (None, product["id"]):
                raise ValidationError(
                    {"session_id": "That time slot belongs to a different product."},
                    message="The selected time is not available for this ticket.",
                )
            if session["date"] != date:
                raise ValidationError(
                    {"session_id": "That time slot is on a different date."},
                    message="The selected time does not match the visit date.",
                )
            return session
        if requirement == "REQUIRED":
            raise ValidationError(
                {"session_id": "Choose a time slot."},
                message="This ticket requires a time slot.",
            )
        daily_capacity = rules.get("max_capacity")
        return self.ensure_day_session(
            ctx,
            venue=venue,
            product=product,
            date=date,
            channel=channel,
            daily_capacity=int(daily_capacity) if daily_capacity is not None else None,
        )

    # ------------------------------------------------------------------ #
    # Allocations (R9)
    # ------------------------------------------------------------------ #

    def create_allocation(
        self,
        ctx: RequestContext,
        *,
        session_id: str,
        alloc_type: str,
        alloc_key: str,
        quantity: int | None = None,
        percent_bp: int | None = None,
        overflow_allowed: bool = False,
        release_minutes_before: int | None = None,
    ) -> dict[str, Any]:
        """Ring-fence capacity for a channel or a partner (R9.1)."""
        if alloc_type not in ("CHANNEL", "PARTNER"):
            raise ValidationError({"alloc_type": "Allocation type must be CHANNEL or PARTNER."})
        if quantity is None and percent_bp is None:
            raise ValidationError(
                {"quantity": "Provide either a fixed quantity or a percentage."},
                message="An allocation needs a size.",
            )
        session = self.get_session(ctx, session_id)
        self.authz.require_page(ctx, "Capacity", "ADD")
        self.authz.require_venue(ctx, session["venue_id"])
        resolved = self._allocation_size(session, quantity, percent_bp)
        existing_total = int(
            self.db.scalar(
                "SELECT COALESCE(SUM(CASE WHEN quantity IS NOT NULL THEN quantity ELSE 0 END), 0) "
                "FROM session_allocations WHERE tenant_id = ? AND session_id = ? AND released_at IS NULL",
                (ctx.tenant_id, session_id),
                default=0,
            )
        )
        if session["capacity"] is not None and existing_total + resolved > int(session["capacity"]):
            # R9.3 — allocations may never sum beyond the session's own capacity.
            raise ConflictError(
                "Allocations would exceed the session capacity.",
                details={
                    "session_capacity": int(session["capacity"]),
                    "already_allocated": existing_total,
                    "requested": resolved,
                },
            )
        allocation_id = new_id("alc")
        self.db.insert(
            "session_allocations",
            {
                "id": allocation_id,
                "tenant_id": ctx.tenant_id,
                "session_id": session_id,
                "alloc_type": alloc_type,
                "alloc_key": alloc_key,
                "quantity": resolved,
                "percent_bp": percent_bp,
                "confirmed": 0,
                "overflow_allowed": 1 if overflow_allowed else 0,
                "release_minutes_before": release_minutes_before,
            },
        )
        self.audit.record(
            ctx.for_venue(session["venue_id"]),
            "CONFIG_CHANGE",
            target_type="session_allocation",
            target_id=allocation_id,
            new={
                "session_id": session_id,
                "alloc_type": alloc_type,
                "alloc_key": alloc_key,
                "quantity": resolved,
                "overflow_allowed": overflow_allowed,
            },
        )
        return dict(
            self.db.query_one("SELECT * FROM session_allocations WHERE id = ?", (allocation_id,))
        )

    def _allocation_size(
        self, session: dict[str, Any], quantity: int | None, percent_bp: int | None
    ) -> int:
        if quantity is not None:
            return int(quantity)
        if session["capacity"] is None:
            raise ValidationError(
                {"percent_bp": "A percentage allocation needs a session with a capacity."},
                message="Set a session capacity before allocating a percentage.",
            )
        return int(int(session["capacity"]) * int(percent_bp) // 10_000)

    def find_allocation(
        self, ctx: RequestContext, session_id: str, *, channel: str, partner_id: str | None
    ) -> dict[str, Any] | None:
        """The allocation a request must draw from, partner taking precedence."""
        if partner_id:
            row = self.db.query_one(
                "SELECT * FROM session_allocations WHERE tenant_id = ? AND session_id = ? "
                "AND alloc_type = 'PARTNER' AND alloc_key = ? AND released_at IS NULL",
                (ctx.tenant_id, session_id, partner_id),
            )
            if row is not None:
                return dict(row)
        row = self.db.query_one(
            "SELECT * FROM session_allocations WHERE tenant_id = ? AND session_id = ? "
            "AND alloc_type = 'CHANNEL' AND alloc_key = ? AND released_at IS NULL",
            (ctx.tenant_id, session_id, channel),
        )
        return dict(row) if row is not None else None

    def release_due_allocations(self, ctx: RequestContext, *, venue_id: str | None = None) -> int:
        """Return unsold allocated capacity to the shared pool at its release time (R9.4)."""
        now = self.clock.now()
        sql = [
            """
            SELECT a.*, s.date, s.start_time, s.venue_id, v.timezone
            FROM session_allocations a
            JOIN sessions s ON s.id = a.session_id AND s.tenant_id = a.tenant_id
            JOIN venues v ON v.id = s.venue_id AND v.tenant_id = s.tenant_id
            WHERE a.tenant_id = ? AND a.released_at IS NULL AND a.release_minutes_before IS NOT NULL
            """
        ]
        params: list[Any] = [ctx.tenant_id]
        if venue_id:
            sql.append("AND s.venue_id = ?")
            params.append(venue_id)
        released = 0
        for row in self.db.query(" ".join(sql), params):
            starts_at = combine_local(row["date"], row["start_time"], row["timezone"])
            release_at = add_minutes(starts_at, -int(row["release_minutes_before"]))
            if now < release_at:
                continue
            self.db.update(
                "session_allocations",
                row["id"],
                {"released_at": to_iso(now)},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(row["venue_id"]).system(),
                "CONFIG_CHANGE",
                target_type="session_allocation",
                target_id=row["id"],
                new={
                    "released": True,
                    "unsold": int(row["quantity"] or 0) - int(row["confirmed"] or 0),
                },
            )
            released += 1
        return released

    def allocation_utilization(self, ctx: RequestContext, session_id: str) -> list[dict[str, Any]]:
        """Utilization per channel and partner (R9.5)."""
        rows = self.db.query(
            "SELECT * FROM session_allocations WHERE tenant_id = ? AND session_id = ? ORDER BY alloc_type, alloc_key",
            (ctx.tenant_id, session_id),
        )
        out = []
        for row in rows:
            allocated = int(row["quantity"] or 0)
            confirmed = int(row["confirmed"] or 0)
            out.append(
                {
                    "allocation_id": row["id"],
                    "alloc_type": row["alloc_type"],
                    "alloc_key": row["alloc_key"],
                    "allocated": allocated,
                    "confirmed": confirmed,
                    "remaining": max(allocated - confirmed, 0),
                    "utilization_bp": int(confirmed * 10_000 / allocated) if allocated else 0,
                    "released_at": row["released_at"],
                    "overflow_allowed": bool(row["overflow_allowed"]),
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Holds (R10)
    # ------------------------------------------------------------------ #

    def hold_duration_minutes(
        self, ctx: RequestContext, *, venue_id: str, product_id: str | None, session_id: str | None
    ) -> int:
        """Configurable per venue, product, session and channel (R10.2)."""
        by_channel = self.config.get(
            ctx,
            f"hold.duration_minutes.{ctx.channel}",
            venue_id=venue_id,
            product_id=product_id,
            session_id=session_id,
            use_platform_default=False,
        )
        if by_channel is not None:
            return int(by_channel)
        return self.config.get_int(
            ctx, "hold.duration_minutes", venue_id=venue_id, product_id=product_id, session_id=session_id
        )

    def acquire_hold(
        self,
        ctx: RequestContext,
        *,
        session_id: str | None,
        quantity: int,
        cart_id: str,
        zone_id: str | None = None,
        partner_id: str | None = None,
        duration_minutes: int | None = None,
        venue_id: str | None = None,
        product_id: str | None = None,
    ) -> Hold | None:
        """Reserve capacity for checkout (R10.1).

        Returns ``None`` when the inventory is not capacity-controlled: there is
        nothing to protect, so no hold is created.
        """
        if quantity <= 0:
            raise ValidationError({"quantity": "Choose at least one ticket."})
        if session_id is None:
            return None
        session = self.get_session(ctx, session_id)
        if session["status"] in _CLOSED_STATUSES and session["status"] != "FULL":
            raise NotAvailable(
                "That time is no longer available.",
                details={"session_id": session_id, "status": session["status"]},
            )
        if session["capacity"] is None:
            return None
        minutes = duration_minutes or self.hold_duration_minutes(
            ctx,
            venue_id=venue_id or session["venue_id"],
            product_id=product_id or session.get("product_id"),
            session_id=session_id,
        )
        now = self.clock.now()
        expires_at = to_iso(add_minutes(now, minutes))
        allocation = self.find_allocation(ctx, session_id, channel=ctx.channel, partner_id=partner_id)

        with self.db.transaction(immediate=True):
            # Expire stale holds first so their capacity is visible to this request.
            self._expire_holds(ctx, session_id=session_id)
            availability = self.availability(ctx, session_id)
            remaining = availability.remaining if availability.remaining is not None else quantity
            if remaining < quantity:
                raise JustSoldOut(
                    details={
                        "session_id": session_id,
                        "requested": quantity,
                        "remaining": remaining,
                        "date": session["date"],
                        "start_time": session["start_time"],
                    }
                )
            if allocation is not None:
                self._assert_allocation_room(ctx, allocation, session_id, quantity)
            hold_id = new_id("hld")
            self.db.insert(
                "holds",
                {
                    "id": hold_id,
                    "tenant_id": ctx.tenant_id,
                    "session_id": session_id,
                    "zone_id": zone_id,
                    "cart_id": cart_id,
                    "channel": ctx.channel,
                    "alloc_type": allocation["alloc_type"] if allocation else None,
                    "alloc_key": allocation["alloc_key"] if allocation else None,
                    "quantity": int(quantity),
                    "state": "ACTIVE",
                    "expires_at": expires_at,
                    "created_at": to_iso(now),
                    "correlation_id": ctx.correlation_id,
                },
            )
            self._refresh_status(ctx, session_id)
        return Hold(
            id=hold_id,
            session_id=session_id,
            cart_id=cart_id,
            channel=ctx.channel,
            quantity=int(quantity),
            state="ACTIVE",
            expires_at=expires_at,
            zone_id=zone_id,
        )

    def _assert_allocation_room(
        self, ctx: RequestContext, allocation: dict[str, Any], session_id: str, quantity: int
    ) -> None:
        """R9.2 — an exhausted allocation stops selling even if shared capacity remains."""
        allocated = int(allocation["quantity"] or 0)
        confirmed = int(allocation["confirmed"] or 0)
        held = int(
            self.db.scalar(
                "SELECT COALESCE(SUM(quantity),0) FROM holds WHERE tenant_id = ? AND session_id = ? "
                "AND state = 'ACTIVE' AND expires_at > ? AND alloc_type = ? AND alloc_key = ?",
                (
                    ctx.tenant_id,
                    session_id,
                    to_iso(self.clock.now()),
                    allocation["alloc_type"],
                    allocation["alloc_key"],
                ),
                default=0,
            )
        )
        if confirmed + held + quantity > allocated and not bool(allocation["overflow_allowed"]):
            raise JustSoldOut(
                "The allocation for this channel is fully booked.",
                details={
                    "alloc_type": allocation["alloc_type"],
                    "alloc_key": allocation["alloc_key"],
                    "allocated": allocated,
                    "used": confirmed + held,
                    "reason": "allocation_exhausted",
                },
            )

    def hold_status(self, ctx: RequestContext, hold_id: str) -> dict[str, Any]:
        """Remaining hold time plus the warn-before-expiry flag (R10.7)."""
        record = self.authz.load_scoped(ctx, "holds", hold_id, entity="hold")
        now = self.clock.now()
        remaining_seconds = max(int(minutes_between(now, parse_instant(record["expires_at"])) * 60), 0)
        threshold = self.config.get_int(ctx, "hold.warning_threshold_minutes")
        expired = record["state"] == "ACTIVE" and remaining_seconds <= 0
        return {
            "hold_id": hold_id,
            "state": "EXPIRED" if expired else record["state"],
            "quantity": int(record["quantity"]),
            "session_id": record["session_id"],
            "expires_at": record["expires_at"],
            "remaining_seconds": remaining_seconds,
            "warning": 0 < remaining_seconds <= threshold * 60,
            "warning_threshold_seconds": threshold * 60,
        }

    def release_hold(self, ctx: RequestContext, hold_id: str, *, reason: str = "abandoned") -> dict[str, Any]:
        """HELD → RELEASED when a customer abandons or cancels checkout (R10.3)."""
        record = self.authz.load_scoped(ctx, "holds", hold_id, entity="hold")
        if record["state"] != "ACTIVE":
            return {"hold_id": hold_id, "state": record["state"], "released": False}
        with self.db.transaction(immediate=True):
            self.db.update(
                "holds",
                hold_id,
                {"state": "RELEASED", "released_at": to_iso(self.clock.now())},
                tenant_id=ctx.tenant_id,
            )
            if record["session_id"]:
                self._refresh_status(ctx, record["session_id"])
        return {"hold_id": hold_id, "state": "RELEASED", "released": True, "reason": reason}

    def release_cart_holds(self, ctx: RequestContext, cart_id: str, *, reason: str = "abandoned") -> int:
        rows = self.db.query(
            "SELECT id FROM holds WHERE tenant_id = ? AND cart_id = ? AND state = 'ACTIVE'",
            (ctx.tenant_id, cart_id),
        )
        for row in rows:
            self.release_hold(ctx, row["id"], reason=reason)
        return len(rows)

    def confirm_hold(
        self,
        ctx: RequestContext,
        hold_id: str,
        *,
        allow_late: bool = False,
        partner_id: str | None = None,
    ) -> dict[str, Any]:
        """HELD → CONFIRMED, converting a hold into consumed capacity.

        Refuses a hold belonging to another channel (R10.10). When the hold has
        lapsed, ``allow_late`` permits re-acquiring equivalent capacity and records
        that late confirmation occurred (R10.9); if the capacity has gone, the
        caller gets :class:`HoldExpired` and must follow the R10.8 remedy path.
        """
        record = self.authz.load_scoped(ctx, "holds", hold_id, entity="hold")
        if record["channel"] != ctx.channel:
            raise ConflictError(
                "This reservation belongs to a different sales channel.",
                details={"hold_id": hold_id},
            )
        if record["state"] == "CONFIRMED":
            return {"hold_id": hold_id, "state": "CONFIRMED", "already_confirmed": True, "late": False}
        if record["state"] in ("RELEASED", "EXPIRED"):
            if not allow_late:
                raise HoldExpired(details={"hold_id": hold_id, "state": record["state"]})
        session_id = record["session_id"]
        quantity = int(record["quantity"])
        now = self.clock.now()
        lapsed = record["state"] != "ACTIVE" or to_iso(now) > record["expires_at"]
        if lapsed and not allow_late:
            with self.db.transaction(immediate=True):
                self.db.update(
                    "holds", hold_id, {"state": "EXPIRED", "released_at": to_iso(now)}, tenant_id=ctx.tenant_id
                )
                if session_id:
                    self._refresh_status(ctx, session_id)
            raise HoldExpired(details={"hold_id": hold_id, "session_id": session_id})

        with self.db.transaction(immediate=True):
            if session_id:
                if lapsed:
                    # R10.9 — the hold is gone, so its quantity is no longer counted
                    # as held. Re-check against live availability before confirming.
                    self._expire_holds(ctx, session_id=session_id)
                    availability = self.availability(ctx, session_id)
                    if availability.remaining is not None and availability.remaining < quantity:
                        raise HoldExpired(
                            details={
                                "hold_id": hold_id,
                                "session_id": session_id,
                                "remaining": availability.remaining,
                                "requested": quantity,
                                "reason": "capacity_gone_after_expiry",
                            }
                        )
                granted = self.db.compare_and_increment(
                    "sessions",
                    session_id,
                    counter="confirmed",
                    delta=quantity,
                    limit_column="capacity",
                    tenant_id=ctx.tenant_id,
                )
                if not granted:
                    raise JustSoldOut(details={"session_id": session_id, "requested": quantity})
                allocation = self.find_allocation(
                    ctx, session_id, channel=record["channel"], partner_id=partner_id
                )
                if allocation is not None:
                    self.db.compare_and_increment(
                        "session_allocations",
                        allocation["id"],
                        counter="confirmed",
                        delta=quantity,
                        tenant_id=ctx.tenant_id,
                    )
            self.db.update(
                "holds",
                hold_id,
                {"state": "CONFIRMED", "confirmed_at": to_iso(now)},
                tenant_id=ctx.tenant_id,
            )
            if session_id:
                self._refresh_status(ctx, session_id)
        return {"hold_id": hold_id, "state": "CONFIRMED", "late": bool(lapsed), "quantity": quantity}

    def confirm_without_hold(
        self,
        ctx: RequestContext,
        *,
        session_id: str,
        quantity: int,
        partner_id: str | None = None,
    ) -> None:
        """Consume capacity directly, for counter sales that never held anything.

        Uses the identical conditional increment, so a walk-up sale contends with
        an online checkout on exactly the same terms (R10.5).
        """
        with self.db.transaction(immediate=True):
            self._expire_holds(ctx, session_id=session_id)
            granted = self.db.compare_and_increment(
                "sessions",
                session_id,
                counter="confirmed",
                delta=int(quantity),
                limit_column="capacity",
                tenant_id=ctx.tenant_id,
            )
            if not granted:
                raise JustSoldOut(details={"session_id": session_id, "requested": int(quantity)})
            allocation = self.find_allocation(ctx, session_id, channel=ctx.channel, partner_id=partner_id)
            if allocation is not None:
                self._assert_allocation_room(ctx, allocation, session_id, int(quantity))
                self.db.compare_and_increment(
                    "session_allocations",
                    allocation["id"],
                    counter="confirmed",
                    delta=int(quantity),
                    tenant_id=ctx.tenant_id,
                )
            self._refresh_status(ctx, session_id)

    def release_confirmed(
        self,
        ctx: RequestContext,
        *,
        session_id: str,
        quantity: int,
        partner_id: str | None = None,
    ) -> None:
        """Give capacity back on cancellation/refund of future-dated inventory (R17.5)."""
        with self.db.transaction(immediate=True):
            self.db.compare_and_increment(
                "sessions",
                session_id,
                counter="confirmed",
                delta=-int(quantity),
                tenant_id=ctx.tenant_id,
            )
            allocation = self.find_allocation(ctx, session_id, channel=ctx.channel, partner_id=partner_id)
            if allocation is not None:
                self.db.compare_and_increment(
                    "session_allocations",
                    allocation["id"],
                    counter="confirmed",
                    delta=-int(quantity),
                    tenant_id=ctx.tenant_id,
                )
            self._refresh_status(ctx, session_id)

    # ------------------------------------------------------------------ #
    # Background maintenance
    # ------------------------------------------------------------------ #

    def reclaim_expired_holds(self, ctx: RequestContext, *, limit: int = 500) -> int:
        """Return lapsed holds to available inventory (R10.4).

        Called on a schedule bounded by ``hold.reclaim_interval_seconds``, and also
        opportunistically inside :meth:`acquire_hold`, so a customer never sees
        stale unavailability.
        """
        now = to_iso(self.clock.now())
        rows = self.db.query(
            "SELECT id, session_id FROM holds WHERE tenant_id = ? AND state = 'ACTIVE' AND expires_at <= ? "
            "LIMIT ?",
            (ctx.tenant_id, now, int(limit)),
        )
        if not rows:
            return 0
        touched: set[str] = set()
        with self.db.transaction(immediate=True):
            for row in rows:
                self.db.update(
                    "holds", row["id"], {"state": "EXPIRED", "released_at": now}, tenant_id=ctx.tenant_id
                )
                if row["session_id"]:
                    touched.add(row["session_id"])
            for session_id in touched:
                self._refresh_status(ctx, session_id)
        return len(rows)

    def _expire_holds(self, ctx: RequestContext, *, session_id: str) -> int:
        """Expire lapsed holds for one session. Assumes the caller holds the write lock."""
        now = to_iso(self.clock.now())
        cursor = self.db.execute(
            "UPDATE holds SET state = 'EXPIRED', released_at = ? "
            "WHERE tenant_id = ? AND session_id = ? AND state = 'ACTIVE' AND expires_at <= ?",
            (now, ctx.tenant_id, session_id, now),
        )
        return cursor.rowcount

    def complete_due_sessions(self, ctx: RequestContext, *, venue_id: str | None = None) -> int:
        """Transition sessions to COMPLETED once their end time has passed (R24.4)."""
        sql = [
            """
            SELECT s.id, s.date, s.start_time, s.end_time, s.venue_id, v.timezone
            FROM sessions s JOIN venues v ON v.id = s.venue_id AND v.tenant_id = s.tenant_id
            WHERE s.tenant_id = ? AND s.status NOT IN ('COMPLETED','CANCELLED')
            """
        ]
        params: list[Any] = [ctx.tenant_id]
        if venue_id:
            sql.append("AND s.venue_id = ?")
            params.append(venue_id)
        now = self.clock.now()
        completed = 0
        for row in self.db.query(" ".join(sql), params):
            ends_at = combine_local(row["date"], row["end_time"], row["timezone"])
            # A session whose end time is earlier than its start time runs past
            # midnight, so its end instant belongs to the following calendar day.
            if row["end_time"] < row["start_time"]:
                ends_at = add_minutes(ends_at, 24 * 60)
            if now <= ends_at:
                continue
            self.db.update("sessions", row["id"], {"status": "COMPLETED"}, tenant_id=ctx.tenant_id)
            completed += 1
        return completed

    def cutoff_passed(self, ctx: RequestContext, session: dict[str, Any], *, timezone: str) -> bool:
        """Has this session's booking cutoff passed? (R8.6, R25.10)"""
        cutoff = session.get("booking_cutoff_minutes")
        if cutoff is None:
            cutoff = self.config.get_int(
                ctx, "session.booking_cutoff_minutes", venue_id=session["venue_id"], session_id=session["id"]
            )
        starts_at = combine_local(session["date"], session["start_time"], timezone)
        return self.clock.now() > add_minutes(starts_at, -int(cutoff or 0))

    def assert_session_bookable(
        self, ctx: RequestContext, session: dict[str, Any], *, timezone: str, quantity: int = 1
    ) -> None:
        """All the reasons a specific session cannot take a new booking (R8.5, R8.6)."""
        if session["status"] == "CANCELLED":
            raise NotAvailable(
                "That session has been cancelled.", details={"session_id": session["id"], "status": "CANCELLED"}
            )
        if session["status"] in ("COMPLETED", "HIDDEN"):
            raise NotAvailable(
                "That session is no longer open for booking.",
                details={"session_id": session["id"], "status": session["status"]},
            )
        if session["capacity"] is not None and (session.get("remaining") or 0) < quantity:
            raise JustSoldOut(
                details={
                    "session_id": session["id"],
                    "remaining": session.get("remaining") or 0,
                    "requested": quantity,
                }
            )
        if self.cutoff_passed(ctx, session, timezone=timezone):
            raise RuleViolation(
                "Booking for that session has closed.",
                details={
                    "session_id": session["id"],
                    "reason_code": "cutoff_passed",
                    "start_time": session["start_time"],
                    "date": session["date"],
                },
            )

    # ------------------------------------------------------------------ #
    # Waiting list (R25.7)
    # ------------------------------------------------------------------ #

    def join_waiting_list(
        self,
        ctx: RequestContext,
        *,
        session_id: str,
        contact_hash: str,
        quantity: int = 1,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(ctx, session_id)
        if not bool(session["waiting_list_enabled"]):
            raise RuleViolation(
                "There is no waiting list for this session.", details={"session_id": session_id}
            )
        position = (
            int(
                self.db.scalar(
                    "SELECT COALESCE(MAX(position), 0) FROM waiting_list WHERE tenant_id = ? AND session_id = ?",
                    (ctx.tenant_id, session_id),
                    default=0,
                )
            )
            + 1
        )
        entry_id = new_id("wlt")
        self.db.insert(
            "waiting_list",
            {
                "id": entry_id,
                "tenant_id": ctx.tenant_id,
                "session_id": session_id,
                "customer_id": customer_id,
                "contact_hash": contact_hash,
                "quantity": int(quantity),
                "position": position,
                "state": "WAITING",
                "created_at": to_iso(self.clock.now()),
            },
        )
        return {"waiting_list_id": entry_id, "position": position, "state": "WAITING"}

    def offer_waiting_list(
        self, ctx: RequestContext, session_id: str, *, offer_minutes: int = 30
    ) -> dict[str, Any] | None:
        """Offer released capacity to the next person in recorded order (R25.7).

        Offers are exclusive and time-boxed: one entry at a time, so two people are
        never invited to claim the same place.
        """
        availability = self.availability(ctx, session_id)
        if availability.remaining is not None and availability.remaining <= 0:
            return None
        now = self.clock.now()
        with self.db.transaction(immediate=True):
            # Expire any lapsed offer first, then take the next waiting entry.
            self.db.execute(
                "UPDATE waiting_list SET state = 'EXPIRED', resolved_at = ? "
                "WHERE tenant_id = ? AND session_id = ? AND state = 'OFFERED' AND offer_expires_at <= ?",
                (to_iso(now), ctx.tenant_id, session_id, to_iso(now)),
            )
            outstanding = self.db.query_one(
                "SELECT id FROM waiting_list WHERE tenant_id = ? AND session_id = ? AND state = 'OFFERED'",
                (ctx.tenant_id, session_id),
            )
            if outstanding is not None:
                return None
            candidate = self.db.query_one(
                "SELECT * FROM waiting_list WHERE tenant_id = ? AND session_id = ? AND state = 'WAITING' "
                "ORDER BY position LIMIT 1",
                (ctx.tenant_id, session_id),
            )
            if candidate is None:
                return None
            if availability.remaining is not None and int(candidate["quantity"]) > availability.remaining:
                # Not enough room for this party; leave them queued rather than
                # offering a place they cannot use.
                return None
            expires_at = to_iso(add_minutes(now, offer_minutes))
            self.db.update(
                "waiting_list",
                candidate["id"],
                {"state": "OFFERED", "offered_at": to_iso(now), "offer_expires_at": expires_at},
                tenant_id=ctx.tenant_id,
            )
        return {
            "waiting_list_id": candidate["id"],
            "session_id": session_id,
            "contact_hash": candidate["contact_hash"],
            "customer_id": candidate["customer_id"],
            "quantity": int(candidate["quantity"]),
            "offer_expires_at": expires_at,
        }

    def resolve_waiting_list_entry(
        self, ctx: RequestContext, entry_id: str, *, state: str
    ) -> dict[str, Any]:
        if state not in ("CLAIMED", "DECLINED", "EXPIRED", "CANCELLED"):
            raise ValidationError({"state": "State must be CLAIMED, DECLINED, EXPIRED or CANCELLED."})
        entry = self.authz.load_scoped(ctx, "waiting_list", entry_id, entity="waiting_list")
        self.db.update(
            "waiting_list",
            entry_id,
            {"state": state, "resolved_at": to_iso(self.clock.now())},
            tenant_id=ctx.tenant_id,
        )
        return {"waiting_list_id": entry_id, "state": state, "session_id": entry["session_id"]}


__all__ = ["Availability", "DAY_SESSION_SOURCE", "Hold", "InventoryService"]
