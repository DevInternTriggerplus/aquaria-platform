"""HTTP surface shared by the Next.js and Flutter clients.

Only read-side catalogue endpoints and the health probe are wired here so far. The
write side (quote, confirm, gate scan) is deliberately still to be ported: those
paths carry the money, capacity and consent guarantees, and porting them without
their tests would replace verified behaviour with unverified behaviour. See the
repository README for the migration order.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.catalog.models import Product, TicketType
from apps.inventory.models import Session
from apps.payments.models import PaymentType
from apps.pricing.models import PriceRule
from apps.tenancy.models import Venue
from apps.venuesettings.services import compute_order_charges

from .serializers import (
    PaymentTypeSerializer,
    ProductSerializer,
    SessionSerializer,
    VenueSerializer,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe. Deliberately reveals nothing about configuration."""
    return Response({"status": "ok"})


def _resolve_venue(venue_code: str) -> Venue:
    return get_object_or_404(
        Venue.objects.prefetch_related("areas"), code=venue_code, status="ACTIVE"
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def venue_detail(request, venue_code: str):
    venue = _resolve_venue(venue_code)
    return Response(VenueSerializer(venue).data)


def _resolve_prices(
    venue: Venue, ticket_types, on_date: dt.date, channel: str
) -> dict[str, PriceRule]:
    """Pick the winning price rule per ticket type.

    Highest priority wins; ties break on the more specific scope (R5.2). A ticket
    type with no matching rule is simply absent from the result, which the
    serializer reports as unavailable rather than guessing a price (R5.6).
    """
    weekday = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][on_date.weekday()]
    candidates = PriceRule.objects.filter(
        venue=venue, ticket_type__in=ticket_types, status="ACTIVE"
    )
    winners: dict[str, PriceRule] = {}
    for rule in candidates:
        if rule.date_from and on_date < rule.date_from:
            continue
        if rule.date_until and on_date > rule.date_until:
            continue
        if rule.weekdays and weekday not in rule.weekdays:
            continue
        if rule.channel and rule.channel != channel:
            continue
        current = winners.get(rule.ticket_type_id)
        if current is None:
            winners[rule.ticket_type_id] = rule
            continue
        if (rule.priority, rule.specificity) > (current.priority, current.specificity):
            winners[rule.ticket_type_id] = rule
    return winners


@api_view(["GET"])
@permission_classes([AllowAny])
def product_list(request, venue_code: str):
    """Sellable products for a date and channel, with resolved prices."""
    venue = _resolve_venue(venue_code)
    channel = request.GET.get("channel", "ONLINE")
    raw_date = request.GET.get("date")
    try:
        on_date = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    except ValueError:
        return Response(
            {
                "error": {
                    "code": "validation_failed",
                    "message": "Enter the date as YYYY-MM-DD.",
                    "message_key": "error.validation_failed",
                }
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    products = (
        Product.objects.filter(venue=venue, status="ACTIVE", customer_visible=True)
        .prefetch_related(
            Prefetch(
                "ticket_types",
                queryset=TicketType.objects.select_related("segment").order_by("display_order"),
            )
        )
        .order_by("display_order")
    )
    products = [p for p in products if p.allows_channel(channel)]

    all_types = [t for p in products for t in p.ticket_types.all()]
    prices = _resolve_prices(venue, all_types, on_date, channel)

    context = {"prices": prices, "currency": venue.currency, "request": request}
    return Response(
        {
            "date": on_date.isoformat(),
            "channel": channel,
            "currency": venue.currency,
            "products": ProductSerializer(products, many=True, context=context).data,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def session_list(request, venue_code: str):
    """Published, customer-visible sessions for a date (R26.14)."""
    venue = _resolve_venue(venue_code)
    raw_date = request.GET.get("date")
    try:
        on_date = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    except ValueError:
        on_date = dt.date.today()

    sessions = Session.objects.filter(
        venue=venue,
        session_date=on_date,
        publication_state="PUBLISHED",
        customer_visible=True,
    ).exclude(status="HIDDEN")
    return Response(
        {
            "date": on_date.isoformat(),
            "timezone": venue.timezone,
            "sessions": SessionSerializer(sessions, many=True).data,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def payment_type_list(request, venue_code: str):
    """Payment methods offered on this channel, in display order."""
    venue = _resolve_venue(venue_code)
    channel = request.GET.get("channel", "ONLINE")
    currency = request.GET.get("currency") or venue.currency
    types = [
        t
        for t in PaymentType.objects.filter(venue=venue, status="ACTIVE").order_by("display_order")
        if t.available_for(channel, currency)
    ]
    return Response({"payment_types": PaymentTypeSerializer(types, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def charge_preview(request, venue_code: str):
    """Show how a base amount breaks down under today's configuration.

    Exists so both clients can verify their display against the server's
    authoritative arithmetic rather than reimplementing tax rules.
    """
    venue = _resolve_venue(venue_code)
    try:
        base_minor = int(request.GET.get("base_minor", "0"))
    except ValueError:
        base_minor = 0
    raw_date = request.GET.get("date")
    try:
        on_date = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    except ValueError:
        on_date = dt.date.today()

    breakdown = compute_order_charges(venue=venue, base_minor=base_minor, on_date=on_date)
    return Response(breakdown.as_dict())
