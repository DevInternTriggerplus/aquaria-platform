# Aquaria — Universal Ticketing, Booking & Access Platform

Multi-tenant platform that sells, reserves, delivers, validates and reconciles
admission across every channel a venue operates. First production tenant is
**Aquaria Phuket**, implemented entirely as configuration data — there is no
aquarium-specific code path anywhere.

## Layout

```
aquaria-platform/
├── backend/            Django 5.2 + PostgreSQL + DRF      ← the API and all business rules
├── frontend-next/      Next.js 15 + React 19 + Tailwind 4 ← customer web booking
├── frontend-flutter/   Flutter 3 (Dart)                   ← customer mobile booking
└── legacy-stdlib/      the previous stdlib-only implementation, kept as the reference spec
```

`legacy-stdlib/` is not dead code. It is a **working, verified implementation** with
150 passing tests covering money, tax, capacity under concurrency, permissions, PDPA
consent, promotions, gate access and the full booking→payment→e-ticket flow. It is
the specification the Django port is measured against, and it stays until the port
reaches parity. Run it with `python legacy-stdlib/serve.py`.

## Verification status — read this before trusting anything

| Component | State | Verified how |
|---|---|---|
| **PostgreSQL 17.7** | **Verified** | real cluster on 127.0.0.1:5433; migrations applied; whole suite green |
| Django backend wiring | Working | `manage.py check` clean (1 documented silence) |
| Database schema | Working | 12 apps migrated onto PostgreSQL; `makemigrations --check` reports no drift |
| Money / tax engine | **Verified** | 28 tests: Cases A–D, snapshot immutability, FX precision, venue-local expiry |
| Capacity / never-oversell | **Verified on PostgreSQL** | 5 real multi-threaded contention tests; `SELECT FOR UPDATE` confirmed active |
| Read API (venue, products, sessions, payment types, charge preview) | **Verified** | live server returning seeded Aquaria data from PostgreSQL |
| Thai (UTF-8) round-trip | **Verified** | codepoints checked in the U+0E00–U+0E7F block on the wire |
| **Booking write path** (consent → quote → payment → confirm → e-ticket) | **Verified** | 12 unit tests + a 2-thread confirm race + 15 live HTTP checks, all on PostgreSQL |
| PDPA consent gate | **Verified** | required item blocks the booking; declining leaves no personal data |
| **Payment webhook** | **Verified** | signature check, idempotent replay, browser-died capture, amount-mismatch guard — 7 tests + 7 live HTTP checks |
| **Gate scan validation** | **Verified** | closed decision set, frozen-window expiry, atomic entry, a 2-thread double-scan race — 18 tests + 8 live HTTP checks |
| **Manage booking** | **Verified** | enumeration-safe lookup, single-use code, policy tiers, atomic reschedule, cancel + refund — 12 tests + 12 live HTTP checks |
| Permission enforcement (DRF) | **Not ported yet** | — |
| Next.js app | **Not built** | Node/npm not installed on the dev machine |
| Flutter app | **Not built** | Flutter/Dart SDK not installed on the dev machine |

**96 tests pass against real PostgreSQL 17.7**, plus live HTTP checks for the booking
flow (15), the payment webhook (7), the gate (8) and manage booking (12).

## Database — PostgreSQL

A portable PostgreSQL 17.7 lives under `%LOCALAPPDATA%\aquaria-pg`: the official
binaries, no Windows service, nothing in Program Files, no administrator rights. It
listens on **127.0.0.1:5433** only — a non-default port so it cannot collide with a
system PostgreSQL, and loopback-only so a development database with a known password
is not on the network. Remove it by deleting that one folder.

```powershell
cd backend
.\scripts\pg.ps1 setup     # first run: download, initdb, create role + database
.\scripts\pg.ps1 start
.\scripts\pg.ps1 status
.\scripts\pg.ps1 psql
.\scripts\pg.ps1 stop
.\scripts\pg.ps1 reset     # drop and recreate the database (destroys local data)
.\scripts\pg.ps1 logs
```

The application role `aquaria` is **not** a superuser (it holds `LOGIN` and
`CREATEDB` only, the latter because Django's test runner creates `test_aquaria`), so
the suite exercises roughly the permission surface production will have.

### Why PostgreSQL specifically

The never-oversell guarantee rests on `SELECT ... FOR UPDATE` serialising competing
requests. SQLite has no row-level locking, so on SQLite those tests prove the
arithmetic is right but *not* that it is safe under contention.
`apps/inventory/tests_concurrency.py` closes that gap with real threads on real
connections, released together from a barrier:

- 10 threads racing for **1** unit → exactly one wins, nine get "just sold out";
- 20 threads racing for **5** units → exactly five win;
- 8 threads each wanting **2** units against a capacity of 5 → exactly two win, and
  the leftover single unit is refused rather than partially filled;
- 6 holds confirmed concurrently → `confirmed_count` lands exactly on capacity.

In every case the invariant `confirmed + held <= capacity` holds.

A SQLite fallback still exists behind `USE_SQLITE_FALLBACK=1` for running the suite
somewhere without a server, but it is refused outright by production settings, and
`run_tests.py` prints a loud warning when the capacity guarantees are not really
being proven.

## Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # then set DATABASE_URL and the secrets

.\scripts\pg.ps1 start
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe seed.py          # minimal Aquaria configuration
.\.venv\Scripts\python.exe manage.py runserver
```

Run the suite with the wrapper rather than `manage.py test` directly:

```powershell
.\.venv\Scripts\python.exe run_tests.py
```

It prints the database vendor, whether row locking is active, and an unambiguous
`RESULT: PASS`/`FAIL` with a real exit code. `manage.py test` writes to stderr, so a
passing run there still reports a non-zero exit code under PowerShell; it also needs
`--noinput` when a previous run left `test_aquaria` behind.

> The project `.env` is loaded with `overwrite=True`, deliberately. Without it a
> stale `USE_SQLITE_FALLBACK=1` in a shell silently beats the committed file and the
> suite runs on SQLite while you believe it is on PostgreSQL. That bit us once during
> development; the vendor line in `run_tests.py` is what caught it.

### Apps

| App | Owns |
|---|---|
| `core` | money, clock, ids, errors, i18n (**ported verbatim** from the verified implementation), base models, audit, correlation ids |
| `tenancy` | Tenant → Organization → Brand → Venue → Area, access points, devices |
| `accounts` | Staff, roles, the 43-page × 4-verb permission registry, scoped assignments |
| `catalog` | customer segments, experiences, products, ticket types |
| `venuesettings` | VAT, service charge, ticket validity, currency, exchange rates |
| `pricing` | price rules and resolution |
| `inventory` | sessions, holds, the never-oversell mechanism |
| `booking` | customers, bookings, booking items, charge snapshots |
| `payments` | payment types, payments, idempotency |
| `ticketing` | issued tickets with frozen validity windows |
| `access` | gate scan events, offline conflicts |
| `api` | DRF serializers, views, routes |

### Invariants enforced at the data layer

These are constraints and model-level guards, not conventions, so no view, admin
action, management command or bulk operation can bypass them:

- confirmed consumption never exceeds session capacity;
- entries used never exceed a ticket's allowance;
- refunds never exceed the payment;
- bookings, booking items, payments, tickets, scan events and exchange rates refuse
  `delete()` — DELETE means cancel / void / archive / deactivate;
- audit events refuse both `delete()` and `save()` on an existing row.

### Money and time conventions

Integer minor units everywhere (satang for THB); `Decimal` only inside
`apps/core/money.py`, never stored; no float ever touches a price. Instants are
stored UTC and every business concept — operating date, cutoff, ticket expiry,
report boundary — is evaluated in the **venue's IANA timezone**. A bare UTC offset
is rejected.

A completed transaction never moves when configuration changes: orders snapshot the
VAT/service-charge rate and mode plus any exchange rate, and tickets snapshot their
timezone, validity policy and resolved window.

## Front ends

Both clients consume the same API and share one design language: ocean palette in
oklch, serif display headings, a hero over a gradient, numbered step cards, and the
gradient "ticket head" order summary. Each ticket type and payment method carries a
**small inline icon** ahead of its label.

Neither client computes money. They format the integer minor units the server sends
and ask the server for every total, so there is exactly one source of truth for what
a guest pays.

```bash
# Next.js
cd frontend-next && npm install
cp .env.local.example .env.local
npm run dev            # http://localhost:3000, proxies /api to the backend

# Flutter
cd frontend-flutter && flutter pub get
flutter run --dart-define=BACKEND_ORIGIN=http://127.0.0.1:8000 --dart-define=VENUE_CODE=aqp
```

Neither was installed or built on the development machine. Expect to fix small
issues on first `npm install` / `flutter pub get`; the source is coherent but
unexercised. Android emulators reach the host at `10.0.2.2`, not `127.0.0.1`.

## Booking write path — done

Ported and verified on PostgreSQL. The flow is `GET consent → POST quote → POST
confirm`, and confirm runs in the required order: check consent before persisting any
personal data, re-resolve the price authoritatively, persist the booking as
`AWAITING_PAYMENT`, take payment, convert holds into confirmed capacity, then issue
tickets. A single idempotent `finalize_paid_booking` completes the booking, shared by
the inline path and (later) the webhook, so a dropped connection after authorization
still yields a confirmed booking.

Endpoints (all venue-scoped, all `AllowAny` for the customer):

```
GET  /api/venues/<code>/consent/     the PDPA dialog contents
POST /api/venues/<code>/quote/       price + holds, returns the authoritative total
POST /api/venues/<code>/confirm/     consent + customer + payment -> booking + tickets
POST /api/webhook/payment/           provider callback (not venue-scoped)
```

The quote is re-resolved from its inputs at confirm time, never trusted from the
client, so a tampered cart cannot force a stale or invented price.

The **payment webhook** verifies the provider signature against the raw request body,
records the delivery under a unique `(tenant, provider, event_id)` constraint *before*
touching payment state — so a duplicate or out-of-order delivery loses the insert race
and is a no-op — and on capture completes the booking through the same idempotent
`finalize_paid_booking`. That shared finalize is why a booking whose browser died after
authorization still confirms and delivers its ticket, and why a second successful
charge is flagged for refund rather than double-confirming.

## Gate validation — done

A scan turns a QR into exactly one decision from a closed set, evaluated cheapest-first:

```
POST /api/venues/<code>/gate/scan/       QR -> ADMIT | ADMIT_WITH_CHECK | REJECT_*
POST /api/venues/<code>/gate/override/   supervisor admits a rejected scan (audited)
GET  /api/venues/<code>/gate/lookup/     manual booking lookup when the QR won't scan
```

The signature is verified before any database lookup, so a forged or garbage code is
rejected without touching storage. State, then the frozen venue-local validity window,
then entry allowance and re-entry are checked in order. An admit consumes one entry
through a `select_for_update` on the ticket, so two simultaneous scans of the last
allowed entry admit exactly one — the loser is `REJECT_ALREADY_USED` with the time and
gate of the previous admission. Every scan, admitted or rejected, is written to the
append-only `ScanEvent` table.

Gate scans run without staff authentication: the device identity is the control
(R32.12), so throughput is not gated on RBAC. Offline sync and full device-credential
verification are the remaining pieces of R32.

## Manage booking — done

Self-service by the customer, with ownership proven by a one-time code emailed to the
address on the booking:

```
POST /api/venues/<code>/manage/request-code/   emails a code; response is enumeration-safe
POST /api/venues/<code>/manage/verify/         consumes the code, returns the booking view
POST /api/venues/<code>/manage/cancel/         cancel (confirmation-gated) + refund per policy
POST /api/venues/<code>/manage/reschedule/     move the booking, target-before-original
```

The lookup response is identical whether the booking exists or not, so it cannot be
used to enumerate; a code is only issued when the number/email pair matches. Codes are
single-use and short-lived. Cancel and reschedule follow a configurable
time-before-visit policy; reschedule acquires the target date's capacity before
releasing the original, and reissues tickets so superseded QR codes stop working. A
used ticket blocks self-service cancel and reschedule.

## Port order for the remaining backend work

Still to do, in this order. Each step should port the matching tests from
`legacy-stdlib/tests/` at the same time — parity means those tests pass against Django.

1. **permission enforcement** — DRF permission classes over the ported registry, with
   venue scoping, for the staff/back-office endpoints.
2. **promotions**, then shows, counter POS, partner API, reporting, seating.
3. **gate offline sync** — signed local dataset, queued scans, conflict detection.

## Before any real deployment

- Set `SECRET_KEY` and `TICKET_SIGNING_KEY`. Production settings refuse placeholders,
  and the committed `.env` holds development-only values.
- Point `DATABASE_URL` at a managed PostgreSQL and leave `USE_SQLITE_FALLBACK` off.
  The local cluster is a development convenience: `trust` local auth, a known
  password, and no backups.
- Add a tenant-aware authentication backend. Staff email is unique per tenant, not
  globally (`auth.E003` is silenced deliberately), so the tenant must be resolved
  before the email lookup.
- Raise the concurrency tests' thread counts and run them against the production-grade
  instance. They prove the mechanism, not the capacity of a particular server.

## Free public demo deployment

The current repository is configured for Vercel (Next.js), Render (Django API), and
Neon (PostgreSQL) without restructuring the applications. See
[`DEPLOYMENT.md`](DEPLOYMENT.md) for the exact setup and the free-tier limitations.
This is a public demonstration configuration, not a production ticket-sales setup.
