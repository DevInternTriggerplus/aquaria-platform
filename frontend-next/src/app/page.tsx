"use client";

/**
 * The booking page.
 *
 * Layout and visual language match the platform's other front ends: a hero over an
 * ocean gradient, numbered step cards down the left, and the sticky ticket-head
 * order summary on the right.
 *
 * Every price and total is fetched from the backend. The client formats money but
 * never computes it, so there is exactly one source of truth for what a guest pays.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { OrderSummary, type SummaryLine } from "@/components/order-summary";
import { StepCard } from "@/components/step-card";
import { TicketTypeRow } from "@/components/ticket-type-row";
import { api, pick, ApiError, type ChargeBreakdown, type Product, type Venue } from "@/lib/api";

const LANG = "en";

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

/** The next 14 days, as a simple date strip. A full calendar comes with the
 *  availability endpoint, which reports per-date state (available / limited /
 *  sold out / closed) rather than letting the client guess. */
function upcomingDates(count = 14): string[] {
  const today = new Date();
  return Array.from({ length: count }, (_, i) => {
    const d = new Date(today);
    d.setDate(today.getDate() + i);
    return isoDate(d);
  });
}

export default function BookingPage() {
  const [venue, setVenue] = useState<Venue | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [visitDate, setVisitDate] = useState<string | null>(null);
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [charges, setCharges] = useState<ChargeBreakdown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const dates = useMemo(() => upcomingDates(), []);
  const currency = venue?.currency ?? "THB";

  useEffect(() => {
    api
      .venue()
      .then(setVenue)
      .catch((e: ApiError) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!visitDate) return;
    setQuantities({});
    api
      .products(visitDate)
      .then((data) => setProducts(data.products))
      .catch((e: ApiError) => setError(e.message));
  }, [visitDate]);

  // Ticket types across every product on sale, flattened for the picker.
  const ticketTypes = useMemo(
    () => products.flatMap((p) => p.ticket_types.map((t) => ({ product: p, ticketType: t }))),
    [products],
  );

  const baseMinor = useMemo(
    () =>
      ticketTypes.reduce((sum, { ticketType }) => {
        const qty = quantities[ticketType.id] ?? 0;
        return sum + qty * (ticketType.unit_price_minor ?? 0);
      }, 0),
    [ticketTypes, quantities],
  );

  // Ask the server for the breakdown whenever the basket changes. Debounced so a
  // rapid tap on the stepper does not fire a request per tap.
  useEffect(() => {
    if (!visitDate || baseMinor <= 0) {
      setCharges(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .chargePreview(baseMinor, visitDate)
        .then(setCharges)
        .catch((e: ApiError) => setError(e.message));
    }, 200);
    return () => clearTimeout(timer);
  }, [baseMinor, visitDate]);

  const lines: SummaryLine[] = ticketTypes
    .filter(({ ticketType }) => (quantities[ticketType.id] ?? 0) > 0)
    .map(({ ticketType }) => ({
      code: ticketType.id,
      label: pick(ticketType.name, LANG, ticketType.code),
      quantity: quantities[ticketType.id] ?? 0,
      amountMinor: (quantities[ticketType.id] ?? 0) * (ticketType.unit_price_minor ?? 0),
    }));

  const setQuantity = useCallback((id: string, next: number) => {
    setQuantities((prev) => ({ ...prev, [id]: next }));
  }, []);

  const venueName = venue ? pick(venue.name, LANG, venue.code) : "Aquaria";
  const hours = venue?.operating_hours?.default;

  const onContinue = () => {
    setBusy(true);
    // The confirm flow (PDPA consent, hold, payment) is the next port from the
    // reference implementation. It must not be faked here: taking money is the one
    // path that has to be right before it ships.
    setError("Checkout is not wired up yet in this client. See the repository README.");
    setBusy(false);
  };

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b bg-card/70 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3">
          <span aria-hidden className="text-primary">
            <WaveGlyph />
          </span>
          <span className="text-lg font-semibold" style={{ fontFamily: "var(--font-display)" }}>
            {venueName}
          </span>
          {hours?.open ? (
            <span className="ml-auto text-xs text-muted-foreground">
              Open {hours.open}–{hours.close}
              {hours.last_admission ? ` · last admission ${hours.last_admission}` : ""}
            </span>
          ) : null}
        </div>
      </header>

      <section className="relative isolate overflow-hidden">
        <div
          aria-hidden
          className="absolute inset-0 -z-10 bg-gradient-to-r from-primary-deep via-primary to-primary/60"
        />
        <div className="mx-auto max-w-6xl px-4 py-14 text-primary-foreground sm:py-16">
          <p className="inline-flex rounded-full bg-accent px-3 py-1 text-xs font-bold text-accent-foreground">
            Online booking demo
          </p>
          <h1 className="mt-4 max-w-2xl text-4xl leading-tight font-semibold sm:text-5xl">
            Book your visit to {venueName}
          </h1>
          <p className="mt-4 max-w-xl text-primary-foreground/85">
            Browse ticket options and pricing for your visit. Checkout is not available in this
            demonstration.
          </p>
        </div>
      </section>

      <main id="main" className="mx-auto max-w-6xl px-4 py-8 sm:py-10">
        {error ? (
          <p
            role="status"
            className="mb-6 rounded-lg border border-[var(--color-danger)] bg-white px-4 py-3 text-sm text-[var(--color-danger)]"
          >
            {error}
          </p>
        ) : null}

        <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr] lg:gap-8">
          <div className="space-y-6">
            <StepCard step={1} title="Choose your visit date">
              <div className="flex gap-2 overflow-x-auto pb-1">
                {dates.map((d) => {
                  const selected = d === visitDate;
                  const day = new Date(`${d}T00:00:00`);
                  return (
                    <button
                      key={d}
                      type="button"
                      aria-pressed={selected}
                      onClick={() => setVisitDate(d)}
                      className={`flex min-h-[64px] min-w-[64px] flex-none flex-col items-center justify-center rounded-lg border px-2 text-sm ${
                        selected
                          ? "border-primary bg-primary text-primary-foreground"
                          : "bg-card hover:border-primary/40"
                      }`}
                    >
                      <span className="text-[0.68rem] uppercase opacity-80">
                        {day.toLocaleDateString(undefined, { weekday: "short" })}
                      </span>
                      <strong>{day.getDate()}</strong>
                      <span className="text-[0.62rem] opacity-75">
                        {day.toLocaleDateString(undefined, { month: "short" })}
                      </span>
                    </button>
                  );
                })}
              </div>
              <p className="mt-3 text-sm text-muted-foreground">
                {visitDate
                  ? `Selected ${new Date(`${visitDate}T00:00:00`).toLocaleDateString(undefined, {
                      weekday: "long",
                      day: "numeric",
                      month: "long",
                      year: "numeric",
                    })}.`
                  : "Choose a date to see today's prices and availability."}
              </p>
            </StepCard>

            {visitDate ? (
              <StepCard step={2} title="Choose your tickets">
                {products.length === 0 ? (
                  <p className="py-6 text-center text-sm text-muted-foreground">
                    No tickets are on sale for this date.
                  </p>
                ) : (
                  products.map((product) => (
                    <div key={product.id} className="mb-4 last:mb-0">
                      <h3 className="mb-1 font-semibold">{pick(product.name, LANG, product.code)}</h3>
                      {pick(product.description, LANG) ? (
                        <p className="mb-2 text-sm text-muted-foreground">
                          {pick(product.description, LANG)}
                        </p>
                      ) : null}
                      {product.ticket_types.map((tt) => (
                        <TicketTypeRow
                          key={tt.id}
                          ticketType={tt}
                          quantity={quantities[tt.id] ?? 0}
                          max={tt.max_quantity ?? product.max_per_booking}
                          onChange={(next) => setQuantity(tt.id, next)}
                          lang={LANG}
                        />
                      ))}
                    </div>
                  ))
                )}
              </StepCard>
            ) : null}
          </div>

          <OrderSummary
            venueName={venueName}
            locality={venue?.address}
            visitDate={visitDate}
            lines={lines}
            charges={charges}
            currency={currency}
            onContinue={onContinue}
            busy={busy}
          />
        </div>
      </main>

      <footer className="border-t py-8 text-center text-xs text-muted-foreground">
        {venue ? [venueName, venue.address].filter(Boolean).join(" · ") : venueName}
      </footer>
    </div>
  );
}

function WaveGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-5 w-5"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
    >
      <path d="M2 8c2.5-2 5-2 7.5 0S17 10 19.5 8" />
      <path d="M2 13c2.5-2 5-2 7.5 0s7.5 2 10-0" />
      <path d="M2 18c2.5-2 5-2 7.5 0s7.5 2 10-0" />
    </svg>
  );
}
