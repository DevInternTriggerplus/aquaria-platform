"""Write-path endpoints: consent, quote and confirm.

The quote is re-resolved from its inputs at confirm time rather than trusted from
the client, so a tampered cart cannot force a stale price or a price the client
invented (R13.7). The client sends the same date and lines to both endpoints; the
server is authoritative for every number.
"""

from __future__ import annotations

import datetime as dt

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

import json

from apps.booking import consent_service
from apps.booking.services import (
    QuoteLine,
    confirm as confirm_booking,
    on_payment_captured,
    quote as build_quote,
)
from apps.core.errors import ValidationError as PlatformValidationError
from apps.payments.gateway import SimulatedGateway
from apps.payments.webhook import handle_webhook
from apps.tenancy.models import Tenant, Venue

from .serializers import ConfirmRequestSerializer, GateScanSerializer, QuoteRequestSerializer

# One gateway instance. A real deployment injects the configured provider; the
# simulated one is deterministic and idempotent, which is what the flow relies on.
_GATEWAY = SimulatedGateway()


def _venue(venue_code: str) -> Venue:
    return get_object_or_404(Venue, code=venue_code, status="ACTIVE")


def _language(request) -> str:
    return request.GET.get("lang") or getattr(request, "LANGUAGE_CODE", "en") or "en"


@api_view(["GET"])
@permission_classes([AllowAny])
def consent_dialog(request, venue_code: str):
    """The PDPA consent dialog contents (R12.3)."""
    venue = _venue(venue_code)
    return Response(consent_service.dialog(venue.tenant, _language(request)))


@api_view(["POST"])
@permission_classes([AllowAny])
def quote(request, venue_code: str):
    venue = _venue(venue_code)
    serializer = QuoteRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    lines = [
        QuoteLine(
            ticket_type_id=line["ticket_type_id"],
            quantity=line["quantity"],
            session_id=line.get("session_id"),
        )
        for line in data["lines"]
    ]
    result = build_quote(venue=venue, visit_date=data["visit_date"], lines=lines)
    return Response(result.as_dict())


@api_view(["POST"])
@permission_classes([AllowAny])
def confirm(request, venue_code: str):
    """Re-resolve the quote and confirm the booking.

    The lines and date are taken from the request and re-priced server-side; the
    client's earlier quote is never trusted for money.
    """
    venue = _venue(venue_code)
    serializer = ConfirmRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    lines = [
        QuoteLine(
            ticket_type_id=line["ticket_type_id"],
            quantity=line["quantity"],
            session_id=line.get("session_id"),
        )
        for line in data["lines"]
    ]
    quote_result = build_quote(venue=venue, visit_date=data["visit_date"], lines=lines)

    result = confirm_booking(
        quote_result=quote_result,
        customer_data={
            "email": data["email"],
            "full_name": data["full_name"],
            "phone": data.get("phone", ""),
        },
        consent_items=data["consent_items"],
        payment_method=data.get("payment_method") or "CARD",
        gateway=_GATEWAY,
        idempotency_key=data.get("idempotency_key", ""),
        language=_language(request),
    )
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def manage_request_code(request, venue_code: str):
    """Email a one-time verification code for Manage Booking (R16.2).

    The response is identical whether the booking exists or not, so it cannot be
    used to enumerate bookings (R16.3). Rate-limited per the DRF scoped throttle.
    """
    from apps.booking import manage_service

    venue = _venue(venue_code)
    result = manage_service.request_access_code(
        tenant=venue.tenant,
        booking_number=request.data.get("booking_number", ""),
        email=request.data.get("email", ""),
    )
    # The demo surfaces the code so the flow can be walked without a mailbox. A real
    # deployment removes this — the code exists only in the email.
    demo_code = result.pop("_code", None)
    return Response({**result, "demo_code": demo_code})


manage_request_code.throttle_scope = "booking_lookup"


@api_view(["POST"])
@permission_classes([AllowAny])
def manage_verify(request, venue_code: str):
    """Consume the one-time code and return the booking view (R16.4)."""
    from apps.booking import manage_service

    venue = _venue(venue_code)
    verified = manage_service.verify_access(
        tenant=venue.tenant,
        booking_number=request.data.get("booking_number", ""),
        email=request.data.get("email", ""),
        code=request.data.get("code", ""),
    )
    view = manage_service.manage_view(tenant=venue.tenant, booking_id=verified["booking_id"])
    return Response(view)


@api_view(["POST"])
@permission_classes([AllowAny])
def manage_cancel(request, venue_code: str):
    """Cancel a booking (R16, R17). Requires a fresh verification each session.

    For now the caller passes the verified ``booking_id``; when sessions are added
    the id will come from a short-lived manage token rather than the request body.
    """
    from apps.booking import manage_service

    venue = _venue(venue_code)
    result = manage_service.cancel(
        tenant=venue.tenant,
        booking_id=request.data.get("booking_id", ""),
        reason=request.data.get("reason", "Customer request"),
        confirmed=bool(request.data.get("confirmed")),
    )
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def manage_reschedule(request, venue_code: str):
    """Reschedule a booking (R16.7)."""
    import datetime as _dt

    from apps.booking import manage_service

    venue = _venue(venue_code)
    raw_date = request.data.get("new_visit_date", "")
    try:
        new_date = _dt.date.fromisoformat(raw_date)
    except ValueError:
        return Response(
            {"error": {"code": "validation_failed", "message": "Enter the date as YYYY-MM-DD.",
                       "message_key": "error.validation_failed"}},
            status=422,
        )
    result = manage_service.reschedule(
        tenant=venue.tenant,
        booking_id=request.data.get("booking_id", ""),
        new_visit_date=new_date,
        new_session_id=request.data.get("new_session_id"),
        reason=request.data.get("reason", ""),
    )
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def gate_scan(request, venue_code: str):
    """Validate a scanned QR at an access point (R32).

    Runs without staff authentication: the device identity is the gate control, not
    a staff login (R32.12), so gate throughput is not bottlenecked on RBAC. An
    unregistered or deactivated device is refused before any ticket lookup.
    """
    from apps.access import services as access_services
    from apps.tenancy.models import AccessPoint, Device

    venue = _venue(venue_code)
    serializer = GateScanSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    device = None
    device_code = data.get("device_code")
    if device_code:
        device = Device.objects.filter(
            tenant=venue.tenant, code=device_code, kind="SCANNER"
        ).first()
        if device is None or device.status != "ACTIVE":
            # Unregistered or deactivated scanner: refuse without disclosing which.
            return Response({
                "decision": "REJECT_DEVICE",
                "admit": False,
                "message": "This scanner is not authorised.",
            })

    access_point = None
    ap_code = data.get("access_point_code")
    if ap_code:
        access_point = AccessPoint.objects.filter(
            tenant=venue.tenant, venue=venue, code=ap_code
        ).first()

    result = access_services.scan(
        tenant=venue.tenant,
        venue=venue,
        qr_payload=data["payload"],
        access_point=access_point,
        device=device,
    )
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def gate_override(request, venue_code: str):
    """Admit a rejected guest on a supervisor's authority (R32.9).

    Requires ``OVERRIDE_ACCESS`` and a mandatory reason. Staff authentication and
    the permission check are wired when the staff/back-office surface lands; until
    then this remains available for the gate demo and is fully audited.
    """
    from apps.access import services as access_services

    venue = _venue(venue_code)
    scan_id = request.data.get("scan_id", "")
    reason = request.data.get("reason", "")
    result = access_services.override_admit(
        tenant=venue.tenant, venue=venue, scan_id=scan_id, reason=reason
    )
    return Response(result)


@api_view(["GET"])
@permission_classes([AllowAny])
def gate_lookup(request, venue_code: str):
    """Manual booking lookup when a QR cannot be scanned (R32.10)."""
    from apps.access import services as access_services

    venue = _venue(venue_code)
    booking_number = request.GET.get("booking_number", "")
    result = access_services.manual_lookup(
        tenant=venue.tenant, venue=venue, booking_number=booking_number
    )
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def payment_webhook(request):
    """Provider payment callback (R14.4–R14.7).

    The signature is verified against the *raw* request body, so this reads
    ``request.body`` rather than the parsed data — a re-serialized body would not
    match the provider's signature. The tenant is carried in the payload because a
    provider callback is not an authenticated session.

    On a captured payment this completes the booking through the shared idempotent
    finalize, which is how a booking whose browser died after authorization still
    confirms and delivers its ticket.
    """
    raw = request.body.decode("utf-8") if request.body else "{}"
    signature = request.headers.get("X-Webhook-Signature", "")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return Response(
            {"error": {"code": "validation_failed", "message": "Malformed callback body.",
                       "message_key": "error.validation_failed"}},
            status=422,
        )

    tenant = Tenant.objects.filter(id=payload.get("tenant_id"), status="ACTIVE").first()
    if tenant is None:
        # Do not disclose whether the tenant exists; a callback for an unknown tenant
        # is simply not processed.
        return Response({"processed": False, "outcome": "IGNORED"})

    result = handle_webhook(
        tenant=tenant,
        gateway=_GATEWAY,
        provider_event_id=str(payload.get("event_id", "")),
        kind=str(payload.get("kind", "")),
        body=raw,
        signature=signature,
        payment_id=payload.get("payment_id"),
        idempotency_key=payload.get("idempotency_key"),
        amount_minor=payload.get("amount_minor"),
        failure_code=payload.get("failure_code", ""),
        on_capture=on_payment_captured,
    )
    return Response(result)
