"""Resolve a venue's charge configuration and run the authoritative calculation.

There is exactly one place in the platform that computes an order total, and this
is the door to it: :func:`compute_order_charges`. Cart, checkout, payment,
receipt, tax invoice and reports all come through here, which is why the numbers
reconcile (R5.5).

The arithmetic itself lives in :mod:`apps.core.money`, ported unchanged from the
verified implementation. This module only decides *which configuration applies*.
"""

from __future__ import annotations

import datetime as dt

from apps.core.money import ChargeBreakdown, ChargeInput, compute_charges

from .models import ExchangeRate, ServiceChargeSetting, VatSetting


def resolve_vat(venue, on_date: dt.date) -> ChargeInput:
    """The VAT configuration in force for ``venue`` on ``on_date``.

    Falls back to the venue's own ``tax_model``/``tax_rate_bp`` when no explicit
    effective-dated setting exists, so a venue configured only at the venue level
    still charges correctly.
    """
    row = VatSetting.objects.effective_on(venue.id, on_date)
    if row is not None:
        return ChargeInput(
            enabled=row.enabled,
            rate_bp=row.rate_bp,
            mode=row.mode,
            display_name=row.display_name,
        )
    return ChargeInput(
        enabled=bool(venue.tax_rate_bp),
        rate_bp=venue.tax_rate_bp or 0,
        mode=venue.tax_model or "INCLUSIVE",
        display_name="VAT",
    )


def resolve_service_charge(venue, on_date: dt.date) -> ChargeInput:
    """The service-charge configuration in force on ``on_date``.

    Absent configuration means no service charge — not a zero-rate one, so the
    snapshot can tell "not applicable" from "0%".
    """
    row = ServiceChargeSetting.objects.effective_on(venue.id, on_date)
    if row is None:
        return ChargeInput(enabled=False, rate_bp=0, mode="EXCLUSIVE", display_name="Service charge")
    return ChargeInput(
        enabled=row.enabled,
        rate_bp=row.rate_bp,
        mode=row.mode,
        display_name=row.display_name,
    )


def compute_order_charges(
    *,
    venue,
    base_minor: int,
    on_date: dt.date,
    line_discount_minor: int = 0,
    order_discount_minor: int = 0,
) -> ChargeBreakdown:
    """The single authoritative total for an order.

    ``on_date`` is the date whose configuration applies — the visit date, so a
    scheduled VAT change that starts before the visit is honoured, and a booking
    made today for next year is priced under next year's rate.
    """
    return compute_charges(
        base_minor=base_minor,
        line_discount_minor=line_discount_minor,
        order_discount_minor=order_discount_minor,
        service_charge=resolve_service_charge(venue, on_date),
        vat=resolve_vat(venue, on_date),
        rounding_mode=venue.rounding_mode or "NONE",
        currency=venue.currency or "THB",
    )


def resolve_exchange_rate(venue, from_currency: str, to_currency: str, on_date: dt.date):
    """The active rate for a pair on a date, or ``None``.

    Returning ``None`` rather than falling back to 1.0 is deliberate: an unknown
    rate must stop the transaction, not silently price at par.
    """
    return (
        ExchangeRate.objects.filter(
            venue=venue,
            from_currency=(from_currency or "").upper(),
            to_currency=(to_currency or "").upper(),
            status="ACTIVE",
            effective_from__lte=on_date,
        )
        .filter(models_q_until(on_date))
        .order_by("-effective_from")
        .first()
    )


def models_q_until(on_date: dt.date):
    """``effective_until`` is null (open-ended) or not yet passed."""
    from django.db.models import Q

    return Q(effective_until__isnull=True) | Q(effective_until__gte=on_date)
