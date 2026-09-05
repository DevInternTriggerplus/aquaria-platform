/**
 * Typed client for the Django API.
 *
 * Every error the backend returns uses one envelope — `{ error: { code, message,
 * message_key, reference, details } }` — so this is the single place that unwraps
 * it. The `message` is already customer-safe and localized; the `reference` is the
 * correlation id to quote to support.
 */

export type LocalizedText = Record<string, string>;

export interface Venue {
  id: string;
  code: string;
  name: LocalizedText;
  short_name: string;
  venue_type: string;
  timezone: string;
  currency: string;
  tax_model: "INCLUSIVE" | "EXCLUSIVE";
  address: string;
  operating_hours: Record<string, { open?: string; close?: string; last_admission?: string }>;
  areas: { id: string; code: string; name: LocalizedText; floor: string }[];
}

export interface TicketType {
  id: string;
  code: string;
  name: LocalizedText;
  description: LocalizedText;
  segment_code: string;
  min_quantity: number;
  max_quantity: number | null;
  entry_allowance: number;
  /** Null means no price rule matched, so the type is not sellable for this request. */
  unit_price_minor: number | null;
  currency: string;
}

export interface Product {
  id: string;
  code: string;
  name: LocalizedText;
  description: LocalizedText;
  admission_model: string;
  session_requirement: "NOT_USED" | "OPTIONAL" | "REQUIRED";
  min_per_booking: number;
  max_per_booking: number;
  ticket_types: TicketType[];
}

export interface PaymentType {
  id: string;
  code: string;
  method: string;
  display_name: LocalizedText;
  description: LocalizedText;
  icon: string;
}

export interface ChargeBreakdown {
  base_minor: number;
  line_discount_minor: number;
  order_discount_minor: number;
  subtotal_minor: number;
  taxable_base_minor: number;
  service_charge_minor: number;
  service_charge_included: boolean;
  vat_minor: number;
  vat_included: boolean;
  rounding_adjustment_minor: number;
  grand_total_minor: number;
  currency: string;
}

export interface ConsentDialog {
  items: { code: string; required: boolean; label: string; granted: boolean }[];
}

export interface ConfirmedBooking {
  status: "CONFIRMED";
  confirmed: boolean;
  booking_number: string;
  total_minor: number;
  currency: string;
  payment: { provider_ref: string; status: string } | null;
  tickets: { id: string; ticket_number: string; visit_date: string; qr_payload: string }[];
}

export class ApiError extends Error {
  code: string;
  reference?: string;
  fields: Record<string, string>;

  constructor(message: string, code: string, reference?: string, fields: Record<string, string> = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.reference = reference;
    this.fields = fields;
  }
}

const VENUE = process.env.NEXT_PUBLIC_VENUE_CODE ?? "aqp";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });

  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};

  if (!response.ok) {
    const err = payload?.error ?? {};
    throw new ApiError(
      // Prefer the specific per-field message when the server sent one: it is far
      // more useful than a generic "check the highlighted fields".
      firstFieldMessage(err?.details?.fields) ?? err.message ?? "Something went wrong. Please try again.",
      err.code ?? "request_failed",
      err.reference,
      err?.details?.fields ?? {},
    );
  }
  return payload as T;
}

function firstFieldMessage(fields: unknown): string | undefined {
  if (!fields || typeof fields !== "object") return undefined;
  const values = Object.values(fields as Record<string, unknown>);
  const first = values[0];
  if (typeof first === "string") return first;
  if (Array.isArray(first) && typeof first[0] === "string") return first[0];
  return undefined;
}

export const api = {
  venue: () => request<Venue>(`/api/venues/${VENUE}/`),

  products: (date: string, channel = "ONLINE") =>
    request<{ date: string; currency: string; products: Product[] }>(
      `/api/venues/${VENUE}/products/?date=${encodeURIComponent(date)}&channel=${channel}`,
    ),

  sessions: (date: string) =>
    request<{ date: string; timezone: string; sessions: unknown[] }>(
      `/api/venues/${VENUE}/sessions/?date=${encodeURIComponent(date)}`,
    ),

  paymentTypes: (channel = "ONLINE", currency?: string) =>
    request<{ payment_types: PaymentType[] }>(
      `/api/venues/${VENUE}/payment-types/?channel=${channel}${
        currency ? `&currency=${currency}` : ""
      }`,
    ),

  /**
   * Ask the server what a base amount actually breaks down to. The client never
   * computes tax itself — that would be a second source of truth.
   */
  chargePreview: (baseMinor: number, date: string) =>
    request<ChargeBreakdown>(
      `/api/venues/${VENUE}/charge-preview/?base_minor=${baseMinor}&date=${encodeURIComponent(date)}`,
    ),

  consent: () => request<ConsentDialog>(`/api/venues/${VENUE}/consent/`),

  quote: (visitDate: string, lines: { ticket_type_id: string; quantity: number }[]) =>
    request<{ charges: ChargeBreakdown }>(`/api/venues/${VENUE}/quote/`, {
      method: "POST",
      body: JSON.stringify({ visit_date: visitDate, lines }),
    }),

  confirm: (payload: {
    visit_date: string;
    lines: { ticket_type_id: string; quantity: number }[];
    email: string;
    full_name: string;
    consent_items: Record<string, boolean>;
    idempotency_key: string;
  }) =>
    request<ConfirmedBooking>(`/api/venues/${VENUE}/confirm/`, {
      method: "POST",
      body: JSON.stringify({ ...payload, payment_method: "CARD" }),
    }),
};

/** Pick a language from a translatable map, falling back to English. */
export function pick(text: LocalizedText | undefined, lang = "en", fallback = ""): string {
  if (!text) return fallback;
  return text[lang] ?? text.en ?? Object.values(text)[0] ?? fallback;
}
