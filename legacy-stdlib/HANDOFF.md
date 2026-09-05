# Handoff — Universal Ticketing Platform (Aquaria Phuket)

Snapshot of where the build is, so the next session can continue without re-deriving
context. Steering files in `.kiro/steering/` (product, tech, conventions) hold the
durable rules; this file is the point-in-time status and the to-do list.

## How to run it right now

```
python serve.py            # http://127.0.0.1:8080  (adds --fresh, --port, --db :memory:)
python run_tests.py        # 112 tests, expect "RESULT: PASS"
python verify_server.py    # end-to-end HTTP checks against a running server (72 checks)
python smoke_check.py      # in-process service smoke test
```

Demo staff credential `Aquaria-Demo-2026`; sign-in emails `manager@`, `cashier@`,
`gate@aquaria.test`. `admin@aquaria.test` (Super Admin) requires MFA and cannot sign in
with a password alone — MFA enrolment is not wired into the demo UI, so use `manager@`
for back-office work in the browser.

Constraints that bite: Python 3.14 stdlib-only (plus `tzdata`/`psycopg`/`boto3`), package
is `utp` (never `platform`), **no git/npm on this machine**, Windows PowerShell (`;` not
`&&`, no heredocs). See `.kiro/steering/tech.md`.

## What is done and verified

- **Core** (`utp/core`): errors, ids, clock (IANA tz), money, schema (v2), db (SQLite),
  context, audit, config resolution, i18n. All exercised by the suite.
- **Domain** (`utp/domain`): enums, permission registry (38 pages × VIEW/ADD/EDIT/DELETE +
  35 action permissions + 11 role templates), cart.
- **Services** (`utp/services`): authz, staff, tenancy, catalog, pricing, **settings**,
  calendar_rules, inventory, promotions, consent, customers, payments, tickets, booking,
  notifications, documents, shows.
- **Security** (`utp/security`): owasp register (self-verifying), validation, headers
  (nonce CSP), csrf, ssrf, uploads, ratelimit, secrets, monitoring.
- **API** (`utp/api/server.py`): threaded HTTP server, router, CSRF, security headers,
  static serving of the web app.
- **Web** (`web/`): plain HTML/CSS/JS booking flow, Peacock Blue, mobile-first, CSP-safe.
- **Business/Venue Settings module** (the most recent work): VAT + service charge
  (effective-dated, inclusive/exclusive), `compute_charges` engine with the fixed
  calculation order and Cases A–D, IANA timezone setting, QR/ticket validity policies
  (default End-of-Visit-Day = 23:59:59 venue-local), base currency, exchange-rate
  management (decimal-exact, no duplicate active pairs, direction "1 USD = 33.10 THB"),
  historical snapshots on orders and tickets, 6 permission pages + 6 `MANAGE_*` actions.

**Tests: 112 pass.** `tests/test_security_owasp.py` (OWASP controls),
`tests/test_payment_to_eticket.py` (payment→e-ticket edge cases),
`tests/test_settings_charges.py` (Case A–D charges, currency, validity/expiry, snapshot
immutability, settings permission enforcement).

Verified behaviours: full booking → payment → e-ticket flow; charge Cases A–D against
the spec's worked numbers; a completed order does not move when VAT later changes; a
ticket's expiry does not move when the venue timezone changes; default expiry is exactly
23:59:59 Bangkok on the visit date; a cashier is refused `MANAGE_*` server-side.

## Not yet implemented (explicitly `None` on `Platform`)

| Area | Requirements | State |
|---|---|---|
| Seating / seat maps | R47–R62 | not started (`platform.seating is None`) |
| Counter POS | R34 | not started (`platform.counter is None`) |
| Partner / agent API | R35 | not started (`platform.partners is None`) |
| Reporting & dashboards | R70–R71 | not started (`platform.reporting is None`) |
| PostgreSQL + S3 | (user request) | not done — unverifiable locally, still on SQLite |

## Recently completed (this session)

- **Settings module surfaced (task 8 of 8):** `/api/staff/settings` (overview) plus
  POST endpoints for VAT, service charge, timezone, ticket validity, base currency and
  exchange rates; Aquaria's 7% inclusive VAT now seeded explicitly via `set_vat`;
  back-office Settings UI in the Operations view (grouped per spec §25); HTTP checks in
  `verify_server.py`.
- **Gate / access validation (R32):** new `utp/services/access.py` (`AccessService`),
  wired on `Platform.access` (no longer `None`). `POST /api/gate/scan` returns a real
  decision (device auth optional); `POST /api/gate/override` (needs `OVERRIDE_ACCESS` +
  reason); `GET /api/gate/lookup` (manual booking lookup). Decisions recorded in the
  append-only `scan_events` table; offline sync flags same-single-entry-ticket-at-two-
  gates conflicts (`OFFLINE_SCAN_CONFLICT`); a gate scanner tool is in the web
  Operations view. Covered by `tests/test_gate_access.py` (17 tests).

**Status: 129 tests pass; verify_server.py 93 checks pass.**

## Previous next task (now done — kept for context: settings module, task 8 of 8)

The settings *engine and service* are done and tested; what remains is surfacing them:

1. **API endpoints** under `/api/staff/settings/...` for VAT, service charge, timezone,
   ticket validity, currency and exchange rates — GET to read, POST/PATCH to change,
   each calling the matching `SettingsService` method (which already enforces the
   `MANAGE_*` permission and audits). Follow the existing staff-endpoint pattern in
   `utp/api/server.py` (`staff_context`, `require_page`).
2. **Back-office navigation** grouping per settings spec §25 (Business / Tax & Charges /
   Ticket & Access / Currency). The permission pages already carry the right groups.
3. **Seed**: configure Aquaria's 7% inclusive VAT through `SettingsService.set_vat`
   (currently it relies on the venue's legacy `tax_model`/`tax_rate_bp`, which
   `get_charge` falls back to — correct, but the spec wants it as an explicit setting).
4. Re-run `run_tests.py` and `verify_server.py`; add HTTP-level settings checks to
   `verify_server.py`.

## Recommended order after that

Gate validation (R32) next — it builds directly on the ticket validity snapshot just
finished, and the platform is not operable end-to-end until you can admit a guest. Then
counter POS (R34, needs the tax engine), reporting (R70–R71), seating (R47–R62), partner
API (R35). PostgreSQL/S3 is best done once the domain settles; it needs a reachable
Postgres and S3 credentials to verify.

## Known gaps / debts

- Eight pre-settings services (tenancy, catalog, pricing, calendar, inventory, promotions,
  consent, shows) have no dedicated unit tests — only `smoke_check.py` and the
  integration paths touch them. Backfill when convenient.
- Secrets are development placeholders (`serve.py` warns "12 of 12 secrets are
  development placeholders"). Set `UTP_SECRET_*` and `UTP_SIGNING_KEY` before any real
  use.
- No repo yet: git is not installed. When it is, `git init`, then commit — a `.gitignore`
  is already in place (ignores `data/`, `__pycache__/`, probe scripts).
