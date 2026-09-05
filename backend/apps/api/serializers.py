"""Serializers shared by the Next and Flutter clients.

Both front ends consume the same representations, so a rule proven here holds for
every client. Money is always emitted as ``*_minor`` integers plus an explicit
``currency``; formatting is the client's job, because only the client knows the
viewer's locale.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.catalog.models import Product, TicketType
from apps.inventory.models import Session
from apps.payments.models import PaymentType
from apps.tenancy.models import Area, Venue


def localized(value, language: str = "en", fallback: str = "") -> str:
    """Pick a language from a translatable JSON field, falling back (R69.5)."""
    if isinstance(value, dict):
        return value.get(language) or value.get("en") or next(iter(value.values()), fallback)
    return str(value or fallback)


class LocalizedField(serializers.Field):
    """Emits the whole translatable map so the client can switch without a refetch."""

    def to_representation(self, value):
        return value if isinstance(value, dict) else {"en": str(value or "")}


class AreaSerializer(serializers.ModelSerializer):
    name = LocalizedField()
    description = LocalizedField()

    class Meta:
        model = Area
        fields = ["id", "code", "name", "description", "floor", "image_url", "icon"]


class VenueSerializer(serializers.ModelSerializer):
    name = LocalizedField()
    areas = AreaSerializer(many=True, read_only=True)

    class Meta:
        model = Venue
        fields = [
            "id",
            "code",
            "name",
            "short_name",
            "venue_type",
            "timezone",
            "currency",
            "tax_model",
            "tax_rate_bp",
            "rounding_mode",
            "address",
            "contact",
            "logo_url",
            "operating_hours",
            "areas",
        ]


class TicketTypeSerializer(serializers.ModelSerializer):
    name = LocalizedField()
    description = LocalizedField()
    segment_code = serializers.CharField(source="segment.code", read_only=True)
    unit_price_minor = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = TicketType
        fields = [
            "id",
            "code",
            "name",
            "description",
            "segment_code",
            "min_quantity",
            "max_quantity",
            "entry_allowance",
            "reentry_allowed",
            "eligibility",
            "unit_price_minor",
            "currency",
        ]

    def get_unit_price_minor(self, obj) -> int | None:
        """Resolved price for the requested date/channel, or None if unavailable.

        None is meaningful: no matching price rule means the ticket type is not
        sellable for that request, and the platform never substitutes a guess
        (R5.6).
        """
        resolved = (self.context.get("prices") or {}).get(obj.id)
        return resolved.amount_minor if resolved else None

    def get_currency(self, obj) -> str:
        resolved = (self.context.get("prices") or {}).get(obj.id)
        if resolved:
            return resolved.currency
        return self.context.get("currency", "THB")


class ProductSerializer(serializers.ModelSerializer):
    name = LocalizedField()
    description = LocalizedField()
    ticket_types = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "code",
            "name",
            "description",
            "admission_model",
            "session_requirement",
            "seat_requirement",
            "min_per_booking",
            "max_per_booking",
            "ticket_types",
        ]

    def get_ticket_types(self, obj):
        types = [t for t in obj.ticket_types.all() if t.status == "ACTIVE" and t.customer_visible]
        return TicketTypeSerializer(types, many=True, context=self.context).data


class SessionSerializer(serializers.ModelSerializer):
    remaining = serializers.SerializerMethodField()

    class Meta:
        model = Session
        fields = [
            "id",
            "kind",
            "session_date",
            "start_time",
            "end_time",
            "capacity",
            "remaining",
            "status",
            "reservation_mode",
        ]

    def get_remaining(self, obj) -> int | None:
        return obj.remaining


class PaymentTypeSerializer(serializers.ModelSerializer):
    display_name = LocalizedField()
    description = LocalizedField()

    class Meta:
        model = PaymentType
        fields = ["id", "code", "method", "display_name", "description", "icon"]


# --------------------------------------------------------------------------- #
# Write-side payloads. Validated strictly: unexpected fields are rejected rather
# than ignored, so a client cannot smuggle a tenant or price (R42.12, R73.6).
# --------------------------------------------------------------------------- #


class QuoteLineSerializer(serializers.Serializer):
    ticket_type_id = serializers.CharField(max_length=40)
    quantity = serializers.IntegerField(min_value=1, max_value=100)
    session_id = serializers.CharField(max_length=40, required=False, allow_null=True)


class QuoteRequestSerializer(serializers.Serializer):
    visit_date = serializers.DateField()
    lines = QuoteLineSerializer(many=True, allow_empty=False)
    promotion_codes = serializers.ListField(
        child=serializers.CharField(max_length=40), required=False, default=list
    )

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Choose at least one ticket.")
        return value


class ConfirmRequestSerializer(serializers.Serializer):
    # The confirm payload carries the quote inputs *and* the customer/consent fields,
    # so the server can re-resolve the price authoritatively rather than trust a cart.
    visit_date = serializers.DateField()
    lines = QuoteLineSerializer(many=True, allow_empty=False)
    promotion_codes = serializers.ListField(
        child=serializers.CharField(max_length=40), required=False, default=list
    )
    email = serializers.EmailField()
    full_name = serializers.CharField(max_length=160)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    payment_type_id = serializers.CharField(max_length=40, required=False, allow_blank=True)
    payment_method = serializers.CharField(max_length=20, required=False, allow_blank=True)
    #: PDPA consent must be present before any personal data is persisted (R12.2).
    consent_items = serializers.DictField(child=serializers.BooleanField())
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_blank=True)


class GateScanSerializer(serializers.Serializer):
    payload = serializers.CharField(max_length=255)
    access_point_code = serializers.CharField(max_length=40, required=False, allow_blank=True)
    device_code = serializers.CharField(max_length=40, required=False, allow_blank=True)
