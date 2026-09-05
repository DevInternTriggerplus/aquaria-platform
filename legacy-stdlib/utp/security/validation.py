"""A03 Injection, and the input side of A04.

Three jobs:

1. **Allow-list request validation** (R73.6). A request is validated against an
   explicit field schema and *unexpected fields are rejected*, not ignored. Ignoring
   them is how mass-assignment bugs happen; rejecting them makes the API's contract
   enforceable.
2. **Safe SQL identifiers.** Values are always bound as parameters, never
   interpolated — but a few internal helpers do interpolate a *table* or *column*
   name. Those names must come from a fixed allow-list, which :func:`safe_identifier`
   enforces so no caller can pass an attacker-influenced string.
3. **Output encoding.** Context-aware escaping for HTML, attributes, JS strings and
   URLs, so encoding is chosen by destination rather than by habit.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
from urllib.parse import quote

from ..core.errors import ValidationError

FieldType = Literal["string", "int", "bool", "date", "time", "email", "enum", "list", "dict", "money", "id"]

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_ID_VALUE = re.compile(r"^[a-z0-9]+_[a-z0-9]{1,40}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
# Deliberately permissive on the local part, strict on structure. Full RFC 5322 is
# not the goal; rejecting anything that cannot be an address is.
_EMAIL = re.compile(r"^[^@\s,;<>\"]{1,64}@[A-Za-z0-9.-]{1,251}\.[A-Za-z]{2,24}$")

#: Characters that must never reach a log line, header, CSV cell or stored value.
#: Newlines and carriage returns are included deliberately: they are the log-forging
#: and header-splitting vector, so a "harmless whitespace" exemption for them would
#: defeat the control.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_BREAKS = re.compile(r"[\r\n\t\u2028\u2029]+")

#: Leading characters that turn a spreadsheet cell into a formula (CSV injection).
_CSV_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")

MAX_STRING_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class Field:
    """One expected request field."""

    name: str
    type: FieldType = "string"
    required: bool = False
    max_length: int | None = None
    min_value: int | None = None
    max_value: int | None = None
    choices: tuple[str, ...] = ()
    item_type: FieldType | None = None
    default: Any = None
    message: str | None = None

    def error(self, detail: str) -> str:
        return self.message or detail


class Schema:
    """An allow-list of fields. Anything else is rejected (R73.6, mass-assignment)."""

    def __init__(self, *fields: Field, allow_extra: bool = False) -> None:
        self.fields = {field.name: field for field in fields}
        self.allow_extra = allow_extra

    def validate(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        data = dict(payload or {})
        errors: dict[str, str] = {}
        if not self.allow_extra:
            unexpected = sorted(set(data) - set(self.fields))
            for name in unexpected:
                errors[name] = "This field is not accepted."
        out: dict[str, Any] = {}
        for name, field in self.fields.items():
            present = name in data and data[name] not in (None, "")
            if not present:
                if field.required:
                    errors[name] = field.error("This field is required.")
                elif field.default is not None:
                    out[name] = field.default
                continue
            try:
                out[name] = _coerce(field, data[name])
            except ValueError as exc:
                errors[name] = field.error(str(exc))
        if errors:
            raise ValidationError(errors)
        return out


def _coerce(field: Field, value: Any) -> Any:
    kind = field.type
    if kind == "string":
        text = normalize_text(value, max_length=field.max_length or MAX_STRING_LENGTH)
        if field.choices and text not in field.choices:
            raise ValueError(f"Choose one of: {', '.join(field.choices)}.")
        return text
    if kind == "enum":
        text = normalize_text(value, max_length=128)
        if text not in field.choices:
            raise ValueError(f"Choose one of: {', '.join(field.choices)}.")
        return text
    if kind in ("int", "money"):
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a whole number.") from exc
        if field.min_value is not None and number < field.min_value:
            raise ValueError(f"Enter {field.min_value} or more.")
        if field.max_value is not None and number > field.max_value:
            raise ValueError(f"Enter {field.max_value} or less.")
        if kind == "money" and number < 0:
            raise ValueError("An amount cannot be negative.")
        return number
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError("Enter true or false.")
    if kind == "date":
        text = normalize_text(value, max_length=10)
        if not _DATE.match(text):
            raise ValueError("Use the format YYYY-MM-DD.")
        return text
    if kind == "time":
        text = normalize_text(value, max_length=8)
        if not _TIME.match(text):
            raise ValueError("Use the format HH:MM.")
        return text
    if kind == "email":
        text = normalize_text(value, max_length=320).lower()
        if not _EMAIL.match(text):
            raise ValueError("Enter a valid email address.")
        return text
    if kind == "id":
        text = normalize_text(value, max_length=64)
        if not _ID_VALUE.match(text):
            raise ValueError("That identifier is not valid.")
        return text
    if kind == "list":
        if not isinstance(value, (list, tuple)):
            raise ValueError("Provide a list.")
        item_field = Field(name=field.name, type=field.item_type or "string", choices=field.choices)
        return [_coerce(item_field, item) for item in value]
    if kind == "dict":
        if not isinstance(value, Mapping):
            raise ValueError("Provide an object.")
        return dict(value)
    raise ValueError("Unsupported field type.")


def normalize_text(value: Any, *, max_length: int = MAX_STRING_LENGTH) -> str:
    """Normalize and bound a string before it is stored or compared.

    NFC normalization first: without it, two visually identical strings can compare
    unequal, which breaks allow-list checks and lets a lookalike slip past a
    denylist. Control characters are then stripped so nothing can forge a log line
    or split a header.
    """
    text = unicodedata.normalize("NFC", str(value))
    text = _CONTROL_CHARS.sub("", text)
    # Collapse rather than delete, so "Somchai\nJaidee" does not become one word.
    text = _LINE_BREAKS.sub(" ", text).strip()
    if len(text) > max_length:
        raise ValueError(f"Keep this to {max_length} characters or fewer.")
    return text


#: Tables the internal helpers may name in interpolated SQL. Kept explicit so a
#: dynamic table name can only ever be one of these.
ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "tenants",
        "organizations",
        "brands",
        "venue_types",
        "venues",
        "areas",
        "access_points",
        "devices",
        "config_values",
        "customer_segments",
        "experiences",
        "products",
        "product_components",
        "ticket_types",
        "price_rules",
        "booking_rules",
        "operating_calendar",
        "sessions",
        "session_allocations",
        "holds",
        "session_patterns",
        "schedule_overrides",
        "waiting_list",
        "seat_layouts",
        "seat_layout_versions",
        "layout_element_types",
        "layout_elements",
        "seat_price_categories",
        "seat_types",
        "seat_zones",
        "seats",
        "seat_holds",
        "seat_reservations",
        "seat_blocks",
        "customers",
        "customer_pii",
        "privacy_notice_versions",
        "consent_records",
        "consent_withdrawals",
        "dsar_requests",
        "breach_incidents",
        "bookings",
        "booking_items",
        "payments",
        "payment_events",
        "refunds",
        "tickets",
        "scan_events",
        "shift_sessions",
        "document_sequences",
        "receipts",
        "tax_invoices",
        "promotions",
        "promotion_redemptions",
        "staff",
        "roles",
        "role_permissions",
        "role_assignments",
        "auth_sessions",
        "audit_events",
        "partners",
        "notification_templates",
        "notification_messages",
        "notification_suppressions",
        "exceptions_log",
        "visit_plan_entries",
        "rate_limit_counters",
        "verification_challenges",
        "offline_caches",
        "media_assets",
    }
)


def safe_identifier(name: str, *, allowed: Iterable[str] | None = None, kind: str = "identifier") -> str:
    """Validate a table or column name destined for interpolated SQL.

    Values are always bound as parameters; only structural names ever reach the SQL
    text, and only through here.
    """
    text = str(name)
    if not _IDENTIFIER.match(text):
        raise ValidationError(
            {kind: "Invalid name."},
            message="That request could not be processed.",
            log_detail=f"rejected {kind}: {text!r}",
        )
    permitted = frozenset(allowed) if allowed is not None else ALLOWED_TABLES
    if text not in permitted:
        raise ValidationError(
            {kind: "Invalid name."},
            message="That request could not be processed.",
            log_detail=f"{kind} not in allow-list: {text!r}",
        )
    return text


def safe_table(name: str) -> str:
    return safe_identifier(name, allowed=ALLOWED_TABLES, kind="table")


def safe_order_by(column: str, allowed: Sequence[str], *, direction: str = "ASC") -> str:
    """Build an ORDER BY fragment from an allow-list (sort-parameter injection)."""
    safe_column = safe_identifier(column, allowed=allowed, kind="sort")
    order = "DESC" if str(direction).upper() == "DESC" else "ASC"
    return f"{safe_column} {order}"


# --------------------------------------------------------------------------- #
# Output encoding
# --------------------------------------------------------------------------- #


def encode_html(value: Any) -> str:
    """Escape for HTML text content."""
    return html.escape("" if value is None else str(value), quote=False)


def encode_attribute(value: Any) -> str:
    """Escape for an HTML attribute value, quotes included."""
    return html.escape("" if value is None else str(value), quote=True)


def encode_js_string(value: Any) -> str:
    """Serialize for embedding inside a script context.

    ``json.dumps`` handles quoting and escapes; the extra replacements close the
    ``</script>`` and HTML-comment escape hatches that JSON alone leaves open.
    """
    encoded = json.dumps("" if value is None else str(value))
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def encode_url_component(value: Any) -> str:
    return quote("" if value is None else str(value), safe="")


def encode_csv_cell(value: Any) -> str:
    """Neutralize CSV/formula injection in exports (R41.7 exports, R71.9).

    A cell beginning with ``=``, ``+``, ``-`` or ``@`` is executed as a formula by
    common spreadsheet software, which turns a benign export into code execution on
    the analyst's machine. Prefixing with an apostrophe keeps the value readable and
    inert.
    """
    text = "" if value is None else str(value)
    # Line breaks are stripped first: an embedded newline would otherwise inject a
    # whole extra row into the export.
    text = _LINE_BREAKS.sub(" ", _CONTROL_CHARS.sub("", text))
    if text.lstrip().startswith(_CSV_FORMULA_PREFIX):
        return f"'{text}"
    return text


def sanitize_log_value(value: Any) -> str:
    """Strip newlines and control characters so log lines cannot be forged."""
    return _LINE_BREAKS.sub(" ", _CONTROL_CHARS.sub("", str(value)))


def redact_middle(value: str, *, keep_start: int = 2, keep_end: int = 2) -> str:
    """Partially reveal a value for support purposes without exposing it."""
    text = str(value or "")
    if len(text) <= keep_start + keep_end:
        return "•" * len(text)
    return f"{text[:keep_start]}{'•' * (len(text) - keep_start - keep_end)}{text[-keep_end:]}"


__all__ = [
    "ALLOWED_TABLES",
    "MAX_STRING_LENGTH",
    "Field",
    "FieldType",
    "Schema",
    "encode_attribute",
    "encode_csv_cell",
    "encode_html",
    "encode_js_string",
    "encode_url_component",
    "normalize_text",
    "redact_middle",
    "safe_identifier",
    "safe_order_by",
    "safe_table",
    "sanitize_log_value",
]
