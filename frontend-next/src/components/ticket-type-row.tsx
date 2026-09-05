"use client";

/**
 * One ticket type as a single-line row: small segment icon, name and price, then the
 * quantity stepper. The icon leads, the label carries the meaning.
 *
 * A ticket type with no resolved price is shown as unavailable rather than free —
 * the backend returns null when no price rule matched, and inventing a number would
 * be worse than saying so.
 */

import { formatMoney } from "@/lib/money";
import { pick, type TicketType } from "@/lib/api";

import { SegmentIcon } from "./segment-icon";

export function TicketTypeRow({
  ticketType,
  quantity,
  max,
  onChange,
  lang = "en",
}: {
  ticketType: TicketType;
  quantity: number;
  max: number;
  onChange: (next: number) => void;
  lang?: string;
}) {
  const label = pick(ticketType.name, lang, ticketType.code);
  const description = pick(ticketType.description, lang);
  const sellable = ticketType.unit_price_minor !== null;

  return (
    <div className="flex items-center gap-3 border-t py-3 first:border-t-0">
      <SegmentIcon segment={ticketType.segment_code || ticketType.code} />

      <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <strong className="min-w-0 font-semibold">{label}</strong>
        <span className="ml-auto font-semibold tabular-nums whitespace-nowrap">
          {sellable ? formatMoney(ticketType.unit_price_minor, ticketType.currency) : "Unavailable"}
        </span>
        {description ? (
          <small className="basis-full text-[0.82rem] text-muted-foreground">{description}</small>
        ) : null}
      </div>

      <div className="flex flex-none items-center gap-1.5">
        <button
          type="button"
          aria-label={`Remove one ${label}`}
          disabled={!sellable || quantity === 0}
          onClick={() => onChange(Math.max(0, quantity - 1))}
          className="h-11 w-11 rounded-lg border border-primary text-lg text-primary-deep disabled:opacity-40"
        >
          &minus;
        </button>
        <output
          aria-label={`${label} quantity`}
          className="min-w-[2.2ch] text-center font-bold tabular-nums"
        >
          {quantity}
        </output>
        <button
          type="button"
          aria-label={`Add one ${label}`}
          disabled={!sellable || quantity >= max}
          onClick={() => onChange(Math.min(max, quantity + 1))}
          className="h-11 w-11 rounded-lg border border-primary text-lg text-primary-deep disabled:opacity-40"
        >
          +
        </button>
      </div>
    </div>
  );
}
