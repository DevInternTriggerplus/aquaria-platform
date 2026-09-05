# Technology & Architecture

## Stack and hard constraints

- **Python 3.14, standard library only.** The only third-party packages permitted are
  `tzdata` (for `zoneinfo`), `psycopg` and `boto3`. No web framework, no ORM, no test
  framework beyond `unittest`. Do not add dependencies.
- **The package is `utp`** — never `platform`, which would shadow the stdlib module.
- **No Node / npm / git** are installed on this machine. The web front end is plain
  HTML/CSS/JS with no build step. Do not write commands that assume git exists; if a
  commit is needed, tell the user rather than attempting it.
- **Windows + PowerShell.** Use `;` not `&&`. No heredocs (`<<'PY'` fails) — write a
  temporary `.py` file instead of piping a script into `python -`. `unittest` writes to
  stderr, so a passing run can still exit non-zero under some shells; filter output with
  `Select-String` and trust the printed `RESULT: PASS`.

## Layout

```
utp/core/      errors, ids, clock, money, schema, db, context, audit, config, i18n
utp/domain/    enums, permissions (pages + actions + role templates), cart
utp/services/  authz, staff, tenancy, catalog, pricing, settings, calendar_rules,
               inventory, promotions, consent, customers, payments, tickets, booking,
               notifications, documents, shows
utp/security/  owasp, validation, headers, csrf, ssrf, uploads, ratelimit, secrets, monitoring
utp/api/       server.py (ThreadingHTTPServer, router, CSRF, security headers, static)
web/           index.html, styles.css, app.js  (Peacock Blue, mobile-first, CSP-safe)
```

`utp/app.py` is the **composition root** — one `Platform` object wires every service,
the database, clock, audit log, config store and security layer. Tests, the HTTP API,
`seed.py` and background jobs all build the same `Platform`, so a rule proven in a test
is the rule the API runs. Cross-service references that would be circular (e.g.
`calendar.availability_fn`, `booking.notifications`, `tickets.settings`) are injected
**after** construction in `Platform._wire()`, not passed to constructors.

## Database

- **SQLite for now**, backed by an ephemeral temp *file* for `:memory:` (not shared-cache
  memory — shared-cache ignores `busy_timeout` and breaks concurrency tests). The user
  intends PostgreSQL + S3 eventually, but that is unverifiable locally (no server on 5432,
  no S3 creds) and has not been done.
- Schema is one module, `utp/core/schema.py`, applied idempotently via
  `CREATE TABLE IF NOT EXISTS`. Bump `SCHEMA_VERSION` when tables/columns change.
  Adding a column does **not** retrofit an existing DB file — use `serve.py --fresh` or
  delete `data/aquaria.db` to pick up schema changes.
- Data-layer invariants are enforced with CHECK constraints, partial unique indexes and
  DELETE/UPDATE triggers, so no service, import or admin tool can bypass them (R46.6).
  `PROTECTED_TABLES` refuse DELETE; `APPEND_ONLY_TABLES` also refuse UPDATE.

## Running things

- Tests: `python run_tests.py` (auto-discovers `tests/test_*.py`); a fragment selects a
  module, e.g. `python run_tests.py settings_charges`. `--security-report` prints the
  OWASP register.
- Serve: `python serve.py` → http://127.0.0.1:8080 (`--fresh` re-provisions, `--port`,
  `--db :memory:`). Demo staff credential `Aquaria-Demo-2026`; emails
  `admin@ | manager@ | cashier@ | gate@ aquaria.test`. `admin@` requires MFA and cannot
  sign in with a password alone.
- Verify the running server end to end: `python verify_server.py`.
- Smoke test the services in-process: `python smoke_check.py`.

## Verification expectation

After any change, run `python run_tests.py` and confirm `RESULT: PASS`. For anything
touching money, tax, capacity, permissions or the HTTP surface, also run
`verify_server.py` against a freshly started server. Write tests for new behaviour;
the settings spec in particular requires an automated test for every tax/charge
combination before the work counts as done.
