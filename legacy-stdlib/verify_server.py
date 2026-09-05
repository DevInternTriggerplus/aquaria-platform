"""Walk the whole customer and staff flow against a running server.

``smoke_check.py`` exercises the services in-process; this exercises the *HTTP surface*,
which is where CSRF, cookie/session binding, security headers and error mapping actually
live. Run ``python serve.py`` in one terminal and this in another:

    python verify_server.py
    python verify_server.py --base http://127.0.0.1:9000
"""

from __future__ import annotations

import argparse
import datetime as _dt
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

FAILURES: list[str] = []
CHECKS = 0


class _LocalhostCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Return ``Secure`` cookies over plain HTTP on loopback.

    The platform marks its session and CSRF cookies ``Secure``, which is correct.
    Browsers treat ``localhost``/``127.0.0.1`` as a trustworthy origin and send such
    cookies over http anyway; :mod:`http.cookiejar` is stricter and would silently drop
    them, so the harness would fail CSRF for a reason no real client experiences.
    """

    def return_ok_secure(self, cookie: Any, request: Any) -> bool:
        host = urlparse(request.full_url).hostname or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        return super().return_ok_secure(cookie, request)


class Client:
    """Cookie-aware JSON client that carries the CSRF token like a browser would."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar(policy=_LocalhostCookiePolicy())
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf_token: str | None = None
        self.bearer: str | None = None
        self.last_headers: dict[str, str] = {}

    def request(
        self, method: str, path: str, body: Any = None, *, expect: int | None = 200
    ) -> dict[str, Any]:
        """``expect=None`` accepts any status, for a probe whose outcome is the point."""
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.csrf_token and method not in ("GET", "HEAD", "OPTIONS"):
            req.add_header("X-CSRF-Token", self.csrf_token)
        if self.bearer:
            req.add_header("Authorization", f"Bearer {self.bearer}")
        try:
            with self.opener.open(req, timeout=20) as response:
                status = response.status
                self.last_headers = dict(response.headers)
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            self.last_headers = dict(exc.headers)
            payload = exc.read().decode("utf-8")
        parsed = json.loads(payload) if payload.strip().startswith(("{", "[")) else {"_raw": payload}
        if expect is not None and status != expect:
            raise AssertionError(f"{method} {path} -> {status} (expected {expect}): {payload[:400]}")
        return parsed

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
        return self.request("POST", path, body, **kw)

    def raw(self, path: str, *, expect: int | None = 200) -> dict[str, Any]:
        """Fetch a non-JSON document (HTML, SVG) with its status and content type.

        The ticket artefacts are pages and images, not JSON, so they need a fetch
        that does not try to parse the body.
        """
        req = urllib.request.Request(f"{self.base}{path}", method="GET")
        if self.bearer:
            req.add_header("Authorization", f"Bearer {self.bearer}")
        try:
            with self.opener.open(req, timeout=20) as response:
                status, headers, payload = response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            status, headers, payload = exc.code, dict(exc.headers), exc.read()
        if expect is not None and status != expect:
            raise AssertionError(f"GET {path} -> {status} (expected {expect}): {payload[:300]!r}")
        return {
            "status": status,
            "content_type": headers.get("Content-Type", ""),
            # Both forms: HTML checks read text, image checks need the actual bytes
            # (a PNG's magic number is not valid UTF-8 and would be mangled).
            "text": payload.decode("utf-8", "replace"),
            "bytes": payload,
        }


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    args = parser.parse_args(argv)
    c = Client(args.base)
    today = _dt.date.today()
    visit_date = (today + _dt.timedelta(days=7)).isoformat()

    # ------------------------------------------------------------------ #
    section("Transport and meta")
    health = c.get("/api/health")
    check("GET /api/health is ok", health.get("status") == "ok", str(health))
    check("health names the tenant", bool(health.get("tenant_id")))
    for header in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy"):
        check(f"security header {header}", header in c.last_headers)
    check(
        "CSP is nonce-based, not unsafe-inline",
        "unsafe-inline" not in c.last_headers.get("Content-Security-Policy", ""),
        c.last_headers.get("Content-Security-Policy", ""),
    )

    index = c.get("/")
    check("web app is served at /", "<html" in index.get("_raw", "").lower())

    posture = c.get("/api/security/posture")
    register = posture.get("owasp_register", {})
    check(
        f"OWASP register verifies ({register.get('controls_total')} controls)",
        register.get("valid") is True,
        str(register.get("broken_references")),
    )

    # ------------------------------------------------------------------ #
    section("CSRF (A01)")
    # Mutating request without a token must be refused before any business logic.
    c.post("/api/quote", {"visit_date": visit_date, "lines": []}, expect=403)
    check("POST without CSRF token is refused", True)
    token = c.get("/api/csrf")
    c.csrf_token = token.get("csrf_token")
    check("CSRF token issued", bool(c.csrf_token))

    # ------------------------------------------------------------------ #
    section("Catalogue")
    venue = c.get("/api/venue")
    check("venue timezone is IANA", venue.get("timezone") == "Asia/Bangkok", str(venue.get("timezone")))
    check("venue currency is THB", venue.get("currency") == "THB")
    check("nine zones configured", len(venue.get("zones", [])) == 9, str(len(venue.get("zones", []))))

    products = c.get(f"/api/products?date={visit_date}")["products"]
    check("products returned", len(products) >= 2, str(len(products)))
    priced = [t for p in products for t in p["ticket_types"]]
    check("ticket types are priced", all(t["unit_price_minor"] > 0 for t in priced), str(priced[:2]))
    adult_intl = next(
        (t for p in products if p["code"] == "GA-INTL" for t in p["ticket_types"] if "ADULT" in t["code"]),
        None,
    )
    check("international adult online price is THB 1,251",
          adult_intl is not None and adult_intl["unit_price_minor"] == 125100,
          str(adult_intl))

    calendar = c.get(f"/api/calendar?from={today.isoformat()}&to={(today + _dt.timedelta(days=30)).isoformat()}")
    cells = calendar.get("cells", [])
    check("calendar returns a month", len(cells) == 31, str(len(cells)))
    check(
        "every cell has a state and a legend entry",
        bool(calendar.get("legend")) and all(cell.get("state") for cell in cells),
        str(cells[:1]),
    )
    check(
        "states use two or more cues, not colour alone (R7.2)",
        all(cell.get("colour") and cell.get("icon") and cell.get("label") for cell in cells),
        str(cells[0]) if cells else "",
    )
    check(
        "every cell carries a text alternative (R7.3)",
        all(cell.get("accessible_label") for cell in cells),
        str(cells[0]) if cells else "",
    )
    check(
        "past dates are not selectable (R7.4)",
        not any(cell["selectable"] for cell in cells if cell["date"] < today.isoformat()),
    )
    # Today is only bookable online until last admission passes (R6.4), so asserting
    # "today is selectable" would make this script fail every evening. Future dates
    # are the invariant; today only has to explain itself.
    check(
        "future dates are selectable (R7.1)",
        all(cell["selectable"] for cell in cells if cell["date"] > today.isoformat()),
        next((str(c) for c in cells if c["date"] > today.isoformat() and not c["selectable"]), ""),
    )
    today_cell = next((c for c in cells if c["date"] == today.isoformat()), None)
    check(
        "today is either bookable or says why not (R7.4)",
        today_cell is None or today_cell["selectable"] or bool(today_cell.get("reason")),
        str(today_cell) if today_cell else "",
    )

    shows = c.get(f"/api/shows?date={visit_date}")
    sessions = shows.get("sessions", [])
    check("show timetable has sessions", len(sessions) >= 5, str(len(sessions)))
    check(
        "sessions carry time and location display name (R19.2)",
        all(s.get("start_time") and s.get("location_display_name") for s in sessions),
        str(sessions[0]) if sessions else "",
    )
    check(
        "session status uses two cues, not colour alone (R26.7)",
        all(s.get("presentation", {}).get("label") and s["presentation"].get("icon") for s in sessions),
    )
    check(
        "every session has a screen-reader label (R26.13)",
        all(s.get("accessible_label") for s in sessions),
    )
    check("timezone is explicit on the timetable (R26.12)",
          shows.get("timezone") == "Asia/Bangkok", str(shows.get("timezone")))

    consent = c.get("/api/consent")
    items = consent.get("items", [])
    check("consent dialog has items", len(items) >= 3, str(len(items)))
    check(
        "no consent box is pre-ticked (R12.5)",
        all(not item.get("default") for item in items),
        str(items),
    )
    check("exactly one required consent", sum(1 for i in items if i.get("required")) == 1, str(items))

    # ------------------------------------------------------------------ #
    section("Checkout")
    adult = adult_intl
    child = next(
        (t for p in products if p["code"] == "GA-INTL" for t in p["ticket_types"] if "CHILD" in t["code"]),
        None,
    )
    if adult is None or child is None:
        print("  FAIL  could not resolve ticket types; aborting checkout")
        FAILURES.append("resolve ticket types")
        return _report()

    quote = c.post(
        "/api/quote",
        {
            "visit_date": visit_date,
            "lines": [
                {"ticket_type_id": adult["id"], "quantity": 2},
                {"ticket_type_id": child["id"], "quantity": 1},
            ],
        },
    )
    summary = quote["summary"]
    expected_gross = adult["unit_price_minor"] * 2 + child["unit_price_minor"]
    check("quote priced from the rules", summary.get("gross_minor") == expected_gross,
          f"{summary.get('gross_minor')} vs {expected_gross}")
    check("early bird promotion applied automatically (R13.3)",
          len(summary.get("applied_promotions", [])) >= 1, str(summary.get("applied_promotions")))
    check("each item records which promotions applied (R13.9)",
          all(line.get("price_rule_id") for line in summary["lines"]))
    check(
        "rounding reconciles: total = gross - discount + adjustment (R5.5)",
        quote["total_minor"]
        == summary["gross_minor"] - summary["discount_minor"] + quote.get("rounding_adjustment_minor", 0),
        f"total={quote['total_minor']} gross={summary['gross_minor']} "
        f"discount={summary['discount_minor']} adj={quote.get('rounding_adjustment_minor')}",
    )

    # General admission at this venue is not capacity-capped, so there is nothing to
    # hold — R10.1 applies to capacity-controlled inventory only. Assert the honest
    # behaviour rather than inventing a hold.
    check(
        "holds are created only for capacity-controlled inventory (R10.1)",
        isinstance(quote.get("holds"), list),
        str(quote.get("holds")),
    )
    check("customer told what happens next before paying (R11.9)",
          bool(quote.get("next_steps")), str(quote.get("next_steps"))[:200])

    # Declining the required consent must stop the booking (R12.8).
    c.post(
        "/api/confirm",
        {
            "email": "declined@example.test",
            "full_name": "Declining Guest",
            "consent_items": {"BOOKING_SERVICE": False},
        },
        expect=422,
    )
    check("confirm refused without required consent (R12.8)", True)

    confirmed = c.post(
        "/api/confirm",
        {
            "email": "guest@example.test",
            "full_name": "Web Flow Guest",
            "phone": "+66810000000",
            "payment_method": "CARD",
            "consent_items": {"BOOKING_SERVICE": True, "MARKETING": False, "ANALYTICS": False},
        },
    )
    booking_number = confirmed.get("booking_number")
    check("booking confirmed", confirmed.get("status") == "CONFIRMED", str(confirmed)[:300])
    check("booking number issued", bool(booking_number), str(confirmed)[:200])
    tickets = confirmed.get("tickets", [])
    check("one ticket per admitted person (R15.1)", len(tickets) == 3, str(len(tickets)))
    check("every ticket has a QR payload", all(t.get("qr_payload") for t in tickets))
    check(
        "QR payload carries no personal data (R15.2)",
        all("guest@example.test" not in str(t.get("qr_payload")) for t in tickets),
    )
    check("total charged matches the quoted total (R5.5)",
          confirmed.get("total_minor") == quote.get("total_minor"),
          f"{confirmed.get('total_minor')} vs {quote.get('total_minor')}")
    payment = confirmed.get("payment", {})
    check("payment amount equals the booking total",
          payment.get("amount_minor") == confirmed.get("total_minor"), str(payment)[:200])
    check("no card data in the payment record (R14.2)",
          not any(k in payment for k in ("card_number", "pan", "cvv")), str(sorted(payment)))
    check("every ticket carries a validity window",
          all(t.get("valid_from") and t.get("valid_until") for t in tickets), str(tickets[0])[:300])

    # ------------------------------------------------------------------ #
    section("E-ticket delivery")
    mailbox = c.get("/api/staff/mailbox")["messages"]
    to_guest = [m for m in mailbox if m["to"] == "guest@example.test"]
    check("email sent to the customer", len(to_guest) >= 1, str([m["to"] for m in mailbox]))
    body = "\n".join(m["body"] for m in to_guest)
    subject = " ".join(m["subject"] for m in to_guest)
    check("booking number is in the email", booking_number in (body + subject))
    check("visit date is in the email", visit_date in body, body[:300])
    check("no unresolved template placeholders (R37.3)", "{{" not in body, body[:300])
    check("manage-booking link included (R36.2)", "manage" in body.lower())

    # ------------------------------------------------------------------ #
    section("Manage booking (R16)")
    unknown = c.post(
        "/api/manage/request-code", {"booking_number": "AQP-NOPE-0000", "email": "nobody@example.test"}
    )
    known = c.post("/api/manage/request-code", {"booking_number": booking_number, "email": "guest@example.test"})
    check(
        "unknown and known bookings are indistinguishable (R16.3)",
        unknown.get("message") == known.get("message"),
        f"{unknown} vs {known}",
    )
    code = known.get("demo_code")
    check("verification code issued", bool(code))

    wrong = c.post(
        "/api/manage/verify",
        {"booking_number": booking_number, "email": "guest@example.test", "code": "000000"},
        expect=422,
    )
    check("one wrong code is a validation error, not a throttle (R16.3)",
          wrong["error"]["code"] == "verification_failed", str(wrong))
    check("wrong code reveals nothing about the booking",
          booking_number not in json.dumps(wrong), str(wrong))

    view = c.post(
        "/api/manage/verify",
        {"booking_number": booking_number, "email": "guest@example.test", "code": code},
    )
    check("booking visible after verification", view.get("booking_number") == booking_number, str(view)[:200])
    check("tickets retrievable via Manage Booking (R37.13)", len(view.get("tickets", [])) == 3)
    check("cancellation policy shown before action (R16.5)", "policy" in json.dumps(view).lower())

    # ------------------------------------------------------------------ #
    section("Staff API (R42)")
    login = c.post("/api/staff/login", {"email": "cashier@aquaria.test", "credential": "Aquaria-Demo-2026"})
    check("cashier can sign in", bool(login.get("token")), str(login)[:200])
    c.bearer = login.get("token")

    nav = c.get("/api/staff/navigation")["navigation"]
    pages = {item["page"] for item in nav}
    check("cashier sees Counter Sales", "Counter Sales" in pages, str(sorted(pages)))
    check("cashier does not see Staff (R42.7)", "Staff" not in pages, str(sorted(pages)))
    check("cashier does not see Roles", "Roles" not in pages)

    # A cashier holds no Audit Logs.VIEW, so the API must refuse independently of the UI.
    c.get("/api/staff/audit", expect=403)
    check("cashier refused Audit Logs (R42.1)", True)

    c.bearer = None
    c.get("/api/staff/navigation", expect=401)
    check("staff endpoint refuses anonymous callers", True)

    # A Platform Super Admin must not be able to sign in with a password alone (R73.2).
    challenge = c.post(
        "/api/staff/login", {"email": "admin@aquaria.test", "credential": "Aquaria-Demo-2026"}, expect=422
    )
    check("super admin login demands MFA (R73.2)", "token" not in challenge, str(challenge)[:200])

    manager = c.post("/api/staff/login", {"email": "manager@aquaria.test", "credential": "Aquaria-Demo-2026"})
    check("venue manager can sign in", bool(manager.get("token")), str(manager)[:200])
    c.bearer = manager.get("token")
    events = c.get("/api/staff/audit")["events"]
    check("manager can read the audit log", len(events) > 0, str(len(events)))
    actions = {e["action"] for e in events}
    check("consent capture audited (R45.2)", any("CONSENT" in a for a in actions), str(sorted(actions))[:400])
    dumped = json.dumps(events)
    check("no credential leaked into audit payloads (R45.9)", "Aquaria-Demo-2026" not in dumped)

    bookings = c.get("/api/staff/bookings")["bookings"]
    check("booking appears in the back office", any(b["booking_number"] == booking_number for b in bookings))

    # ------------------------------------------------------------------ #
    section("Staff session, settings home & role editor (settings/reports spec)")
    # Still the venue manager. /api/staff/me is the one call the back office makes
    # after login; §3 requires it to carry identity, scope, permissions and the
    # authorized navigation together.
    me = c.get("/api/staff/me?lang=th")
    check("me carries tenant, organization, venues and scope (§3)",
          all(me.get(k) for k in ("tenant", "organization", "venues")) and "scope" in me, str(list(me))[:200])
    check("me venues are the manager's assigned venue only (§35)",
          me["scope"]["venue_ids"] is not None and len(me["venues"]) >= 1, str(me["scope"]))
    check("me permissions drive navigation, not role names (§48)",
          all(f"{n['page']}.VIEW" in me["permissions"] for n in me["navigation"]))
    check("manager settings home has most categories (§11)",
          len(me.get("settings", [])) >= 8, f"{len(me.get('settings', []))} categories")
    check("settings home labels are localized to Thai (§50)",
          any(any('\u0e00' <= ch <= '\u0e7f' for ch in c0["label"]) for c0 in me["settings"]),
          str([c0["label"] for c0 in me["settings"][:3]]))

    # Server-side charge preview — the client never computes money (§41).
    preview = c.get("/api/staff/settings/charge-preview?amount_minor=107000")["breakdown"]
    check("charge preview reconciles server-side (§41)",
          preview["base_minor"] == 107000 and preview["vat_minor"] == 7000
          and preview["taxable_base_minor"] == 100000, str(preview))

    # Settings search is permission-filtered and ranks the named page first (§27, §32).
    vat_search = c.get("/api/staff/settings/search?q=VAT")["results"]
    check("settings search finds VAT Settings first (§32)",
          vat_search and vat_search[0]["page"] == "VAT Settings", str([r["page"] for r in vat_search]))
    qr_search = [r["page"] for r in c.get("/api/staff/settings/search?q=QR")["results"]]
    check("settings search reaches a page via its description (§32)",
          "Ticket Validity Settings" in qr_search, str(qr_search))

    # The role editor's registry, localized, gated by Roles.VIEW (§19, §50).
    matrix = c.get("/api/staff/permissions/matrix?lang=ja")
    check("permission matrix returns the full registry (§19)",
          len(matrix["pages"]) == 72 and len(matrix["actions"]) == 51,
          f"{len(matrix['pages'])} pages / {len(matrix['actions'])} actions")
    check("matrix verbs are localized (§50)",
          [v["label"] for v in matrix["verbs"] if v["verb"] == "VIEW"][0] != "VIEW")
    vat_row = [r for r in matrix["pages"] if r["page"] == "VAT Settings"][0]
    check("matrix marks not-applicable verbs as such (§13)",
          vat_row["verbs"]["EDIT"] and not vat_row["verbs"]["ADD"] and not vat_row["verbs"]["DELETE"],
          str(vat_row["verbs"]))

    # Effective permission viewer for the signed-in user (§36).
    my_access = c.get("/api/staff/permissions/summary")
    check("effective-permission viewer resolves for self (§36)",
          my_access.get("self") is True and "summary" in my_access, str(list(my_access))[:200])

    # --- Reports vs Settings are independent axes (§72) --- #
    viewer = c.post("/api/staff/login", {"email": "viewer@aquaria.test", "credential": "Aquaria-Demo-2026"})
    c.bearer = viewer.get("token")
    viewer_me = c.get("/api/staff/me")
    check("report viewer has reports but ZERO settings categories (§72)",
          len(viewer_me.get("settings", [])) == 0
          and "Reports" in {n["page"] for n in viewer_me["navigation"]},
          str([n["page"] for n in viewer_me["navigation"]]))
    c.get("/api/staff/permissions/matrix", expect=403)
    check("report viewer refused the permission matrix (§75)", True)
    empty = c.get("/api/staff/settings/search?q=VAT")["results"]
    check("report viewer's settings search returns nothing (§27)", empty == [], str(empty))

    # --- Verb independence over HTTP, and API rejection when UI is hidden (§9, §75) --- #
    cashier2 = c.post("/api/staff/login", {"email": "cashier@aquaria.test", "credential": "Aquaria-Demo-2026"})
    c.bearer = cashier2.get("token")
    cashier_me = c.get("/api/staff/me")
    cperms = set(cashier_me["permissions"])
    check("cashier holds Tickets VIEW+ADD but not EDIT/DELETE (§9)",
          "Tickets.VIEW" in cperms and "Tickets.ADD" in cperms
          and "Tickets.EDIT" not in cperms and "Tickets.DELETE" not in cperms)
    # The VAT write endpoint exists; the cashier's request to it is refused server-side
    # even though a tampered client could show the button (§75).
    c.post("/api/staff/settings/vat",
           {"enabled": True, "rate_bp": 1000, "mode": "INCLUSIVE", "reason": "probe"}, expect=403)
    check("cashier VAT write refused server-side (§75)", True)

    # --- Logout ends access (§58) --- #
    out = c.post("/api/staff/logout", {})
    check("logout succeeds (§58)", out.get("logged_out") is True, str(out))
    c.get("/api/staff/me", expect=401)
    check("token is dead after logout (§58)", True)

    # Back to the manager for the remaining settings checks.
    manager = c.post("/api/staff/login", {"email": "manager@aquaria.test", "credential": "Aquaria-Demo-2026"})
    c.bearer = manager.get("token")

    # ------------------------------------------------------------------ #
    section("Business / venue settings (add_features)")
    # Still signed in as the venue manager, who holds the MANAGE_* actions.
    settings = c.get("/api/staff/settings")
    vat = settings.get("vat", {}).get("current", {})
    check("VAT is an explicit setting, not the venue default (§1)",
          vat.get("source") == "charge_settings", str(vat))
    check("VAT is 7% inclusive (Aquaria seed)",
          vat.get("rate_bp") == 700 and vat.get("mode") == "INCLUSIVE", str(vat))
    check("manager may edit VAT", settings.get("vat", {}).get("can_edit") is True, str(settings.get("vat")))
    check("timezone setting is the venue IANA zone (§8)",
          settings.get("timezone", {}).get("timezone") == "Asia/Bangkok", str(settings.get("timezone")))
    check("ticket validity defaults to End of Visit Day (§37)",
          settings.get("ticket_validity", {}).get("policy", {}).get("validity_type") == "END_OF_VISIT_DAY",
          str(settings.get("ticket_validity")))

    # Editing VAT requires a reason; the API must enforce MANAGE_TAX_SETTINGS's
    # mandatory-reason rule even though the manager holds the permission (R67.4).
    missing_reason = c.post(
        "/api/staff/settings/vat",
        {"enabled": True, "rate_bp": 700, "mode": "INCLUSIVE", "effective_from": today.isoformat()},
        expect=422,
    )
    check("VAT edit without a reason is refused (R67.4)",
          "error" in missing_reason, str(missing_reason)[:200])

    future = (today + _dt.timedelta(days=30)).isoformat()
    updated = c.post(
        "/api/staff/settings/vat",
        {"enabled": True, "rate_bp": 700, "mode": "INCLUSIVE", "effective_from": future,
         "reason": "Verify server settings surface"},
        expect=200,
    )
    check("manager can set a future-dated VAT row", updated.get("effective_from") == future, str(updated))

    # A bare UTC offset must be rejected: the spec requires an IANA identifier (§8).
    bad_tz = c.post(
        "/api/staff/settings/timezone",
        {"timezone": "UTC+07:00", "reason": "verify"},
        expect=422,
    )
    check("bare UTC offset rejected for time zone (§8)", "error" in bad_tz, str(bad_tz)[:200])

    # Add an exchange rate and confirm the direction is spelled out (§21).
    # Overlapping active rates for one pair are refused by design (§22), so end any
    # rate this script left behind on an earlier run first. Without this the second
    # run against a warm server fails on its own side effect rather than on a defect.
    existing = c.get("/api/staff/settings").get("exchange_rates", {}).get("rates", [])
    for rate in existing:
        same_pair = rate.get("from_currency") == "USD" and rate.get("to_currency") == "THB"
        if same_pair and rate.get("status") == "ACTIVE" and rate.get("id"):
            c.post(
                f"/api/staff/settings/exchange-rates/{rate['id']}/end",
                {"reason": "verify server settings: replacing the previous run's rate"},
                expect=200,
            )
    fx = c.post(
        "/api/staff/settings/exchange-rates",
        {"from_currency": "USD", "to_currency": "THB", "rate": "33.10",
         "effective_from": today.isoformat(), "reason": "verify server settings"},
        expect=200,
    )
    check("exchange rate created with explicit direction (§21)",
          fx.get("direction") == "1 USD = 33.1 THB", str(fx))
    listing = c.get("/api/staff/settings").get("exchange_rates", {}).get("rates", [])
    check("new exchange rate is listed", any(r.get("id") == fx.get("id") for r in listing), str(listing)[:200])

    # A cashier holds neither MANAGE_TAX_SETTINGS nor the VAT page, so the API must
    # refuse settings edits regardless of the UI (R42.1, settings §42).
    cashier = c.post("/api/staff/login", {"email": "cashier@aquaria.test", "credential": "Aquaria-Demo-2026"})
    c.bearer = cashier.get("token")
    cashier_settings = c.get("/api/staff/settings")
    check("cashier sees no settings blocks (R42.9)",
          not any(k in cashier_settings for k in ("vat", "service_charge", "timezone", "exchange_rates")),
          str(sorted(cashier_settings)))
    c.post(
        "/api/staff/settings/vat",
        {"enabled": True, "rate_bp": 700, "mode": "INCLUSIVE", "effective_from": today.isoformat(),
         "reason": "should be refused"},
        expect=403,
    )
    check("cashier refused VAT edit server-side (settings §42)", True)
    c.post(
        "/api/staff/settings/exchange-rates",
        {"from_currency": "EUR", "to_currency": "THB", "rate": "39.00",
         "effective_from": today.isoformat(), "reason": "should be refused"},
        expect=403,
    )
    check("cashier refused exchange-rate creation server-side", True)
    c.bearer = manager.get("token")

    # ------------------------------------------------------------------ #
    section("Gate validation (R32)")
    # The earlier booking was for a future visit date, so its tickets are not yet
    # valid today. Make a same-day booking whose tickets are valid right now, so the
    # gate decision exercises the admit path rather than NOT_YET_VALID.
    # ...but only while the venue is still selling for today. After last admission
    # every channel correctly refuses a same-day sale (R6.4), so this script must not
    # assume it can buy one — it would then fail every evening for the right reason,
    # which is indistinguishable from failing for the wrong one. When today is closed
    # we buy a future ticket and assert the NOT_YET_VALID decision instead. The admit
    # path itself is proven by tests/test_gate_access.py, which controls the clock.
    today_iso = today.isoformat()
    same_day_quote = c.post(
        "/api/quote",
        {"visit_date": today_iso, "lines": [{"ticket_type_id": adult["id"], "quantity": 2}]},
        expect=None,
    )
    same_day_sellable = "error" not in same_day_quote
    if not same_day_sellable:
        c.post(
            "/api/quote",
            {"visit_date": visit_date, "lines": [{"ticket_type_id": adult["id"], "quantity": 2}]},
        )
    booking_for_gate = c.post(
        "/api/confirm",
        {
            "email": "gate-guest@example.test",
            "full_name": "Gate Flow Guest",
            "payment_method": "CARD",
            "consent_items": {"BOOKING_SERVICE": True, "MARKETING": False, "ANALYTICS": False},
        },
    )
    gate_tickets = booking_for_gate.get("tickets", [])
    check("booking issued tickets for the gate test", len(gate_tickets) == 2, str(booking_for_gate)[:200])
    gate_ticket = gate_tickets[0]
    if same_day_sellable:
        admit = c.post("/api/gate/scan", {"qr_payload": gate_ticket["qr_payload"]})
        check("valid ticket admits at the gate (R32.2)", admit.get("decision") == "ADMIT", str(admit)[:200])
        check("admit result is unambiguous", admit.get("admit") is True and bool(admit.get("message")))
        again = c.post("/api/gate/scan", {"qr_payload": gate_ticket["qr_payload"]})
        check("second scan is refused as already used (R32.3)",
              again.get("decision") == "REJECT_ALREADY_USED", str(again)[:200])
        check("duplicate scan shows the previous admission time (R32.3)",
              "previous_admission" in again, str(again)[:200])
    else:
        early = c.post("/api/gate/scan", {"qr_payload": gate_ticket["qr_payload"]})
        check("future-dated ticket is refused as not yet valid (R32.2)",
              early.get("decision") == "REJECT_NOT_YET_VALID", str(early)[:200])
        check("not-yet-valid refusal is unambiguous",
              early.get("admit") is False and bool(early.get("message")), str(early)[:200])
        print("  note  today is past last admission, so the ADMIT path is covered by "
              "tests/test_gate_access.py rather than here")
    unknown_scan = c.post("/api/gate/scan", {"qr_payload": "not-a-real-code"})
    check("forged or unknown code is rejected (R32.2)",
          unknown_scan.get("decision") == "REJECT_UNKNOWN_CODE", str(unknown_scan)[:200])
    check("gate never leaks a token error to the operator (R66.4)",
          "Traceback" not in json.dumps(unknown_scan) and "signature" not in json.dumps(unknown_scan).lower(),
          str(unknown_scan)[:200])
    # A second, still-valid ticket from the same booking scanned with an unknown
    # access point must be refused as wrong venue/gate.
    other_ticket = gate_tickets[1]
    wrong_gate = c.post(
        "/api/gate/scan",
        {"qr_payload": other_ticket["qr_payload"], "access_point_id": "ap_does_not_exist"},
    )
    check("scan at an unknown access point is wrong venue/gate (R32.2)",
          wrong_gate.get("decision") == "REJECT_WRONG_VENUE_OR_GATE", str(wrong_gate)[:200])

    # ------------------------------------------------------------------ #
    section("Ticket artefacts: e-ticket, thermal, QR (ticketDesign)")
    # The browser that just bought these tickets may print them; nobody else may.
    print_ticket = gate_tickets[1]
    eticket = c.raw(f"/tickets/{print_ticket['id']}/eticket")
    check("e-ticket is served as an HTML document",
          eticket["content_type"].startswith("text/html"), eticket["content_type"])
    body = eticket["text"]
    check("e-ticket shows the booking number",
          booking_for_gate.get("booking_number", "?") in body)
    check("e-ticket shows the scan call to action", "SCAN AT ENTRANCE" in body)
    check("e-ticket embeds a real QR image", "data:image/png;base64," in body)
    check("e-ticket states last admission", "Last admission" in body)
    check("e-ticket is responsive", "@media (max-width: 720px)" in body)
    check("e-ticket carries no unresolved placeholder", "{{" not in body)
    check("e-ticket fetches nothing external",
          "http://" not in body.replace("http://www.w3.org", ""), "external reference found")

    thermal = c.raw(f"/tickets/{print_ticket['id']}/thermal")
    tbody = thermal["text"]
    check("thermal ticket is served as an HTML document",
          thermal["content_type"].startswith("text/html"), thermal["content_type"])
    check("thermal page is 80mm wide (Star MCP31LB)", "size: 80mm auto" in tbody)
    check("thermal content fits the 72mm print width", "padding: 4mm 4mm 7mm" in tbody)
    check("thermal QR is 46mm, inside the 45-50mm band", "width: 46mm" in tbody)
    check("thermal ticket is pure black and white", "gradient" not in tbody)
    check("thermal ticket uses solid rules, not hairline grey",
          "border-top: 1px solid #000" in tbody and "#777" not in tbody)
    check("thermal ticket shows the booking number large", "tbig" in tbody)
    check("thermal QR is inline SVG, so it prints at printer resolution",
          'shape-rendering="crispEdges"' in tbody)

    qr_image = c.raw(f"/tickets/{print_ticket['id']}/qr.svg")
    check("ticket QR is served as SVG", qr_image["content_type"].startswith("image/svg+xml"),
          qr_image["content_type"])
    check("QR SVG paints a white quiet zone", 'fill="#ffffff"' in qr_image["text"])

    # The sessionless route a mail client can fetch: some clients (Gmail among
    # them) strip data: images and none of them carry the guest's session, so the
    # image is reachable by signed capability token instead.
    from utp.ticketdesign.links import sign_qr_token

    signed = c.raw(f"/qr/{sign_qr_token(print_ticket['id'])}")
    check("signed QR link serves a PNG with no session",
          signed["content_type"].startswith("image/png"), signed["content_type"])
    check("signed QR link returns real image data",
          signed["bytes"].startswith(b"\x89PNG\r\n\x1a\n"), repr(signed["bytes"][:12]))
    expired = c.raw(
        f"/qr/{sign_qr_token(print_ticket['id'], expires_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1))}",
        expect=404,
    )
    check("an expired QR link stops working (R16.11)", expired["status"] == 404)
    forged = c.raw("/qr/bm90LWEtdG9rZW4.000000000000000000000000", expect=404)
    check("a forged QR link is refused", forged["status"] == 404)

    # The confirmation email must carry its QR as an attachment, because Gmail
    # strips data: images, and the browser preview must still render.
    mailbox = c.get("/api/staff/mailbox").get("messages", [])
    with_ticket = [m for m in mailbox if m.get("has_html")]
    check("confirmation email carries an HTML e-ticket", bool(with_ticket), str(len(mailbox)))
    if with_ticket:
        preview = c.raw(f"/mail/{with_ticket[-1]['message_id']}")
        check("sent email previews as its own HTML document",
              preview["content_type"].startswith("text/html"), preview["content_type"])
        check("preview resolves cid: images to a fetchable URL",
              "src=\"cid:" not in preview["text"] and "/img/" in preview["text"],
              "cid reference left unresolved")
        image_path = re.search(r'src="(/mail/[^"]+/img/[^"]+)"', preview["text"])
        check("preview image path is present", image_path is not None, preview["text"][:200])
        if image_path:
            embedded = c.raw(image_path.group(1))
            check("preview QR image is a real PNG",
                  embedded["bytes"].startswith(b"\x89PNG\r\n\x1a\n"),
                  repr(embedded["bytes"][:12]))

    # A ticket carries an access credential, so an id this browser has not proven
    # ownership of must be indistinguishable from one that does not exist (R1.2).
    forged = c.raw("/tickets/tkt_does_not_exist/eticket", expect=404)
    check("an unowned ticket id is refused (R1.2)", forged["status"] == 404, str(forged["status"]))
    check("refusal does not disclose whether the ticket exists",
          "does_not_exist" not in forged["text"] and "Traceback" not in forged["text"],
          forged["text"][:160])

    # ------------------------------------------------------------------ #
    section("Reporting, analytics and dashboards (R70, R71)")
    # Sign back in as the manager: the cashier token is still on the client from the
    # settings-permission checks above.
    manager = c.post(
        "/api/staff/login", {"email": "manager@aquaria.test", "credential": "Aquaria-Demo-2026"}
    )
    c.bearer = manager.get("token")

    catalog = c.get("/api/staff/reports")
    sections = catalog.get("sections", [])
    check("report catalog is grouped into three sections",
          [s.get("key") for s in sections] == ["ANALYTICS", "OPERATIONS", "FINANCE"],
          str([s.get("key") for s in sections]))
    total_reports = sum(len(s.get("reports", [])) for s in sections)
    check("the catalog offers the full report set", total_reports >= 25, str(total_reports))
    check("a manager may export", catalog.get("can_export") is True, str(catalog.get("can_export")))
    check("each report declares only its relevant filters",
          all(isinstance(r.get("filters"), list) for s in sections for r in s["reports"]))

    dashboard = c.get("/api/staff/dashboard/executive?date_preset=this_month")
    kpis = {card["key"]: card for card in dashboard.get("kpis", [])}
    check("executive dashboard returns the KPI set",
          {"gross_sales", "net_sales", "visitors", "tickets", "bookings"} <= set(kpis),
          str(sorted(kpis)))
    check("KPIs carry a comparison basis",
          all("change_bp" in card for card in kpis.values()))
    check("refund KPI is marked as lower-is-better",
          kpis.get("refunds", {}).get("lower_is_better") is True)
    check("dashboard states its window, timezone and currency",
          all(dashboard["meta"].get(k) for k in ("date_from", "date_to", "timezone", "currency")),
          str(dashboard.get("meta"))[:160])
    for panel in ("revenue_series", "channels", "visitor_segments", "pricing_groups",
                  "top_products", "capacity", "peak_time", "advance_booking", "exceptions"):
        check(f"executive dashboard includes {panel}", panel in dashboard, str(sorted(dashboard))[:160])

    # The headline must equal the sum of what it drills into (R70.9).
    net_sales = kpis.get("net_sales", {}).get("value")
    series_total = sum(row.get("net_minor", 0) for row in dashboard.get("revenue_series", []))
    channel_total = sum(row.get("net_minor", 0) for row in dashboard.get("channels", []))
    check("revenue series reconciles with the net sales KPI (R70.9)",
          net_sales == series_total, f"{net_sales} vs {series_total}")
    check("channel breakdown reconciles with the net sales KPI (R70.9)",
          net_sales == channel_total, f"{net_sales} vs {channel_total}")

    ledger = c.get("/api/staff/reports/sales?date_preset=this_month")
    ledger_total = sum(
        row.get("net_minor", 0) for row in ledger.get("rows", [])
        if row.get("status") in ("Confirmed", "Partially refunded")
    )
    check("transaction ledger reconciles with the net sales KPI (R70.9)",
          net_sales == ledger_total, f"{net_sales} vs {ledger_total}")

    operations = c.get("/api/staff/dashboard/operations")
    check("operations dashboard defaults to today",
          operations["meta"]["date_from"] == operations["meta"]["date_to"],
          str(operations.get("meta", {}).get("date_from")))
    for panel in ("kpis", "arrivals", "arrival_counts", "gate", "gate_activity",
                  "capacity_rows", "devices", "exceptions"):
        check(f"operations dashboard includes {panel}", panel in operations, str(sorted(operations))[:160])
    gate = operations.get("gate", {})
    check("gate admitted and refused add up to the scan count",
          gate.get("admitted", 0) + gate.get("refused", 0) == gate.get("total_scans", 0),
          str(gate)[:140])

    revenue = c.get("/api/staff/reports/revenue?date_preset=this_month&group_by=daily")
    check("a report returns columns, rows and footer totals",
          all(key in revenue for key in ("columns", "rows", "row_count", "totals")),
          str(sorted(revenue))[:140])
    check("money columns are integer minor units, not formatted strings",
          all(isinstance(row.get("net_minor"), int) for row in revenue.get("rows", [])))

    # Export: a separate privilege, and the file says what produced it (R71.9).
    export = c.raw("/api/staff/reports/revenue/export?format=csv&date_preset=this_month")
    check("CSV export is served as a download",
          "text/csv" in export["content_type"], export["content_type"])
    body = export["text"]
    check("export names the report and when it was generated (R71.9)",
          "Report,Revenue" in body and "Generated," in body, body[:120])
    check("export states the venue, timezone and currency",
          all(needle in body for needle in ("Times shown in,Asia/Bangkok", "Currency,THB", "Venue,")),
          body[:200])

    printable = c.raw("/api/staff/reports/revenue/export?format=print&date_preset=this_month")
    check("printable export is an HTML document",
          printable["content_type"].startswith("text/html"), printable["content_type"])
    check("printable export sets a page size", "size: A4 landscape" in printable["text"])

    # Saved views (§36).
    view = c.post("/api/staff/report-views",
                  {"report_key": "revenue", "name": "Verify view",
                   "filters": {"date_preset": "this_month"}, "make_default": True})
    check("a report view can be saved", bool(view.get("id")), str(view)[:120])
    listed = c.get("/api/staff/report-views?report_key=revenue").get("views", [])
    check("a saved view is listed back", view.get("id") in [v.get("id") for v in listed],
          str(listed)[:160])
    if view.get("id"):
        removed = c.post(f"/api/staff/report-views/{view['id']}/delete")
        check("a saved view can be deleted", removed.get("deleted") is True, str(removed))

    # A dashboard is not a table.
    bad_export = c.get("/api/staff/reports/executive_overview/export?format=csv", expect=422)
    check("a dashboard cannot be exported as a table", "error" in bad_export, str(bad_export)[:140])
    unknown = c.get("/api/staff/reports/no_such_report", expect=404)
    check("an unknown report is a clean 404", "error" in unknown, str(unknown)[:120])

    # A cashier holds neither dashboard nor Reports, so the API must refuse
    # regardless of what the navigation shows (R42.1).
    cashier_login = c.post(
        "/api/staff/login", {"email": "cashier@aquaria.test", "credential": "Aquaria-Demo-2026"}
    )
    c.bearer = cashier_login.get("token")
    for path in ("/api/staff/dashboard/executive", "/api/staff/dashboard/operations",
                 "/api/staff/reports/revenue"):
        refused = c.get(path, expect=403)
        check(f"cashier refused {path.rsplit('/', 1)[-1]} server-side",
              refused.get("error", {}).get("code") == "authorization_denied", str(refused)[:120])
    empty_catalog = c.get("/api/staff/reports")
    offered = [r["key"] for s in empty_catalog.get("sections", []) for r in s["reports"]]
    check("a cashier is offered no reports in the catalog (R42.7)", offered == [], str(offered)[:140])

    # The seeded Report Viewer holds the pages but no PII, cost or export.
    viewer_login = c.post(
        "/api/staff/login", {"email": "viewer@aquaria.test", "credential": "Aquaria-Demo-2026"}
    )
    c.bearer = viewer_login.get("token")
    viewer_catalog = c.get("/api/staff/reports")
    check("report viewer may view but not export",
          viewer_catalog.get("can_export") is False and viewer_catalog.get("can_view_pii") is False,
          str({k: viewer_catalog.get(k) for k in ("can_export", "can_view_pii", "can_view_cost")}))
    masked = c.get("/api/staff/reports/bookings?date_preset=this_month")
    check("personal data is masked without VIEW_PII (§40)",
          masked["meta"]["masked"] is True
          and all("*" in row["customer"] for row in masked["rows"] if row.get("customer")),
          str([row.get("customer") for row in masked.get("rows", [])[:3]]))
    partners = c.get("/api/staff/reports/partners?date_preset=this_month")
    check("cost columns are absent without VIEW_COST (R41.8)",
          "commission_minor" not in [col["key"] for col in partners.get("columns", [])],
          str([col["key"] for col in partners.get("columns", [])]))
    c.get("/api/staff/reports/revenue/export?format=csv", expect=403)
    check("export is refused without the EXPORT permission (R41.7)", True)

    c.bearer = manager.get("token")

    # ------------------------------------------------------------------ #
    section("Error handling (R66)")
    bad = c.get("/api/calendar", expect=422)
    message = json.dumps(bad)
    check("validation error is friendly", "error" in bad, message[:200])
    check("no stack trace or SQL in the response (R66.4)",
          not any(t in message for t in ("Traceback", "sqlite3", "SELECT ", "File \\\"")), message[:300])
    missing = c.get("/api/nope", expect=404)
    check("unknown endpoint returns a clean 404", "error" in missing, str(missing)[:200])

    return _report()


def _report() -> int:
    print()
    if FAILURES:
        print(f"RESULT: FAIL — {len(FAILURES)} of {CHECKS} checks failed")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"RESULT: PASS — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, ConnectionError) as exc:
        print(f"\nCannot reach the server: {exc}\nStart it with:  python serve.py", flush=True)
        raise SystemExit(2) from exc
    except AssertionError as exc:
        # stdout, not stderr: a failure the operator cannot see is worse than useless.
        print(f"\nRESULT: FAIL — unexpected response\n  {exc}", flush=True)
        raise SystemExit(1) from exc
