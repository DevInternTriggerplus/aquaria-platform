"""HTTP server and router.

Built on ``http.server`` so the platform runs with no third-party web framework. It is
threaded, which matters: the concurrency guarantees in the capacity engine are only
meaningful if requests can actually overlap.

Every response passes through the security layer — headers, CSRF, error mapping — and
every mutating endpoint calls the same service methods the tests exercise. There is no
"API-only" shortcut anywhere.
"""

from __future__ import annotations

import json
import mimetypes
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
# Aliased: a route handler in this module is already named ``quote``.
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote as url_quote

from ..app import Platform
from ..core.context import Principal, RequestContext
from ..core.db import decode
from ..domain import permissions as perms
from ..core.errors import AuthenticationRequired, NotFound, PlatformError, ValidationError
from ..core.i18n import localize_error
from ..core.ids import new_correlation_id, secure_token
from ..security import csrf as csrf_module
from ..security.headers import CSRF_COOKIE, SESSION_COOKIE, Profile
from .record_crud import record_page

Handler = Callable[["Request"], "Response"]

WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "web"

#: Anonymous browser sessions, held in memory. A real deployment uses a shared store;
#: for a single-process demo server this keeps the CSRF binding honest without adding
#: infrastructure.
_BROWSER_SESSIONS: dict[str, dict[str, Any]] = {}


class Request:
    """Parsed request plus the resolved principal and context."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: Any,
        body: bytes,
        params: dict[str, str],
        platform: Platform,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.raw_body = body
        self.params = params
        self.platform = platform
        self.correlation_id = headers.get("X-Correlation-Id") or new_correlation_id()
        self.cookies = _parse_cookies(headers.get("Cookie", ""))
        self.browser_session_id = self.cookies.get("utp_session")
        self.issued_session = False
        if not self.browser_session_id or self.browser_session_id not in _BROWSER_SESSIONS:
            self.browser_session_id = secure_token(18)
            _BROWSER_SESSIONS[self.browser_session_id] = {}
            self.issued_session = True

    # ------------------------------------------------------------------ #

    def json(self) -> dict[str, Any]:
        if not self.raw_body:
            return {}
        try:
            payload = json.loads(self.raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(
                {"body": "Send a valid JSON object."}, message="That request could not be read."
            ) from exc
        if not isinstance(payload, dict):
            raise ValidationError({"body": "Send a JSON object."})
        return payload

    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def state(self) -> dict[str, Any]:
        return _BROWSER_SESSIONS.setdefault(self.browser_session_id, {})

    @property
    def language(self) -> str:
        """Resolve the request language to one the platform ships (update spec §1).

        Matches the primary subtag (``zh-CN`` → ``zh``) against the built-in set, so
        the five supported languages all resolve; anything else falls back to English
        (R33). Previously this only ever returned ``th`` or ``en``, which silently
        collapsed Chinese, Japanese and Russian to English.
        """
        from ..core.i18n import BUILTIN_LANGUAGES, DEFAULT_LANGUAGE

        requested = self.q("lang") or self.headers.get("Accept-Language", "") or ""
        primary = str(requested).strip().lower().split(",")[0].split("-")[0]
        return primary if primary in BUILTIN_LANGUAGES else DEFAULT_LANGUAGE


class Response:
    """A JSON or file response."""

    def __init__(
        self,
        body: Any = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json; charset=utf-8",
        profile: Profile = "API",
    ) -> None:
        self.status = status
        self.extra_headers = dict(headers or {})
        self.content_type = content_type
        self.profile = profile
        if raw is not None:
            self.payload = raw
        else:
            self.payload = json.dumps(body if body is not None else {}, default=str).encode("utf-8")


class ApiApplication:
    """Router plus the security middleware."""

    def __init__(self, platform: Platform, *, tenant_id: str, venue_id: str) -> None:
        self.platform = platform
        self.tenant_id = tenant_id
        self.venue_id = venue_id
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self._register()

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def decorate(handler: Handler) -> Handler:
            self.routes.append((method.upper(), regex, handler))
            return handler

        return decorate

    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        for route_method, regex, handler in self.routes:
            match = regex.match(path)
            if match and route_method == method.upper():
                return handler, match.groupdict()
        return None

    def path_exists(self, path: str) -> bool:
        return any(regex.match(path) for _m, regex, _h in self.routes)

    # ------------------------------------------------------------------ #
    # Ticket print authorisation
    # ------------------------------------------------------------------ #

    def _grant_ticket_access(self, request: Request, ticket_ids: list[str]) -> None:
        """Record that this browser has proven ownership of these tickets.

        Either by having just completed the purchase, or by having verified a
        one-time code in Manage Booking.
        """
        granted = set(request.state.get("ticket_access") or [])
        granted.update(tid for tid in ticket_ids if tid)
        request.state["ticket_access"] = sorted(granted)

    def _authorized_ticket(self, request: Request) -> str:
        """Resolve the ticket id from the path, refusing anything not granted.

        A ticket carries an access credential, so it must not be reachable by
        guessing an identifier. An unauthorised id gets the same not-found answer
        as a non-existent one, so the endpoint cannot confirm whether a ticket
        exists (R1.2, R16.3).
        """
        ticket_id = request.params.get("ticket_id", "")
        granted = set(request.state.get("ticket_access") or [])
        if not ticket_id or ticket_id not in granted:
            raise NotFound()
        return ticket_id

    # ------------------------------------------------------------------ #
    # Settings record readers
    #
    # A handful of settings pages list rows from tables that have a full write
    # service but no dedicated list method (templates, devices, shows). These read
    # them directly, masking nothing sensitive, purely so the settings screen can
    # display them. Every caller has already passed the page's VIEW check.
    # ------------------------------------------------------------------ #

    def _list_templates(self, platform: Platform, venue_id: str) -> list[dict[str, Any]]:
        rows = platform.db.query(
            "SELECT id, event_type, language, version, subject, state, created_at "
            "FROM notification_templates WHERE tenant_id = ? AND (venue_id = ? OR venue_id IS NULL) "
            "ORDER BY event_type, language, version DESC",
            (self.tenant_id, venue_id),
        )
        return [dict(r) for r in rows]

    def _list_devices(self, platform: Platform, venue_id: str, page: str) -> list[dict[str, Any]]:
        # POS Devices / Printers / Gate Devices / Kiosks filter the one devices table
        # by kind so each page shows only its own fleet; "Devices" shows all.
        kind_filter = {
            "Kiosks": ("KIOSK",),
            "POS Devices": ("POS", "COUNTER"),
            "Printers": ("PRINTER",),
            "Gate Devices": ("SCANNER", "GATE", "TURNSTILE"),
        }.get(page)
        rows = platform.db.query(
            "SELECT id, code, name_json, kind, channel, status, last_seen_at, access_point_id "
            "FROM devices WHERE tenant_id = ? AND venue_id = ? ORDER BY code",
            (self.tenant_id, venue_id),
        )
        out = []
        for r in rows:
            record = dict(r)
            if kind_filter and record.get("kind") not in kind_filter:
                continue
            record["name"] = decode(record.pop("name_json", None), {})
            out.append(record)
        return out

    def _list_shows(self, platform: Platform, venue_id: str) -> list[dict[str, Any]]:
        rows = platform.db.query(
            "SELECT id, code, name_json, kind, status FROM experiences "
            "WHERE tenant_id = ? AND kind = 'SHOW' ORDER BY code",
            (self.tenant_id,),
        )
        out = []
        for r in rows:
            record = dict(r)
            record["name"] = decode(record.pop("name_json", None), {})
            out.append(record)
        return out

    def _decoded(self, rows: Any, *name_fields: str) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            record = dict(r)
            for field in name_fields:
                json_field = f"{field}_json"
                if json_field in record:
                    record[field] = decode(record.pop(json_field), {})
            out.append(record)
        return out

    def _list_records(self, platform: Platform, page: str) -> list[dict[str, Any]] | None:
        """Read the rows for a record-collection settings page, uniformly.

        Returns ``None`` for a page that is not a record collection, so the caller
        answers 404 the same way an unknown page would. Reads are direct table
        queries scoped to this tenant/venue; the page's VIEW permission has already
        been enforced by the caller.
        """
        db = platform.db
        tid = self.tenant_id
        vid = self.venue_id
        if page == "Ticket Types":
            return self._decoded(db.query(
                "SELECT tt.id, tt.code, tt.name_json, tt.status, tt.entry_allowance, tt.display_order, "
                "p.name_json AS product_name_json FROM ticket_types tt "
                "JOIN products p ON p.id = tt.product_id AND p.tenant_id = tt.tenant_id "
                "WHERE tt.tenant_id = ? AND p.venue_id = ? ORDER BY tt.display_order, tt.code",
                (tid, vid)), "name", "product_name")
        if page == "Customer Segments":
            return self._decoded(db.query(
                "SELECT id, code, name_json, proof_required, display_order, status FROM customer_segments "
                "WHERE tenant_id = ? ORDER BY display_order, code", (tid,)), "name")
        if page == "Products":
            return self._decoded(db.query(
                "SELECT id, code, name_json, admission_model, status, display_order FROM products "
                "WHERE tenant_id = ? AND venue_id = ? ORDER BY display_order, code", (tid, vid)), "name")
        if page == "Experiences":
            return self._decoded(db.query(
                "SELECT id, code, name_json, kind, category, status FROM experiences "
                "WHERE tenant_id = ? AND venue_id = ? ORDER BY code", (tid, vid)), "name")
        if page == "Pricing":
            return [dict(r) for r in db.query(
                "SELECT pr.id, pr.currency, pr.amount_minor, pr.priority, pr.channel, pr.status, "
                "tt.code AS ticket_type_code FROM price_rules pr "
                "JOIN ticket_types tt ON tt.id = pr.ticket_type_id AND tt.tenant_id = pr.tenant_id "
                "WHERE pr.tenant_id = ? ORDER BY pr.priority DESC, tt.code", (tid,))]
        if page in ("Promotions", "Coupon Codes", "Cash Coupons", "Member Rewards"):
            # Each promotions-family page filters the shared promotions table by the
            # shape it manages: coupon pages need a code, cash coupons are stored
            # value, member rewards are point/gift mechanics. (Partner Benefits is a
            # config-backed page of commercial terms, handled by SettingsConfigService.)
            where = "tenant_id = ?"
            params: list[Any] = [tid]
            if page == "Coupon Codes":
                where += " AND code IS NOT NULL AND code <> ''"
            elif page == "Cash Coupons":
                where += " AND accounting_treatment IN ('STORED_VALUE','PAYMENT','LIABILITY')"
            elif page == "Member Rewards":
                where += " AND mechanic IN ('MEMBER_POINTS','FREE_GIFT')"
            return self._decoded(db.query(
                f"SELECT id, code, internal_code, name_json, mechanic, accounting_treatment, "
                f"usage_limit, usage_count, budget_minor, status FROM promotions "
                f"WHERE {where} ORDER BY priority DESC, internal_code", params), "name")
        if page == "Email Templates":
            return self._list_templates(platform, vid)
        if page in ("Gates", "Access Points"):
            return self._decoded(db.query(
                "SELECT id, code, name_json, kind, direction, status FROM access_points "
                "WHERE tenant_id = ? AND venue_id = ? ORDER BY code", (tid, vid)), "name")
        if page in ("Kiosks", "POS Devices", "Printers", "Gate Devices", "Devices"):
            return self._list_devices(platform, vid, page)
        if page in ("Shows", "Show Schedule"):
            return self._list_shows(platform, vid)
        if page == "Staff":
            return [dict(r) for r in db.query(
                "SELECT id, display_name, email, status, mfa_required, last_login_at FROM staff "
                "WHERE tenant_id = ? ORDER BY display_name", (tid,))]
        if page == "Roles":
            return [dict(r) for r in db.query(
                "SELECT id, code, name, authority_level, status FROM roles "
                "WHERE tenant_id = ? ORDER BY authority_level DESC, code", (tid,))]
        if page == "Venues":
            return self._decoded(db.query(
                "SELECT id, code, name_json, timezone, currency, status FROM venues "
                "WHERE tenant_id = ? ORDER BY code", (tid,)), "name")
        if page in ("Organization",):
            return [dict(r) for r in db.query(
                "SELECT id, code, name, legal_name, tax_id, country, status FROM organizations "
                "WHERE tenant_id = ? ORDER BY code", (tid,))]
        if page == "Brand":
            return self._decoded(db.query(
                "SELECT id, code, name, status FROM brands WHERE tenant_id = ? ORDER BY code", (tid,)))
        if page == "Terms & Conditions":
            return [dict(r) for r in db.query(
                "SELECT id, version, language, published_at FROM privacy_notice_versions "
                "WHERE tenant_id = ? ORDER BY published_at DESC", (tid,))]
        if page == "Seat Type":
            return [dict(r) for r in db.query(
                "SELECT id, code, name, colour, shape, sellable, accessible, status FROM seat_types "
                "WHERE tenant_id = ? ORDER BY display_priority, code", (tid,))]
        if page == "Seat Zone":
            return self._decoded(db.query(
                "SELECT id, code, name_json, colour, zone_kind, capacity FROM seat_zones "
                "WHERE tenant_id = ? ORDER BY display_order, code", (tid,)), "name")
        if page == "Seat Layout":
            return [dict(r) for r in db.query(
                "SELECT id, code, name, is_template, status, created_at FROM seat_layouts "
                "WHERE tenant_id = ? AND venue_id = ? ORDER BY code", (tid, vid))]
        if page == "Areas":
            return self._decoded(db.query(
                "SELECT id, code, name_json, status FROM areas "
                "WHERE tenant_id = ? AND venue_id = ? ORDER BY code", (tid, vid)), "name")
        if page in ("Capacity", "Time Slots"):
            # Both surface the sessions table; Time Slots is product sessions, Capacity
            # shows the capacity/confirmed of each. Recent, forward-looking rows.
            kind = "PRODUCT" if page == "Time Slots" else None
            sql = ("SELECT id, date, start_time, end_time, capacity, confirmed, status, kind "
                   "FROM sessions WHERE tenant_id = ? AND venue_id = ?")
            params: list[Any] = [tid, vid]
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            sql += " ORDER BY date DESC, start_time LIMIT 100"
            return [dict(r) for r in db.query(sql, params)]
        if page == "Audit Logs":
            return [dict(r) for r in db.query(
                "SELECT id, action, target_type, actor_role, severity, at_local FROM audit_events "
                "WHERE tenant_id = ? ORDER BY at_utc DESC LIMIT 100", (tid,))]
        if page == "Permissions":
            # The registry itself: every grantable page and its verbs. Read-only — it
            # is the map of what can be assigned, edited on the Roles screen.
            return [
                {"page": pg.key, "group": pg.group,
                 "verbs": ", ".join(v for v in ("VIEW", "ADD", "EDIT", "DELETE") if v in pg.verbs),
                 "protected": bool(pg.protected)}
                for pg in perms.PAGES
            ]
        return None

    def _record_field_options(
        self, platform: Platform, ctx: RequestContext, crud: Any
    ) -> dict[str, list[dict[str, str]]]:
        """Dropdown choices for a record form's ``options_source`` fields.

        Only the sources a form actually references are looked up, and each is scoped
        to this tenant/venue. Values are the ids/codes the create call expects; labels
        are human-friendly so the operator does not pick by opaque id.
        """
        wanted = {f.get("options_source") for f in crud.fields if f.get("options_source")}
        out: dict[str, list[dict[str, str]]] = {}
        tid, vid = self.tenant_id, self.venue_id
        if "products" in wanted:
            out["products"] = [
                {"value": r["id"], "label": decode(r["name_json"], {}).get("en") or r["code"]}
                for r in platform.db.query(
                    "SELECT id, code, name_json FROM products "
                    "WHERE tenant_id = ? AND venue_id = ? AND status = 'ACTIVE' ORDER BY display_order, code",
                    (tid, vid),
                )
            ]
        if "segments" in wanted:
            out["segments"] = [
                {"value": r["code"], "label": decode(r["name_json"], {}).get("en") or r["code"]}
                for r in platform.db.query(
                    "SELECT code, name_json FROM customer_segments "
                    "WHERE tenant_id = ? AND status = 'ACTIVE' ORDER BY display_order, code",
                    (tid,),
                )
            ]
        if "shows" in wanted:
            out["shows"] = [
                {"value": r["id"], "label": decode(r["name_json"], {}).get("en") or r["code"]}
                for r in platform.db.query(
                    "SELECT id, code, name_json FROM experiences "
                    "WHERE tenant_id = ? AND venue_id = ? AND kind = 'SHOW' AND status = 'ACTIVE' "
                    "ORDER BY code", (tid, vid),
                )
            ]
        if "layouts" in wanted:
            from .seating_admin import seat_layout_options

            out["layouts"] = seat_layout_options(platform, ctx, vid)
        return out

    # ------------------------------------------------------------------ #
    # Contexts
    # ------------------------------------------------------------------ #

    def guest_context(self, request: Request) -> RequestContext:
        ctx = self.platform.guest_context(
            self.tenant_id,
            venue_id=self.venue_id,
            channel=request.q("channel", "ONLINE") or "ONLINE",
            language=request.language,
            ip_address=request.headers.get("X-Forwarded-For", "127.0.0.1").split(",")[0].strip(),
            user_agent=request.headers.get("User-Agent"),
        )
        ctx.correlation_id = request.correlation_id
        return ctx

    def staff_context(self, request: Request) -> RequestContext:
        """Resolve a staff principal from the bearer token, or refuse."""
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise AuthenticationRequired()
        base = self.platform.guest_context(self.tenant_id, venue_id=self.venue_id)
        principal = self.platform.staff.authenticate_token(base, authorization[7:].strip())
        ctx = base.with_principal(principal)
        ctx.correlation_id = request.correlation_id
        ctx.language = request.language
        ctx.channel = request.q("channel", "STAFF") or "STAFF"
        ctx.ip_address = request.headers.get("X-Forwarded-For", "127.0.0.1").split(",")[0].strip()
        return ctx

    def system_context(self) -> RequestContext:
        return self.platform.system_context(self.tenant_id, venue_id=self.venue_id)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def dispatch(self, request: Request) -> Response:
        resolved = self.resolve(request.method, request.path)
        if resolved is None:
            if self.path_exists(request.path):
                return Response({"error": {"code": "method_not_allowed"}}, status=405)
            return Response({"error": {"code": "not_found", "message": "Unknown endpoint."}}, status=404)
        handler, params = resolved
        request.params = params

        # CSRF on every mutating request, bound to the browser session (A01-8).
        if request.method.upper() not in csrf_module.SAFE_METHODS:
            has_bearer = request.headers.get("Authorization", "").startswith("Bearer ")
            self.platform.csrf.require(
                method=request.method,
                session_id=request.browser_session_id,
                header_token=request.headers.get(csrf_module.CSRF_HEADER),
                cookie_token=request.cookies.get(csrf_module.CSRF_COOKIE_NAME),
                has_bearer_auth=has_bearer,
                correlation_id=request.correlation_id,
            )
        return handler(request)

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #

    def _register(self) -> None:
        platform = self.platform

        # --- meta ------------------------------------------------------- #

        @self.route("GET", "/api/health")
        def health(request: Request) -> Response:
            return Response(
                {
                    "status": "ok",
                    "tenant_id": self.tenant_id,
                    "venue_id": self.venue_id,
                    "server_time": platform.clock.now().isoformat(),
                }
            )

        @self.route("GET", "/api/security/posture")
        def posture(request: Request) -> Response:
            return Response(platform.security_posture(self.tenant_id))

        @self.route("GET", "/api/csrf")
        def csrf_token(request: Request) -> Response:
            token = platform.csrf.issue(session_id=request.browser_session_id)
            return Response(
                {"csrf_token": token},
                headers={"Set-Cookie": CSRF_COOKIE.render(csrf_module.CSRF_COOKIE_NAME, token)},
            )

        # --- catalogue -------------------------------------------------- #

        @self.route("GET", "/api/venue")
        def venue(request: Request) -> Response:
            ctx = self.guest_context(request)
            record = platform.tenancy.get_venue(ctx, self.venue_id)
            areas = platform.tenancy.list_areas(ctx, self.venue_id)
            return Response(
                {
                    "id": record["id"],
                    "code": record["code"],
                    "name": record["name"],
                    "timezone": record["timezone"],
                    "currency": record["currency"],
                    "tax_model": record["tax_model"],
                    "tax_rate_bp": record["tax_rate_bp"],
                    "operating_hours": record["operating_hours"],
                    "address": record["address"],
                    "contact": record["contact"],
                    "zones": [
                        {
                            "code": area["code"],
                            "name": area["name"],
                            "description": area["description"],
                            "floor": area["floor"],
                        }
                        for area in areas
                    ],
                }
            )

        @self.route("GET", "/api/products")
        def products(request: Request) -> Response:
            ctx = self.guest_context(request)
            date = request.q("date")
            out = []
            for product in platform.catalog.list_products(
                ctx, self.venue_id, channel=ctx.channel, customer_visible_only=True, on_date=date
            ):
                prices = (
                    platform.pricing.price_list(
                        ctx, product_id=product["id"], date=date, channel=ctx.channel
                    )
                    if date
                    else []
                )
                out.append(
                    {
                        "id": product["id"],
                        "code": product["code"],
                        "name": product["name"],
                        "description": product["description"],
                        "session_requirement": product["session_requirement"],
                        "max_per_booking": product["max_per_booking"],
                        "ticket_types": [
                            {
                                "id": price["ticket_type_id"],
                                "code": price["code"],
                                "name": price["name"],
                                "unit_price_minor": price["unit_price_minor"],
                                "currency": price["currency"],
                                "max_quantity": price["max_quantity"],
                            }
                            for price in prices
                        ],
                    }
                )
            return Response({"products": out})

        @self.route("GET", "/api/calendar")
        def calendar(request: Request) -> Response:
            ctx = self.guest_context(request)
            record = platform.tenancy.get_venue(ctx, self.venue_id)
            date_from = request.q("from")
            date_to = request.q("to")
            if not date_from or not date_to:
                raise ValidationError({"from": "Provide from and to dates."})
            return Response(
                platform.calendar.calendar(
                    ctx,
                    venue=record,
                    date_from=date_from,
                    date_to=date_to,
                    channel=ctx.channel,
                    product_id=request.q("product_id"),
                    language=request.language,
                )
            )

        @self.route("GET", "/api/shows")
        def shows(request: Request) -> Response:
            ctx = self.guest_context(request)
            date = request.q("date")
            if not date:
                raise ValidationError({"date": "Provide a date."})
            return Response(
                platform.shows.customer_timetable(
                    ctx,
                    venue_id=self.venue_id,
                    date=date,
                    language=request.language,
                    filter_key=request.q("filter", "ALL") or "ALL",
                )
            )

        @self.route("GET", "/api/consent")
        def consent_dialog(request: Request) -> Response:
            ctx = self.guest_context(request)
            return Response(
                platform.consent.dialog(ctx, language=request.language, channel=ctx.channel)
            )

        @self.route("GET", "/api/languages")
        def languages(request: Request) -> Response:
            """The customer language selector options (update spec §1)."""
            from ..core.i18n import LANGUAGE_DISPLAY

            return Response({"languages": [dict(entry) for entry in LANGUAGE_DISPLAY]})

        @self.route("GET", "/api/payment-types")
        def payment_types(request: Request) -> Response:
            """Active payment types for the customer's channel (update spec §49).

            No hard-coded list on the client — the UI renders exactly what is enabled
            for this venue and channel, in display order.
            """
            ctx = self.guest_context(request)
            return Response(
                {
                    "payment_types": platform.payment_types.customer_payment_types(
                        ctx, venue_id=self.venue_id, channel=ctx.channel, currency=request.q("currency")
                    )
                }
            )

        # --- checkout ---------------------------------------------------- #

        @self.route("POST", "/api/quote")
        def quote(request: Request) -> Response:
            from ..services.booking import QuoteLineRequest

            payload = request.json()
            ctx = self.guest_context(request)
            lines = [
                QuoteLineRequest(
                    ticket_type_id=item.get("ticket_type_id"),
                    quantity=int(item.get("quantity", 0)),
                    session_id=item.get("session_id"),
                )
                for item in payload.get("lines", [])
                if int(item.get("quantity", 0)) > 0
            ]
            if not lines:
                raise ValidationError({"lines": "Choose at least one ticket."})
            result = platform.booking.quote(
                ctx,
                venue_id=self.venue_id,
                visit_date=payload.get("visit_date", ""),
                lines=lines,
                promotion_codes=payload.get("promotion_codes", []),
            )
            checkout = platform.booking.start_checkout(ctx, result)
            request.state["quote"] = {
                "cart_id": checkout.cart.cart_id,
                "visit_date": checkout.cart.visit_date,
                "total_minor": checkout.total_minor,
                "lines": [
                    {
                        "ticket_type_id": line.ticket_type_id,
                        "quantity": line.quantity,
                        "session_id": line.session_id,
                    }
                    for line in checkout.cart.lines
                ],
                "promotion_codes": list(checkout.cart.promotion_codes),
            }
            return Response(checkout.as_dict())

        @self.route("POST", "/api/confirm")
        def confirm(request: Request) -> Response:
            from ..services.booking import QuoteLineRequest

            payload = request.json()
            ctx = self.guest_context(request)
            saved = request.state.get("quote")
            if not saved:
                raise ValidationError(
                    {"quote": "Your selection expired. Please choose your tickets again."},
                    message="Your selection expired. Please start again.",
                )
            # Rebuild and re-hold, then confirm. The service revalidates everything
            # anyway (R13.7), so a stale client cannot force a stale price.
            rebuilt = platform.booking.quote(
                ctx,
                venue_id=self.venue_id,
                visit_date=saved["visit_date"],
                lines=[
                    QuoteLineRequest(
                        ticket_type_id=line["ticket_type_id"],
                        quantity=line["quantity"],
                        session_id=line.get("session_id"),
                    )
                    for line in saved["lines"]
                ],
                promotion_codes=saved.get("promotion_codes", []),
                cart_id=saved["cart_id"],
            )
            rebuilt = platform.booking.start_checkout(ctx, rebuilt)
            result = platform.booking.confirm(
                ctx,
                rebuilt,
                customer={
                    "email": payload.get("email", ""),
                    "full_name": payload.get("full_name"),
                    "phone": payload.get("phone"),
                },
                consent_items=payload.get("consent_items", {}),
                payment_method=payload.get("payment_method", "CARD"),
                idempotency_key=payload.get("idempotency_key") or secure_token(16),
                reconfirmed=bool(payload.get("reconfirmed")),
            )
            request.state.pop("quote", None)
            # Remember which tickets this browser is entitled to print. The print
            # endpoints authorise against this rather than accepting any ticket id,
            # so tickets cannot be enumerated (same reasoning as R16.3).
            self._grant_ticket_access(request, [t["id"] for t in result.get("tickets", [])])
            return Response(result)

        # --- ticket artefacts (e-ticket, thermal, QR) --------------------- #

        @self.route("GET", "/tickets/{ticket_id}/eticket")
        def eticket(request: Request) -> Response:
            """The designed HTML e-ticket, for viewing and for ordinary printing."""
            ticket_id = self._authorized_ticket(request)
            html = platform.render_eticket(
                self.system_context(),
                ticket_id=ticket_id,
                language=request.language,
                auto_print=request.q("print") == "1",
            )
            return Response(
                raw=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                profile="CUSTOMER",
                headers={"Cache-Control": "no-store"},
            )

        @self.route("GET", "/tickets/{ticket_id}/thermal")
        def thermal(request: Request) -> Response:
            """The 80mm gate ticket for a Star MCP31LB, sized for an 80mm roll."""
            ticket_id = self._authorized_ticket(request)
            html = platform.render_thermal_ticket(
                self.system_context(),
                ticket_id=ticket_id,
                language=request.language,
                auto_print=request.q("print") == "1",
            )
            return Response(
                raw=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                profile="CUSTOMER",
                headers={"Cache-Control": "no-store"},
            )

        @self.route("GET", "/tickets/{ticket_id}/qr.svg")
        def ticket_qr(request: Request) -> Response:
            """The access credential as a standalone image, for reuse elsewhere."""
            from ..ticketdesign.qr import qr_svg

            ticket_id = self._authorized_ticket(request)
            ticket = platform.tickets.get(self.system_context(), ticket_id, include_qr=True)
            svg = qr_svg(ticket["qr_payload"], level="M", title="Entrance access QR code")
            return Response(
                raw=svg.encode("utf-8"),
                content_type="image/svg+xml; charset=utf-8",
                profile="CUSTOMER",
                headers={"Cache-Control": "no-store"},
            )

        @self.route("GET", "/qr/{token}")
        def qr_by_token(request: Request) -> Response:
            """A ticket's QR as a PNG, authorised by a signed capability token.

            This is the sessionless route a mail client can fetch. It exists because
            some clients (Gmail among them) strip ``data:`` images, and none of them
            carry the guest's browser session. The token is signed and expiring, so
            the image is reachable without a session yet still cannot be found by
            guessing a ticket id.
            """
            from ..ticketdesign.links import verify_qr_token
            from ..ticketdesign.qr import qr_png

            ticket_id = verify_qr_token(request.params.get("token", ""))
            if not ticket_id:
                raise NotFound()
            try:
                ticket = platform.tickets.get(self.system_context(), ticket_id, include_qr=True)
            except PlatformError:
                raise NotFound() from None
            png = qr_png(ticket["qr_payload"], level="M", scale=8)
            return Response(
                raw=png,
                content_type="image/png",
                profile="CUSTOMER",
                # Safe to cache: the token bounds its own lifetime, and the image is
                # a pure function of a credential that does not change unless the
                # ticket is reissued (which mints a new token anyway).
                headers={"Cache-Control": "private, max-age=3600"},
            )

        # --- manage booking --------------------------------------------- #

        @self.route("POST", "/api/manage/request-code")
        def request_code(request: Request) -> Response:
            payload = request.json()
            ctx = self.guest_context(request)
            result = platform.booking.request_access_code(
                ctx,
                booking_number=payload.get("booking_number", ""),
                email=payload.get("email", ""),
            )
            # The demo surfaces the code so the flow can be walked without a mailbox.
            # A real deployment removes this: the code exists only in the email.
            return Response({**result, "demo_code": result.pop("_code", None)})

        @self.route("POST", "/api/manage/verify")
        def verify(request: Request) -> Response:
            payload = request.json()
            ctx = self.guest_context(request)
            verified = platform.booking.verify_access(
                ctx,
                booking_number=payload.get("booking_number", ""),
                email=payload.get("email", ""),
                code=payload.get("code", ""),
            )
            view = platform.booking.manage_view(
                self.system_context(), verified["booking_id"], language=request.language
            )
            # Ownership is now proven by the one-time code, so this browser may print
            # this booking's tickets.
            self._grant_ticket_access(
                request,
                [t["ticket_id"] for t in view.get("tickets", []) if t.get("ticket_id")],
            )
            return Response(view)

        # --- gate (R32) -------------------------------------------------- #

        @self.route("POST", "/api/gate/scan")
        def scan(request: Request) -> Response:
            """Validate a scanned QR at an access point.

            The scanner authenticates with its own device credential, not a staff
            login: the device identity is the gate control (R32.12). An unregistered
            or deactivated device is refused inside the service. The scan itself runs
            under a system context so gate throughput is not gated by staff RBAC, but
            every decision is recorded against the device (R32.5).
            """
            payload = request.json()
            ctx = self.system_context()
            ctx.correlation_id = request.correlation_id
            ctx.channel = "GATE"
            return Response(
                platform.access.scan(
                    ctx,
                    qr_payload=payload.get("qr_payload", ""),
                    access_point_id=payload.get("access_point_id"),
                    device_code=payload.get("device_code"),
                    device_secret=payload.get("device_secret"),
                )
            )

        @self.route("POST", "/api/gate/override")
        def gate_override(request: Request) -> Response:
            """Admit a rejected guest on a supervisor's authority (R32.9)."""
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.access.override_admit(
                    ctx, scan_id=payload.get("scan_id", ""), reason=payload.get("reason", "")
                )
            )

        @self.route("GET", "/api/gate/lookup")
        def gate_lookup(request: Request) -> Response:
            """Manual booking lookup when a QR cannot be scanned (R32.10)."""
            ctx = self.staff_context(request)
            return Response(
                platform.access.manual_lookup(ctx, booking_number=request.q("booking_number", "") or "")
            )

        # --- payment gateway callback ------------------------------------ #

        @self.route("POST", "/api/webhook/payment")
        def webhook(request: Request) -> Response:
            payload = request.json()
            ctx = self.system_context()
            body = request.raw_body.decode("utf-8")
            result = platform.payments.handle_webhook(
                ctx,
                provider_event_id=payload.get("event_id", ""),
                kind=payload.get("kind", "payment.succeeded"),
                body=body,
                signature=request.headers.get("X-Signature", ""),
                payment_id=payload.get("payment_id"),
                idempotency_key=payload.get("idempotency_key"),
                amount_minor=payload.get("amount_minor"),
            )
            return Response(result)

        # --- staff ------------------------------------------------------- #

        @self.route("POST", "/api/staff/login")
        def login(request: Request) -> Response:
            payload = request.json()
            ctx = self.guest_context(request)
            result = platform.staff.login(
                ctx,
                email=payload.get("email", ""),
                credential=payload.get("credential", ""),
                mfa_code=payload.get("mfa_code"),
                channel="STAFF",
            )
            return Response(result)

        @self.route("POST", "/api/staff/forgot-password")
        def forgot_password(request: Request) -> Response:
            """Begin a self-service password reset (settings spec §1 "Recover").

            Unauthenticated by design — it helps someone who cannot sign in. The
            response is enumeration-safe: identical whether or not the email exists.
            In this demo the one-time token is returned as ``demo_token`` (and would
            arrive by email in production) so the flow can be completed without a real
            mailbox; a real deployment drops that field.
            """
            payload = request.json()
            ctx = self.guest_context(request)
            result = platform.staff.request_password_reset(ctx, email=payload.get("email", ""))
            token = result.pop("reset_token", None)
            result.pop("staff_id", None)
            result.pop("email", None)
            if token:
                result["demo_token"] = token
            return Response(result)

        @self.route("POST", "/api/staff/reset-password")
        def reset_password(request: Request) -> Response:
            """Consume a reset token and set a new password (settings spec §1)."""
            payload = request.json()
            ctx = self.guest_context(request)
            return Response(
                platform.staff.complete_password_reset(
                    ctx,
                    email=payload.get("email", ""),
                    token=payload.get("token", ""),
                    credential=payload.get("credential", ""),
                )
            )

        @self.route("POST", "/api/staff/logout")
        def logout(request: Request) -> Response:
            """End the session that presented this token (§58).

            Logout is authenticated: without resolving the session there is nothing
            to revoke. A token that has already expired therefore gets the same
            ``AuthenticationRequired`` any other request would — which is the right
            answer, because it is already logged out.
            """
            ctx = self.staff_context(request)
            return Response(platform.staff.logout(ctx))

        @self.route("GET", "/api/staff/me")
        def staff_me(request: Request) -> Response:
            """Identity, scope, permissions and authorized navigation in one call (§3).

            This is the call the back office makes immediately after login and again
            on reload. It is the only place the client learns what it may draw, and
            everything in it is re-enforced per request server-side.
            """
            ctx = self.staff_context(request)
            return Response(platform.staff.session_profile(ctx, language=request.language))

        @self.route("GET", "/api/staff/navigation")
        def navigation(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(
                {
                    "navigation": platform.authz.navigation(ctx, language=request.language),
                    "permissions_changed": platform.authz.permission_changed(ctx),
                }
            )

        @self.route("GET", "/api/staff/permissions/matrix")
        def permission_matrix(request: Request) -> Response:
            """The grantable registry, localized, for the role editor (§19, §50)."""
            ctx = self.staff_context(request)
            return Response(platform.authz.permission_matrix(ctx, language=request.language))

        @self.route("GET", "/api/staff/permissions/summary")
        def permission_summary(request: Request) -> Response:
            """Effective permissions for one staff member (§36).

            Defaults to the caller so anyone can answer "what am I allowed to do?"
            about themselves without holding ``Staff.VIEW``; asking about *someone
            else* goes through ``permission_summary``, which requires it.
            """
            ctx = self.staff_context(request)
            staff_id = request.q("staff_id") or ctx.principal.id or ""
            if staff_id == ctx.principal.id:
                effective = platform.authz.effective_permissions(ctx)
                return Response(
                    {
                        "staff_id": staff_id,
                        "self": True,
                        "permissions": sorted(effective.granted),
                        "roles": list(effective.roles),
                        "authority_level": effective.authority_level,
                        "summary": platform.authz.grant_summary(
                            effective.granted, language=request.language
                        ),
                        "settings": platform.authz.settings_home(ctx, language=request.language),
                    }
                )
            return Response(
                platform.authz.permission_summary(ctx, staff_id, language=request.language)
            )

        # --- staff role assignment (settingsAndReports §35, Fix.md §1.2/§1.3) --- #
        # Assign/remove a role for a staff member at a given scope. The staff service
        # enforces Staff.EDIT + MANAGE_PERMISSION, refuses self-assignment and any
        # grant above the actor's own authority (§52), and audits every change. The
        # target's next request re-resolves permissions, so a change takes effect
        # promptly (§53, Fix.md §1.7).

        @self.route("POST", "/api/staff/staff/{staff_id}/roles")
        def assign_staff_role(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            result = platform.staff.assign_role(
                ctx,
                staff_id=request.params["staff_id"],
                role_id=payload.get("role_id", ""),
                scope_type=payload.get("scope_type", "VENUE"),
                scope_id=payload.get("scope_id") or self.venue_id,
                operating_point=payload.get("operating_point"),
                reason=payload.get("reason"),
                approver_id=payload.get("approver_id"),
            )
            return Response({"assignment": result}, status=201)

        @self.route("POST", "/api/staff/role-assignments/{assignment_id}/remove")
        def remove_staff_role(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.staff.remove_role_assignment(
                    ctx, request.params["assignment_id"], reason=payload.get("reason")
                )
            )

        @self.route("GET", "/api/staff/assignable-roles")
        def assignable_roles(request: Request) -> Response:
            """Roles and venues an administrator can pick when assigning access.

            Only shown to a principal who may edit staff; the actual grant is still
            re-checked server-side against the actor's authority (§52).
            """
            ctx = self.staff_context(request)
            platform.authz.require_page(ctx.for_venue(self.venue_id), "Staff", "VIEW")
            roles = [
                {"id": r["id"], "code": r["code"], "name": r["name"],
                 "authority_level": r["authority_level"]}
                for r in platform.db.query(
                    "SELECT id, code, name, authority_level FROM roles "
                    "WHERE tenant_id = ? AND status = 'ACTIVE' ORDER BY authority_level DESC, name",
                    (self.tenant_id,),
                )
            ]
            venues = [
                {"id": v["id"], "code": v["code"], "name": decode(v["name_json"], {})}
                for v in platform.db.query(
                    "SELECT id, code, name_json FROM venues WHERE tenant_id = ? AND status = 'ACTIVE' ORDER BY code",
                    (self.tenant_id,),
                )
            ]
            return Response({"roles": roles, "venues": venues})

        @self.route("GET", "/api/staff/settings/home")
        def settings_home(request: Request) -> Response:
            """Settings categories this principal may open (§11, §26, §71).

            Separate from ``/api/staff/settings``, which loads the *contents* of the
            configuration blocks. The home only needs the map, so an administrator
            who can view one category does not pay for reading every other one.
            """
            ctx = self.staff_context(request)
            platform.authz.require_authenticated(ctx)
            return Response(
                {
                    "categories": platform.authz.settings_home(ctx, language=request.language),
                    "permissions_changed": platform.authz.permission_changed(ctx),
                }
            )

        @self.route("GET", "/api/staff/settings/search")
        def settings_search(request: Request) -> Response:
            """Search settings, filtered to pages this principal may VIEW (§27, §32)."""
            ctx = self.staff_context(request)
            platform.authz.require_authenticated(ctx)
            return Response(
                {
                    "query": request.q("q", "") or "",
                    "results": platform.authz.settings_search(
                        ctx, request.q("q", "") or "", language=request.language
                    ),
                }
            )

        @self.route("GET", "/api/staff/bookings")
        def staff_bookings(request: Request) -> Response:
            ctx = self.staff_context(request)
            bookings = platform.booking.list_bookings(ctx, limit=int(request.q("limit", "25") or 25))
            return Response(
                {
                    "bookings": [
                        {
                            "booking_number": b["booking_number"],
                            "status": b["status"],
                            "visit_date": b["visit_date"],
                            "net_minor": b["net_minor"],
                            "currency": b["currency"],
                            "channel": b["channel"],
                            "ticket_count": len(b["tickets"]),
                            "customer": (b.get("customer") or {}).get("email"),
                        }
                        for b in bookings
                    ]
                }
            )

        @self.route("GET", "/api/staff/audit")
        def audit(request: Request) -> Response:
            ctx = self.staff_context(request)
            platform.authz.require_page(ctx, "Audit Logs", "VIEW")
            return Response(
                {
                    "events": platform.audit.search(
                        ctx, venue_ids=platform.authz.scoped_venue_ids(ctx), limit=50
                    )
                }
            )

        # --- staff: business / venue settings (add_features §25-§32) ------ #
        # Every write calls the matching SettingsService method, which enforces the
        # MANAGE_* action permission and audits the change server-side (settings §31,
        # §32). Reads require only the page's VIEW permission — looking at a rate is
        # not changing it. All are scoped to this server's venue.

        def _settings_venue(request: Request) -> tuple[RequestContext, dict[str, Any]]:
            ctx = self.staff_context(request)
            venue = platform.tenancy.get_venue(ctx, self.venue_id)
            return ctx, venue

        @self.route("GET", "/api/staff/settings")
        def settings_overview(request: Request) -> Response:
            """Everything the back-office Settings screen renders, in one call.

            Each block is gated by its own page VIEW; a block the principal cannot
            view is omitted rather than half-filled, so the UI shows only what the
            role is entitled to see (R42.7, R42.9).
            """
            ctx, venue = _settings_venue(request)
            on_date = request.q("date")
            out: dict[str, Any] = {"venue_id": self.venue_id, "currency": venue["currency"]}

            if platform.authz.can_page(ctx, "VAT Settings", "VIEW"):
                out["vat"] = {
                    "current": platform.settings.get_charge(
                        ctx, charge_kind="VAT", venue_id=self.venue_id, on_date=on_date
                    ),
                    "history": platform.settings.charge_history(
                        ctx, charge_kind="VAT", venue_id=self.venue_id
                    ),
                    "can_edit": platform.authz.can_page(ctx, "VAT Settings", "EDIT")
                    and platform.authz.can_action(ctx, "MANAGE_TAX_SETTINGS"),
                }
            if platform.authz.can_page(ctx, "Service Charge Settings", "VIEW"):
                out["service_charge"] = {
                    "current": platform.settings.get_charge(
                        ctx, charge_kind="SERVICE_CHARGE", venue_id=self.venue_id, on_date=on_date
                    ),
                    "history": platform.settings.charge_history(
                        ctx, charge_kind="SERVICE_CHARGE", venue_id=self.venue_id
                    ),
                    "can_edit": platform.authz.can_page(ctx, "Service Charge Settings", "EDIT")
                    and platform.authz.can_action(ctx, "MANAGE_SERVICE_CHARGE"),
                }
            if platform.authz.can_page(ctx, "Time Zone Settings", "VIEW"):
                out["timezone"] = {
                    "timezone": venue["timezone"],
                    "can_edit": platform.authz.can_page(ctx, "Time Zone Settings", "EDIT")
                    and platform.authz.can_action(ctx, "MANAGE_TIMEZONE"),
                }
            if platform.authz.can_page(ctx, "Ticket Validity Settings", "VIEW"):
                from ..services.settings import VALIDITY_TYPES

                out["ticket_validity"] = {
                    "policy": platform.settings.validity_policy(ctx, venue_id=self.venue_id),
                    "validity_types": list(VALIDITY_TYPES),
                    "can_edit": platform.authz.can_page(ctx, "Ticket Validity Settings", "EDIT")
                    and platform.authz.can_action(ctx, "MANAGE_TICKET_VALIDITY"),
                }
            if platform.authz.can_page(ctx, "Currency Settings", "VIEW"):
                out["base_currency"] = {
                    "currency": venue["currency"],
                    "info": platform.settings.currency_info(venue["currency"]),
                    "can_edit": platform.authz.can_page(ctx, "Currency Settings", "EDIT")
                    and platform.authz.can_action(ctx, "MANAGE_CURRENCY"),
                }
            if platform.authz.can_page(ctx, "Exchange Rates", "VIEW"):
                # Rates are configured at organization scope by default (venue-agnostic
                # conversion), which is where the add endpoint writes, so the overview
                # lists that scope. Venue-specific overrides are an advanced case.
                out["exchange_rates"] = {
                    "rates": platform.settings.list_exchange_rates(
                        ctx, organization_id=venue["organization_id"]
                    ),
                    "can_add": platform.authz.can_page(ctx, "Exchange Rates", "ADD")
                    and platform.authz.can_action(ctx, "MANAGE_EXCHANGE_RATE"),
                    "can_edit": platform.authz.can_page(ctx, "Exchange Rates", "EDIT")
                    and platform.authz.can_action(ctx, "MANAGE_EXCHANGE_RATE"),
                }
            # Every config-backed page the principal may view, so the client can open
            # any of them (operating hours, booking rules, rounding, languages, …)
            # without a call each. Filtered by VIEW inside overview().
            out["config_pages"] = platform.settings_pages.overview(
                ctx, venue_id=self.venue_id, organization_id=venue["organization_id"]
            )
            return Response(out)

        @self.route("GET", "/api/staff/settings/charge-preview")
        def charge_preview(request: Request) -> Response:
            """Server-computed VAT / service-charge breakdown for a sample amount (§41).

            The Settings screen shows "Product Price 1,070 → Before VAT 1,000 → VAT 70"
            so an administrator can see the consequence of the rate before saving. That
            preview is computed *here*, through the same ``compute_charges`` the booking
            path uses, because a client that did its own arithmetic would eventually
            show a number the checkout disagrees with — and the operator would trust
            the wrong one.

            Read-only: it requires VAT VIEW and changes nothing.
            """
            from ..core.money import compute_charges

            ctx, venue = _settings_venue(request)
            platform.authz.require_page(ctx, "VAT Settings", "VIEW")
            try:
                amount = int(request.q("amount_minor", "107000") or 107000)
            except ValueError:
                amount = 107000
            amount = max(0, min(amount, 10_000_000_00))
            on_date = request.q("date")
            service_charge, vat = platform.settings.charge_inputs(
                ctx, venue_id=self.venue_id, on_date=on_date
            )
            breakdown = compute_charges(
                base_minor=amount,
                service_charge=service_charge,
                vat=vat,
                currency=venue["currency"],
            )
            return Response(
                {
                    "sample_base_minor": amount,
                    "vat": vat.as_dict(),
                    "service_charge": service_charge.as_dict(),
                    "breakdown": breakdown.as_dict(),
                }
            )

        @self.route("POST", "/api/staff/settings/vat")
        def set_vat(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.settings.set_vat(
                    ctx,
                    venue_id=self.venue_id,
                    enabled=bool(payload.get("enabled", True)),
                    rate_bp=int(payload.get("rate_bp", 0)),
                    mode=payload.get("mode", "INCLUSIVE"),
                    effective_from=payload.get("effective_from", ""),
                    display_name=payload.get("display_name", "VAT"),
                    tax_registration=payload.get("tax_registration"),
                    reason=payload.get("reason"),
                )
            )

        @self.route("POST", "/api/staff/settings/service-charge")
        def set_service_charge(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.settings.set_service_charge(
                    ctx,
                    venue_id=self.venue_id,
                    enabled=bool(payload.get("enabled", True)),
                    rate_bp=int(payload.get("rate_bp", 0)),
                    mode=payload.get("mode", "EXCLUSIVE"),
                    effective_from=payload.get("effective_from", ""),
                    display_name=payload.get("display_name", "Service Charge"),
                    reason=payload.get("reason"),
                )
            )

        @self.route("POST", "/api/staff/settings/timezone")
        def set_timezone(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.settings.set_timezone(
                    ctx,
                    venue_id=self.venue_id,
                    timezone=payload.get("timezone", ""),
                    reason=payload.get("reason"),
                )
            )

        @self.route("POST", "/api/staff/settings/ticket-validity")
        def set_ticket_validity(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.settings.set_validity_policy(
                    ctx,
                    venue_id=self.venue_id,
                    policy=payload.get("policy", {}),
                    product_id=payload.get("product_id"),
                    reason=payload.get("reason"),
                )
            )

        @self.route("POST", "/api/staff/settings/base-currency")
        def set_base_currency(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.settings.set_base_currency(
                    ctx,
                    venue_id=self.venue_id,
                    currency=payload.get("currency", ""),
                    reason=payload.get("reason"),
                )
            )

        @self.route("POST", "/api/staff/settings/exchange-rates")
        def add_exchange_rate(request: Request) -> Response:
            payload = request.json()
            ctx, venue = _settings_venue(request)
            return Response(
                platform.settings.set_exchange_rate(
                    ctx,
                    organization_id=venue["organization_id"],
                    from_currency=payload.get("from_currency", ""),
                    to_currency=payload.get("to_currency", ""),
                    rate=payload.get("rate", ""),
                    effective_from=payload.get("effective_from", ""),
                    effective_until=payload.get("effective_until"),
                    venue_id=self.venue_id if payload.get("scope") == "VENUE" else None,
                    reason=payload.get("reason"),
                )
            )

        @self.route("POST", "/api/staff/settings/exchange-rates/{rate_id}/end")
        def end_exchange_rate(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.settings.end_exchange_rate(
                    ctx,
                    rate_id=request.params["rate_id"],
                    effective_until=payload.get("effective_until"),
                    reason=payload.get("reason"),
                )
            )

        # --- config-backed settings pages (settings/reports spec §16) ---- #
        # A single pair of routes serves every page whose configuration is one
        # scoped value (operating hours, booking rules, rounding, languages, login
        # security, …). The page is named in the query/body rather than the path
        # because page keys contain spaces, and the service resolves it to its
        # config key, scope, permission and validator. Each still enforces the page's
        # own VIEW/EDIT server-side, so this generality does not weaken authorization.

        @self.route("GET", "/api/staff/settings/page")
        def get_settings_page(request: Request) -> Response:
            ctx, venue = _settings_venue(request)
            return Response(
                platform.settings_pages.get(
                    ctx,
                    request.q("page", "") or "",
                    venue_id=self.venue_id,
                    organization_id=venue["organization_id"],
                )
            )

        @self.route("POST", "/api/staff/settings/page")
        def set_settings_page(request: Request) -> Response:
            payload = request.json()
            ctx, venue = _settings_venue(request)
            return Response(
                platform.settings_pages.set(
                    ctx,
                    payload.get("page", ""),
                    payload.get("value", {}),
                    venue_id=self.venue_id,
                    organization_id=venue["organization_id"],
                    reason=payload.get("reason"),
                )
            )

        # --- record-based settings pages (read + status change) --------- #
        # These reuse the existing catalog/tenancy/staff/promotions/notifications
        # services rather than reimplementing them. Reads are gated by the page's
        # VIEW; the services themselves enforce ADD/EDIT/DELETE on writes.

        @self.route("GET", "/api/staff/settings/records")
        def list_settings_records(request: Request) -> Response:
            """One read endpoint for the record-collection settings pages.

            The page's own VIEW permission guards it, and the rows are read directly
            from the backing table so the shape is uniform across pages and does not
            depend on each service's per-context list signature. Writes still go
            through the owning service (Staff, Roles, Promotions, …), which enforces
            ADD/EDIT/DELETE and audits — this endpoint only reads.
            """
            ctx = self.staff_context(request)
            vctx = ctx.for_venue(self.venue_id)
            page = request.q("page", "") or ""
            platform.authz.require_page(vctx, page, "VIEW")
            records = self._list_records(platform, page)
            if records is None:
                raise NotFound()
            body: dict[str, Any] = {"page": page, "records": records}
            # If this page has a CRUD descriptor, tell the client which controls to
            # draw — but only the verbs this principal actually holds, so the UI never
            # offers an action the server would reject (§14, §15, §16, §66).
            crud = record_page(page)
            if crud is not None:
                descriptor = crud.descriptor()
                descriptor["can_create"] = descriptor["can_create"] and platform.authz.can_page(
                    vctx, page, "ADD"
                )
                descriptor["can_update"] = descriptor["can_update"] and platform.authz.can_page(
                    vctx, page, "EDIT"
                )
                descriptor["can_delete"] = descriptor["can_delete"] and platform.authz.can_page(
                    vctx, page, "DELETE"
                )
                descriptor["options"] = self._record_field_options(platform, vctx, crud)
                body["crud"] = descriptor
            return Response(body)

        @self.route("POST", "/api/staff/settings/records")
        def create_settings_record(request: Request) -> Response:
            """Create a record on a record-collection settings page (§15).

            The owning service enforces the page's ADD permission and writes the audit
            entry; this endpoint only resolves the page and forwards the payload.
            """
            payload = request.json()
            ctx = self.staff_context(request).for_venue(self.venue_id)
            page = payload.get("page", "") or ""
            crud = record_page(page)
            if crud is None or crud.create is None:
                raise NotFound()
            result = crud.create(platform, ctx, self.venue_id, payload)
            return Response({"page": page, "record": result}, status=201)

        @self.route("POST", "/api/staff/settings/records/{record_id}")
        def update_settings_record(request: Request) -> Response:
            """Edit an existing record (§16). Owning service enforces EDIT + audits."""
            payload = request.json()
            ctx = self.staff_context(request).for_venue(self.venue_id)
            page = payload.get("page", "") or ""
            crud = record_page(page)
            if crud is None or crud.update is None:
                raise NotFound()
            result = crud.update(platform, ctx, request.params["record_id"], payload)
            return Response({"page": page, "record": result})

        @self.route("POST", "/api/staff/settings/records/{record_id}/delete")
        def delete_settings_record(request: Request) -> Response:
            """Delete / deactivate / archive a record (§17, §51).

            DELETE never means silent data loss here: the owning service maps it to
            deactivate or archive whenever history exists, and audits the outcome.
            """
            payload = request.json()
            ctx = self.staff_context(request).for_venue(self.venue_id)
            page = payload.get("page", "") or ""
            crud = record_page(page)
            if crud is None or crud.delete is None:
                raise NotFound()
            result = crud.delete(platform, ctx, request.params["record_id"], payload.get("reason"))
            return Response({"page": page, "result": result})

        # --- payment types (update spec §22, §39) ----------------------- #

        @self.route("GET", "/api/staff/payment-types")
        def list_payment_types(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(
                {
                    "payment_types": platform.payment_types.list(
                        ctx,
                        venue_id=self.venue_id,
                        include_archived=request.q("include_archived") == "true",
                    )
                }
            )

        @self.route("POST", "/api/staff/payment-types")
        def create_payment_type(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.payment_types.create(
                    ctx,
                    venue_id=self.venue_id,
                    code=payload.get("code", ""),
                    method=payload.get("method", ""),
                    display_name=payload.get("display_name", {}),
                    icon=payload.get("icon"),
                    description=payload.get("description"),
                    provider=payload.get("provider"),
                    provider_config_ref=payload.get("provider_config_ref"),
                    supported_currencies=payload.get("supported_currencies"),
                    web_enabled=bool(payload.get("web_enabled", True)),
                    kiosk_enabled=bool(payload.get("kiosk_enabled", True)),
                    counter_enabled=bool(payload.get("counter_enabled", True)),
                    display_order=int(payload.get("display_order", 0)),
                    reason=payload.get("reason"),
                )
            )

        # Registered before the {pt_id} route so "reorder" is not captured as an id.
        @self.route("POST", "/api/staff/payment-types/reorder")
        def reorder_payment_types(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                {
                    "payment_types": platform.payment_types.reorder(
                        ctx,
                        venue_id=self.venue_id,
                        ordered_ids=payload.get("ordered_ids", []),
                        reason=payload.get("reason"),
                    )
                }
            )

        @self.route("POST", "/api/staff/payment-types/{pt_id}/archive")
        def archive_payment_type(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.payment_types.archive(
                    ctx, request.params["pt_id"], reason=payload.get("reason")
                )
            )

        @self.route("POST", "/api/staff/payment-types/{pt_id}")
        def update_payment_type(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.payment_types.update(
                    ctx,
                    request.params["pt_id"],
                    changes=payload.get("changes", {}),
                    reason=payload.get("reason"),
                )
            )

        # --- counter POS (R34) ------------------------------------------ #

        @self.route("POST", "/api/staff/counter/shift/open")
        def open_shift(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.counter.open_shift(
                    ctx,
                    venue_id=self.venue_id,
                    counter_code=payload.get("counter_code", ""),
                    opening_float_minor=int(payload.get("opening_float_minor", 0)),
                )
            )

        @self.route("GET", "/api/staff/counter/shift")
        def current_shift(request: Request) -> Response:
            ctx = self.staff_context(request)
            shift = platform.counter.current_shift(
                ctx, venue_id=self.venue_id, counter_code=request.q("counter_code", "") or ""
            )
            if shift is None:
                return Response({"shift": None})
            return Response(platform.counter.shift_report(ctx, shift["id"]))

        @self.route("POST", "/api/staff/counter/shift/{shift_id}/close")
        def close_shift(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.counter.close_shift(
                    ctx,
                    shift_id=request.params["shift_id"],
                    counted_minor=int(payload.get("counted_minor", 0)),
                    approval_reason=payload.get("approval_reason"),
                )
            )

        @self.route("POST", "/api/staff/counter/quote")
        def counter_quote(request: Request) -> Response:
            from ..services.booking import QuoteLineRequest

            payload = request.json()
            ctx = self.staff_context(request)
            lines = [
                QuoteLineRequest(
                    ticket_type_id=item.get("ticket_type_id"),
                    quantity=int(item.get("quantity", 0)),
                    session_id=item.get("session_id"),
                )
                for item in payload.get("lines", [])
                if int(item.get("quantity", 0)) > 0
            ]
            return Response(
                platform.counter.quote(
                    ctx,
                    venue_id=self.venue_id,
                    visit_date=payload.get("visit_date", ""),
                    lines=lines,
                    promotion_codes=payload.get("promotion_codes", []),
                )
            )

        @self.route("POST", "/api/staff/counter/sell")
        def counter_sell(request: Request) -> Response:
            from ..services.booking import QuoteLineRequest

            payload = request.json()
            ctx = self.staff_context(request)
            lines = [
                QuoteLineRequest(
                    ticket_type_id=item.get("ticket_type_id"),
                    quantity=int(item.get("quantity", 0)),
                    session_id=item.get("session_id"),
                )
                for item in payload.get("lines", [])
                if int(item.get("quantity", 0)) > 0
            ]
            tendered = payload.get("tendered_minor")
            return Response(
                platform.counter.sell(
                    ctx,
                    venue_id=self.venue_id,
                    visit_date=payload.get("visit_date", ""),
                    lines=lines,
                    customer={
                        "email": payload.get("email", ""),
                        "full_name": payload.get("full_name"),
                        "phone": payload.get("phone"),
                    },
                    consent_items=payload.get("consent_items", {}),
                    payment_method=payload.get("payment_method", "CASH"),
                    idempotency_key=payload.get("idempotency_key") or secure_token(16),
                    shift_id=payload.get("shift_id"),
                    tendered_minor=int(tendered) if tendered is not None else None,
                    promotion_codes=payload.get("promotion_codes", []),
                    reconfirmed=bool(payload.get("reconfirmed")),
                )
            )

        @self.route("POST", "/api/staff/counter/void")
        def counter_void(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.counter.void(
                    ctx,
                    booking_id=payload.get("booking_id", ""),
                    reason=payload.get("reason", ""),
                    confirmed=bool(payload.get("confirmed")),
                )
            )

        @self.route("GET", "/api/staff/mailbox")
        def mailbox(request: Request) -> Response:
            """Demo aid: what the simulated email provider actually sent."""
            provider = platform.notifications.provider
            sent = getattr(provider, "sent", [])
            return Response(
                {
                    "messages": [
                        {
                            "to": m["to"],
                            "subject": m["subject"],
                            "body": m["body"],
                            "message_id": m.get("message_id"),
                            # The HTML alternative is not inlined into the JSON: it
                            # carries its own stylesheet and would fight the host
                            # page. It is fetched as its own document instead.
                            "has_html": bool(m.get("html")),
                        }
                        for m in sent[-25:]
                    ]
                }
            )

        def _sent_message(message_id: str) -> dict[str, Any]:
            provider = platform.notifications.provider
            for message in reversed(getattr(provider, "sent", [])):
                if message.get("message_id") == message_id:
                    return message
            raise NotFound()

        # --- reporting, analytics and dashboards (R70, R71) --------------- #

        def report_filters(request: Request) -> dict[str, Any]:
            """Read filters from the query string.

            Only keys the catalog declares are read; anything else is ignored
            rather than passed through, so a crafted parameter cannot widen a
            query beyond what the report is defined to filter on (R42.12).
            """
            known = (
                "date_preset", "date_from", "date_to", "date_basis", "venue", "channel",
                "product", "ticket_type", "segment", "pricing_group", "promotion",
                "partner", "payment_method", "staff", "counter", "device", "show",
                "session", "booking_status", "payment_status", "scan_result",
                "group_by", "compare_with", "currency", "measure",
            )
            filters: dict[str, Any] = {}
            for key in known:
                values = request.query.get(key) or []
                values = [value for value in values if value != ""]
                if not values:
                    continue
                filters[key] = values if len(values) > 1 else values[0]
            return filters

        @self.route("GET", "/api/staff/reports")
        def report_catalog(request: Request) -> Response:
            """The navigation, filtered to what this principal may open."""
            return Response(platform.reporting.catalog_for(self.staff_context(request)))

        @self.route("GET", "/api/staff/dashboard/executive")
        def executive_dashboard(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(platform.reporting.executive_overview(ctx, report_filters(request)))

        @self.route("GET", "/api/staff/dashboard/operations")
        def operations_dashboard(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(platform.reporting.operations_today(ctx, report_filters(request)))

        @self.route("GET", "/api/staff/reports/{report_key}")
        def run_report(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(
                platform.reporting.run(
                    ctx, request.params.get("report_key", ""), report_filters(request)
                )
            )

        @self.route("GET", "/api/staff/reports/{report_key}/export")
        def export_report(request: Request) -> Response:
            """Download a report. Requires EXPORT, and the download is audited."""
            ctx = self.staff_context(request)
            result = platform.reporting.export(
                ctx,
                request.params.get("report_key", ""),
                fmt=request.q("format", "csv") or "csv",
                filters=report_filters(request),
            )
            return Response(
                raw=result["body"].encode("utf-8"),
                content_type=result["content_type"],
                profile="CUSTOMER" if result["content_type"].startswith("text/html") else "API",
                headers={
                    # attachment for CSV, inline for the printable document so the
                    # browser's own print-to-PDF can be used.
                    "Content-Disposition": (
                        f'inline; filename="{result["filename"]}"'
                        if result["content_type"].startswith("text/html")
                        else f'attachment; filename="{result["filename"]}"'
                    ),
                    "Cache-Control": "no-store",
                },
            )

        @self.route("GET", "/api/staff/report-views")
        def list_report_views(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(
                {"views": platform.reporting.list_views(ctx, report_key=request.q("report_key"))}
            )

        @self.route("POST", "/api/staff/report-views")
        def create_report_view(request: Request) -> Response:
            payload = request.json()
            ctx = self.staff_context(request)
            return Response(
                platform.reporting.save_view(
                    ctx,
                    report_key=payload.get("report_key", ""),
                    name=payload.get("name", ""),
                    filters=payload.get("filters") or {},
                    make_default=bool(payload.get("make_default")),
                )
            )

        @self.route("POST", "/api/staff/report-views/{view_id}/delete")
        def remove_report_view(request: Request) -> Response:
            ctx = self.staff_context(request)
            return Response(
                platform.reporting.delete_view(ctx, request.params.get("view_id", ""))
            )

        @self.route("GET", "/mail/{message_id}")
        def mail_html(request: Request) -> Response:
            """Render a sent message's HTML body as its own document.

            This is the "view in browser" equivalent, and the honest way to preview
            an HTML email: the real stylesheet, in a real document, at its own URL.

            The stored HTML references its QR images by ``cid:``, which is correct
            inside a MIME message and meaningless to a browser. So the references
            are rewritten to this message's own image route — which is exactly what
            a mail client's "view in browser" link does, rather than pretending the
            email was built differently.
            """
            wanted = request.params.get("message_id", "")
            message = _sent_message(wanted)
            html = message.get("html")
            if not html:
                raise NotFound()
            for cid in message.get("inline_images") or {}:
                html = html.replace(
                    f'src="cid:{cid}"',
                    f'src="/mail/{url_quote(wanted)}/img/{url_quote(cid)}"',
                )
            return Response(
                raw=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                profile="CUSTOMER",
                headers={"Cache-Control": "no-store"},
            )

        @self.route("GET", "/mail/{message_id}/img/{cid}")
        def mail_image(request: Request) -> Response:
            """One inline image from a sent message, for the browser preview."""
            message = _sent_message(request.params.get("message_id", ""))
            blob = (message.get("inline_images") or {}).get(request.params.get("cid", ""))
            if not blob:
                raise NotFound()
            return Response(
                raw=blob,
                content_type="image/png",
                profile="CUSTOMER",
                headers={"Cache-Control": "no-store"},
            )


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #


def _parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "UTP/1.0"
    protocol_version = "HTTP/1.1"
    application: ApiApplication  # set on the server instance

    def log_message(self, format: str, *args: Any) -> None:
        # Sanitized single-line access log; nothing user-supplied reaches the format.
        from ..security.validation import sanitize_log_value

        print(f"{self.address_string()} {sanitize_log_value(format % args)}", flush=True)

    # ------------------------------------------------------------------ #

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        app = self.application
        origin = self.headers.get("Origin")
        headers = app.platform.header_policy.cors_headers(origin)
        self._send(204, b"", "text/plain", headers, profile="API")

    def _handle(self, method: str) -> None:
        app = self.application
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # Everything under /api/ is routed. Outside it, a registered route still
        # wins over the static tree, which is how the customer-facing HTML
        # documents (/tickets/{id}/eticket, /mail/{id}) are served as pages rather
        # than being mistaken for files on disk.
        if not path.startswith("/api/") and not app.path_exists(path):
            self._serve_static(path)
            return

        request = Request(
            method=method,
            path=path,
            query=parse_qs(parsed.query),
            headers=self.headers,
            body=body,
            params={},
            platform=app.platform,
        )
        try:
            response = app.dispatch(request)
        except PlatformError as exc:
            # R66.4/R66.5 — friendly message plus a correlation reference; the detail
            # goes to the server log only.
            exc.correlation_id = exc.correlation_id or request.correlation_id
            if exc.log_detail:
                print(
                    f"[{request.correlation_id}] {type(exc).__name__}: {exc.log_detail}", flush=True
                )
            response = Response(
                localize_error(exc, request.language), status=exc.http_status, profile="API"
            )
        except Exception:  # noqa: BLE001 - never leak an internal error to a client
            print(f"[{request.correlation_id}] unhandled:\n{traceback.format_exc()}", flush=True)
            response = Response(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Something went wrong. Please try again.",
                        "reference": request.correlation_id,
                    }
                },
                status=500,
                profile="API",
            )

        headers = dict(response.extra_headers)
        headers["X-Correlation-Id"] = request.correlation_id
        origin = self.headers.get("Origin")
        headers.update(app.platform.header_policy.cors_headers(origin))
        if request.issued_session:
            existing = headers.get("Set-Cookie")
            cookie = SESSION_COOKIE.render(
                "utp_session", request.browser_session_id, max_age_seconds=3600
            )
            headers["Set-Cookie"] = f"{existing}\r\nSet-Cookie: {cookie}" if existing else cookie
        self._send(response.status, response.payload, response.content_type, headers, response.profile)

    def _serve_static(self, path: str) -> None:
        app = self.application
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            # Path traversal attempt: refuse without confirming what exists.
            self._send(404, b"Not found", "text/plain", {}, profile="CUSTOMER")
            return
        if not target.is_file():
            target = WEB_ROOT / "index.html"
            if not target.is_file():
                self._send(404, b"Not found", "text/plain", {}, profile="CUSTOMER")
                return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type = f"{content_type}; charset=utf-8"
        # The app shell (HTML/JS/CSS) must never be served from a stale browser
        # cache: after a back-office change the client would otherwise keep running
        # the old bundle against a new server and render a blank or broken screen.
        # `no-store` forces a fresh fetch on every load. Fonts/images stay revalidated.
        app_shell = target.suffix.lower() in (".html", ".js", ".css")
        self._send(
            200,
            target.read_bytes(),
            content_type,
            {"Cache-Control": "no-store"} if app_shell else {},
            profile="CUSTOMER",
            cacheable=True,
        )

    def _send(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        headers: dict[str, str],
        profile: Profile = "API",
        *,
        cacheable: bool = False,
    ) -> None:
        app = self.application
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        explicit_cache = "Cache-Control" in headers
        for name, value in app.platform.header_policy.headers(profile=profile).items():
            if cacheable and name in ("Cache-Control", "Pragma"):
                continue
            self.send_header(name, value)
        if cacheable and not explicit_cache:
            self.send_header("Cache-Control", "no-cache")
        for name, value in headers.items():
            if name == "Set-Cookie" and "\r\nSet-Cookie: " in value:
                for cookie in value.split("\r\nSet-Cookie: "):
                    self.send_header("Set-Cookie", cookie)
                continue
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


def create_server(
    platform: Platform, *, tenant_id: str, venue_id: str, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """Build a threaded HTTP server bound to ``host:port``."""
    application = ApiApplication(platform, tenant_id=tenant_id, venue_id=venue_id)

    class BoundHandler(_RequestHandler):
        pass

    BoundHandler.application = application
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    return server


def serve(
    platform: Platform, *, tenant_id: str, venue_id: str, host: str = "127.0.0.1", port: int = 8080
) -> None:  # pragma: no cover - long running
    server = create_server(platform, tenant_id=tenant_id, venue_id=venue_id, host=host, port=port)
    print(f"Serving on http://{host}:{port}", flush=True)
    server.serve_forever()


__all__ = ["ApiApplication", "Request", "Response", "create_server", "serve"]
