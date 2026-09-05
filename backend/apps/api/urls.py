"""API routes.

Venue-scoped paths carry the venue code, because the platform is multi-tenant and
multi-venue by construction: there is no implicit "the venue".
"""

from django.urls import path

from . import views
from . import booking_views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("venues/<slug:venue_code>/", views.venue_detail, name="venue-detail"),
    path("venues/<slug:venue_code>/products/", views.product_list, name="product-list"),
    path("venues/<slug:venue_code>/sessions/", views.session_list, name="session-list"),
    path(
        "venues/<slug:venue_code>/payment-types/",
        views.payment_type_list,
        name="payment-type-list",
    ),
    path(
        "venues/<slug:venue_code>/charge-preview/",
        views.charge_preview,
        name="charge-preview",
    ),
    # Write path.
    path("venues/<slug:venue_code>/consent/", booking_views.consent_dialog, name="consent-dialog"),
    path("venues/<slug:venue_code>/quote/", booking_views.quote, name="quote"),
    path("venues/<slug:venue_code>/confirm/", booking_views.confirm, name="confirm"),
    # Manage booking (R16).
    path("venues/<slug:venue_code>/manage/request-code/", booking_views.manage_request_code, name="manage-request-code"),
    path("venues/<slug:venue_code>/manage/verify/", booking_views.manage_verify, name="manage-verify"),
    path("venues/<slug:venue_code>/manage/cancel/", booking_views.manage_cancel, name="manage-cancel"),
    path("venues/<slug:venue_code>/manage/reschedule/", booking_views.manage_reschedule, name="manage-reschedule"),
    # Gate (R32).
    path("venues/<slug:venue_code>/gate/scan/", booking_views.gate_scan, name="gate-scan"),
    path("venues/<slug:venue_code>/gate/override/", booking_views.gate_override, name="gate-override"),
    path("venues/<slug:venue_code>/gate/lookup/", booking_views.gate_lookup, name="gate-lookup"),
    # Provider callback. Not venue-scoped — the tenant is in the signed payload.
    path("webhook/payment/", booking_views.payment_webhook, name="payment-webhook"),
]
