"use client";

/**
 * The order summary: a gradient "ticket head" above a white body, sticky on desktop
 * so the total is always in view.
 *
 * Every figure shown here comes from the server's charge breakdown. The client does
 * not add up prices itself, so what the customer reads is exactly what will be
 * charged.
 */

import { formatMoney } from "@/lib/money";
import type { ChargeBreakdown } from "@/lib/api";

export interface SummaryLine {
  code: string;
  label: string;
  quantity: number;
  amountMinor: number;
}

export function OrderSummary({
  venueName,
  locality,
  visitDate,
  lines,
  charges,
  currency,
  onContinue,
  busy,
  completed,
}: {
  venueName: string;
  locality?: string;
  visitDate: string | null;
  lines: SummaryLine[];
  charges: ChargeBreakdown | null;
  currency: string;
  onContinue: () => void;
  busy?: boolean;
  completed?: boolean;
}) {
  const totalTickets = lines.reduce((n, l) => n + l.quantity, 0);
  const discount =
    (charges?.line_discount_minor ?? 0) + (charges?.order_discount_minor ?? 0);

  return (
    <aside className="lg:sticky lg:top-6">
      <div className="overflow-hidden rounded-[var(--radius-card)] border bg-card shadow-sm">
        {/* Ticket head */}
        <div className="relative isolate overflow-hidden bg-gradient-to-br from-primary via-primary to-accent px-6 py-6 text-primary-foreground">
          <div
            aria-hidden
            className="absolute -right-10 -top-10 h-32 w-32 rounded-full border border-white/20"
          />
          <div aria-hidden className="absolute right-6 top-10 h-16 w-16 rounded-full bg-white/10" />
          <p className="text-[0.64rem] font-bold uppercase tracking-[0.25em] opacity-80">
            Your visit
          </p>
          <p
            className="mt-1 text-[1.75rem] leading-tight font-semibold"
            style={{ fontFamily: "var(--font-display)" }}
          >
            {venueName}
          </p>
          {locality ? <p className="mt-2 text-xs opacity-85">{locality}</p> : null}
        </div>

        <div className="space-y-4 p-5 sm:p-6">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-lg font-semibold">Your order</h2>
            {totalTickets > 0 ? (
              <span className="rounded-full bg-secondary px-2.5 py-0.5 text-xs font-bold text-primary-deep">
                {totalTickets} {totalTickets === 1 ? "ticket" : "tickets"}
              </span>
            ) : null}
          </div>

          <div className="grid grid-cols-2 gap-2.5">
            <Tile label="Date" value={visitDate ?? "Not selected"} />
            <Tile label="Venue" value={venueName} />
          </div>

          {lines.length === 0 ? (
            <p className="text-sm text-muted-foreground">No tickets added yet.</p>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {lines.map((line) => (
                <li key={line.code} className="flex justify-between gap-3">
                  <span className="text-muted-foreground">
                    {line.quantity} × {line.label}
                  </span>
                  <span className="font-semibold tabular-nums">
                    {formatMoney(line.amountMinor, currency)}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {charges ? (
            <div className="space-y-1.5 border-t pt-3 text-sm">
              <Row label="Subtotal" value={formatMoney(charges.subtotal_minor, currency)} />
              {discount > 0 ? (
                <Row
                  label="Discount"
                  value={`−${formatMoney(discount, currency)}`}
                  className="text-[var(--color-success)]"
                />
              ) : null}
              {charges.service_charge_minor ? (
                <Row
                  label={`Service charge${charges.service_charge_included ? " (included)" : ""}`}
                  value={formatMoney(charges.service_charge_minor, currency)}
                />
              ) : null}
              {charges.vat_minor ? (
                <Row
                  label={`VAT${charges.vat_included ? " (included)" : ""}`}
                  value={formatMoney(charges.vat_minor, currency)}
                />
              ) : null}
              {/* Never let the displayed lines silently fail to sum to the total. */}
              {charges.rounding_adjustment_minor !== 0 ? (
                <Row
                  label="Rounding"
                  value={`${charges.rounding_adjustment_minor < 0 ? "−" : "+"}${formatMoney(
                    Math.abs(charges.rounding_adjustment_minor),
                    currency,
                  )}`}
                />
              ) : null}
            </div>
          ) : null}

          <div className="flex items-end justify-between gap-4 border-t pt-3">
            <div>
              <p className="text-sm font-semibold">Total</p>
              <p className="text-[0.7rem] text-muted-foreground">
                {charges?.vat_included ? "VAT included" : "VAT added at payment"}
              </p>
            </div>
            <p
              className="text-3xl font-semibold tabular-nums text-primary"
              style={{ fontFamily: "var(--font-display)" }}
            >
              {formatMoney(charges?.grand_total_minor ?? 0, currency)}
            </p>
          </div>

          <button
            type="button"
            onClick={onContinue}
            disabled={busy || completed || totalTickets === 0}
            className="flex min-h-[52px] w-full items-center justify-between gap-2 rounded-lg bg-primary px-5 font-semibold text-primary-foreground disabled:opacity-50"
          >
            <span>{completed ? "Booking confirmed" : busy ? "Confirming…" : "Confirm mock payment"}</span>
            <span aria-hidden>→</span>
          </button>
          <p className="flex items-center justify-center gap-1.5 text-center text-xs text-muted-foreground">
            <LockGlyph /> Mock payment · no funds are captured
          </p>
        </div>
      </div>
    </aside>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-secondary/45 px-3 py-2.5">
      <p className="text-[0.68rem] text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-sm font-semibold break-words">{value}</p>
    </div>
  );
}

function Row({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className={`flex justify-between gap-3 ${className ?? ""}`}>
      <span className="text-muted-foreground">{label}</span>
      <span className="font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function LockGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-3 w-3"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden
    >
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </svg>
  );
}
