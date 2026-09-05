"""Config-backed settings pages.

A large number of settings pages are, underneath, a single scoped configuration
value: a small object an administrator edits and the platform reads. Operating
hours, booking rules, rounding, price display, the language list, numbering
formats, login-security policy — none of these are collections and none need
their own table. They are exactly what :class:`~utp.core.config.ConfigStore`
already stores: one current value per scope, versioned so history survives, read
by nearest scope.

Rather than write a near-identical service method per page, this module declares
each page once — its config key, the scope it applies at, the page permission it
needs, the sensitive action it may also require, its default value and a validator
— and drives read/write generically. Adding a page is a table entry, not code.

Every write still goes through the same discipline the hand-written settings use:
the page's ``EDIT`` permission, the venue-scope check, the optional ``MANAGE_*``
action with its mandatory reason, and an audited ``CONFIG_CHANGE`` with the old and
new value (settings/reports spec §7, §16, §21, §47, §54). Reads require only
``VIEW`` — looking at a value is not changing it (§14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.audit import AuditLog
from ..core.clock import Clock
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.errors import NotFound, ValidationError
from .authz import AuthorizationService

# A validator receives the incoming value and the previously stored value (or None),
# and returns the cleaned value to persist. Most validators ignore ``previous``; the
# credential pages use it to keep a secret the caller left blank because they were
# never shown it.
Validator = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ConfigPage:
    """One config-backed settings page.

    ``scope`` is where the value lives: ``VENUE`` for anything venue-local (hours,
    access rules), ``TENANT`` for platform-wide policy (languages, login security).
    ``action`` is the sensitive :class:`ActionPermission` a change also demands, or
    ``None`` when the page's ``EDIT`` verb is authority enough. ``default`` is what a
    venue that has configured nothing sees, so the page never renders empty.
    """

    page: str
    key: str
    scope: str
    default: dict[str, Any]
    action: str | None = None
    validate: Validator | None = None
    #: True when a change affects money/access on future transactions and the UI
    #: should therefore confirm before saving (§40). Advisory to the client; the
    #: server enforces the permission regardless.
    sensitive: bool = False
    #: True for pages that hold masked credentials. On save the previously stored
    #: value is passed to the validator under ``_existing`` so a blank secret keeps
    #: the one on file — the client never has to (and cannot) echo a secret it was
    #: not shown.
    has_secrets: bool = False
    description: str = ""


# --------------------------------------------------------------------------- #
# Validators
# --------------------------------------------------------------------------- #

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _hhmm(value: Any) -> str | None:
    """Accept 'HH:MM' (24h) or None; reject anything else with a clear message."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValidationError({"time": "Enter a time as HH:MM, for example 18:00."})
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValidationError({"time": "That is not a valid time of day."})
    return f"{hh:02d}:{mm:02d}"


def _validate_operating_hours(value: dict[str, Any]) -> dict[str, Any]:
    days = value.get("days")
    if not isinstance(days, dict):
        raise ValidationError({"days": "Operating hours must be provided per weekday."})
    cleaned: dict[str, Any] = {}
    for code in _WEEKDAYS:
        day = days.get(code) or {}
        closed = bool(day.get("closed"))
        if closed:
            cleaned[code] = {"closed": True, "open": None, "close": None, "last_admission": None}
            continue
        opens = _hhmm(day.get("open"))
        closes = _hhmm(day.get("close"))
        last = _hhmm(day.get("last_admission"))
        if opens and closes and closes <= opens:
            raise ValidationError({code: "Closing time must be after opening time."})
        # Last admission cannot be after closing (settings spec §22.4).
        if last and closes and last > closes:
            raise ValidationError({code: "Last admission cannot be after closing time."})
        cleaned[code] = {"closed": False, "open": opens, "close": closes, "last_admission": last}
    return {"days": cleaned}


def _validate_last_admission(value: dict[str, Any]) -> dict[str, Any]:
    offset = value.get("cutoff_minutes_before_close")
    if offset is not None:
        offset = int(offset)
        if offset < 0:
            raise ValidationError({"cutoff_minutes_before_close": "Enter zero or more minutes."})
    fixed = _hhmm(value.get("fixed_time"))
    return {"cutoff_minutes_before_close": offset, "fixed_time": fixed}


def _validate_booking_rules(value: dict[str, Any]) -> dict[str, Any]:
    max_adv = int(value.get("max_days_in_advance") or 0)
    if not (0 <= max_adv <= 3650):
        raise ValidationError({"max_days_in_advance": "Enter between 0 and 3650 days."})
    per_booking = int(value.get("max_per_booking") or 0)
    if not (1 <= per_booking <= 200):
        raise ValidationError({"max_per_booking": "Enter between 1 and 200 tickets per booking."})
    weekdays = value.get("available_weekdays") or list(_WEEKDAYS)
    weekdays = [d for d in weekdays if d in _WEEKDAYS]
    if not weekdays:
        raise ValidationError({"available_weekdays": "Choose at least one day."})
    return {
        "max_days_in_advance": max_adv,
        "max_per_booking": per_booking,
        "same_day_enabled": bool(value.get("same_day_enabled", True)),
        "min_lead_time_minutes": max(0, int(value.get("min_lead_time_minutes") or 0)),
        "cutoff_time": _hhmm(value.get("cutoff_time")),
        "available_weekdays": weekdays,
        "blackout_dates": [str(d) for d in (value.get("blackout_dates") or [])][:200],
    }


def _validate_advance_booking(value: dict[str, Any]) -> dict[str, Any]:
    opens = int(value.get("window_opens_days_before") or 0)
    if not (0 <= opens <= 3650):
        raise ValidationError({"window_opens_days_before": "Enter between 0 and 3650 days."})
    return {
        "window_opens_days_before": opens,
        "open_at_time": _hhmm(value.get("open_at_time")),
    }


def _validate_qr_access(value: dict[str, Any]) -> dict[str, Any]:
    grace = int(value.get("grace_minutes") or 0)
    if grace < 0:
        raise ValidationError({"grace_minutes": "Enter zero or more minutes."})
    return {
        "entry_start_time": _hhmm(value.get("entry_start_time")),
        "entry_cutoff_time": _hhmm(value.get("entry_cutoff_time")),
        "grace_minutes": grace,
    }


def _validate_reentry(value: dict[str, Any]) -> dict[str, Any]:
    window = int(value.get("window_minutes") or 0)
    if window < 0:
        raise ValidationError({"window_minutes": "Enter zero or more minutes."})
    return {
        "reentry_allowed": bool(value.get("reentry_allowed")),
        "window_minutes": window,
        "max_entries": max(1, int(value.get("max_entries") or 1)),
    }


def _validate_scanner(value: dict[str, Any]) -> dict[str, Any]:
    max_age = int(value.get("offline_cache_max_age_minutes") or 0)
    if max_age < 0:
        raise ValidationError({"offline_cache_max_age_minutes": "Enter zero or more minutes."})
    return {
        "offline_allowed": bool(value.get("offline_allowed", True)),
        "offline_cache_max_age_minutes": max_age,
        "audible_feedback": bool(value.get("audible_feedback", True)),
        "prompt_on_override": bool(value.get("prompt_on_override", True)),
    }


_ROUNDING_MODES = (
    "NONE", "NEAREST_1", "NEAREST_5", "NEAREST_10", "UP_1", "DOWN_1",
    "ROUND_UP", "ROUND_DOWN", "ROUND_HALF_UP",
)
# Increments the UI offers, in minor units (0.01, 0.05, 0.10, 0.25, 0.50, 1.00 major
# for a 2-decimal currency). The engine accepts any positive integer, but bounding
# the setting keeps a fat-finger from rounding a total to the nearest 1,000.
_ROUNDING_INCREMENTS_MINOR = (1, 5, 10, 25, 50, 100)


def _validate_rounding(value: dict[str, Any]) -> dict[str, Any]:
    mode = str(value.get("mode") or "NONE")
    if mode not in _ROUNDING_MODES:
        raise ValidationError({"mode": f"Choose one of: {', '.join(_ROUNDING_MODES)}."})
    out: dict[str, Any] = {"mode": mode}
    # The directional methods carry a configurable increment; nearest/legacy modes do
    # not (their step is fixed by name). Default one major unit (100 minor).
    if mode in ("ROUND_UP", "ROUND_DOWN", "ROUND_HALF_UP"):
        raw = value.get("increment_minor", 100)
        try:
            increment = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"increment_minor": "Enter a whole number of minor units."})
        if increment <= 0:
            raise ValidationError({"increment_minor": "The rounding increment must be greater than zero."})
        out["increment_minor"] = increment
    return out


def _validate_price_display(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol_first": bool(value.get("symbol_first", True)),
        "show_tax_inclusive_label": bool(value.get("show_tax_inclusive_label", True)),
        "thousands_separator": bool(value.get("thousands_separator", True)),
    }


_SUPPORTED_LANGUAGES = ("en", "th", "zh", "ja", "ru")


def _validate_languages(value: dict[str, Any]) -> dict[str, Any]:
    enabled = [c for c in (value.get("enabled") or []) if c in _SUPPORTED_LANGUAGES]
    if not enabled:
        raise ValidationError({"enabled": "Enable at least one language."})
    default = value.get("default") or enabled[0]
    if default not in enabled:
        raise ValidationError({"default": "The default language must be one of the enabled languages."})
    # Preserve caller order for display; dedupe.
    seen: list[str] = []
    for code in enabled:
        if code not in seen:
            seen.append(code)
    return {"enabled": seen, "default": default}


def _validate_numbering(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for doc in ("booking", "receipt", "tax_invoice", "credit_note"):
        row = value.get(doc) or {}
        prefix = str(row.get("prefix") or "").strip()[:12]
        if any(ch in prefix for ch in " /\\"):
            raise ValidationError({doc: "A prefix cannot contain spaces or slashes."})
        pad = int(row.get("pad") or 0)
        if not (0 <= pad <= 12):
            raise ValidationError({doc: "Padding must be between 0 and 12 digits."})
        out[doc] = {"prefix": prefix, "pad": pad}
    return out


def _validate_login_security(value: dict[str, Any]) -> dict[str, Any]:
    lockout = int(value.get("max_failed_logins") or 0)
    if not (1 <= lockout <= 20):
        raise ValidationError({"max_failed_logins": "Enter between 1 and 20 attempts."})
    idle = int(value.get("session_idle_minutes") or 0)
    if not (1 <= idle <= 1440):
        raise ValidationError({"session_idle_minutes": "Enter between 1 and 1440 minutes."})
    absolute = int(value.get("session_absolute_minutes") or 0)
    if not (idle <= absolute <= 43200):
        raise ValidationError(
            {"session_absolute_minutes": "The absolute limit must be at least the idle limit and at most 30 days."}
        )
    return {
        "max_failed_logins": lockout,
        "lockout_minutes": max(1, int(value.get("lockout_minutes") or 15)),
        "session_idle_minutes": idle,
        "session_absolute_minutes": absolute,
        "require_mfa_high_authority": bool(value.get("require_mfa_high_authority", True)),
    }


def _validate_advanced(value: dict[str, Any]) -> dict[str, Any]:
    # A small, explicit allow-list; the "advanced" page is not a raw JSON editor.
    return {
        "availability_cache_seconds": max(0, int(value.get("availability_cache_seconds") or 15)),
        "hold_duration_minutes": max(1, int(value.get("hold_duration_minutes") or 10)),
        "maintenance_mode": bool(value.get("maintenance_mode")),
    }


# --------------------------------------------------------------------------- #
# Secret handling for integration/API/webhook pages
#
# These pages hold credentials. A stored value must never contain a live secret in
# clear text and a read must never return one — the config store is read by staff
# who can view the page but should not be able to copy a gateway key or a webhook
# signing secret. So a secret is stored as {"last4": "…", "set": true} and the raw
# value is dropped after it is recorded elsewhere is out of scope here; what matters
# is that the config row is safe to read. A blank incoming secret means "leave the
# existing one", which is how an administrator edits other fields without re-typing
# a key they cannot see. This mirrors PaymentTypeService masking of provider config.
# --------------------------------------------------------------------------- #

_SECRET_SENTINEL = "••••••••"


def _mask_secret(raw: Any) -> dict[str, Any] | None:
    if raw in (None, "", _SECRET_SENTINEL):
        return None
    text = str(raw)
    return {"set": True, "last4": text[-4:] if len(text) >= 4 else "····", "length": len(text)}


def _validate_url(value: Any, field: str, *, allow_blank: bool = True) -> str | None:
    if value in (None, ""):
        if allow_blank:
            return None
        raise ValidationError({field: "Enter a URL."})
    text = str(value).strip()
    # HTTPS only for anything the platform will call out to — a plaintext callback
    # or webhook target leaks the payload and the signature (safety guardrail).
    if not text.lower().startswith("https://"):
        raise ValidationError({field: "Use an https:// URL."})
    if " " in text or len(text) > 500:
        raise ValidationError({field: "That does not look like a valid URL."})
    return text


def _validate_integrations(value: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    prev = previous or {}
    out: dict[str, Any] = {}
    for name in ("accounting", "crm", "marketing"):
        row = value.get(name) or {}
        enabled = bool(row.get("enabled"))
        endpoint = _validate_url(row.get("endpoint"), name)
        if enabled and not endpoint:
            raise ValidationError({name: "An enabled integration needs an https:// endpoint."})
        entry: dict[str, Any] = {"enabled": enabled, "endpoint": endpoint}
        # A newly supplied key is masked; a blank key keeps the stored one.
        secret = _mask_secret(row.get("api_key"))
        if secret is None:
            secret = (prev.get(name) or {}).get("api_key")
        if secret:
            entry["api_key"] = secret
        out[name] = entry
    return out


def _validate_api_config(value: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    prev_by_id = {c.get("id"): c for c in ((previous or {}).get("clients") or []) if c.get("id")}
    clients = []
    for row in (value.get("clients") or [])[:50]:
        name = str(row.get("name") or "").strip()[:60]
        if not name:
            raise ValidationError({"clients": "Each API client needs a name."})
        scopes = [s for s in (row.get("scopes") or []) if s in ("read", "write")]
        client_id = str(row.get("id") or "").strip() or None
        entry: dict[str, Any] = {
            "id": client_id,
            "name": name,
            "scopes": scopes or ["read"],
            "status": "ACTIVE" if row.get("status", "ACTIVE") == "ACTIVE" else "REVOKED",
        }
        secret = _mask_secret(row.get("key"))
        if secret is None and client_id in prev_by_id:
            secret = prev_by_id[client_id].get("key")
        if secret:
            entry["key"] = secret
        clients.append(entry)
    return {"clients": clients}


def _validate_webhooks(value: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    prev_by_id = {h.get("id"): h for h in ((previous or {}).get("hooks") or []) if h.get("id")}
    hooks = []
    known_events = ("booking.confirmed", "payment.captured", "ticket.scanned",
                    "booking.cancelled", "refund.completed")
    for row in (value.get("hooks") or [])[:50]:
        url = _validate_url(row.get("url"), "url", allow_blank=False)
        events = [e for e in (row.get("events") or []) if e in known_events]
        if not events:
            raise ValidationError({"events": "Choose at least one event to send."})
        hook_id = str(row.get("id") or "").strip() or None
        entry: dict[str, Any] = {
            "id": hook_id,
            "url": url,
            "events": events,
            "status": "ACTIVE" if row.get("status", "ACTIVE") == "ACTIVE" else "INACTIVE",
        }
        secret = _mask_secret(row.get("signing_secret"))
        if secret is None and hook_id in prev_by_id:
            secret = prev_by_id[hook_id].get("signing_secret")
        if secret:
            entry["signing_secret"] = secret
        hooks.append(entry)
    return {"hooks": hooks}


def _validate_notifications(value: dict[str, Any]) -> dict[str, Any]:
    flags = ("booking_confirmation", "payment_confirmation", "eticket", "reminder",
             "cancellation", "refund", "channel_email", "channel_sms")
    out = {f: bool(value.get(f)) for f in flags}
    if not (out["channel_email"] or out["channel_sms"]):
        raise ValidationError({"channel_email": "Enable at least one delivery channel."})
    return out


def _validate_payment_providers(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("promptpay", "card", "alipay", "wechat"):
        row = value.get(name) or {}
        entry: dict[str, Any] = {"enabled": bool(row.get("enabled"))}
        if row.get("provider"):
            entry["provider"] = str(row["provider"]).strip()[:40]
        out[name] = entry
    return out


def _validate_ticket_templates(value: dict[str, Any]) -> dict[str, Any]:
    delivery = str(value.get("qr_delivery") or "CID").upper()
    if delivery not in ("CID", "LINK", "DATA_URL"):
        raise ValidationError({"qr_delivery": "Choose CID, LINK or DATA_URL."})
    return {
        "eticket_enabled": bool(value.get("eticket_enabled", True)),
        "thermal_enabled": bool(value.get("thermal_enabled", True)),
        "qr_delivery": delivery,
    }


def _validate_seat_reservation(value: dict[str, Any]) -> dict[str, Any]:
    hold = int(value.get("hold_minutes") or 0)
    if not (1 <= hold <= 120):
        raise ValidationError({"hold_minutes": "Enter between 1 and 120 minutes."})
    max_seats = int(value.get("max_seats_per_booking") or 0)
    if not (1 <= max_seats <= 100):
        raise ValidationError({"max_seats_per_booking": "Enter between 1 and 100 seats."})
    return {
        "hold_minutes": hold,
        "prefer_adjacent": bool(value.get("prefer_adjacent", True)),
        "avoid_single_gaps": bool(value.get("avoid_single_gaps", True)),
        "max_seats_per_booking": max_seats,
    }


def _validate_partner_benefits(value: dict[str, Any]) -> dict[str, Any]:
    partners = []
    for row in (value.get("partners") or [])[:200]:
        code = str(row.get("code") or "").strip().upper()[:24]
        if not code or any(c in code for c in " /\\"):
            raise ValidationError({"partners": "Each partner needs a code without spaces or slashes."})
        discount = int(row.get("discount_bp") or 0)
        if not (0 <= discount <= 10000):
            raise ValidationError({code: "Discount must be between 0% and 100%."})
        commission = int(row.get("commission_bp") or 0)
        if not (0 <= commission <= 10000):
            raise ValidationError({code: "Commission must be between 0% and 100%."})
        partners.append({
            "code": code,
            "name": str(row.get("name") or code).strip()[:120],
            "kind": str(row.get("kind") or "AGENT").upper()[:24],
            "discount_bp": discount,
            "commission_bp": commission,
            "complimentary_allowance": max(0, int(row.get("complimentary_allowance") or 0)),
            "status": "ACTIVE" if row.get("status", "ACTIVE") == "ACTIVE" else "INACTIVE",
        })
    return {"partners": partners}


# --------------------------------------------------------------------------- #
# Registry — one row per config-backed settings page
# --------------------------------------------------------------------------- #

CONFIG_PAGES: tuple[ConfigPage, ...] = (
    ConfigPage(
        "Operating Hours", "venue.operating_hours", "VENUE",
        default={"days": {d: {"closed": d == "MON" and False, "open": "10:30", "close": "19:00",
                              "last_admission": "18:00"} for d in _WEEKDAYS}},
        validate=_validate_operating_hours,
        description="Daily opening, closing and last-admission times in the venue's own time zone.",
    ),
    ConfigPage(
        "Last Admission", "venue.last_admission", "VENUE",
        default={"cutoff_minutes_before_close": 60, "fixed_time": None},
        validate=_validate_last_admission, sensitive=True,
        description="How late a guest may still be admitted before closing.",
    ),
    ConfigPage(
        "Booking Rules", "venue.booking_rules", "VENUE",
        default={"max_days_in_advance": 90, "max_per_booking": 20, "same_day_enabled": True,
                 "min_lead_time_minutes": 0, "cutoff_time": None,
                 "available_weekdays": list(_WEEKDAYS), "blackout_dates": []},
        validate=_validate_booking_rules,
        description="Party-size limits, sales cutoff, sellable weekdays and blackout dates.",
    ),
    ConfigPage(
        "Advance Booking", "venue.advance_booking", "VENUE",
        default={"window_opens_days_before": 90, "open_at_time": None},
        validate=_validate_advance_booking,
        description="How far ahead a date becomes sellable and when the window opens.",
    ),
    ConfigPage(
        "QR Access Rules", "venue.qr_access_rules", "VENUE",
        default={"entry_start_time": None, "entry_cutoff_time": None, "grace_minutes": 0},
        validate=_validate_qr_access, sensitive=True,
        description="Entry window and grace period applied when a QR is scanned at a gate.",
    ),
    ConfigPage(
        "Re-entry Rules", "venue.reentry_rules", "VENUE",
        default={"reentry_allowed": False, "window_minutes": 0, "max_entries": 1},
        validate=_validate_reentry, sensitive=True,
        description="Whether a guest may leave and return, within what window, and how many times.",
    ),
    ConfigPage(
        "Scanner Configuration", "venue.scanner_config", "VENUE",
        default={"offline_allowed": True, "offline_cache_max_age_minutes": 720,
                 "audible_feedback": True, "prompt_on_override": True},
        validate=_validate_scanner,
        description="Scanner behaviour: offline allowance, audible feedback and override prompts.",
    ),
    ConfigPage(
        "Rounding", "venue.rounding", "VENUE",
        default={"mode": "NONE"},
        validate=_validate_rounding, sensitive=True,
        description="Rounding applied once, after tax, so a receipt reconciles with the charge.",
    ),
    ConfigPage(
        "Price Display", "venue.price_display", "VENUE",
        default={"symbol_first": True, "show_tax_inclusive_label": True, "thousands_separator": True},
        validate=_validate_price_display,
        description="How prices are shown: symbol placement, inclusive labelling, grouping.",
    ),
    ConfigPage(
        "Languages", "tenant.languages", "TENANT",
        default={"enabled": list(_SUPPORTED_LANGUAGES), "default": "en"},
        validate=_validate_languages,
        description="Languages offered to customers and the default for new visitors.",
    ),
    ConfigPage(
        "Numbering", "tenant.numbering", "TENANT",
        default={"booking": {"prefix": "AQ", "pad": 6}, "receipt": {"prefix": "RC", "pad": 6},
                 "tax_invoice": {"prefix": "INV", "pad": 6}, "credit_note": {"prefix": "CN", "pad": 6}},
        validate=_validate_numbering,
        description="Number formats for bookings, receipts, tax invoices and credit notes.",
    ),
    ConfigPage(
        "Login Security", "tenant.login_security", "TENANT",
        default={"max_failed_logins": 5, "lockout_minutes": 15, "session_idle_minutes": 30,
                 "session_absolute_minutes": 720, "require_mfa_high_authority": True},
        validate=_validate_login_security, action="MANAGE_LOGIN_SECURITY", sensitive=True,
        description="Lockout threshold, session timeouts and multi-factor policy.",
    ),
    ConfigPage(
        "Advanced Configuration", "tenant.advanced", "TENANT",
        default={"availability_cache_seconds": 15, "hold_duration_minutes": 10, "maintenance_mode": False},
        validate=_validate_advanced, action="MANAGE_INTEGRATION", sensitive=True,
        description="Low-level switches; change only with support guidance.",
    ),
    # --- system integrations (Bucket C): credentials never stored or returned in
    #     clear text; each is gated by MANAGE_INTEGRATION on top of the page EDIT. ---
    ConfigPage(
        "Integrations", "tenant.integrations", "TENANT",
        default={"accounting": {"enabled": False, "endpoint": None},
                 "crm": {"enabled": False, "endpoint": None},
                 "marketing": {"enabled": False, "endpoint": None}},
        validate=_validate_integrations, action="MANAGE_INTEGRATION", sensitive=True, has_secrets=True,
        description="Outbound connections to accounting, CRM and marketing systems.",
    ),
    ConfigPage(
        "API Configuration", "tenant.api_clients", "TENANT",
        default={"clients": []},
        validate=_validate_api_config, action="MANAGE_INTEGRATION", sensitive=True, has_secrets=True,
        description="API clients and their scopes. Keys are shown only when first created.",
    ),
    ConfigPage(
        "Webhooks", "tenant.webhooks", "TENANT",
        default={"hooks": []},
        validate=_validate_webhooks, action="MANAGE_INTEGRATION", sensitive=True, has_secrets=True,
        description="Endpoints notified of platform events, with per-hook signing secrets.",
    ),
    # Partner benefits are venue-configurable commercial terms, not credentials.
    ConfigPage(
        "Partner Benefits", "venue.partner_benefits", "VENUE",
        default={"partners": []},
        validate=_validate_partner_benefits, action="APPLY_PARTNER_DISCOUNT", sensitive=True,
        description="Partner-specific discounts, commission and complimentary allowances.",
    ),
    # Which transactional messages are sent, on which channel (R36, R37). The
    # templates themselves are records (Email Templates page); this toggles delivery.
    ConfigPage(
        "Customer Notifications", "venue.notifications", "VENUE",
        default={"booking_confirmation": True, "payment_confirmation": True, "eticket": True,
                 "reminder": True, "cancellation": True, "refund": True,
                 "channel_email": True, "channel_sms": False},
        validate=_validate_notifications,
        description="Which transactional messages are sent to customers, and on which channel.",
    ),
    # Payment provider connections. Credentials live in the secret store; this holds
    # the non-secret connection settings and which providers are enabled.
    ConfigPage(
        "Payment Providers", "venue.payment_providers", "VENUE",
        default={"promptpay": {"enabled": True}, "card": {"enabled": True, "provider": "SIMULATED"},
                 "alipay": {"enabled": False}, "wechat": {"enabled": False}},
        validate=_validate_payment_providers, action="MANAGE_PAYMENT_PROVIDER_CONFIG", sensitive=True,
        description="Which payment providers are connected and their non-secret settings.",
    ),
    # Which ticket layouts are active. The layout designs are code
    # (utp/ticketdesign); this selects and toggles them per venue.
    ConfigPage(
        "Ticket Templates", "venue.ticket_templates", "VENUE",
        default={"eticket_enabled": True, "thermal_enabled": True, "qr_delivery": "CID"},
        validate=_validate_ticket_templates,
        description="Which ticket layouts are active and how the QR is delivered.",
    ),
    # Reserved-seating hold and release policy (venue-local).
    ConfigPage(
        "Seat Reservation Rules", "venue.seat_reservation_rules", "VENUE",
        default={"hold_minutes": 10, "prefer_adjacent": True, "avoid_single_gaps": True,
                 "max_seats_per_booking": 10},
        validate=_validate_seat_reservation,
        description="Hold duration, adjacency preference and per-booking seat limits.",
    ),
)

CONFIG_PAGES_BY_KEY: dict[str, ConfigPage] = {p.page: p for p in CONFIG_PAGES}


class SettingsConfigService:
    """Read/write the config-backed settings pages, uniformly and safely."""

    def __init__(
        self,
        db: Any,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config

    # ------------------------------------------------------------------ #

    def _page(self, page: str) -> ConfigPage:
        definition = CONFIG_PAGES_BY_KEY.get(page)
        if definition is None:
            raise NotFound()
        return definition

    def _scope_args(self, definition: ConfigPage, *, venue_id: str, organization_id: str | None) -> dict[str, Any]:
        if definition.scope == "VENUE":
            return {"scope_type": "VENUE", "scope_id": venue_id}
        if definition.scope == "ORGANIZATION":
            return {"scope_type": "ORGANIZATION", "scope_id": organization_id}
        return {"scope_type": "TENANT", "scope_id": None}

    def can_edit(self, ctx: RequestContext, page: str) -> bool:
        definition = self._page(page)
        if not self.authz.can_page(ctx, page, "EDIT"):
            return False
        if definition.action:
            return self.authz.can_action(ctx, definition.action)
        return True

    def get(
        self, ctx: RequestContext, page: str, *, venue_id: str, organization_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve the current value for a page, plus provenance and edit rights.

        Reads require only the page's ``VIEW`` — a principal who can open the page
        can read its value. The returned ``scope`` and ``inherited`` let the UI tell
        an operator whether they are looking at a venue override or a tenant default
        (§34, §37).
        """
        definition = self._page(page)
        self.authz.require_page(ctx.for_venue(venue_id), page, "VIEW")
        resolved = self.config.resolve(
            ctx,
            definition.key,
            venue_id=venue_id if definition.scope == "VENUE" else None,
            organization_id=organization_id if definition.scope == "ORGANIZATION" else None,
            default=definition.default,
        )
        # Merge over the default so a value stored before a field was added still
        # renders every control.
        value = {**definition.default, **(resolved.value or {})} if isinstance(resolved.value, dict) else resolved.value
        return {
            "page": page,
            "key": definition.key,
            "scope": definition.scope,
            "value": value,
            "default": definition.default,
            "inherited": resolved.is_platform_default,
            "version": resolved.version,
            "sensitive": definition.sensitive,
            "can_edit": self.can_edit(ctx, page),
        }

    def set(
        self,
        ctx: RequestContext,
        page: str,
        value: dict[str, Any],
        *,
        venue_id: str,
        organization_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Validate and persist a page's value, audited, permission- and scope-checked.

        Order mirrors the hand-written settings mutators: venue scope → page EDIT →
        optional MANAGE_* action (with its mandatory reason) → validate → versioned
        write → audit with old and new value.
        """
        definition = self._page(page)
        vctx = ctx.for_venue(venue_id)
        self.authz.require_page(vctx, page, "EDIT")
        if definition.action:
            self.authz.require_action(
                vctx, definition.action, reason=reason, target_type="settings_page", target_id=page
            )
        scope = self._scope_args(definition, venue_id=venue_id, organization_id=organization_id)
        previous = self.config.resolve(
            ctx,
            definition.key,
            venue_id=venue_id if definition.scope == "VENUE" else None,
            organization_id=organization_id if definition.scope == "ORGANIZATION" else None,
            default=None,
            use_platform_default=False,
        ).value
        if not definition.validate:
            clean = dict(value or {})
        elif definition.has_secrets:
            # Credential pages need the stored value so a blank secret keeps the one
            # on file (the caller was never shown it).
            clean = definition.validate(value, previous or {})
        else:
            clean = definition.validate(value)
        # ConfigStore.set already versions and audits CONFIG_CHANGE; we add a second,
        # page-named audit line so the settings audit reads in business terms and
        # carries the reason and scope the spec asks for (§54, §55).
        self.config.set(vctx, definition.key, clean, **scope)
        self.audit.record(
            vctx,
            "CONFIG_CHANGE",
            target_type="settings_page",
            target_id=f"{page}:{scope['scope_type']}:{scope['scope_id'] or '-'}",
            previous={"value": previous} if previous is not None else None,
            new={"value": clean},
            reason=reason,
        )
        return self.get(ctx, page, venue_id=venue_id, organization_id=organization_id)

    def overview(
        self, ctx: RequestContext, *, venue_id: str, organization_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Every config-backed page the principal may VIEW, keyed by page.

        Used by the settings overview so the client can render any of these pages
        without a round-trip each. Pages the principal cannot view are omitted, the
        same rule the navigation and search follow (§14, §26).
        """
        out: dict[str, dict[str, Any]] = {}
        for definition in CONFIG_PAGES:
            if not self.authz.can_page(ctx.for_venue(venue_id), definition.page, "VIEW"):
                continue
            out[definition.page] = self.get(
                ctx, definition.page, venue_id=venue_id, organization_id=organization_id
            )
        return out


__all__ = ["ConfigPage", "CONFIG_PAGES", "CONFIG_PAGES_BY_KEY", "SettingsConfigService"]
