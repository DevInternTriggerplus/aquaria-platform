"""Security layer.

Organised around the OWASP Top 10 (2021) and ASVS, and deliberately made of
*enforcement code* rather than advice. Every control in
:mod:`utp.security.owasp` points at the module that implements it, so the register
can be checked against reality instead of being trusted.

Modules
-------
``owasp``       the control register: OWASP category -> concrete control -> code
``validation``  allow-list input validation, safe identifiers, output encoding
``headers``     response security headers, cookie policy, CORS allow-list
``csrf``        double-submit CSRF tokens bound to the session
``ssrf``        outbound URL guard for webhooks, media and map references
``uploads``     upload validation by magic bytes, size and declared type
``ratelimit``   reusable limiter plus abuse quotas for holds and promo codes
``secrets``     secret provider interface with rotation support
``monitoring``  security event detection and alert thresholds

The controls that already live elsewhere are referenced, not duplicated:
authorization in :mod:`utp.services.authz`, authentication and session lifecycle in
:mod:`utp.services.staff`, audit in :mod:`utp.core.audit`, parameterized queries in
:mod:`utp.core.db`, and PII handling in :mod:`utp.services.customers`.
"""

__all__ = [
    "csrf",
    "headers",
    "monitoring",
    "owasp",
    "ratelimit",
    "secrets",
    "ssrf",
    "uploads",
    "validation",
]
