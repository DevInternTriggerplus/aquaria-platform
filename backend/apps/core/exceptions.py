"""Turn domain errors into safe HTTP responses.

Nothing that reaches a client may contain SQL text, a stack trace, an internal
service name, an internal identifier or a payment provider payload (R66.4). Every
error therefore has a public part — stable machine code, friendly localized
message, safe details — and a private ``log_detail`` that only goes to the log,
tied to the response by the correlation id (R66.5).
"""

from __future__ import annotations

import logging

from django.db import IntegrityError
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_handler

from .errors import NotFound, PlatformError
from .i18n import localize_error
from .middleware import current_correlation_id

logger = logging.getLogger(__name__)


def drf_exception_handler(exc, context):
    """DRF exception hook registered in settings."""
    request = context.get("request")
    language = _language_of(request)
    cid = current_correlation_id()

    if isinstance(exc, PlatformError):
        exc.correlation_id = exc.correlation_id or cid
        if exc.log_detail:
            logger.warning("%s: %s", type(exc).__name__, exc.log_detail)
        payload = localize_error(exc, language)
        return Response(payload, status=exc.http_status)

    if isinstance(exc, Http404):
        # Genuine absence and cross-tenant access are indistinguishable (R1.2).
        not_found = NotFound(correlation_id=cid)
        return Response(localize_error(not_found, language), status=404)

    if isinstance(exc, IntegrityError):
        # A database constraint fired. That is the data layer defending an
        # invariant, so log the detail and return a conflict without echoing the
        # constraint text, which would leak schema internals.
        logger.error("IntegrityError: %s", exc, exc_info=True)
        return Response(
            {
                "error": {
                    "code": "conflict",
                    "message": "That conflicts with something already recorded. Please try again.",
                    "message_key": "error.conflict",
                    "reference": cid,
                }
            },
            status=http_status.HTTP_409_CONFLICT,
        )

    response = drf_default_handler(exc, context)
    if response is None:
        # Genuinely unexpected. Log everything, tell the client nothing.
        logger.exception("Unhandled exception")
        return Response(
            {
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong. Please try again.",
                    "message_key": "error.generic",
                    "reference": cid,
                }
            },
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # Reshape DRF's own errors into the platform envelope so clients parse one shape.
    response.data = {
        "error": {
            "code": _drf_code(response.status_code),
            "message": _drf_message(response.status_code),
            "message_key": "error.generic",
            "reference": cid,
            "details": {"fields": response.data} if isinstance(response.data, dict) else {},
        }
    }
    return response


def _language_of(request) -> str:
    if request is None:
        return "en"
    lang = request.GET.get("lang") or ""
    if lang:
        return lang
    return getattr(request, "LANGUAGE_CODE", "en") or "en"


def _drf_code(code: int) -> str:
    return {
        400: "validation_failed",
        401: "authentication_required",
        403: "authorization_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "rate_limited",
    }.get(code, "request_failed")


def _drf_message(code: int) -> str:
    return {
        400: "Please check the highlighted fields and try again.",
        401: "Please sign in to continue.",
        403: "You do not have permission to perform this action.",
        404: "We could not find what you were looking for.",
        405: "That action is not available here.",
        409: "That conflicts with something already recorded. Please try again.",
        429: "Too many attempts. Please wait a moment and try again.",
    }.get(code, "Something went wrong. Please try again.")
