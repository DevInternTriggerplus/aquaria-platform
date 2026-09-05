# Universal Ticketing, Booking & Access Platform — Project Summary

> **Restructured into a three-part monorepo.** The project is now split into
> `backend/` (Django 5.2 + PostgreSQL + DRF), `frontend-next/` (Next.js 15) and
> `frontend-flutter/` (Flutter 3). The previous stdlib-only implementation moved
> intact to `legacy-stdlib/`, where its 150 tests still pass and serve as the
> reference specification for the port. **Read `README.md` for the current layout,
> how to run each part, and a per-component verification table** — much of this
> document below describes the legacy implementation, which remains the more
> complete of the two backends until the port reaches parity.

A multi-tenant, configuration-driven platform that sells, reserves, delivers,
validates and reconciles admission across every channel a venue operates: online
booking, self-service kiosk, counter POS, partner/agent, entrance-gate scanners and
back office. The first production tenant is **Aquaria Phuket**, implemented entirely
as configuration data — there is no aquarium-specific code path.

- **Stack:** Python 3.14, standard library only (plus `tzdata`, `psycopg`, `boto3`).
  No web framework, no ORM, no third-party test runner. Web front end is plain
  HTML/CSS/JS with no build step.
- **Package:** `utp` (never `platform`, which would shadow the stdlib module).
- **Database:** SQLite today (schema version **10**), with the domain shaped so a
  PostgreSQL + S3 port is possible later.
- **Status:** **350 automated tests pass**; `verify_server.py` runs **191 end-to-end
  HTTP checks** against a live server. Money, tax, capacity, permissions and PDPA
  correctness are treated as the highest-priority properties.

---

## Non-negotiable design principles

1. **Configuration over code.** A new venue type launches without a code change or
   deployment. Business behaviour lives in tenant-scoped configuration records.
2. **Generic domain language.** Entities are `Tenant`, `Organization`, `Venue`,
   `Experience`, `Show`, `Session`, `Product`, `TicketType`, `CustomerSegment`,
   `Booking`, `Ticket`, `AccessRight`, `AccessPoint`, `Device`. Names like
   `AquariumShow` are prohibited.
3. **Default deny.** Any permission not explicitly granted is denied.
4. **Never oversell.** Capacity is authoritative and enforced under concurrency.
5. **Financial and audit records are never physically deleted.** DELETE maps to
   Cancel / Void / Archive / Deactivate for protected records, enforced at the data
   layer with triggers so no service, import or admin tool can bypass it.
6. **Fast and obvious for the operator; effortless for the customer.**

---

## Architecture

`utp/app.py` is the **composition root**: one `Platform` object wires every service,
the database, clock, audit log, config store and security layer. Tests, the HTTP
API, `seed.py` and background jobs all build the same `Platform`, so a rule proven in
a test is the rule the API runs. Circular cross-service references (e.g.
`booking.members`, `tickets.settings`, `access.tickets`) are injected after
construction in `Platform._wire()`.

```
utp/core/      errors, ids, clock (IANA tz), money, schema, db, context, audit,
               config resolution, i18n
utp/domain/    enums, permissions (pages + actions + role templates), cart
utp/services/  authz, staff, tenancy, catalog, pricing, settings, calendar_rules,
               inventory, promotions, consent, customers, members, payments,
               payment_types, tickets, booking, notifications, documents, shows,
               access, counter
utp/security/  owasp register, validation, headers (nonce CSP), csrf, ssrf,
               uploads, ratelimit, secrets, monitoring
utp/ticketdesign/  qr (dependency-free encoder), payload, strings (5 languages),
               email_ticket (the e-ticket), thermal (80mm Star MCP31LB)
utp/reporting/ definitions (the 30-report catalog), metrics, rows, exceptions,
               export, service (permission + scope + masking + audit)
utp/api/       server.py (ThreadingHTTPServer, router, CSRF, security headers, static)
web/           index.html, styles.css, app.js, print.js (Peacock Blue, mobile-first,
               CSP-safe)
```

### Key cross-cutting conventions
- **Money:** integer minor units (satang) everywhere; `Decimal` only inside
  `utp/core/money.py`, never stored. All rounding flows through `apply_rounding` so
  cart, payment, receipt, invoice and reports reconcile exactly.
- **Time:** instants stored in UTC; business concepts (operating date, cutoffs,
  ticket expiry, reports) evaluated in the **venue's IANA timezone**. A bare UTC
  offset is rejected.
- **Historical snapshots:** a completed transaction never moves when current settings
  change. Orders snapshot the VAT/service-charge rate, currency and exchange rate;
  tickets snapshot timezone and validity policy; promotion redemptions snapshot their
  computed values; point redemptions snapshot the conversion rate.
- **Permissions:** every service method enforces permission server-side via
  `authz.require_page` / `authz.require_action` before doing work. Hiding a UI control
  is convenience, not the control.

---

## Implemented capability areas

| Area | Requirements | State |
|---|---|---|
| Multi-tenant configuration foundation | R1–R10 | Implemented |
| Online booking (quote → consent → pay → e-ticket) | R11–R17 | Implemented |
| Show schedule & timetable (recurring, overrides, publish) | R18–R31 | Implemented |
| Gate & access validation | R32 | Implemented (`platform.access`) |
| Self-service kiosk flow | R33 | Implemented (web) |
| Counter POS | R34 | Implemented (`platform.counter`) |
| Notifications (transactional + templates) | R36–R37 | Implemented |
| Identity, roles, page/action permissions, audit | R38–R46 | Implemented |
| Presentation, accessibility, localization (TH/EN) | R63–R69 | Implemented |
| Financial documents (receipts, tax invoices) | R72 | Implemented |
| Security & PDPA consent | R12, R73 | Implemented |
| **Business / Venue Settings module** | add_features | Implemented |
| **Advanced Promotion Engine** | add_features | Implemented |
| **E-ticket + 80mm thermal ticket, QR encoder** | R15, ticketDesign | Implemented |
| **Reporting, analytics & dashboards** | R70–R71, reports | Implemented (`platform.reporting`) |
| Seating / seat maps | R47–R62 | Not started (`platform.seating is None`) |
| Partner / agent API | R35 | Not started (`platform.partners is None`) |
| PostgreSQL + S3 port | (user request) | Not done — still SQLite |

---

## Feature detail — most recent work

### Every Settings page completed end-to-end (settingsAndReports.md completion pass)
The back office previously showed "Not configurable in this build yet" on most
Settings pages. That placeholder is gone: every settings page now has a real
backend, a real editor, permission enforcement, audit and tests.

**A generic config-backed settings service.** Most settings pages are, underneath, a
single scoped configuration value — operating hours, booking rules, rounding, price
display, languages, numbering, login security, integrations, webhooks. Rather than
hand-write a service per page, `utp/services/settings_pages.py` declares each page
once (config key, scope, page permission, optional `MANAGE_*` action, default,
validator) and drives read/write generically on top of the existing versioned,
audited `ConfigStore`. Adding a page is a table row, not code. **21 pages** are
config-backed this way. No schema change was needed — they persist in the existing
`config_values` table with full version history, so a completed transaction never
moves when a setting changes later.

Each write runs the same discipline as the hand-written settings: venue scope → page
`EDIT` → the sensitive `MANAGE_*` action (with its mandatory reason) → validation →
versioned write → an audited `CONFIG_CHANGE` carrying old and new value and the
reason. Reads require only `VIEW`. Validators are authoritative and speak business
language ("Last admission cannot be after closing time", "Use an https:// URL").

**Credentials never round-trip in clear text.** The Integrations, API Configuration
and Webhooks pages hold secrets; a stored value keeps only a masked descriptor
(`{set, last4, length}`), a read never returns the raw secret, and a blank
replacement keeps the one on file — the operator was never shown it and does not have
to re-type it.

**Record-collection pages** (ticket types, staff, roles, promotions, devices, shows,
seats, areas, audit logs, the permission registry itself) read their rows through one
`GET /api/staff/settings/records?page=` endpoint, each gated by the page's own `VIEW`.
Creation and editing stay with the owning module (Staff, Promotions, …), which
already enforces `ADD`/`EDIT`/`DELETE`.

**Frontend.** `settingsPageBody` in `web/backoffice.js` dispatches every page to a
real editor: a generic form for config pages (weekday hours grid, language picker,
masked-secret integration/webhook lists, numbering, partner terms), a premium white
table for record pages. Read-only when the role lacks `EDIT`, empty and loading
states, unsaved-changes and sensitive-change confirmation, and the server's error
message shown verbatim. The placeholder strings were removed from all five languages.

**New endpoints** (`utp/api/server.py`): `GET/POST /api/staff/settings/page`,
`GET /api/staff/settings/records`, and the settings overview now carries
`config_pages`. A `tech@aquaria.test` (Technical Support) demo account was added so
the integration/API/webhook pages are reachable without the MFA-gated super admin.

**Verified.** A completeness test asserts **no settings page falls through to a
placeholder** — every `SETTINGS_PAGE_KEYS` entry is covered by a config page, a record
reader or a pre-built screen. `tests/test_settings_pages.py` (12 tests) covers
persistence, VIEW/EDIT/MANAGE-action authorization, validation, venue-scope isolation,
secret masking and audit. The full suite is **350 tests** and `verify_server.py`
passes (191 checks) — no regression.

### Topic-icon system + premium report tables (designIcon.md, designReports.md)
A shared icon family across every Settings and Reports screen, and a calmer,
more readable table style — both applied through one reusable mechanism rather than
per-page.

**One icon family, one source of truth.** New `web/icons.js` injects a single hidden
inline-SVG sprite of 59 duotone line `<symbol>`s and exposes `window.utpIcons`. Each
icon is a tiny `<svg><use href="#ic-…">` that tints via `currentColor` (primary
stroke) plus a soft-teal `.uic-accent` fill, sitting on a rounded peacock tile. No
icon font, no image fetch, no emoji — so the CSP (`font-src 'self'`,
`img-src 'self' data:`) is untouched, and the brief's "do not mix icon styles" rule
holds because there is only one place to add an icon. The module owns the topic→icon
mapping too: every one of the 72 settings pages and 11 categories, plus the report
catalog and KPI keys, resolve to a symbol by their **internal, language-free key**, so
the icon never shifts when the UI language does (§42).

**Settings** now carry a recognizable glyph everywhere the eye lands — category cards,
the category and page titles, every sidebar page link, the category page list and the
search results — replacing the previous abstract CSS-border shapes that only the 11
category cards had. **Reports** replaced the old colour-only gradient squares with the
same symbols on KPI cards, operational tiles, the three section headers (Analytics /
Operations / Finance), every report link, panel headers and the empty/all-clear
states. Icons are always paired with their text label (§38); the glyph is
`aria-hidden` because the label is the accessible name.

**Report tables became a premium white surface** (designReports.md). Every `.rp-table`
now sits in a white, softly-rounded (14px), lightly-shadowed card; body text is ~14px
dark-navy with tabular figures, the header is a light bar (not a dark all-caps strip),
rows breathe at ~48px with a barely-there separator instead of a cell grid, money
columns are right-aligned and slightly heavier so they compare down the column, and
the hover is a soft peacock wash rather than a dark highlight. It is one shared style,
so no report can drift to its own look (§26). Status stays a soft text-carrying pill,
and red is reserved for genuinely critical states (§15).

**Verified:** icons.js, backoffice.js/css and reports.js/css all serve 200 with the
right content types and the script/link tags are present in the served HTML; the
icon module reports 59 symbols with zero referenced-but-undefined slugs; every
utpIcons call site matches the module API; no stale `iconClass`/`data-icon` remains.
The full suite still passes **338 tests** and `verify_server.py` passes on a fresh
server (191–193 checks depending on demo data), including all the settings/auth HTTP
checks. The change is presentation-only — no server behaviour moved.

### Staff authentication, protected back office & page-level permissions (settingsAndReports.md)
The whole back office is now behind a real sign-in with per-page, per-verb
authorization, and every one of the 76 spec sections is enforced server-side rather
than only reflected in the UI.

**Permission registry grew to fit the spec.** `utp/domain/permissions.py` went from 44
pages to **72** (the ~28 settings pages the spec's §13 matrix names — Organization,
Brand, Operating Hours, Booking Rules, Gates, Login Security, API Configuration,
Webhooks and the rest) and from 45 to **51 action permissions** (RESET_ACCESS,
ASSIGN_ROLE, APPROVE_EXCHANGE_RATE, APPROVE_TAX_CHANGE, MANAGE_LOGIN_SECURITY,
MANAGE_INTEGRATION). Singleton configurations declare `("VIEW","EDIT")` only, so the
spec's "-" cells are a real property: `page_key` refuses to build a key that would
never be honoured, and the matrix renders the cell as *not applicable* rather than an
empty box. A new **11-category settings information architecture**
(`SETTINGS_CATEGORIES`, `SETTINGS_CATEGORY_BY_PAGE`) maps every settings page to
exactly one home card, checked at import so a typo cannot silently hide a page.
`settings_tree(granted)` filters that architecture to what a principal may VIEW.

**Permission labels are language-free keys with a separate translation table.** New
`utp/domain/permission_labels.py` carries all five languages (en/th/zh/ja/ru) for 72
pages, 51 actions, 21 groups, 11 categories + descriptions, 4 verbs and 9
delete-semantics, with a `coverage_gaps()` self-check the test suite asserts is empty.
The stored grant is always the internal key (`"Payment Type.EDIT"`); only the display
is translated (§49, §50), so a translator can edit every string without touching a
single role.

**Authorization service gained the read models the back office needs**
(`utp/services/authz.py`): `navigation(language=)`, `settings_home()` (§11/§26/§71
filtered), `settings_search()` (§27/§32 — word-anchored for spaced scripts, substring
for Thai/CJK/Hangul, so "VAT" stops matching "deacti**vat**es"), `permission_matrix()`
(the grantable registry, localized, gated by Roles.VIEW *or* Permissions.VIEW),
`grant_summary()` (the §21 pre-save summary in counts + plain language, treating "no
access" as distinct from "read only"), and an extended `permission_summary()` (§36
effective-permission viewer, per venue). `StaffService.session_profile()` is the one
call §3 asks for: identity, tenant, organization, assigned venues, roles, scope,
permissions and authorized navigation together, so the menu is never drawn from stale
authority.

**New HTTP surface** (`utp/api/server.py`): `POST /api/staff/logout`,
`GET /api/staff/me`, `/permissions/matrix`, `/permissions/summary`, `/settings/home`,
`/settings/search`, and a server-computed `/settings/charge-preview` (§41 — the client
never does money arithmetic). Bearer-authenticated requests skip CSRF, so logout needs
no token.

**The web client became a real back office.** `web/backoffice.js` + `backoffice.css`
add a Login view (§2, business-friendly validation, show/hide password, MFA field only
when required), a hash router with a **route guard** (an unauthenticated protected
route lands on Login and returns after sign-in; §5), an **Access Denied** page that
does not loop back to Login (§6), a Settings home of permission-filtered category cards
with live status lines and search (§11/§59), read-only rendering when EDIT is absent
(§16/§68), sensitive-change confirmation dialogs (§40), the collapsible permission
matrix / role editor (§19/§20) and the effective-permission viewer (§36). It reads
`can(page, verb)` from the server's permission set — **never a role name** (§48). The
session token lives in `sessionStorage`; a 401 on any staff request clears it and shows
"your session has expired" (§57). `nav_backoffice` added to `app.js` in all five
languages.

**Verified.** 35 new unit tests in `tests/test_settings_permissions.py` (verb
independence, category hiding, reports-vs-settings independence, search permission
filtering + script-awareness, matrix gating, session-profile shape, venue-scope
rejection, logout revocation, immediate permission change on suspend, zero translation
gaps) plus **19 new HTTP checks** in `verify_server.py` covering the whole §61 flow:
`/me` shape and Thai labels, charge-preview reconciliation, search ranking, matrix
size/localization/NA-cells, REPORT_VIEWER seeing zero settings + a 403 on the matrix
(§72/§75), cashier verb independence and a server-side 403 on a VAT write (§9/§75), and
logout killing the token (§58). **338 tests pass; verify_server.py 193 checks pass** on
a fresh server.

*Note: run `verify_server.py` against a server started **without** `--demo-history`.
The demo-history maintenance thread queues ~100 notifications that flood the simulated
mailbox and evict the verify booking's own e-ticket, so four e-ticket-delivery checks
fail for that reason alone; a plain `serve.py --fresh` passes all 193.*

### Reporting, analytics and the two dashboards (R70–R71, reports.md, reportDashboard.md)
`platform.reporting` is no longer `None`. New package `utp/reporting/`:

```
utp/reporting/
├── definitions.py    the report catalog — 30 reports across three sections
├── metrics.py        aggregates (revenue, visitors, capacity, channels, promotions…)
├── rows.py           the row-level queries a summary drills down into
├── exceptions.py     what looks wrong, with severity and a suggested action
├── export.py         CSV and a printable document
└── service.py        the only entry point: permission, scope, masking, audit
```

plus `web/reports.js` and `web/reports.css` for the UI, and `demo_history.py` for
generating trading history to look at.

**Reports are declarations, not screens.** `definitions.py` holds 30 records —
Analytics (11), Operations (10), Finance (9) — each naming its section, its
required permission page, *only the filters that change its answer* (§34), and its
columns. The sidebar, the filter bar, the tables, the export header and the
permission check are all derived from that, so adding a report is a declaration
plus a metric function. It is also what lets the same module serve an aquarium, a
theatre, a gym or a tour operator: a venue with no seating simply has no seat
inventory to report.

**The permission is per report, not per section.** Most need `Reports.VIEW`, but
the tax-invoice report needs `Tax Invoices.VIEW` and the dashboards have pages of
their own — so granting someone "reports" does not quietly grant them finance
documents. Two additions to the registry: a new **Operations Dashboard** page
(an executive wants business health, a duty manager wants what needs attention
now, and venue supervisors are often granted one and denied the other) and a
**SCHEDULE_REPORT** action, separate from `EXPORT` because scheduling sends data
out repeatedly and unattended.

**Three bugs found and fixed while building it**, all of which produced
plausible-looking wrong numbers:

1. **SQLite binds parameters in the order they appear in the SQL *text*.** Two
   `CASE WHEN … IN (?,?)` groups sat in the SELECT list, ahead of the WHERE
   clause, and their parameters were appended last — so every binding shifted and
   `totals()` and `by_channel()` returned **zero for every money column** while
   the rest of the dashboard showed real figures.
2. **Net revenue was overstated by the refunded amount.** A partially refunded
   booking keeps its original `net_minor` and records the refund separately, so
   summing `net_minor` counted money that had been handed back (R70.6). Net is now
   `net_minor − refunded_minor`, in one shared SQL fragment.
3. **Order-level discounts never reached the product breakdown.** A cart-level
   discount and the final rounding live on the order, not on any line, so the
   product report was short by ~1% of revenue. The residual is now allocated
   across lines with `money.allocate`, the same largest-remainder helper the
   charge engine uses.

**Figures reconcile, and the one residual is declared.** The tests and
`verify_server.py` both prove the net-sales KPI equals the revenue series, the
channel breakdown *and* the transaction ledger (R70.9). One legitimate gap
remains: a partially refunded order has all its booking items deactivated, so its
retained revenue belongs to no product line. Rather than leave an unexplained
discrepancy — which is a reason to distrust every other number on the page — the
product report returns a reconciliation note naming the cause and the amount.

**Scope, masking and export are enforced in the service**, so no new report can
skip them: venue scope is applied *in the WHERE clause* rather than filtered
afterwards (out-of-scope venues never reach a `SUM`, R43.7); asking for a venue
you do not hold *narrows* the result to nothing rather than widening it; personal
data is masked in the response unless `VIEW_PII`, cost columns are dropped
without `VIEW_COST`, and an unmasked read is itself audited; export requires
`EXPORT` and is audited with its filters and row count (R41.7). The seed now
creates `viewer@aquaria.test` (Report Viewer — no PII, no cost, no export) so the
masking can be demonstrated rather than only described.

**Exceptions answer three questions** — what is unusual, how unusual, and what to
do — each with a drill-down target. Severity is used sparingly: red is reserved
for something broken *now* (a gate offline, payments failing), so a high refund
rate is amber however large, because nobody is standing at a closed door because
of it. Every threshold is configuration, since a venue that refunds 8% by design
should not be told daily that it refunds too much.

**The UI is dependency-free.** Charts are hand-drawn inline SVG — a flat
multi-series line chart with four gridlines, horizontal bar lists, and a
day-of-week × hour heatmap — which suited a brief asking for flat, clean, minimal
charts better than a library would have. KPI cards show value, comparison, trend
and period, and know that a rise in refunds is not good news. Peacock Blue is used
for navigation, active states and emphasis rather than on everything. Status is
never colour alone; the heatmap prints values in its busiest cells; skeletons keep
the layout still while data lands; empty and error states offer a way out.
Drill-down preserves filters and leaves a breadcrumb.

**Demo history goes through the real booking path.** `demo_history.py` drives
`quote → start_checkout → confirm → scan` with the clock wound back per day, so
the aggregates are tested against numbers the platform actually produces rather
than hand-written INSERTs. It also surfaces what the platform refused and why —
which is how it became clear that a counter sale needs a signed-in staff actor to
record consent on a guest's behalf (R12.19) and that partner bookings need a
consent attestation (R12.20).

**Verified:** **303 tests pass** (up from 244) — 59 new covering reconciliation,
the net-revenue definition, date presets and comparison windows, scope isolation,
masking, export auditing and the exception thresholds. `verify_server.py` reports
**174 checks passed** (up from 122), including reconciliation over real HTTP and a
cashier being refused all three reporting endpoints while seeing an empty catalog.

Run it with history to look at:

```
python serve.py --demo-history 28      # then open the Reports tab
```

**Two pre-existing defects found and left alone** rather than changed under the
covers, because both touch money:

- `money.allocate()` loses one minor unit when the total is **negative** — the
  shortfall goes negative and the largest-remainder loop becomes a no-op.
- A **partially** refunded booking deactivates *all* its booking items, not just
  the refunded ones, which is why its revenue cannot be attributed to a product.

### E-ticket redesign + 80mm thermal printing (Star Micronics MCP31LB)
The booking confirmation was redesigned to `ticketDesign.md`, and a real print path
was added for the counter's thermal printer. New package `utp/ticketdesign/`:

```
utp/ticketdesign/
├── qr.py             dependency-free QR encoder (SVG + PNG)
├── payload.py        the one render payload both templates read
├── strings.py        ticket labels in en/th/zh/ja/ru
├── links.py          signed, expiring QR image URLs
├── email_ticket.py   the Booking Confirmation / E-Ticket
└── thermal.py        the 80mm admission ticket
```

plus `utp/services/mail_mime.py`, which composes the MIME message.

**The QR is now real.** The app previously displayed the first 44 characters of the
access token as monospace text — there was no QR encoder anywhere in the project, so
no ticket was actually scannable. Since the platform may not add dependencies, an
ISO/IEC 18004 encoder was written against the standard library: Reed-Solomon over
GF(256), byte-mode segments, block interleaving, the eight data masks with the
standard penalty scoring, BCH-protected format and version information, versions
1–15 at levels L/M/Q/H. It renders SVG (sharp at any size) or an 8-bit greyscale
PNG data URL (embeddable, no network fetch at print time — the CSP already allowed
`img-src 'self' data:`).

Because a QR cannot be checked by eye, it is verified from two directions: the 32
published format strings and the version strings are asserted as known answers,
Reed-Solomon is checked by evaluating each codeword at the generator's roots (the
algebraic definition, not a restatement of the code), and an **independent decoder**
in the test walks the matrix back to bytes using its own traversal, mask conditions
and de-interleaving. A real bug surfaced immediately: in Python, skipping the
vertical timing column by reassigning a `for ... in range()` loop variable does not
affect the next value, so columns after the timing pattern were traversed twice and
column 0 never — fixed with a `while` loop.

**Two separate designs, one payload.** A premium colour e-ticket converted to
greyscale makes a poor thermal ticket, so they are not the same document:

- **E-ticket** — Peacock Blue hero, generous whitespace, subtle dividers rather than
  a box around every value, inline line-art icons each paired with a text label, and
  the QR as the strongest element on a plain white panel with a real quiet zone and
  nothing behind it. Responsive to one column at 720px, its own print stylesheet.
- **Thermal** — sized from the printer's actual specification: 80mm stock, **72mm
  printable at 203 dpi (8 dots/mm)**, so the page is `80mm auto` with 4mm side
  padding. The QR is **46mm** (inside the brief's 45–50mm) as inline SVG, which
  works out at ~7.5 printer dots per module, comfortably above the ~3 where thermal
  scanning starts to fail. Pure black on white, no gradients, no grey text, solid
  1px rules, tight vertical rhythm, and `height: auto` so a longer order prints a
  longer ticket instead of clipping.

**One QR per admitted person.** A booking for three renders three cards with three
*distinct* credentials. A single shared code would have turned two of the three
guests away at the gate.

**QR delivery: the QR now survives Gmail.** A `data:` image renders in a browser
and in several desktop clients, but **Gmail strips it** — which would have left a
guest holding a ticket with a blank code. So how the QR travels is now
configuration (`notification.qr_delivery`), not a code path:

| Mode | How the QR arrives | Use |
|---|---|---|
| `CID` (default) | attached to the message, referenced by Content-ID | the only mode every mail client renders |
| `LINK` | signed, expiring `/qr/{token}` URL | clients that prefer a fetched image |
| `DATA_URL` | inlined base64 | the browser page, a saved copy |

`CID` meant composing real MIME rather than handing a provider one HTML string, so
`utp/services/mail_mime.py` builds the conventional tree with stdlib `email`:
`multipart/alternative` → [`text/plain`, `multipart/related` → [`text/html`,
`image/png` per ticket]]. It **refuses to build** a message whose `cid:` references
and attachments disagree in either direction — a dangling reference is a blank QR
at the gate, and an unreferenced attachment means a ticket lost its image. The
simulated provider composes the same MIME even though nothing is transmitted, which
is the only way a local run can prove the structure a real client will receive.

`LINK` mode needed a URL fetchable with **no session**, which rules out
`/tickets/{id}/qr.svg`. `utp/ticketdesign/links.py` mints a signed, expiring
capability token instead, so the image is reachable without a session yet still not
findable by guessing a ticket id. Expired and forged tokens both return a plain 404,
so neither can be used to probe which tickets exist. The token is not the access
credential — it only authorises rendering the image.

The browser preview at `/mail/{id}` rewrites `cid:` references to a per-message
image route, which is exactly what a mail client's "view in browser" does, rather
than pretending the email was composed differently.

**Supporting changes**
- `notification_messages.rendered_html` and `inline_images_json` (schema 7 → 9).
  Attachments are held on the row rather than regenerated at send time, so a retry,
  a resend, or a delivery inspected months later reproduces exactly the message that
  was composed and the cid references can never drift.
- The plain-text body is still always populated, so a client that cannot render
  HTML still receives a complete message. `EmailProvider.send` grew optional `html`
  and `inline_images` arguments; because it is a plug-in point, the signature is
  inspected so an existing text-only provider keeps working rather than breaking on
  upgrade.
- The renderer is injected on `Platform` as `notifications.eticket_renderer`, so the
  notification service depends on no presentation code, and a rendering failure
  degrades the mail rather than costing the guest their ticket (R37.13).
- New routes: `GET /tickets/{id}/eticket`, `/thermal`, `/qr.svg`, and `/mail/{id}`
  for previewing a sent HTML message as its own document. Non-`/api/` registered
  routes now win over the static tree.
- **Ownership.** A ticket carries an access credential, so the print endpoints
  authorise against ticket ids this browser has proven it owns — either by
  completing the purchase or by verifying a Manage Booking one-time code. An
  unowned id returns the same 404 as a non-existent one (R1.2, R16.3).
- Auto-print via `web/print.js`, an external same-origin script (the CSP mints a
  nonce per response, so an inline snippet would have to be threaded through the
  template). It waits for images before opening the dialog, because a print dialog
  raised over an unpainted QR can produce a blank code.
- Confirmation and Manage Booking now show the real QR image and per-ticket
  **Print e-ticket** / **Print gate ticket (80mm)** buttons, translated in all five
  languages. Validity is rendered in the ticket's own timezone — the old code did
  `.replace('T',' ').replace('Z',' UTC')`, telling a Bangkok guest their ticket
  expired seven hours early. Fixed a shadowed `t` in the Manage Booking renderer
  that had been forcing those labels to stay English.

**Two defects found in `verify_server.py`** while verifying, both time-of-day bugs
in the script rather than the platform: a check asserted every date from today
onward is selectable, which fails every evening once last admission passes (correct
platform behaviour, R6.4); and the gate section bought a same-day ticket, which no
channel will sell after 18:00. The calendar check now asserts the real invariant
(future dates selectable, today must explain itself) and the gate section falls back
to asserting `REJECT_NOT_YET_VALID`, noting that the ADMIT path is covered by the
clock-controlled unit tests. The exchange-rate check now ends its own leftover row
so a second run fails on a defect rather than on its own side effect.

**Verified:** **244 tests pass** (up from 178) — 28 QR encoder, 35 ticket design and
31 delivery tests. `verify_server.py` reports **122 checks passed** (up from 93) on a
fresh server. Confirmed over real HTTP: the confirmation email carries the e-ticket
with one **distinct** QR per guest attached by Content-ID and no `data:` image, the
browser preview resolves those references and serves a real PNG, a signed QR link
works with no session while an expired or forged one 404s, the thermal page is 80mm
with a 46mm inline-SVG QR, a second browser gets 404 for someone else's ticket, and
an issued ticket does not move when VAT or the venue timezone changes afterwards.

The strongest check is that the **PNG actually attached to the email is decoded back
to its payload** by the independent decoder and compared against the ticket's own
credential — so "the right QR is in the email" is verified, not assumed.

### Customer web app — localization gaps + segment wording (legacy web on :8080)
The customer flow still showed English in several spots when another language was
chosen. Closed the gaps and reworded the segment labels:

1. **Header/hero/nav now translate.** Added `data-t` to the Language label, the four
   nav buttons (Book / What's on / My booking / Operations), the hero badge
   ("Online booking") and two hero facts ("Instant QR e-ticket", "No account needed").
   The header hours line ("Open 10:30–19:00 · last admission 18:00 · Asia/Bangkok")
   and the short hero hours line now build from parameterized keys `hours_full` /
   `hours_short`, and the header line was moved into `renderVenueChrome` so a language
   switch re-localizes it too.
2. **Dates and month names follow the language.** Added a `LOCALE_BY_LANG` map and
   `localeFor()` so `fmtDate`, the calendar month label and `money()` format in the
   chosen locale (en-GB / th-TH / zh-CN / ja-JP / ru-RU) instead of the browser's.
   The calendar weekday header is now rendered from `Intl.DateTimeFormat` (Monday-first)
   so Mon/Tue/… translate automatically; the cell short-labels (Few left / Full /
   Closed / Soon) route through `t()`.
3. **Segment wording → "Adult(s)", "Child(ren)", "Senior(s)", translated.** In
   `seed.py` the `SEGMENTS` tuple now carries a full per-language name dict
   (en/th/zh/ja/ru), used for both the customer segment and the ticket type — the
   ticket type name was previously English-only (`segment_code.title()`). The order
   summary and review dialog looked up the raw `ticket_type_code` ("GA-INTL-ADULT");
   a new `ttNameByCode()` helper now resolves the localized display name from the
   loaded products.
4. **Shows and Manage views translated.** The shows list badges ("Reservation
   required", "Included with your ticket"), the empty/next-shows hints, the duration
   and countdown units, and the whole Manage-booking form (title, hint, field labels,
   buttons) now go through `t()` / `data-t`.

Added **28 new i18n keys** across all five languages (en/th/zh/ja/ru); a checker
confirmed each appears exactly 5×, and braces/parens stay balanced. Verified on a
fresh server: `/api/products` returns the multi-language segment names (EN "Adult(s)"
… TH "ผู้ใหญ่" … ZH "成人"), the served HTML carries every new `data-t`, and
`verify_server.py` reports **93 checks PASS**. The `--fresh` restart cleared the
in-memory data (needed for the seed change), so earlier test bookings are gone.

### Customer web app — payment-method bug + required-field markers (legacy web on :8080)
Two more reported issues fixed:

1. **"That payment method is not available here" after choosing Alipay/WeChat.** A seed
   inconsistency: the venue offered four customer payment *types* (PromptPay, Card,
   Alipay, WeChat) but its configured `payment.methods.ONLINE/KIOSK` only allowed
   `CARD` and `QR_BANK_TRANSFER` — so the two e-wallet types (`method=EWALLET`) were
   offered but rejected at confirm. Added `EWALLET` to the online/kiosk/counter method
   lists in `seed.py`, so everything offered is payable. Verified end to end: a booking
   now confirms with EWALLET, QR and CARD alike.
2. **Required fields now show a red asterisk on the heading, everywhere.** A CSS rule
   (`.field:has(input[required]) > label::after`, plus `.is-required`) appends a red
   `*` without touching each label's text or its translations. The customer details
   form and the Manage Booking fields are marked `required`; the PDPA required consent
   item gets a red `*` via a `.req-star`. "Required"/"Optional"/"Lawful basis" in the
   consent dialog are now translated too.

### Customer web app — three UI fixes (legacy web on :8080)
Reported from the running app and fixed:

1. **Untranslated strings.** Several customer-facing labels and toasts were hardcoded
   English that never went through `t()`. Added 32 new i18n keys — step headings,
   details-form labels, the confirmation summary and e-ticket field labels, and the
   quantity-limit / hold-expired / booking-done / payment-selected toasts — in **all
   five languages** (en/th/zh/ja/ru), added matching `data-t` attributes in the HTML,
   and routed the JS toasts and validation messages through `t()`. A checker confirmed
   every new key exists in every language block.
2. **"Who are the tickets for?" layout.** The flag icon sat *above* the label.
   `.gc-body` is now a two-column grid so the flag leads inline with the name
   (globe then "International"), with the note beneath.
3. **Calendar legend had no colour.** The swatches now use distinct saturated colours —
   teal Available, amber Limited, red Sold out, grey Closed, deep-blue Not yet on sale —
   each with a subtle inset ring so a pale swatch stays visible on white. Colour remains
   a reinforcement only; every state still carries an icon and a text label.

Verified against the live server: the legend API returns the new colours, the served
HTML/CSS/JS carry the new `data-t`, grid layout and ring, and the other-language keys
resolve.

### Monorepo split: Django + PostgreSQL backend, Next.js and Flutter front ends
The project was reorganised into separate backend and front-end folders at the
user's request, with Django as the backend, PostgreSQL as the database, and two
independent clients.

**Layout**

```
backend/            Django 5.2 + DRF 3.16 + psycopg 3, PostgreSQL-first
frontend-next/      Next.js 15 + React 19 + Tailwind 4 (TypeScript strict)
frontend-flutter/   Flutter 3 / Dart 3, Material 3
legacy-stdlib/      the previous stdlib implementation, moved intact
```

**Strategy.** The legacy implementation was *moved, not deleted*. It holds 150
passing tests encoding subtle correctness — money rounding, tax Cases A–D, capacity
under concurrency, the permission matrix, PDPA consent, gate decisions. Replacing
that with a hastily rewritten Django app would have destroyed verified behaviour, so
it stays as the reference spec until the port reaches parity. It still passes 150/150
from its new location.

**What was ported rather than rewritten.** `money.py`, `clock.py`, `ids.py`,
`errors.py`, `i18n.py` and the 856-line permission registry (43 pages × 4 verbs + 44
action permissions + 11 role templates) are all pure modules with no DB or framework
dependency, so they moved across **verbatim**. The charge engine — the fixed
calculation order and the four VAT/service-charge combinations — is therefore the
same code that was already proven, not a reimplementation.

**Backend, 12 Django apps:** core, tenancy, accounts, catalog, venuesettings,
pricing, inventory, booking, payments, ticketing, access, api. Invariants are
enforced as database constraints and model guards rather than conventions, so no
view, admin action or bulk operation can bypass them: confirmed consumption never
exceeds capacity; entries never exceed a ticket's allowance; refunds never exceed the
payment; financial/ticketing/scan records refuse `delete()`; audit rows refuse both
`delete()` and `save()` on an existing row.

**Both clients share one design language** (the ocean palette, serif display
headings, hero, numbered step cards, gradient ticket-head summary) and keep the
**small inline icon in front of each ticket type and payment method**. Neither client
computes money: they format the integer minor units the server sends and ask the
server for every total, so there is exactly one source of truth for what a guest pays.

**Verified in this session**
- `manage.py check` clean (one documented silence, see below).
- Migrations generate and apply across all 12 apps; `makemigrations --check` reports
  no drift from the models.
- **47 backend tests pass against real PostgreSQL 17.7** (see the next section).
- Live server: `/api/health/` returns 200; an unknown venue returns 404 with the
  platform error envelope and a correlation reference, without disclosing whether the
  record exists.
- `legacy-stdlib` still passes 150/150.

### PostgreSQL stood up, and the capacity guarantee actually proven
The suite previously ran on a SQLite fallback because no PostgreSQL server was
reachable. That has been fixed properly.

**The cluster.** Portable PostgreSQL 17.7 under `%LOCALAPPDATA%\aquaria-pg` — official
binaries, no Windows service, nothing in Program Files, no administrator rights
required (none were available). Listens on **127.0.0.1:5433** only: a non-default port
so it cannot collide with a system PostgreSQL, loopback-only so a development database
with a known password is never on the network. `backend/scripts/pg.ps1` wraps
setup / start / stop / restart / status / psql / reset / logs, so it is reproducible
rather than a one-off; the stop→start cycle was tested. The app role `aquaria` is not
a superuser (`LOGIN` + `CREATEDB` only, the latter for Django's `test_aquaria`), so the
suite runs at roughly production privilege.

**Why it mattered.** The never-oversell guarantee rests on `SELECT ... FOR UPDATE`.
SQLite has no row-level locking, so the old capacity tests proved the arithmetic but
not the concurrency. New `apps/inventory/tests_concurrency.py` uses real threads on
real connections, released together from a barrier:

| Scenario | Result |
|---|---|
| 10 threads, 1 unit | exactly 1 wins, 9 get "just sold out" |
| 20 threads, 5 units | exactly 5 win |
| 8 threads × 2 units, capacity 5 | exactly 2 win; the spare single unit is refused, not partially filled |
| 6 holds confirmed concurrently | `confirmed_count` lands exactly on capacity |

`confirmed + held <= capacity` holds throughout, and the suite prints
`vendor=postgresql has_select_for_update=True` so a run can never quietly claim more
than it proved.

**Also verified end to end on PostgreSQL:** `seed.py` writes a real Aquaria
configuration (tenant, org, venue with `Asia/Bangkok`, 7% inclusive VAT, Adult/Child/
Senior segments, the real THB 1,251 / 675 online prices, PromptPay + card); the HTTP
API serves it (`/api/venues/aqp/products/` returns the priced ticket types, and
`charge-preview` for two adults returns tax base 233,832 + VAT 16,368 = 250,200, which
reconciles exactly); and Thai display names round-trip as genuine UTF-8, confirmed by
checking codepoints fall in the U+0E00–U+0E7F block rather than trusting a console.

**One real bug found and fixed.** A stale `USE_SQLITE_FALLBACK=1` shell variable
silently overrode the committed `.env`, so a run reported success while executing on
SQLite. `config/settings/base.py` now loads the project `.env` with `overwrite=True`,
and `backend/run_tests.py` prints the vendor, whether row locking is active, and an
unambiguous `RESULT: PASS/FAIL` with a correct exit code. Verified by setting the
hostile variable and confirming PostgreSQL is still used.

**Deliberate deviations, recorded**
- `auth.E003` is silenced. Django wants `USERNAME_FIELD` globally unique, but the
  spec requires staff email unique *per tenant* (R38.8). Consequence: a tenant-aware
  authentication backend is required before login is exposed, or a duplicate across
  tenants raises `MultipleObjectsReturned`.
- The suite runs against SQLite through an explicit opt-in `USE_SQLITE_FALLBACK`,
  because no PostgreSQL server was reachable on the development machine. PostgreSQL
  is the configured target and the fallback is refused outright in production
  settings. The capacity guarantees rely on `select_for_update`, so the suite must be
  re-run against PostgreSQL before launch.

**Not verified — no toolchain on this machine**
- Next.js: Node/npm absent, so never `npm install`-ed, type-checked or built.
- Flutter: SDK absent, so never `pub get`-ted, analysed or built.
- Both are coherent and complete but unexercised; expect small first-run fixes.

**Still to port** (with their tests, in order): DRF permission enforcement,
promotions, shows, counter POS, partner API, reporting, seating, gate offline sync.
The README gives the order.

### Manage booking ported and verified on PostgreSQL
Self-service by the customer (R16), with ownership proven by a one-time code emailed
to the address on the booking. Endpoints: `manage/request-code/`, `manage/verify/`,
`manage/cancel/`, `manage/reschedule/`.

The lookup response is identical whether the booking exists or not, so it cannot be
used to enumerate (R16.3) — a code is only issued when the number/email pair matches.
Codes are single-use and short-lived (R16.11); a wrong code is a plain error until
repeated failures throttle. Cancel and reschedule follow a configurable
time-before-visit refund policy (default tiers: full ≥48h, 50% ≥24h, none after).
Reschedule acquires the target date's capacity *before* releasing the original, so a
failed move leaves the booking untouched (R16.7), and reissues tickets so superseded
QR codes stop working (R16.9). A used ticket blocks self-service cancel and reschedule
(R16.8). Cancel is confirmation-gated, stating the scope and refund amount before
acting (R17.8), and restores future-dated capacity (R17.5).

**Verified:** 96 tests pass on PostgreSQL (up from 84) — 12 new covering enumeration
indistinguishability, single-use and expired codes, the throttle threshold, the used-
ticket block, confirmation-gated cancel with the tiered refund, double-cancel
rejection, capacity-moving reschedule with ticket reissue, and a failed reschedule
leaving the original intact. A live 12-check HTTP run confirmed all of it end to end,
and the database showed one CANCELLED booking, exactly one (consumed) verification
challenge despite three request-code calls — the two enumeration probes correctly
created none — and audit rows for the manage login and the cancellation.

**Note:** the actual money movement on cancel is a computed-and-recorded placeholder;
the refund-to-provider path (R17.7 retryable failed refunds) ports with the refund
work. Manage endpoints currently take the verified `booking_id` in the request body;
a short-lived manage token replaces that when sessions land.

### Gate validation ported and verified on PostgreSQL
`POST /gate/scan/` turns a QR into exactly one decision from the closed set,
evaluated in the reference implementation's fixed cheapest-first order:
signature → existence → venue/gate → terminal state → validity window (frozen
venue-local snapshot) → entry allowance and re-entry.

An admit consumes one entry through `select_for_update` on the ticket, so two
simultaneous scans of the last allowed entry admit exactly one — the loser sees
REJECT_ALREADY_USED with the time and gate of the previous admission. Every scan
(admitted or rejected) is written to the append-only `ScanEvent` table.

Supervisor override (`POST /gate/override/`): records a distinct ADMIT scan, audits
the original rejection alongside the reason, and both rows are retained (append-only).
Manual lookup (`GET /gate/lookup/`): resolves by booking number for when the QR
won't scan.

**Verified:** 84 tests pass on PostgreSQL (up from 66) — 17 new unit tests covering
admit, already-used, forged/garbage code, wrong venue/gate, expired, not-yet-valid,
cancelled, blocked, re-entry within/outside window, override with and without reason,
manual lookup with and without a match, and append-only enforcement; plus a 2-thread
concurrent double-scan on PostgreSQL (`select_for_update`) proving exactly one admit.
A live 8-check HTTP run scanned a real issued ticket: ADMIT, second scan REJECT,
forged code REJECT_UNKNOWN_CODE (no token error leaks), and manual lookup returning
the ticket. PostgreSQL's `access_scanevent` table showed all three scans with Bangkok
local timestamps.

**Bug found during port:** the `ScanEvent.decision` column was 24 chars and
`REJECT_WRONG_VENUE_OR_GATE` is 25 — caught by the real data, migrated to 30.
The re-entry test also failed because `record_entry` used `timezone.now()` while
the test passed a fixed 2026 moment — now accepts an `at` parameter so both the
test and a real scan use the same clock.

### Payment webhook ported and verified on PostgreSQL
The provider callback (`POST /api/webhook/payment/`) is the authoritative confirmation
of a charge (R14.7). It verifies the signature against the **raw** request body, then
records the delivery in a new `PaymentEvent` table under a unique
`(tenant, provider, provider_event_id)` constraint **before** any payment state
changes. That ordering is the whole trick: a duplicate or out-of-order delivery loses
the insert race and is recognised as a replay, so the state transition and the booking
completion happen exactly once (R14.4).

On a captured event it completes the booking through the same idempotent
`finalize_paid_booking` the inline path uses, via an `on_payment_captured` hook — so a
booking whose browser or kiosk session died after authorization still confirms and
delivers its ticket (R14.6). A second successful charge for an already-confirmed
booking is flagged for refund rather than double-confirming (R14.5), and an amount that
disagrees with the platform is a reconciliation exception, not a silent capture.

**Verified:** 66 tests pass on PostgreSQL (up from 59) — 7 new webhook tests covering
signature rejection, the browser-died capture, duplicate and out-of-order delivery,
amount mismatch, orphaned authorization and duplicate-charge flagging. A 7-check live
HTTP run confirmed the endpoint rejects a bad signature (422), processes a verified
capture idempotently, recognises a duplicate delivery, and leaves exactly one confirmed
booking with one ticket — and the database showed exactly one `PaymentEvent` row, with
the rejected and duplicate deliveries creating none.

### Booking write path ported and verified on PostgreSQL
The quote → consent → payment → confirm → e-ticket flow is live on Django, proven on
real PostgreSQL 17.7 (not the SQLite fallback).

**Confirm ordering** matches the reference implementation exactly: check the required
consent *before* persisting any personal data (R12.2); re-resolve the price
authoritatively (R13.7); persist the booking as `AWAITING_PAYMENT`; take payment;
convert holds into confirmed capacity; issue tickets. A single idempotent
`finalize_paid_booking` completes the booking and is shared by the inline path and the
future webhook, so a browser dying after authorization still yields a confirmed
booking (R14.6).

**New services:** consent (immutable versioned `PrivacyNotice` + append-only
`ConsentRecord`, required-item gate), deterministic price resolution (priority →
specificity → stable id tiebreak, no fallback price), a `PaymentGateway` protocol with
a deterministic idempotent `SimulatedGateway`, `start_payment` keyed on an idempotency
key, and ticket issuance with signed opaque QR payloads (no PII) and a validity window
frozen from the venue's policy and timezone.

**New endpoints:** `GET /consent/`, `POST /quote/`, `POST /confirm/`, venue-scoped. The
confirm payload re-sends the quote inputs so the server re-prices authoritatively and
never trusts a client cart.

**Verified:**
- 59 tests pass on PostgreSQL (up from 47), including 12 write-path unit tests and a
  two-thread confirm race for a single seat that confirms exactly one booking.
- A 15-check live HTTP run: catalogue, consent dialog with nothing pre-ticked, quote,
  consent-refused confirm (422 `consent_required`), a confirmed booking with two
  signed PII-free tickets whose expiry is venue-local, and an idempotent replay that
  returns the same booking and creates no duplicate payment.
- Inspected the PostgreSQL rows directly: one CONFIRMED booking (VAT snapshot 700,
  fully paid), two VALID Bangkok-timezone tickets, exactly one consent record and one
  payment.

**Bugs found and fixed during the port:** `bulk_create` bypasses `Model.save()`, so
ticket string ids came out empty — now assigned explicitly; a test compared UTC string
form against an in-memory venue-local value — now compares instants; and a concurrency
test's barrier sat after the hold was already taken — moved so the threads genuinely
race the last seat.

### Customer booking UI — visual language aligned with the `aquaria-book-now` app
The sibling Lovable/TanStack prototype (`aquaria-book-now`) had a stronger landing page,
so its design language was ported onto this app's booking flow. Plain CSS only — no
build step, no framework, no new dependency.

- **Tokens** (`web/styles.css`): adopted the prototype's oklch ocean palette — deep
  peacock primary `oklch(0.44 0.115 213)`, warm amber accent `oklch(0.75 0.155 55)`,
  near-white ocean background, and separate `--success`/`--warning` status hues. Every
  pre-existing token *name* was kept, so all 500 lines of prior rules still apply.
  Added `--font-display`, a wider radius scale and a deeper shadow.
- **Hero**: full-bleed photograph (`web/aquarium-hero.jpg`, copied from the prototype
  and served same-origin because the CSP allows `img-src 'self'` only) behind a
  multi-stop ocean gradient held at ≥0.82 alpha across the text column so the headline
  keeps AA contrast whatever the image does. Carries a badge, a serif headline, a lead
  line and three at-a-glance facts. Shown on the booking view only.
- **Cards and steps**: panels became rounded white cards on a tinted page; each booking
  step heading carries a circular number generated from a `data-step` attribute.
- **Order summary**: rebuilt as the prototype's "ticket head" — a gradient panel with
  two decorative rings, the venue name in the display serif over its locality, then a
  white body with a ticket-count badge, Date/Venue tiles, line items, and the total in
  large display type. Sticky on desktop.
- **Previously unstyled components now styled**: the pricing-group cards, the
  Make-a-Payment CTA (label left, arrow right), the secure-checkout reassurance line,
  the hold countdown, the mobile sticky pay bar and the *entire* review dialog
  (`.review-*`/`.rv-*`) had **no CSS at all** and were rendering unstyled. All are now
  designed. The arrow, padlock, wave brand mark and selection ticks are drawn in CSS,
  so nothing extra is fetched and no icon font is needed.
- **Kept**: the small inline type icons from the previous change — `.tt-icon`/`.seg-svg`
  in front of each ticket type and `.pay-icon`/`.pay-svg` in front of each payment
  method, still on one line with the name, price and control.
- **Localization**: new client strings (`your_order`, `date_label`, `venue_label`,
  `hero_title`, `hero_lead`) added in all five supported languages; the hero and ticket
  head re-render on a language switch and read from `/api/venue`, so another
  tenant/venue re-skins the page from configuration alone.

**Deviation worth knowing:** the prototype uses Fraunces + Manrope from Google Fonts.
This app's CSP is `font-src 'self' data:`, so those files cannot load. The pairing is
reproduced with system faces — a high-contrast serif for display, a clean grotesque for
body, `Noto Sans Thai` first so Thai never clips (R69.8). Self-hosting the two woff2
files under `web/` would give the exact faces without touching the CSP.

Verified: 150 tests pass; `verify_server.py` 93/93 on a fresh server; `/`, `/styles.css`,
`/app.js` and `/aquarium-hero.jpg` all serve with correct content types; no external
font or stylesheet references, no `@import`, no inline handlers.

*Note: run `verify_server.py` against a freshly started server. The exchange-rate
check now cleans up after itself, but a repeated run still trips the booking-lookup
rate limiter (429) — which is R73.5 working, not a regression.*

### Bug fix — payment error hid the real reason ("check the highlighted fields")
A customer paying could get the generic "Please check the highlighted fields and try
again. (ref …)" with no indication of what was wrong. Two defects:

1. **`localize_error` (utp/core/i18n.py) discarded caller-supplied messages.** It always
   overwrote `error.message` with the translation of `message_key`, so a specific,
   actionable message such as "Your selection expired. Please start again." was replaced
   by the generic `error.validation_failed` text. Fixed so localization applies **only
   when the error carries its class default message**; a message a raise site passed
   explicitly is preserved. Default-message errors still localize (Thai etc.) as before.
   This is the real trigger: the confirm handler raises this when the session-saved quote
   is gone — most often a **double-submit / retry after a prior confirm consumed the
   quote**, or a refreshed session.
2. **The web `pay()` catch (web/app.js) showed only the generic message.** It now prefers
   the server's per-field text (`details.fields`) when present, and when the failure is an
   expired selection/hold it closes the dialogs, clears the dead quote and returns the
   guest to ticket selection instead of looping on a quote that can never succeed.

Verified by reproducing the exact HTTP flow (CSRF → quote → confirm) across variants
(no prior quote, empty consent, blank email, double-confirm): the "selection expired"
cases now return the specific message; Thai localization of default-message errors still
works. 150 tests pass; 93 HTTP checks pass.

### Customer booking UI — type rows with small inline icons
The ticket-type list (booking step 2) and the payment-method list (review dialog)
emitted card markup (`.tt-card`/`.tt-icon`/`.tt-info`, `.pay-card`/`.pay-icon`/
`.pay-text`) that had **no matching CSS**, so each type stacked its icon, name, price
and control on separate lines. Added the missing rules to `web/styles.css` so every
type now renders as a compact single-line row: a small segment/method icon
(`.seg-svg` 24px, `.pay-svg` 22px, stroked in the peacock palette) sits inline with
the name, price and quantity/selection control. No JS or markup change was needed —
the icons and classes already existed; only the layout/sizing CSS was missing.
Verified by serving the app and confirming the new rules ship in `styles.css`
(now ~24 KB) with the page loading 200; the 150-test suite and 93 HTTP checks remain
green (CSS is presentation-only).

### Business / Venue Settings (schema, service, API, back-office UI)
- VAT and service charge: effective-dated, inclusive/exclusive, via `compute_charges`
  with a fixed calculation order (base → line discount → order discount → service
  charge → VAT → rounding → grand total) and Cases A–D covered by tests.
- IANA timezone, QR/ticket validity policies (default End-of-Visit-Day = 23:59:59
  venue-local), base currency, decimal-exact exchange-rate management.
- Surfaced via `/api/staff/settings/*` endpoints and a back-office Settings screen in
  the web Operations view. Six permission pages + six `MANAGE_*` action permissions.

### Gate & access validation (R32)
- `AccessService.scan` turns a QR into exactly one decision from a closed set
  (ADMIT, ADMIT_WITH_CHECK, and the specific REJECT_* reasons), evaluated in a fixed
  cheapest-first order and recorded in the append-only `scan_events` table.
- Signature verified before any DB lookup; ticket state, validity window (venue-local
  snapshot), date/session, entry allowance and re-entry window all checked.
- Admits consume an entry through an atomic counter, so two simultaneous scans of the
  last allowed entry admit exactly one; the duplicate is refused with the time and
  gate of the previous admission.
- Device authentication (unregistered/deactivated scanners refused), supervisor
  override (`OVERRIDE_ACCESS` + mandatory reason, audited), manual booking lookup, and
  offline-sync conflict detection (same single-entry ticket at two gates is flagged,
  never silently dropped). Endpoints: `POST /api/gate/scan`, `/api/gate/override`,
  `GET /api/gate/lookup`. A gate scanner tool is in the web Operations view.

### Advanced Promotion Engine
The base engine already covered 22 mechanics over six effect kinds, stacking,
priority, budget/usage caps under concurrency, best-for-customer selection,
deterministic combination, coupon codes with friendly rejections, and historical
immutability. Added this cycle:

- **Nth-item discounts** (`SECOND_ITEM_DISCOUNT` → `NTH_ITEM` effect): second / third
  / nth / cheapest / most-expensive targeting, percent or fixed, deterministic unit
  selection.
- **Cash-coupon accounting treatment** (the critical distinction): a coupon is a
  `DISCOUNT` (reduces revenue) or a `STORED_VALUE` / `PAYMENT` / `LIABILITY`
  instrument that **settles the bill without being a sales discount**. Stored value
  lives in a separate `settlements` list on the cart; revenue and VAT stay on the full
  price and only the amount collected by the payment method goes down.
- **Stored value wired through the payment path**: the gateway is charged only
  `total − settlement`; a gift card covering the whole bill settles via a
  `STORED_VALUE` payment method with no gateway call. Booking records
  `settlement_minor` / `amount_paid_minor`.
- **Settlement-redemption ledger**: gift-card redemptions consume the promotion's
  usage cap atomically, so a `usage_limit=1` card is single-use across bookings and is
  restored on cancellation.
- **Member loyalty points** (`MemberService` + `members` / append-only `point_ledger`):
  enrol, earn, redeem (atomic floor-guarded, concurrency-safe), configurable
  point-to-cash rate snapshotted per redemption, restore on cancel/refund/void/failed
  payment. Points redeemed at checkout settle the bill like a gift card.
- **Free-gift with reward inventory** (`FREE_GIFT` mechanic/effect): a spend threshold
  or product trigger grants a zero-cost reward (product / add-on / voucher). Stock is
  enforced through the promotion's usage cap; once depleted the gift is no longer
  offered. Gifts are snapshotted onto the booking.
- Four new permission pages (Coupon Codes, Cash Coupons, Member Rewards, Partner
  Benefits) and seven action permissions (PUBLISH_PROMOTION, PAUSE_PROMOTION,
  OVERRIDE_PROMOTION, MANAGE_PROMOTION_BUDGET, MANAGE_ACCOUNTING_TREATMENT,
  APPLY_PARTNER_DISCOUNT, APPLY_COMPLIMENTARY).

---

## Running it

```
python serve.py                    # http://127.0.0.1:8080  (--fresh, --port, --db :memory:)
python serve.py --demo-history 28  # ...plus 28 days of trading history for the dashboards
python run_tests.py                # 350 tests, expect "RESULT: PASS"
python verify_server.py            # 191 end-to-end HTTP checks against a running server
python smoke_check.py              # in-process service smoke test
```

Demo staff credential `Aquaria-Demo-2026`; sign-in emails `manager@`, `cashier@`,
`gate@`, `viewer@aquaria.test`. `admin@aquaria.test` (Super Admin) requires MFA,
which is not wired into the demo UI, so use `manager@` for back-office work in the
browser. `viewer@` is a Report Viewer — dashboards and reports, but no personal
data, no cost columns and no export — which is the account to sign in as if you
want to see the masking working.

**Note on the local database file:** the on-disk `data/aquaria.db` predates recent
schema bumps and may be locked by a stale process. Run with `--db :memory:` on a
fresh port (or delete the file and use `--fresh`) to pick up the current schema.

---

## Known gaps / debts

- **Not yet implemented:** seating (R47–R62), partner API (R35), and the
  PostgreSQL/S3 port.
- **Reporting gaps.** Scheduled report delivery (§38) has its permission
  (`SCHEDULE_REPORT`) and nothing behind it yet. Export is CSV and a printable
  document; there is no hand-rolled PDF writer, because the browser's own
  print-to-PDF produces a better-typeset document than one would and needs no
  dependency. The seat and partner reports are declared and return nothing until
  those modules exist, which is the honest answer rather than a hidden menu.
- **Loyalty point earning** is available (`MemberService.earn`) but not yet triggered
  automatically on booking confirmation — members can redeem but do not auto-accrue
  from spend. Wiring an earn call into `finalize_paid_booking` with a configurable
  earn rate is a small follow-on.
- **Secrets** are development placeholders; set `UTP_SECRET_*` and `UTP_SIGNING_KEY`
  before any real use.
- Several early services lack dedicated unit tests and are exercised only through
  integration paths and `smoke_check.py`.
