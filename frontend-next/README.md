# Aquaria — Next.js customer booking app

Next.js 15 (App Router) + React 19 + Tailwind CSS 4, TypeScript strict.

> **Not built on the development machine.** Node and npm were not installed, so this
> source has never been through `npm install`, a type-check or a build. It is
> coherent and complete but unexercised — expect to fix small issues on first run.

## Run

```bash
npm install
cp .env.local.example .env.local
npm run dev          # http://localhost:3000
npm run typecheck    # worth running first
```

`next.config.mjs` proxies `/api/*` to `BACKEND_ORIGIN` (default
`http://127.0.0.1:8000`), so the browser only ever talks to this origin and there is
no CORS or cookie complication in development.

## Structure

```
src/
├── app/
│   ├── globals.css     design tokens (oklch ocean palette) + base layer
│   ├── layout.tsx      root shell, skip link
│   └── page.tsx        the booking flow
├── components/
│   ├── segment-icon.tsx      small inline icon per ticket type
│   ├── ticket-type-row.tsx   single-line row: icon, name, price, stepper
│   ├── step-card.tsx         numbered step card
│   └── order-summary.tsx     gradient ticket-head summary
└── lib/
    ├── api.ts          typed client, single error-envelope unwrap
    └── money.ts        formatting only, never arithmetic
```

## Rules this client follows

- **It never computes money.** Prices and totals come from the backend as integer
  minor units; `money.ts` formats them and nothing else. Two places computing tax is
  how a receipt stops matching a charge.
- **It never assumes two decimal places.** JPY has none.
- **A missing price means unavailable, not free.** The API returns
  `unit_price_minor: null` when no price rule matched, and the UI says so.
- **Colour is never the only cue.** Every state also carries a label or icon.
- **Server errors are shown as sent.** The API returns one envelope with a
  customer-safe message and a correlation reference; the client prefers the specific
  per-field message over the generic one.

## Not wired up yet

Checkout — PDPA consent, capacity hold, payment — is deliberately absent. Those paths
are being ported to Django with their tests first; faking them here would be worse
than the button being honest about it. See the repository README for the port order.
