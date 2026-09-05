# Conventions & Key Decisions

Patterns to follow and decisions already made, so they are not re-litigated or
accidentally broken.

## Money

- **Integer minor units everywhere** (satang for THB). No float ever touches a price,
  tax, discount, rate or total. `Decimal` is used *internally* in `utp/core/money.py`,
  never stored.
- All rounding goes through `apply_rounding` so cart, payment, receipt, invoice and
  reports reconcile. When a rounding rule shifts a total, expose the gap as
  `rounding_adjustment_minor` — never let displayed lines silently fail to sum to the
  charged total (R5.5).
- The charge engine is `compute_charges(...)` returning a `ChargeBreakdown`. Calculation
  order is fixed and documented: base → line discount → order discount → service charge →
  VAT → rounding → grand total. Included vs excluded are two independent flags; the four
  combinations (Cases A–D) are covered by `tests/test_settings_charges.py`. Do not add a
  second place that computes totals.
- Exchange rates are stored as exact decimal strings (`rate_text`), parsed with
  `parse_rate` (6 dp). Direction is always "1 USD = 33.10 THB". Never assume 2 decimal
  places — JPY has none; use `currency_decimals` / `format_currency`.

## Historical snapshots (never recompute the past)

A completed transaction must never move when current settings change. Orders store a
`charge_snapshot_json` (VAT/service-charge rate + mode + amounts) and currency/rate
fields; tickets store `validity_timezone`, `validity_type`, `validity_policy_json` and
frozen `valid_from`/`valid_until`. When adding a setting that affects money or access,
snapshot it onto the transaction at creation time and read the snapshot afterwards.

## Time

- Store instants in UTC; evaluate business concepts (operating date, cutoffs, expiry,
  reports) in the **venue's IANA timezone** via `utp/core/clock.py`. Never store or accept
  a bare UTC offset — `SettingsService.set_timezone` rejects `UTC+07:00`.
- Default ticket validity is **End of Visit Day = 23:59:59 venue-local** on the visit
  date. `combine_local(date, "23:59:59", tz)` is the idiom. This is verified in tests.

## Permissions & security

- Permission registry is `utp/domain/permissions.py`: pages carry independent VIEW / ADD /
  EDIT / DELETE, plus separate action permissions (`ACTION:NAME`). VIEW/ADD/EDIT/DELETE are
  fully independent — none implies another. New permissions default to **denied**.
- Singleton settings pages (VAT, Service Charge, Time Zone, Ticket Validity) expose
  VIEW+EDIT only; Currency and Exchange Rates get full CRUD. Sensitive settings changes
  are gated by `MANAGE_TAX_SETTINGS`, `MANAGE_SERVICE_CHARGE`, `MANAGE_TIMEZONE`,
  `MANAGE_TICKET_VALIDITY`, `MANAGE_CURRENCY`, `MANAGE_EXCHANGE_RATE`, each requiring a
  reason and audited.
- Every service method enforces permission **server-side** via `authz.require_page` /
  `authz.require_action` before doing work. Hiding a UI control is convenience, not the
  control. The `system_context` (authority 100) bypasses checks and is for internal/seed
  calls only — never expose it to a request handler.
- Errors are friendly and leak nothing: no SQL, stack traces, internal ids or provider
  payloads reach a client. Full detail goes to the server log with a correlation id.
  Cross-tenant access returns not-found, not a distinguishable error (R1.2).

## Booking / payments lifecycle

- Persist the booking as `AWAITING_PAYMENT` **before** charging the gateway, then a single
  idempotent `finalize_paid_booking()` completes it — called by both the inline path and
  the webhook (`payments.on_payment_captured`). This is why R14.6 (browser dies after auth)
  works. Do not add a second completion path.
- Sessions and ShowSessions share the `sessions` table via `kind='PRODUCT'|'SHOW'` so all
  capacity flows through one authoritative, non-overselling mechanism.
- Holds apply only to capacity-controlled inventory. General admission at Aquaria is
  uncapped, so a quote there legitimately has no hold.

## Code style

- Match the surrounding style: module docstrings explain *why*, comments cite the
  requirement (e.g. `# R5.5`, `# settings spec §37`). Keep customer-facing strings plain,
  warm and actionable. No emojis in code.
- Read a file before editing it. Prefer targeted edits over rewrites.
- Do not create markdown docs unless asked. Do not add dependencies. Do not weaken a
  data-layer invariant to make a change easier.

## Current status (keep roughly updated)

Implemented and green (112 tests): core, domain, most services incl. the settings module,
security layer, HTTP API, web app, seed. Verified: full booking→payment→e-ticket flow,
Case A–D charges, snapshot immutability, 23:59:59 expiry, permission enforcement.

Not yet implemented (explicitly `None` on `Platform`): seating (R47–R62), gate access
`/api/gate/scan` returns 501 (R32), counter POS (R34), partner API (R35), reporting
(R70–R71). PostgreSQL/S3 port not done. Settings module task remaining: expose via API
endpoints + back-office nav and configure the venue's VAT through the service in seed.
