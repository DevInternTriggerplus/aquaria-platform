"""Domain error hierarchy.

Two rules govern this module:

1. Nothing that reaches a customer or a staff member may contain SQL text, a
   stack trace, an internal service name, an internal identifier or a payment
   provider payload (R66.4). Every error therefore carries a *public* part
   (stable machine code + friendly message key + safe details) and a private
   ``log_detail`` that only ever goes to the server log (R66.5).
2. Authorization and tenant-isolation failures must not disclose whether the
   target record exists (R1.2, R42.3). ``NotFound`` and ``AuthorizationDenied``
   both use fixed, non-specific public messages.
"""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    """Base class for every expected domain failure.

    Parameters
    ----------
    code:
        Stable machine-readable code. Clients and partner APIs branch on this,
        never on the message text.
    message:
        Customer/staff-safe message in the platform default language. The
        localization layer resolves ``message_key`` when a language is known.
    details:
        Safe, structured context (e.g. which item sold out, nearest bookable
        date). Must never contain PII or internals.
    log_detail:
        Private diagnostic text. Server-side logs only.
    """

    code = "platform_error"
    http_status = 400
    message_key = "error.generic"
    default_message = "Something went wrong. Please try again."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        log_detail: str | None = None,
        message_key: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.code = code or self.code
        self.message_key = message_key or self.message_key
        self.details: dict[str, Any] = dict(details or {})
        self.log_detail = log_detail
        self.correlation_id = correlation_id
        super().__init__(self.message)

    def public_dict(self) -> dict[str, Any]:
        """Representation safe to return over any channel."""
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "message_key": self.message_key,
            }
        }
        if self.details:
            payload["error"]["details"] = self.details
        if self.correlation_id:
            payload["error"]["reference"] = self.correlation_id
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------- #
# Authentication / authorization / isolation
# --------------------------------------------------------------------------- #


class AuthenticationRequired(PlatformError):
    code = "authentication_required"
    http_status = 401
    message_key = "error.authentication_required"
    default_message = "Please sign in to continue."


class AuthorizationDenied(PlatformError):
    """Generic denial. Deliberately reveals nothing about configuration (R42.3)."""

    code = "authorization_denied"
    http_status = 403
    message_key = "error.authorization_denied"
    default_message = "You do not have permission to perform this action."

    def __init__(
        self,
        *,
        required: str | None = None,
        log_detail: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        # ``required`` is captured for the audit trail and server log only. It is
        # intentionally excluded from the public payload.
        detail = log_detail or (f"missing permission {required}" if required else None)
        super().__init__(log_detail=detail, correlation_id=correlation_id)
        self.required = required


class NotFound(PlatformError):
    """Used for genuine absence *and* for cross-tenant access (R1.2)."""

    code = "not_found"
    http_status = 404
    message_key = "error.not_found"
    default_message = "We could not find what you were looking for."


class RateLimited(PlatformError):
    code = "rate_limited"
    http_status = 429
    message_key = "error.rate_limited"
    default_message = "Too many attempts. Please wait a moment and try again."

    def __init__(self, retry_after_seconds: int = 60, **kwargs: Any) -> None:
        details = dict(kwargs.pop("details", None) or {})
        details["retry_after_seconds"] = retry_after_seconds
        super().__init__(details=details, **kwargs)
        self.retry_after_seconds = retry_after_seconds


# --------------------------------------------------------------------------- #
# Validation and business rules
# --------------------------------------------------------------------------- #


class ValidationError(PlatformError):
    """Field-level validation failure with actionable, per-field messages (R11.8)."""

    code = "validation_failed"
    http_status = 422
    message_key = "error.validation_failed"
    default_message = "Please check the highlighted fields and try again."

    def __init__(
        self,
        field_errors: dict[str, str] | None = None,
        message: str | None = None,
        **kwargs: Any,
    ) -> None:
        details = dict(kwargs.pop("details", None) or {})
        self.field_errors = dict(field_errors or {})
        if self.field_errors:
            details["fields"] = self.field_errors
        super().__init__(message, details=details, **kwargs)


class RuleViolation(PlatformError):
    """A configured business rule rejected the request.

    ``details`` should explain which constraint applied and what the nearest
    bookable option is, in customer-friendly language (R6.4).
    """

    code = "rule_violation"
    http_status = 409
    message_key = "error.rule_violation"
    default_message = "That option is not available."


class NotAvailable(RuleViolation):
    """Requested inventory, date or session is not sellable."""

    code = "not_available"
    message_key = "error.not_available"
    default_message = "That is not available for the date you selected."


class JustSoldOut(RuleViolation):
    """Lost the race for the last unit. A distinct outcome by requirement (R10.6)."""

    code = "just_sold_out"
    message_key = "error.just_sold_out"
    default_message = "That has just sold out. Please choose another date or time."


class SeatTaken(RuleViolation):
    """Another guest obtained the seat first (R57.8)."""

    code = "seat_just_taken"
    message_key = "error.seat_just_taken"
    default_message = "That seat has just been taken. Please choose another one."


class HoldExpired(RuleViolation):
    code = "hold_expired"
    http_status = 410
    message_key = "error.hold_expired"
    default_message = "Your reservation time ran out. Please confirm your choices again."


class ConsentRequired(PlatformError):
    """Required PDPA consent absent; personal data must not be persisted (R12.2)."""

    code = "consent_required"
    http_status = 422
    message_key = "error.consent_required"
    default_message = (
        "We cannot continue until you accept the processing needed to create your "
        "booking, issue your tickets and take payment."
    )


class ConflictError(PlatformError):
    code = "conflict"
    http_status = 409
    message_key = "error.conflict"
    default_message = "That change conflicts with the current state. Please review and retry."


class ConfirmationRequired(PlatformError):
    """A sensitive action needs explicit, informed confirmation (R67)."""

    code = "confirmation_required"
    http_status = 409
    message_key = "error.confirmation_required"
    default_message = "Please confirm this action before it can be applied."


class ImmutableRecord(PlatformError):
    """Attempt to erase or alter a protected financial/audit record (R46)."""

    code = "immutable_record"
    http_status = 409
    message_key = "error.immutable_record"
    default_message = "This record is kept for audit and cannot be deleted."


class PaymentFailed(PlatformError):
    """Provider failure mapped to a configured, customer-safe message (R14.12)."""

    code = "payment_failed"
    http_status = 402
    message_key = "error.payment_failed"
    default_message = "The payment could not be completed. Please try another method."


class ConfigurationError(PlatformError):
    """Operator-facing configuration problem (never shown to customers)."""

    code = "configuration_error"
    http_status = 422
    message_key = "error.configuration_error"
    default_message = "This configuration is incomplete. Please review the highlighted items."


__all__ = [
    "AuthenticationRequired",
    "AuthorizationDenied",
    "ConfigurationError",
    "ConfirmationRequired",
    "ConflictError",
    "ConsentRequired",
    "HoldExpired",
    "ImmutableRecord",
    "JustSoldOut",
    "NotAvailable",
    "NotFound",
    "PaymentFailed",
    "PlatformError",
    "RateLimited",
    "RuleViolation",
    "SeatTaken",
    "ValidationError",
]
