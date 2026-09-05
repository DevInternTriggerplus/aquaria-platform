"""Shared Django settings for the Aquaria backend.

The platform is PostgreSQL-first. ``DATABASE_URL`` drives the connection and the
engine is ``django.db.backends.postgresql`` via psycopg 3. A SQLite fallback
exists for one narrow purpose — running the suite on a machine with no
PostgreSQL server — and is opt-in through ``USE_SQLITE_FALLBACK``. It is never
appropriate beyond local development, because several data-layer guarantees
(capacity under concurrency, append-only enforcement) depend on PostgreSQL.

Money, tax and time conventions are inherited from ``apps.core``: amounts are
integer minor units, instants are stored UTC, and business dates are evaluated
in the venue's IANA timezone.
"""

from __future__ import annotations

from pathlib import Path

import environ

# backend/config/settings/base.py -> backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
    USE_SQLITE_FALLBACK=(bool, False),
    CORS_ALLOWED_ORIGINS=(list, []),
    SECRET_KEY=(str, "insecure-development-key-replace-me"),
    TICKET_SIGNING_KEY=(str, "insecure-development-signing-key"),
    DATABASE_URL=(str, "postgres://aquaria:aquaria@127.0.0.1:5432/aquaria"),
)

# Load backend/.env when present. Deployment injects real environment variables and
# ships no .env file, so this only affects local development.
#
# ``overwrite=True`` is deliberate. Without it a stale shell variable — most
# damagingly a leftover USE_SQLITE_FALLBACK=1 — silently wins over the committed
# .env, and the suite then runs against SQLite while the developer believes it is on
# PostgreSQL. The project file is the source of truth for local runs.
_env_file = BASE_DIR / ".env"
if _env_file.is_file():
    environ.Env.read_env(str(_env_file), overwrite=True)

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

TICKET_SIGNING_KEY = env("TICKET_SIGNING_KEY")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    # Platform apps. Names stay generic — no venue-type-specific app exists.
    "apps.core",
    "apps.tenancy",
    "apps.accounts",
    "apps.catalog",
    "apps.venuesettings",
    "apps.pricing",
    "apps.inventory",
    "apps.booking",
    "apps.payments",
    "apps.ticketing",
    "apps.access",
    "apps.api",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Attaches a correlation id to every request so a customer-facing error
    # reference can be traced to the server log without leaking internals.
    "apps.core.middleware.CorrelationIdMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------- #
# Database — PostgreSQL
# --------------------------------------------------------------------------- #

if env("USE_SQLITE_FALLBACK"):
    # Local development only. See the module docstring.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "local-fallback.sqlite3",
        }
    }
else:
    DATABASES = {"default": env.db("DATABASE_URL")}
    DATABASES["default"].setdefault("CONN_MAX_AGE", 60)
    DATABASES["default"].setdefault("OPTIONS", {})

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.Staff"

# Django's auth.E003 requires USERNAME_FIELD to be globally unique. This platform
# is multi-tenant and the spec requires staff email to be unique *within a tenant*
# (R38.8) — two tenants may legitimately employ the same person. The per-tenant
# uniqueness is enforced by a database constraint on (tenant, email).
#
# Consequence, deliberately recorded: authentication must resolve the tenant before
# looking up the email. Never call the default ModelBackend against email alone, or
# a duplicate across tenants raises MultipleObjectsReturned. A tenant-aware backend
# is required before login is exposed.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------- #
# Time — store UTC, evaluate business dates in the venue's timezone
# --------------------------------------------------------------------------- #

LANGUAGE_CODE = "en"
# The platform default only. Every venue carries its own IANA timezone and all
# operating dates, cutoffs and ticket expiry are evaluated against that, never
# against this value or the server clock.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ("en", "English"),
    ("th", "ไทย"),
    ("zh", "中文"),
    ("ja", "日本語"),
    ("ru", "Русский"),
]

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# --------------------------------------------------------------------------- #
# REST framework
# --------------------------------------------------------------------------- #

REST_FRAMEWORK = {
    # Default deny: an endpoint must opt in to public access explicitly.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "apps.core.exceptions.drf_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Booking lookup and promotion-code validation are enumeration targets.
        "booking_lookup": "10/min",
        "promo_code": "20/min",
        "gate_scan": "600/min",
    },
}

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

# --------------------------------------------------------------------------- #
# Security headers. Tightened further in production settings.
# --------------------------------------------------------------------------- #

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "[{asctime}] {levelname} {name} cid={correlation_id} {message}",
            "style": "{",
        },
    },
    "filters": {
        "correlation": {"()": "apps.core.logging.CorrelationIdFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
            "filters": ["correlation"],
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
