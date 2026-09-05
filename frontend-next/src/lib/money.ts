/**
 * Money formatting for the client.
 *
 * The server is authoritative for every amount and sends integer minor units plus
 * a currency code. The client's only job is presentation, and it must not assume
 * two decimal places — JPY has none.
 *
 * No arithmetic on prices happens here. If the UI needs a total, it asks the
 * server, because there must be exactly one place that computes money.
 */

const MINOR_UNITS: Record<string, number> = {
  THB: 100,
  USD: 100,
  EUR: 100,
  SGD: 100,
  MYR: 100,
  CNY: 100,
  IDR: 100,
  JPY: 1,
};

const SYMBOLS: Record<string, string> = {
  THB: "฿",
  USD: "$",
  EUR: "€",
  SGD: "S$",
  MYR: "RM",
  CNY: "¥",
  JPY: "¥",
};

export function minorUnits(currency: string): number {
  return MINOR_UNITS[currency?.toUpperCase()] ?? 100;
}

export function currencyDecimals(currency: string): number {
  return minorUnits(currency) > 1 ? 2 : 0;
}

/** Render an amount in minor units, e.g. `฿1,251.00` or `¥5,000`. */
export function formatMoney(amountMinor: number | null | undefined, currency = "THB"): string {
  if (amountMinor === null || amountMinor === undefined) return "—";
  const code = (currency || "THB").toUpperCase();
  const decimals = currencyDecimals(code);
  const major = amountMinor / minorUnits(code);
  const text = major.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `${SYMBOLS[code] ?? code + " "}${text}`;
}
