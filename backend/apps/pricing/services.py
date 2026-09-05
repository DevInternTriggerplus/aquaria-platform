"""Deterministic price resolution.

The rule with the highest priority wins; on a tie, the more specific scope wins;
and if that still ties, the lower id, so the quote and the confirmation can never
disagree about which rule applied. If nothing matches, the caller gets ``None`` and
must treat the ticket type as unavailable — there is no fallback price (R5.6).
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Q

from .models import PriceRule

_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _matches(rule: PriceRule, *, on_date: dt.date, weekday: str, channel: str,
             quantity: int, session_id: str | None) -> bool:
    if rule.date_from and on_date < rule.date_from:
        return False
    if rule.date_until and on_date > rule.date_until:
        return False
    if rule.weekdays and weekday not in rule.weekdays:
        return False
    if rule.channel and rule.channel != channel:
        return False
    if rule.session_id and rule.session_id != session_id:
        return False
    if rule.quantity_min is not None and quantity < rule.quantity_min:
        return False
    if rule.quantity_max is not None and quantity > rule.quantity_max:
        return False
    return True


def resolve_price(
    *,
    venue,
    ticket_type_id: str,
    on_date: dt.date,
    channel: str = "ONLINE",
    quantity: int = 1,
    session_id: str | None = None,
) -> PriceRule | None:
    """The winning price rule for one ticket type under these conditions, or None."""
    weekday = _WEEKDAYS[on_date.weekday()]
    candidates = PriceRule.objects.filter(
        venue=venue, ticket_type_id=ticket_type_id, status="ACTIVE"
    )
    best: PriceRule | None = None
    best_key: tuple[int, int, str] | None = None
    for rule in candidates:
        if not _matches(
            rule, on_date=on_date, weekday=weekday, channel=channel,
            quantity=quantity, session_id=session_id,
        ):
            continue
        # Higher priority and specificity win; a lower id is the final, stable
        # tie-breaker so resolution is fully deterministic.
        key = (rule.priority, rule.specificity, _neg(rule.id))
        if best_key is None or key > best_key:
            best, best_key = rule, key
    return best


def resolve_prices_for(
    *,
    venue,
    ticket_types,
    on_date: dt.date,
    channel: str = "ONLINE",
) -> dict[str, PriceRule]:
    """Winning rule per ticket type, for the product listing at quantity 1."""
    winners: dict[str, PriceRule] = {}
    for tt in ticket_types:
        rule = resolve_price(
            venue=venue, ticket_type_id=tt.id, on_date=on_date, channel=channel
        )
        if rule is not None:
            winners[tt.id] = rule
    return winners


def _neg(identifier: str) -> str:
    """Make "lower id wins" express as "larger key wins" for the max comparison.

    Ids are fixed-width sortable strings, so inverting each character's ordinal
    yields a key that is larger for the id we want to prefer.
    """
    return "".join(chr(0x10FFFF - ord(c)) for c in identifier)
