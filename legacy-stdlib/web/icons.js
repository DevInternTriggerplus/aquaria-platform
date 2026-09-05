/* Shared topic-icon system for Settings and Reports (designIcon.md).
 *
 * One icon family, one place. The brief's rule is "users should recognize the
 * section before they finish reading its title", and its anti-rule is "do not mix
 * outline, emoji, 3D and material icons on the same navigation". So this module is
 * the single source of both the drawings and the topic→icon mapping (§36, §41): no
 * component picks an icon by hand, and there is nowhere for a second style to creep
 * in.
 *
 * How the icons are delivered: one hidden inline <svg> sprite of <symbol> defs is
 * injected into the document once, and every icon on the page is a tiny
 * <svg><use href="#ic-…"></svg> that references a symbol. That keeps the markup
 * light, lets the browser cache one copy of each path, and needs no network fetch
 * and no icon font — which matters because the CSP is font-src 'self' and
 * img-src 'self' data:. Inline SVG is DOM, not a fetched image, so it is allowed as
 * is and needs no nonce.
 *
 * Style: a clean duotone line mark on a soft rounded peacock tile. The primary
 * stroke is currentColor; a secondary ".uic-accent" fill is the teal highlight. The
 * depth is subtle (the tile, a soft shadow) rather than a glossy 3D render, matching
 * the platform's flat-charts / iconography-carries-recognition direction.
 */
(function () {
  'use strict';

  // Each entry is the inner markup of a 24×24 <symbol>. Strokes use currentColor so
  // one symbol tints to any tone; ".uic-accent" is the duotone fill. Deliberately
  // simple silhouettes — recognizable at 16px, no scene, no detail (§1, §37).
  var SYMBOLS = {
    // --- settings categories (§4) --- //
    business: '<path class="uic-accent" d="M4 20V6l7-2v16z"/><path d="M4 20V6l7-2v16M11 20V9l9 2v9M3 20h18M7 8v0M7 12v0M7 16v0M15 13v0M15 17v0"/>',
    ticket: '<path class="uic-accent" d="M4 8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1 0 4 2 2 0 0 1-2 2H6a2 2 0 0 1-2-2 2 2 0 0 0 0-4 2 2 0 0 1 0-4z"/><path d="M4 8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1 0 4 2 2 0 0 1-2 2H6a2 2 0 0 1-2-2 2 2 0 0 0 0-4 2 2 0 0 1 0-4zM14 6v12"/>',
    receipt: '<path class="uic-accent" d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6M9 16h3"/>',
    card: '<rect class="uic-accent" x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18M3 8a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM7 14h4"/>',
    gift: '<path class="uic-accent" d="M4 11h16v9H4z"/><path d="M4 11h16v9H4zM3 7h18v4H3zM12 7v13M12 7c-1-3-5-4-5-1 0 2 3 1 5 1 2 0 5 1 5-1 0-3-4-2-5 1z"/>',
    globe: '<circle class="uic-accent" cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="9" fill="none"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
    door: '<path class="uic-accent" d="M6 3h9v18H6z"/><path d="M6 3h9v18H6zM4 21h13M12 12v1"/>',
    stage: '<path class="uic-accent" d="M4 10c0 5 3.6 8 8 8s8-3 8-8z"/><path d="M4 10h16M4 10c0 5 3.6 8 8 8s8-3 8-8M9 6.5c1-1 5-1 6 0"/>',
    shield: '<path class="uic-accent" d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z"/><path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6zM9 12l2 2 4-4"/>',
    device: '<rect class="uic-accent" x="3" y="4" width="18" height="12" rx="2"/><path d="M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM8 20h8M12 16v4"/>',
    system: '<circle class="uic-accent" cx="12" cy="12" r="3"/><path d="M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M19 12l1.5-1-1.4-2.4-1.8.6a6 6 0 0 0-1.2-.7L15.8 6h-3.6l-.5 1.8a6 6 0 0 0-1.2.7l-1.8-.6L7.3 10 8.8 11a6 6 0 0 0 0 1.4L7.3 14l1.4 2.4 1.8-.6c.4.3.8.5 1.2.7l.5 1.8h3.6l.5-1.8c.4-.2.8-.4 1.2-.7l1.8.6L21 14z"/>',

    // --- settings pages / topics (§5-§15) --- //
    location: '<path class="uic-accent" d="M12 21c4-4 7-7 7-11a7 7 0 1 0-14 0c0 4 3 7 7 11z"/><path d="M12 21c4-4 7-7 7-11a7 7 0 1 0-14 0c0 4 3 7 7 11z"/><circle cx="12" cy="10" r="2.5" fill="none"/>',
    clock: '<circle class="uic-accent" cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="9" fill="none"/><path d="M12 7v5l3 2"/>',
    clockdoor: '<circle class="uic-accent" cx="9" cy="12" r="7"/><circle cx="9" cy="12" r="7" fill="none"/><path d="M9 8v4l2.5 1.5M16 5h4v14h-4"/>',
    people: '<circle class="uic-accent" cx="9" cy="8" r="3"/><path d="M9 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6M3 20a6 6 0 0 1 12 0M16 6a3 3 0 0 1 0 6M15 15c3 0 6 2 6 5"/>',
    gauge: '<path class="uic-accent" d="M4 15a8 8 0 0 1 16 0z"/><path d="M4 15a8 8 0 0 1 16 0M12 15l4-4"/>',
    percent: '<path class="uic-accent" d="M6 3h12v18H6z"/><path d="M6 3h12v18H6zM9 8h4M15 8h.01M9 8l6 8M15 16h.01"/>',
    coins: '<ellipse class="uic-accent" cx="9" cy="7" rx="6" ry="3"/><path d="M3 7c0-1.7 2.7-3 6-3s6 1.3 6 3-2.7 3-6 3-6-1.3-6-3zM3 7v5c0 1.7 2.7 3 6 3M15 11.5c3 .3 6 1.5 6 3.5 0 1.7-2.7 3-6 3s-6-1.3-6-3v-3"/>',
    exchange: '<path class="uic-accent" d="M3 8h14l-3-3M21 16H7l3 3"/><path d="M3 8h14M14 5l3 3-3 3M21 16H7M10 13l-3 3 3 3"/>',
    tag: '<path class="uic-accent" d="M4 4h8l8 8-8 8-8-8z"/><path d="M12 4H4v8l8 8 8-8zM8 8h.01"/>',
    wallet: '<path class="uic-accent" d="M4 7h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4z"/><path d="M4 7v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2H5a1 1 0 0 1 0-2h11M17 13h.01"/>',
    qr: '<path class="uic-accent" d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4z"/><path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h2v2h-2M18 14h2v2M14 18h2M18 18h2v2h-2"/>',
    calendar: '<rect class="uic-accent" x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM8 3v4M16 3v4"/>',
    coupon: '<path class="uic-accent" d="M3 8a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1 0 4 2 2 0 0 1-2 2H5a2 2 0 0 1-2-2 2 2 0 0 0 0-4 2 2 0 0 1 0-4z"/><path d="M3 8a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1 0 4 2 2 0 0 1-2 2H5a2 2 0 0 1-2-2 2 2 0 0 0 0-4 2 2 0 0 1 0-4zM9 9l6 6M9.5 9.5h.01M14.5 14.5h.01"/>',
    star: '<path class="uic-accent" d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.2l1-5.8L3.5 9.2l5.9-.9z"/>',
    handshake: '<path class="uic-accent" d="M3 8l4-2 5 3 5-3 4 2v8l-4 2-5-3-5 3-4-2z"/><path d="M12 9l-2.5 2.5a1.5 1.5 0 0 0 2 2L13 12l2 2a1.4 1.4 0 0 0 2-2M7 6L3 8v8l4 2M17 6l4 2v8l-4 2"/>',
    envelope: '<rect class="uic-accent" x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM4 6l8 6 8-6"/>',
    bell: '<path class="uic-accent" d="M6 16V10a6 6 0 0 1 12 0v6l2 2H4z"/><path d="M6 16V10a6 6 0 0 1 12 0v6l2 2H4zM10 20a2 2 0 0 0 4 0"/>',
    document: '<path class="uic-accent" d="M6 3h8l4 4v14H6z"/><path d="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6"/>',
    seat: '<path class="uic-accent" d="M6 11h10a2 2 0 0 1 2 2v5H6z"/><path d="M6 4v14M6 11h10a2 2 0 0 1 2 2v5M6 18h12"/>',
    seatmap: '<rect class="uic-accent" x="4" y="4" width="16" height="16" rx="2"/><path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM9 8h2M13 8h2M9 12h2M13 12h2M9 16h2M13 16h2"/>',
    staff: '<circle class="uic-accent" cx="12" cy="8" r="3.5"/><path d="M12 4.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7M5 20a7 7 0 0 1 14 0"/>',
    role: '<circle class="uic-accent" cx="12" cy="8" r="3.5"/><path d="M12 4.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7M5 20a7 7 0 0 1 14 0M16 4l2 1-2 1"/>',
    key: '<circle class="uic-accent" cx="8" cy="12" r="4"/><path d="M8 8a4 4 0 1 0 0 8 4 4 0 0 0 3.9-3H16v2h2v-2h3v-3h-9.1A4 4 0 0 0 8 8z"/>',
    lock: '<rect class="uic-accent" x="5" y="11" width="14" height="9" rx="2"/><path d="M5 13a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM8 11V8a4 4 0 0 1 8 0v3M12 15v2"/>',
    audit: '<path class="uic-accent" d="M6 3h9l3 3v15H6z"/><path d="M6 3h9l3 3v15H6zM9 11l1.5 1.5L13 10M9 16h5"/>',
    printer: '<path class="uic-accent" d="M6 13h12v6H6z"/><path d="M6 9V4h12v5M4 9h16a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-2M6 17H4a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1M6 13h12v6H6zM17 12h.01"/>',
    kiosk: '<rect class="uic-accent" x="6" y="3" width="12" height="14" rx="1"/><path d="M6 4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1zM9 20h6M12 17v3M9 7h6"/>',
    pos: '<rect class="uic-accent" x="4" y="8" width="16" height="12" rx="1"/><path d="M4 9a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM7 4h10l1 4H6zM8 12h3M8 16h8"/>',
    scanner: '<path class="uic-accent" d="M4 4h5M20 4h-5M4 20h5M20 20h-5"/><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3M3 12h18"/>',
    numbering: '<path class="uic-accent" d="M4 4h16v16H4z"/><path d="M4 4h16v16H4zM8 8v8M8 8L6.5 9M13 8h3v3l-3 1v1h3"/>',
    plug: '<path class="uic-accent" d="M8 10h8v3a4 4 0 0 1-8 0z"/><path d="M9 3v4M15 3v4M8 7h8v6a4 4 0 0 1-8 0zM12 17v4"/>',
    webhook: '<circle class="uic-accent" cx="12" cy="7" r="3"/><path d="M12 4a3 3 0 0 0-1.5 5.6L8 15a3 3 0 1 0 2 1M14 10l2 4M16 14a3 3 0 1 1-2 5h-4"/>',
    sliders: '<path class="uic-accent" d="M4 8h16M4 16h16"/><path d="M4 8h16M4 16h16M9 6v4M15 14v4"/>',
    integration: '<path class="uic-accent" d="M6 6h5v5H6zM13 13h5v5h-5z"/><path d="M6 6h5v5H6zM13 13h5v5h-5zM11 8h3v3"/>',

    // --- reports (§16-§30) --- //
    dashboard: '<rect class="uic-accent" x="3" y="3" width="8" height="8" rx="1"/><path d="M3 3h8v8H3zM13 3h8v5h-8zM13 12h8v9h-8zM3 15h8v6H3z"/>',
    revenue: '<path class="uic-accent" d="M4 20V4h2v16z"/><path d="M4 20h16M6 16l4-5 3 3 5-7M18 7h-3M18 7v3"/>',
    channels: '<circle class="uic-accent" cx="6" cy="6" r="2.5"/><path d="M6 3.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5M18 8l-9 3M18 16l-9-3M18 4a2 2 0 1 1 0 4 2 2 0 0 1 0-4M18 14a2 2 0 1 1 0 4 2 2 0 0 1 0-4"/>',
    partner: '<path class="uic-accent" d="M3 8l4-2 5 3 5-3 4 2v8l-4 2-5-3-5 3-4-2z"/><path d="M12 9l-2.5 2.5a1.5 1.5 0 0 0 2 2L13 12l2 2a1.4 1.4 0 0 0 2-2M7 6L3 8v8l4 2M17 6l4 2v8l-4 2"/>',
    alert: '<path class="uic-accent" d="M12 3l10 18H2z"/><path d="M12 3l10 18H2zM12 9v5M12 17v.5"/>',
    activity: '<path class="uic-accent" d="M3 12h4l2-6 4 12 2-6h6"/><path d="M3 12h4l2-6 4 12 2-6h6"/>',
    finance: '<rect class="uic-accent" x="4" y="4" width="16" height="16" rx="2"/><path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM8 8h4M8 12h8M8 16h8M15 7v3"/>',
    refund: '<path class="uic-accent" d="M4 8a8 8 0 1 1-1 4"/><path d="M4 12a8 8 0 1 1 2 5M4 6v4h4M12 8v4l3 2"/>',
    booking: '<rect class="uic-accent" x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM8 3v4M16 3v4M9 14l2 2 4-4"/>',
    visitor: '<circle class="uic-accent" cx="12" cy="8" r="3.5"/><path d="M12 4.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7M5 20a7 7 0 0 1 14 0"/>',
    box: '<path class="uic-accent" d="M12 3l8 4v10l-8 4-8-4V7z"/><path d="M12 3l8 4v10l-8 4-8-4V7zM4 7l8 4 8-4M12 11v10"/>',

    // --- fallbacks / states --- //
    report: '<rect class="uic-accent" x="4" y="4" width="16" height="16" rx="2"/><path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM8 15v-3M12 15V9M16 15v-5"/>',
    info: '<circle class="uic-accent" cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="9" fill="none"/><path d="M12 11v5M12 8v.5"/>',
    check: '<circle class="uic-accent" cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="9" fill="none"/><path d="M8 12l3 3 5-6"/>'
  };

  // Settings pages → icon slug. Keyed by the internal page key (which is language-
  // free), so the mapping is stable across translations (§42). Categories reuse the
  // same tile slugs the back office already names in data-icon.
  var SETTINGS_ICON = {
    // categories
    'cat:business': 'business', 'cat:booking_ticketing': 'ticket', 'cat:pricing_tax': 'receipt',
    'cat:payment': 'card', 'cat:promotions': 'gift', 'cat:customer_experience': 'globe',
    'cat:access_control': 'door', 'cat:shows_seating': 'stage', 'cat:staff_security': 'shield',
    'cat:devices': 'device', 'cat:system': 'system',
    // business
    'Organization': 'business', 'Brand': 'tag', 'Venues': 'location', 'Areas': 'location',
    'Operating Hours': 'clock', 'Last Admission': 'clockdoor', 'Time Zone Settings': 'globe',
    // booking & ticketing
    'Ticket Types': 'ticket', 'Customer Segments': 'people', 'Booking Rules': 'calendar',
    'Advance Booking': 'calendar', 'Capacity': 'gauge', 'Time Slots': 'clock',
    'Ticket Validity Settings': 'ticket', 'QR Access Rules': 'qr',
    // pricing & tax
    'Currency Settings': 'coins', 'Exchange Rates': 'exchange', 'VAT Settings': 'percent',
    'Service Charge Settings': 'receipt', 'Rounding': 'system', 'Price Display': 'tag',
    // payment
    'Payment Type': 'wallet', 'Payment Providers': 'card',
    // promotions
    'Promotions': 'gift', 'Coupon Codes': 'coupon', 'Cash Coupons': 'coupon',
    'Member Rewards': 'star', 'Partner Benefits': 'handshake',
    // customer experience
    'Languages': 'globe', 'Email Templates': 'envelope', 'Ticket Templates': 'ticket',
    'Customer Notifications': 'bell', 'Terms & Conditions': 'document',
    // access control
    'Gates': 'door', 'Access Points': 'door', 'Re-entry Rules': 'refund', 'Scanner Configuration': 'scanner',
    // shows & seating
    'Shows': 'stage', 'Show Schedule': 'calendar', 'Seat Type': 'seat', 'Seat Zone': 'seatmap',
    'Seat Layout': 'seatmap', 'Seat Reservation Rules': 'seat',
    // staff & security
    'Staff': 'staff', 'Roles': 'role', 'Permissions': 'key', 'Login Security': 'lock',
    'Audit Logs': 'audit',
    // devices
    'Kiosks': 'kiosk', 'POS Devices': 'pos', 'Printers': 'printer', 'Gate Devices': 'scanner',
    'Devices': 'device',
    // system
    'Numbering': 'numbering', 'Integrations': 'integration', 'API Configuration': 'plug',
    'Webhooks': 'webhook', 'Advanced Configuration': 'sliders',
    // insight pages that appear in nav
    'Dashboard': 'dashboard', 'Operations Dashboard': 'activity', 'Reports': 'report'
  };

  // Report catalog key → icon slug (§16-§30). Prefixes cover families of report
  // keys ("revenue_daily", "revenue_by_channel", …) without listing each.
  var REPORT_ICON_EXACT = {
    executive: 'dashboard', executive_overview: 'dashboard', operations: 'activity',
    today: 'calendar', revenue: 'revenue', visitors: 'visitor', channels: 'channels',
    products: 'box', capacity: 'gauge', promotions: 'gift', partners: 'partner',
    exceptions: 'alert', bookings: 'booking', admissions: 'door', counter_sales: 'pos',
    shifts: 'clock', payments: 'wallet', refunds: 'refund', 'refund_void': 'refund',
    shows: 'stage', seats: 'seat', devices: 'device', sales: 'revenue',
    reconciliation: 'exchange', payment_reconciliation: 'exchange', tax: 'receipt',
    tax_invoices: 'document', discounts: 'percent', exchange_rate: 'exchange',
    exchange_rates: 'exchange',
    // KPI keys used by the dashboards
    gross_sales: 'coins', net_sales: 'wallet', tickets: 'ticket', atv: 'receipt',
    rpv: 'visitor', refunded: 'refund', expected: 'calendar', no_show: 'alert',
    cancelled: 'alert', checked_in: 'check', walk_ins: 'people', online: 'globe'
  };

  var REPORT_ICON_PREFIX = [
    ['revenue', 'revenue'], ['sales', 'revenue'], ['visitor', 'visitor'],
    ['channel', 'channels'], ['product', 'box'], ['capacity', 'gauge'],
    ['promotion', 'gift'], ['partner', 'partner'], ['exception', 'alert'],
    ['booking', 'booking'], ['admission', 'door'], ['gate', 'door'],
    ['counter', 'pos'], ['shift', 'clock'], ['payment', 'wallet'],
    ['refund', 'refund'], ['void', 'refund'], ['show', 'stage'], ['seat', 'seat'],
    ['device', 'device'], ['tax', 'receipt'], ['invoice', 'document'],
    ['discount', 'percent'], ['exchange', 'exchange'], ['reconcil', 'exchange']
  ];

  function ensureSprite() {
    if (document.getElementById('utp-icon-sprite')) return;
    var symbols = Object.keys(SYMBOLS).map(function (name) {
      return '<symbol id="ic-' + name + '" viewBox="0 0 24 24">' + SYMBOLS[name] + '</symbol>';
    }).join('');
    var host = document.createElement('div');
    host.setAttribute('aria-hidden', 'true');
    // Zero-footprint but not display:none — Safari/WebKit stops rendering <use>
    // references into a display:none sprite, so it is clipped out of layout instead.
    host.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
    host.innerHTML = '<svg id="utp-icon-sprite" xmlns="http://www.w3.org/2000/svg">' + symbols + '</svg>';
    document.body.insertBefore(host, document.body.firstChild);
  }

  function has(slug) { return Object.prototype.hasOwnProperty.call(SYMBOLS, slug); }

  function resolveSettings(pageOrCategory) {
    return SETTINGS_ICON[pageOrCategory] || 'system';
  }

  function resolveReport(key) {
    if (REPORT_ICON_EXACT[key]) return REPORT_ICON_EXACT[key];
    var k = String(key || '').toLowerCase();
    for (var i = 0; i < REPORT_ICON_PREFIX.length; i++) {
      if (k.indexOf(REPORT_ICON_PREFIX[i][0]) !== -1) return REPORT_ICON_PREFIX[i][1];
    }
    return 'report';
  }

  // Return the inline-SVG markup for a symbol. `slug` is an icon name; callers that
  // hold a page key or report key resolve it first via the helpers below. The result
  // is decorative by default (aria-hidden) because §38 requires a text label beside
  // every icon — the label is the accessible name, not the glyph.
  function markup(slug, options) {
    var opts = options || {};
    var name = has(slug) ? slug : 'system';
    var size = opts.size || 20;
    var cls = 'uic' + (opts.tone ? ' uic-' + opts.tone : '') + (opts.className ? ' ' + opts.className : '');
    var label = opts.label;
    var a11y = label ? ' role="img" aria-label="' + String(label).replace(/"/g, '&quot;') + '"'
      : ' aria-hidden="true" focusable="false"';
    return '<svg class="' + cls + '" width="' + size + '" height="' + size + '"'
      + ' viewBox="0 0 24 24"' + a11y + '><use href="#ic-' + name + '"></use></svg>';
  }

  ensureSprite();
  // If icons.js loads before <body> exists in some environment, inject on ready.
  if (!document.body) {
    document.addEventListener('DOMContentLoaded', ensureSprite);
  }

  window.utpIcons = {
    markup: markup,
    settings: function (pageOrCategory, options) { return markup(resolveSettings(pageOrCategory), options); },
    categoryIcon: function (categoryKey, options) { return markup(resolveSettings('cat:' + categoryKey), options); },
    report: function (key, options) { return markup(resolveReport(key), options); },
    slugForSettings: resolveSettings,
    slugForCategory: function (c) { return resolveSettings('cat:' + c); },
    slugForReport: resolveReport,
    has: has
  };
})();
