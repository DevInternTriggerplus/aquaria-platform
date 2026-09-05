"""Generate a few weeks of trading history, for demonstrating the dashboards.

**This is a development tool. It is never called by the application.** The only
entry points are ``serve.py --demo-history`` and the reporting tests, so nothing
in a production path can reach it (reports spec §18: keep mock data clearly
isolated from production data sources).

It deliberately goes through the **real services** rather than inserting rows.
Driving `booking.quote` → `start_checkout` → `confirm` → `access.scan` means the
history it produces obeys every rule the platform enforces: prices come from the
price rules, VAT is snapshotted at the rate of the day, capacity is decremented
authoritatively, tickets are signed, and refunds respect the collected ceiling.
A dashboard built on hand-written INSERTs would look plausible and prove nothing,
because the aggregates would never have been tested against numbers the booking
path actually produces.

The clock is wound back for each day so bookings are created with historical
timestamps, then wound to the visit date to scan the tickets. That is why the
generator takes a ``FixedClock``: with a real clock every order would carry
today's timestamp and every time series would be a single spike.

The shape is a plausible aquarium week, not a random scatter: weekends busier
than midweek, mornings quieter than early afternoon, international guests
outnumbering residents, most sales online, a minority at the counter, and a small
tail of cancellations, refunds and no-shows so the exception panel has something
truthful to report.
"""

from __future__ import annotations

import datetime as _dt
import random
from typing import Any

from utp.core.clock import FixedClock
from utp.services.booking import QuoteLineRequest

#: Relative volume by weekday, Monday first. A Saturday is roughly twice a Tuesday.
_WEEKDAY_WEIGHT = (0.75, 0.7, 0.8, 0.9, 1.15, 1.6, 1.5)

#: Channel mix. Online dominates; the counter is the walk-up tail.
#:
#: PARTNER is absent on purpose. A partner booking must carry a consent
#: attestation (R12.20) and reference a configured partner, and the Aquaria seed
#: has no partners, so generating them would mean either faking an attestation or
#: inventing a partner — both of which would make the partner report describe
#: something that does not exist.
_CHANNEL_MIX = (
    ("ONLINE", 0.64),
    ("COUNTER", 0.23),
    ("KIOSK", 0.13),
)

#: Segment mix within a party.
_SEGMENT_MIX = (("ADULT", 0.58), ("CHILD", 0.32), ("SENIOR", 0.10))

#: Share of bookings from international visitors (the research report's mix).
_INTERNATIONAL_SHARE = 0.68


def _pick(mix: tuple[tuple[str, float], ...], rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    for value, weight in mix:
        cumulative += weight
        if roll <= cumulative:
            return value
    return mix[-1][0]


def generate(
    platform: Any,
    info: dict[str, Any],
    *,
    days: int = 28,
    base_bookings_per_day: int = 14,
    seed_value: int = 20260831,
    verbose: bool = False,
) -> dict[str, Any]:
    """Create ``days`` of history ending yesterday.

    Requires the platform to have been built with a :class:`FixedClock`; a real
    clock cannot be wound back and every order would land on today.
    """
    clock = platform.clock
    if not isinstance(clock, FixedClock):
        raise RuntimeError(
            "demo history needs a FixedClock so orders can be dated in the past; "
            "build the Platform with clock=FixedClock(...)"
        )

    rng = random.Random(seed_value)
    tenant_id = info["tenant_id"]
    venue_id = info["venue_id"]
    ticket_types = info["ticket_types"]
    staff_ctx = platform.system_context(tenant_id).for_venue(venue_id)
    original_now = clock.now()
    today = original_now.date()

    stats: dict[str, Any] = {
        "bookings": 0, "tickets": 0, "scans": 0, "cancelled": 0,
        "refunded": 0, "no_show": 0, "rejected": 0, "reject_reasons": {},
    }

    try:
        # Includes today (offset 0) so the operational dashboard has live arrivals,
        # scans and takings to show rather than an empty screen.
        for offset in range(days, -1, -1):
            visit_date = today - _dt.timedelta(days=offset)
            weight = _WEEKDAY_WEIGHT[visit_date.weekday()]
            count = max(1, int(base_bookings_per_day * weight * rng.uniform(0.8, 1.2)))

            for _ in range(count):
                channel = _pick(_CHANNEL_MIX, rng)
                # Guests book somewhere between the same day and two months ahead,
                # weighted towards the last fortnight, so the advance-booking
                # report has a real distribution to show.
                booked_on = visit_date - _dt.timedelta(days=_lead_days(rng))
                if booked_on > today:
                    booked_on = visit_date
                order_time = _dt.datetime.combine(
                    booked_on,
                    _dt.time(hour=rng.randint(8, 21), minute=rng.randint(0, 59)),
                    tzinfo=_dt.timezone.utc,
                )
                clock.set(order_time)

                group = "INTL" if rng.random() < _INTERNATIONAL_SHARE else "LOCAL"
                lines = _party(group, ticket_types, rng)
                if not lines:
                    continue

                # A counter sale is staff-assisted: consent is recorded on the
                # guest's behalf and cash is a staff-only method, both of which the
                # platform refuses from an anonymous context (R12.19, R34).
                if channel in ("COUNTER", "STAFF"):
                    guest = staff_ctx.with_channel(channel)
                else:
                    guest = platform.guest_context(
                        tenant_id, venue_id=venue_id, channel=channel, language="en"
                    )
                try:
                    quote = platform.booking.quote(
                        guest, venue_id=venue_id, visit_date=visit_date.isoformat(), lines=lines
                    )
                    quote = platform.booking.start_checkout(guest, quote)
                    result = platform.booking.confirm(
                        guest,
                        quote,
                        customer={
                            "email": f"guest{stats['bookings']}@example.test",
                            "full_name": _name(rng),
                            "phone": "+6681%07d" % rng.randint(0, 9_999_999),
                        },
                        consent_items={"BOOKING_SERVICE": True, "MARKETING": rng.random() < 0.4},
                        payment_method=_payment_method(channel, rng),
                        idempotency_key=f"demo-{visit_date}-{stats['bookings']}",
                    )
                except Exception as exc:  # noqa: BLE001
                    # Counted and surfaced rather than swallowed: a generator that
                    # quietly drops a whole channel produces a dashboard with a
                    # missing column and no clue why.
                    stats["rejected"] = stats.get("rejected", 0) + 1
                    reasons = stats.setdefault("reject_reasons", {})
                    label = f"{channel}: {type(exc).__name__}: {str(exc)[:70]}"
                    reasons[label] = reasons.get(label, 0) + 1
                    continue

                if not result.get("confirmed"):
                    continue
                stats["bookings"] += 1
                tickets = result.get("tickets", [])
                stats["tickets"] += len(tickets)
                booking_id = result["booking_id"]

                # A small tail of cancellations, before the visit.
                if rng.random() < 0.05:
                    clock.set(order_time + _dt.timedelta(days=1))
                    try:
                        platform.booking.cancel(
                            staff_ctx,
                            booking_id,
                            reason="Demo history: guest cancelled",
                            confirmed=True,
                            actor_is_staff=True,
                        )
                        stats["cancelled"] += 1
                        continue
                    except Exception:  # noqa: BLE001
                        pass

                # Scan the party in on the day, unless they never showed.
                if rng.random() < 0.08:
                    stats["no_show"] += 1
                    continue
                arrival = _dt.datetime.combine(
                    visit_date,
                    _dt.time(hour=_arrival_hour(rng), minute=rng.randint(0, 59)),
                    tzinfo=_dt.timezone.utc,
                )
                # For today, a party whose slot has not come round yet has genuinely
                # not arrived. Leaving them unscanned is what gives the operational
                # dashboard its Arriving / Late / Checked-in split.
                if arrival > original_now:
                    continue
                clock.set(arrival)
                for ticket in tickets:
                    # A couple of the party occasionally never make it through.
                    if rng.random() < 0.04:
                        continue
                    try:
                        platform.access.scan(
                            staff_ctx, qr_payload=ticket.get("qr_payload") or "", device_id=None
                        )
                        stats["scans"] += 1
                    except Exception:  # noqa: BLE001
                        pass

            if verbose and offset % 7 == 0:
                print(f"  demo history: {visit_date} ({stats['bookings']} bookings so far)")
    finally:
        clock.set(original_now)

    return stats


def _lead_days(rng: random.Random) -> int:
    """Lead time, weighted so most bookings are within a fortnight."""
    roll = rng.random()
    if roll < 0.18:
        return 0
    if roll < 0.42:
        return rng.randint(1, 3)
    if roll < 0.68:
        return rng.randint(4, 7)
    if roll < 0.92:
        return rng.randint(8, 30)
    return rng.randint(31, 60)


def _arrival_hour(rng: random.Random) -> int:
    """Arrivals cluster early afternoon; the venue opens 10:30 and last entry is 18:00."""
    return rng.choices(
        [10, 11, 12, 13, 14, 15, 16, 17],
        weights=[6, 10, 13, 16, 17, 15, 12, 8],
    )[0]


def _party(group: str, ticket_types: dict[str, str], rng: random.Random) -> list[QuoteLineRequest]:
    """A plausible party: mostly adults, often with children."""
    lines: list[QuoteLineRequest] = []
    adults = rng.choices([1, 2, 3, 4], weights=[18, 52, 18, 12])[0]
    children = rng.choices([0, 1, 2, 3], weights=[46, 26, 21, 7])[0]
    seniors = rng.choices([0, 1, 2], weights=[84, 11, 5])[0]
    for segment, quantity in (("ADULT", adults), ("CHILD", children), ("SENIOR", seniors)):
        if quantity <= 0:
            continue
        key = f"GA-{group}-{segment}"
        ticket_type_id = ticket_types.get(key)
        if ticket_type_id:
            lines.append(QuoteLineRequest(ticket_type_id=ticket_type_id, quantity=quantity))
    return lines


def _payment_method(channel: str, rng: random.Random) -> str:
    if channel == "COUNTER":
        return rng.choices(["CASH", "CARD", "QR_BANK_TRANSFER"], weights=[42, 34, 24])[0]
    if channel == "PARTNER":
        return "CARD"
    return rng.choices(["CARD", "QR_BANK_TRANSFER", "EWALLET"], weights=[46, 38, 16])[0]


_FIRST = ("Somchai", "Anong", "Niran", "Kanya", "Wei", "Yuki", "Ivan", "Olga",
          "James", "Emma", "Liam", "Sofia", "Arun", "Priya", "Chen", "Hana")
_LAST = ("Jaidee", "Suksawat", "Wong", "Tanaka", "Petrov", "Ivanova", "Smith",
         "Brown", "Silva", "Kumar", "Li", "Sato", "Nguyen", "Garcia")


def _name(rng: random.Random) -> str:
    return f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"


__all__ = ["generate"]
