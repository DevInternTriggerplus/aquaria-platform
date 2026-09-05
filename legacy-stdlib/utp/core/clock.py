"""Time handling.

Every operating date, cutoff, session time and reporting day boundary is
evaluated in the venue's local timezone (R1.9, R6.8, R26.12). Two consequences
shape this module:

* Services never call ``datetime.now()`` directly. They receive a ``Clock`` so
  that tests can advance time deterministically and so that hold expiry,
  reminder scheduling and session completion are reproducible.
* Timestamps are stored twice where the requirement asks for it: UTC for
  ordering and correlation, venue-local for operational reading (R12.10, R45.1).
"""

from __future__ import annotations

import datetime as _dt
from typing import Protocol

try:  # pragma: no cover - exercised implicitly by import
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python without zoneinfo
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


UTC = _dt.timezone.utc

# Fallback offsets used only when the OS/interpreter has no tz database. Keeps
# the platform runnable in minimal containers; production installs tzdata.
_FALLBACK_OFFSETS: dict[str, int] = {
    "UTC": 0,
    "Asia/Bangkok": 7 * 3600,
    "Asia/Kuala_Lumpur": 8 * 3600,
    "Asia/Jakarta": 7 * 3600,
    "Asia/Singapore": 8 * 3600,
    "Asia/Tokyo": 9 * 3600,
    "Europe/London": 0,
    "Australia/Sydney": 10 * 3600,
}


def timezone_for(name: str) -> _dt.tzinfo:
    """Resolve a timezone name to a ``tzinfo``.

    Raises ``ValueError`` for an unknown zone so that venue configuration cannot
    silently fall back to UTC and shift a whole venue's operating day.
    """
    name = name or "UTC"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            pass
    if name in _FALLBACK_OFFSETS:
        return _dt.timezone(_dt.timedelta(seconds=_FALLBACK_OFFSETS[name]), name)
    raise ValueError(f"unknown timezone: {name}")


class Clock(Protocol):
    """Minimal time source."""

    def now(self) -> _dt.datetime:
        """Current instant, timezone-aware, in UTC."""


class SystemClock:
    """Real time."""

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(tz=UTC)


class FixedClock:
    """Controllable clock for tests and for replaying scenarios.

    ``advance`` moves time forward, which is how hold expiry, reminder windows,
    re-entry windows and session completion are exercised without sleeping.
    """

    def __init__(self, start: _dt.datetime | str) -> None:
        self._now = parse_instant(start) if isinstance(start, str) else _as_utc(start)

    def now(self) -> _dt.datetime:
        return self._now

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0, days: int = 0) -> _dt.datetime:
        self._now = self._now + _dt.timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return self._now

    def set(self, moment: _dt.datetime | str) -> _dt.datetime:
        self._now = parse_instant(moment) if isinstance(moment, str) else _as_utc(moment)
        return self._now


def _as_utc(moment: _dt.datetime) -> _dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_instant(text: str) -> _dt.datetime:
    """Parse an ISO-8601 instant, defaulting a naive value to UTC."""
    value = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return _as_utc(value)


def to_iso(moment: _dt.datetime) -> str:
    """Canonical UTC ISO-8601 string used for storage and ordering."""
    return _as_utc(moment).isoformat(timespec="seconds").replace("+00:00", "Z")


def local(moment: _dt.datetime, tz_name: str) -> _dt.datetime:
    """Convert an instant into a venue-local aware datetime."""
    return _as_utc(moment).astimezone(timezone_for(tz_name))


def local_iso(moment: _dt.datetime, tz_name: str) -> str:
    """Venue-local ISO-8601 string, offset included so it is unambiguous."""
    return local(moment, tz_name).isoformat(timespec="seconds")


def operating_date(moment: _dt.datetime, tz_name: str, day_boundary_hour: int = 0) -> _dt.date:
    """Venue operating date for an instant.

    ``day_boundary_hour`` supports venues whose business day does not start at
    midnight (a late-night show venue may close its day at 04:00). Reporting,
    cutoffs and calendar availability all use this function so that a single
    definition of "today" applies platform-wide (R6.8).
    """
    moment_local = local(moment, tz_name)
    if day_boundary_hour:
        moment_local = moment_local - _dt.timedelta(hours=day_boundary_hour)
    return moment_local.date()


def parse_date(value: str | _dt.date) -> _dt.date:
    """Parse an ``YYYY-MM-DD`` operating date."""
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(value)


def parse_time(value: str) -> _dt.time:
    """Parse ``HH:MM`` or ``HH:MM:SS`` into a ``time``."""
    parts = value.split(":")
    if len(parts) == 2:
        return _dt.time(int(parts[0]), int(parts[1]))
    if len(parts) == 3:
        return _dt.time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(f"invalid time: {value!r}")


def combine_local(day: str | _dt.date, time_text: str, tz_name: str) -> _dt.datetime:
    """Build the UTC instant for a venue-local date and ``HH:MM`` time.

    Session start/end times and cutoffs are configured as local wall-clock times
    because that is how an operator thinks about them. This function is the
    single place where a wall-clock time becomes an absolute instant.
    """
    tz = timezone_for(tz_name)
    naive = _dt.datetime.combine(parse_date(day), parse_time(time_text))
    return naive.replace(tzinfo=tz).astimezone(UTC)


def minutes_between(earlier: _dt.datetime, later: _dt.datetime) -> float:
    """Signed minutes from ``earlier`` to ``later``."""
    return (_as_utc(later) - _as_utc(earlier)).total_seconds() / 60.0


def add_minutes(moment: _dt.datetime, minutes: float) -> _dt.datetime:
    return _as_utc(moment) + _dt.timedelta(minutes=minutes)


def end_time_from_duration(start_time: str, duration_minutes: int) -> str:
    """Derive ``HH:MM`` end time from a start time plus duration (R20.7).

    Wraps past midnight for late sessions, returning the wall-clock time only;
    the owning session's date plus timezone resolve the absolute instant.
    """
    start = parse_time(start_time)
    total = start.hour * 60 + start.minute + int(duration_minutes)
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def weekday_code(day: str | _dt.date) -> str:
    """Weekday code used by booking rules and recurrence selectors."""
    return WEEKDAY_CODES[parse_date(day).weekday()]


__all__ = [
    "Clock",
    "FixedClock",
    "SystemClock",
    "UTC",
    "WEEKDAY_CODES",
    "add_minutes",
    "combine_local",
    "end_time_from_duration",
    "local",
    "local_iso",
    "minutes_between",
    "operating_date",
    "parse_date",
    "parse_instant",
    "parse_time",
    "timezone_for",
    "to_iso",
    "weekday_code",
]
