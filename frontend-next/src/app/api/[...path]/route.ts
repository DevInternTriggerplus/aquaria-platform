import { NextRequest, NextResponse } from "next/server";

const venue = {
  id: "demo-aqp",
  code: "aqp",
  name: { en: "Aquaria Phuket", th: "อควาเรีย ภูเก็ต" },
  short_name: "Aquaria Phuket",
  venue_type: "AQUARIUM",
  timezone: "Asia/Bangkok",
  currency: "THB",
  tax_model: "INCLUSIVE",
  address: "B1, Central Phuket Floresta, Wichit, Mueang Phuket, Phuket 83000",
  operating_hours: { default: { open: "10:30", close: "19:00", last_admission: "18:00" } },
  areas: [],
};

const ticketTypes = [
  ["adult", "Adult", 125100],
  ["child", "Child", 67500],
  ["senior", "Senior", 67500],
].map(([code, label, unitPriceMinor]) => ({
  id: `demo-ga-intl-${code}`,
  code: `ga-intl-${code}`,
  name: { en: label },
  description: {},
  segment_code: code,
  min_quantity: 0,
  max_quantity: 10,
  entry_allowance: 1,
  unit_price_minor: unitPriceMinor,
  currency: "THB",
}));

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path: pathParts } = await params;
  const path = pathParts.join("/");
  const url = request.nextUrl;

  if (path === "venues/aqp") return NextResponse.json(venue);

  if (path === "venues/aqp/products") {
    return NextResponse.json({
      date: url.searchParams.get("date"),
      currency: "THB",
      products: [
        {
          id: "demo-ga-intl",
          code: "ga-intl",
          name: { en: "General Admission - International" },
          description: { en: "Nine zones, all shows included." },
          admission_model: "GENERAL_ADMISSION",
          session_requirement: "NOT_USED",
          min_per_booking: 1,
          max_per_booking: 10,
          ticket_types: ticketTypes,
        },
      ],
    });
  }

  if (path === "venues/aqp/sessions") {
    return NextResponse.json({ date: url.searchParams.get("date"), timezone: venue.timezone, sessions: [] });
  }

  if (path === "venues/aqp/payment-types") {
    return NextResponse.json({
      payment_types: [
        { id: "demo-promptpay", code: "promptpay", method: "QR_BANK_TRANSFER", display_name: { en: "PromptPay QR" }, description: {}, icon: "" },
        { id: "demo-card", code: "card", method: "CARD", display_name: { en: "Credit or debit card" }, description: {}, icon: "" },
      ],
    });
  }

  if (path === "venues/aqp/charge-preview") {
    const baseMinor = Number(url.searchParams.get("base_minor"));
    if (!Number.isSafeInteger(baseMinor) || baseMinor < 0) {
      return NextResponse.json({ error: { code: "invalid_amount", message: "Invalid amount." } }, { status: 400 });
    }
    const vatMinor = Math.round((baseMinor * 7) / 107);
    return NextResponse.json({
      base_minor: baseMinor,
      line_discount_minor: 0,
      order_discount_minor: 0,
      subtotal_minor: baseMinor,
      taxable_base_minor: baseMinor - vatMinor,
      service_charge_minor: 0,
      service_charge_included: false,
      vat_minor: vatMinor,
      vat_included: true,
      rounding_adjustment_minor: 0,
      grand_total_minor: baseMinor,
      currency: "THB",
    });
  }

  return NextResponse.json({ error: { code: "not_found", message: "Not found." } }, { status: 404 });
}
