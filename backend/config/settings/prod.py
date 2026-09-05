"""Production settings.

Fails fast rather than starting up insecurely: a missing or placeholder secret
is a startup error, not a warning nobody reads.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

_PLACEHOLDERS = {
    "insecure-development-key-replace-me",
    "insecure-development-signing-key",
    "replace-me-before-any-real-use",
    "",
}

if SECRET_KEY in _PLACEHOLDERS:  # noqa: F405
    raise ImproperlyConfigured("SECRET_KEY is unset or still a development placeholder.")
if TICKET_SIGNING_KEY in _PLACEHOLDERS:  # noqa: F405
    raise ImproperlyConfigured(
        "TICKET_SIGNING_KEY is unset or still a development placeholder. "
        "Issued QR codes would be forgeable."
    )
if env("USE_SQLITE_FALLBACK"):
    raise ImproperlyConfigured(
        "USE_SQLITE_FALLBACK must be off in production. The capacity and "
        "append-only guarantees depend on PostgreSQL."
    )

# Transport security. Assumes TLS terminates at a managed load balancer or CDN.
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 63_072_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Financial and personal data must not be cached by a shared proxy.
CSRF_TRUSTED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
