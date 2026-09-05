"""Temporary development check. Deleted once the real suite exists."""

from __future__ import annotations

import sys
import threading

sys.path.insert(0, ".")

from utp.app import Platform
from utp.core.clock import FixedClock
from utp.core.errors import JustSoldOut, HoldExpired, ConflictError, NotAvailable, RuleViolation
from utp.core.money import to_minor

clock = FixedClock("2026-09-01T03:00:00Z")  # 10:00 Bangkok
p = Platform(clock=clock)
tenant = p.tenancy.create_tenant(code="aquaria", name="Aquawalk Thailand", default_language="th",
                                 languages=["th", "en", "zh", "ru"])
tid = tenant["id"]
ctx = p.system_context(tid)
org = p.tenancy.create_organization(ctx, code="AQW-TH", name="Aquawalk (Thailand) Co., Ltd.")
p.tenancy.create_venue_type(None, code="AQUARIUM", name="Aquarium", platform_level=True,
                            template={"tax_model": "INCLUSIVE", "tax_rate_bp": 700,
                                      "operating_hours": {"default": {"open": "10:30", "close": "19:00"}}})
venue = p.tenancy.create_venue(ctx, organization_id=org["id"], venue_type_code="AQUARIUM", code="AQP",
                               short_code="AQP", name={"en": "Aquaria Phuket"}, timezone="Asia/Bangkok",
                               currency="THB")
vid = venue["id"]
vctx = ctx.for_venue(vid)

for i, code in enumerate(["ADULT", "CHILD", "SENIOR"]):
    p.catalog.create_segment(vctx, code=code, name={"en": code.title()}, display_order=i)

exp = p.catalog.create_experience(vctx, venue_id=vid, code="GA", name={"en": "General Admission"})
prod = p.catalog.create_product(vctx, venue_id=vid, code="GA-DAY", name={"en": "General Admission"},
                                admission_model="GENERAL_ADMISSION", experience_id=exp["id"])
tt_adult = p.catalog.create_ticket_type(vctx, product_id=prod["id"], segment_code="ADULT",
                                        code="GA-ADULT-INTL", name={"en": "Adult (International)"})

# ---- pricing -------------------------------------------------------------- #
p.pricing.create_price_rule(vctx, ticket_type_id=tt_adult["id"], amount_minor=to_minor(1390), currency="THB",
                            code="WALKUP", priority=0)
p.pricing.create_price_rule(vctx, ticket_type_id=tt_adult["id"], amount_minor=to_minor(1251), currency="THB",
                            code="ONLINE", priority=10, channel="ONLINE")
from utp.services.pricing import PriceRequest

online = p.pricing.resolve(vctx, PriceRequest(tt_adult["id"], "2026-09-10", "ONLINE"))
counter = p.pricing.resolve(vctx, PriceRequest(tt_adult["id"], "2026-09-10", "COUNTER"))
print("online price", online.unit_price_minor, online.price_rule_code, "tax", online.tax.tax_minor)
print("counter price", counter.unit_price_minor, counter.price_rule_code)
try:
    p.pricing.resolve(vctx, PriceRequest(tt_adult["id"], "2026-09-10", "ONLINE", currency="MYR"))
except NotAvailable as e:
    print("no-fallback ->", e.code, e.details["reason"])

# ---- booking rules & calendar -------------------------------------------- #
p.calendar.set_booking_rules(vctx, scope_type="VENUE", scope_id=vid,
                             settings={"max_days_in_advance": 90, "max_capacity": 2400})
p.calendar.set_booking_rules(vctx, scope_type="PRODUCT", scope_id=prod["id"], channel="ONLINE",
                             settings={"cutoff_time": "15:00", "same_day_enabled": True})
p.calendar.set_calendar_entry(vctx, venue_id=vid, date="2026-09-15", kind="CLOSED", note="Annual maintenance")
p.calendar.set_calendar_entry(vctx, venue_id=vid, date="2026-09-20", kind="BLACKOUT", note="Private event")

cal = p.calendar.calendar(vctx, venue=venue, date_from="2026-09-01", date_to="2026-09-05",
                          channel="ONLINE", product_id=prod["id"])
print("cells", [(c["date"], c["state"], c["icon"]) for c in cal["cells"]])
ev = p.calendar.evaluate_date(vctx, venue=venue, date="2026-09-15", channel="ONLINE", product_id=prod["id"])
print("closed ->", ev.state, ev.reason)
ev = p.calendar.evaluate_date(vctx, venue=venue, date="2027-06-01", channel="ONLINE", product_id=prod["id"])
print("beyond window ->", ev.state, ev.on_sale_from)
try:
    p.calendar.assert_bookable(vctx, venue=venue, date="2026-09-20", channel="ONLINE", product_id=prod["id"])
except RuleViolation as e:
    print("blackout rejected ->", e.code, "alternatives", e.details["nearest_available_dates"])

# ---- sessions + holds + no-oversell -------------------------------------- #
mermaid = p.catalog.create_experience(vctx, venue_id=vid, code="MERMAID", name={"en": "Mermaid Show"},
                                      kind="SHOW", default_duration_minutes=20, reservation_mode="REQUIRED")
show_prod = p.catalog.create_product(vctx, venue_id=vid, code="MERMAID-RES", name={"en": "Mermaid Show Seat"},
                                     admission_model="SESSION_BOOKING", experience_id=mermaid["id"])
tt_show = p.catalog.create_ticket_type(vctx, product_id=show_prod["id"], segment_code="ADULT",
                                       code="MERMAID-ADULT", name={"en": "Mermaid Adult"})
p.pricing.create_price_rule(vctx, ticket_type_id=tt_show["id"], amount_minor=0, currency="THB", code="INCLUDED")

sess = p.inventory.create_session(vctx, venue_id=vid, date="2026-09-10", start_time="14:00",
                                  kind="SHOW", experience_id=mermaid["id"], product_id=show_prod["id"],
                                  duration_minutes=20, capacity=3, check_in_required=True)
print("session", sess["start_time"], sess["end_time"], sess["capacity"], sess["remaining"])

h1 = p.inventory.acquire_hold(vctx.with_channel("ONLINE"), session_id=sess["id"], quantity=2, cart_id="cart1")
print("hold1", h1.quantity, p.inventory.availability(vctx, sess["id"]).as_dict())
h2 = p.inventory.acquire_hold(vctx.with_channel("KIOSK"), session_id=sess["id"], quantity=1, cart_id="cart2")
print("after hold2 remaining", p.inventory.availability(vctx, sess["id"]).remaining, "status",
      p.inventory.get_session(vctx, sess["id"])["status"])
try:
    p.inventory.acquire_hold(vctx.with_channel("ONLINE"), session_id=sess["id"], quantity=1, cart_id="cart3")
except JustSoldOut as e:
    print("third hold ->", e.code, e.details["remaining"])

p.inventory.confirm_hold(vctx.with_channel("ONLINE"), h1.id)
print("after confirm", p.inventory.availability(vctx, sess["id"]).as_dict())

# wrong channel cannot confirm someone else's hold
try:
    p.inventory.confirm_hold(vctx.with_channel("COUNTER"), h2.id)
except ConflictError as e:
    print("cross-channel confirm ->", e.code)

# hold expiry then reclaim
clock.advance(minutes=11)
print("reclaimed", p.inventory.reclaim_expired_holds(vctx))
print("after reclaim", p.inventory.availability(vctx, sess["id"]).as_dict())
try:
    p.inventory.confirm_hold(vctx.with_channel("KIOSK"), h2.id)
except HoldExpired as e:
    print("expired confirm ->", e.code)
# late confirmation succeeds because capacity is available again (R10.9)
late = p.inventory.confirm_hold(vctx.with_channel("KIOSK"), h2.id, allow_late=True)
print("late confirm ->", late)

# ---- concurrency: 12 threads race for the last 2 places ------------------ #
sess2 = p.inventory.create_session(vctx, venue_id=vid, date="2026-09-11", start_time="14:00",
                                   kind="SHOW", experience_id=mermaid["id"], product_id=show_prod["id"],
                                   duration_minutes=20, capacity=2)
results: list[str] = []
lock = threading.Lock()


def race(n: int) -> None:
    c = p.system_context(tid).for_venue(vid).with_channel("ONLINE")
    try:
        hold = p.inventory.acquire_hold(c, session_id=sess2["id"], quantity=1, cart_id=f"race{n}")
        p.inventory.confirm_hold(c, hold.id)
        outcome = "won"
    except JustSoldOut:
        outcome = "sold_out"
    except Exception as exc:  # noqa: BLE001
        outcome = f"error:{type(exc).__name__}:{exc}"
    with lock:
        results.append(outcome)


threads = [threading.Thread(target=race, args=(i,)) for i in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
final = p.inventory.availability(vctx, sess2["id"])
print("race results", {r: results.count(r) for r in set(results)})
print("final confirmed", final.confirmed, "capacity", final.capacity, "remaining", final.remaining)
assert final.confirmed <= 2, "OVERSOLD"
assert results.count("won") == 2, f"expected exactly 2 winners, got {results.count('won')}"

# ---- allocations --------------------------------------------------------- #
sess3 = p.inventory.create_session(vctx, venue_id=vid, date="2026-09-12", start_time="14:00", kind="SHOW",
                                   experience_id=mermaid["id"], product_id=show_prod["id"], capacity=10)
p.inventory.create_allocation(vctx, session_id=sess3["id"], alloc_type="CHANNEL", alloc_key="ONLINE", quantity=2)
a = p.inventory.acquire_hold(vctx.with_channel("ONLINE"), session_id=sess3["id"], quantity=2, cart_id="ac1")
p.inventory.confirm_hold(vctx.with_channel("ONLINE"), a.id)
try:
    p.inventory.acquire_hold(vctx.with_channel("ONLINE"), session_id=sess3["id"], quantity=1, cart_id="ac2")
except JustSoldOut as e:
    print("allocation exhausted ->", e.details["reason"], "shared remaining",
          p.inventory.availability(vctx, sess3["id"]).remaining)
b = p.inventory.acquire_hold(vctx.with_channel("COUNTER"), session_id=sess3["id"], quantity=1, cart_id="ac3")
print("counter still sells ->", b.quantity)
print("utilization", p.inventory.allocation_utilization(vctx, sess3["id"]))

# ---- capacity reduction guard (R8.9) ------------------------------------- #
try:
    p.inventory.set_capacity(vctx, sess3["id"], 1)
except ConflictError as e:
    print("capacity below confirmed ->", e.code, e.details)
p.inventory.set_capacity(vctx, sess3["id"], 1, override=True, reason="Stage rebuild")
print("override capacity ->", p.inventory.get_session(vctx, sess3["id"])["capacity"],
      "confirmed", p.inventory.get_session(vctx, sess3["id"])["confirmed"])

print("\nALL CHECKS PASSED")

# =========================================================================== #
# Promotions (R13) + PDPA consent (R12)
# =========================================================================== #
from utp.core.errors import ConsentRequired, ConfirmationRequired, ValidationError
from utp.domain.cart import CartLine, CartSnapshot

tt_child = p.catalog.create_ticket_type(vctx, product_id=prod["id"], segment_code="CHILD",
                                        code="GA-CHILD-INTL", name={"en": "Child (International)"})
p.pricing.create_price_rule(vctx, ticket_type_id=tt_child["id"], amount_minor=to_minor(750), currency="THB",
                            code="CHILD-WALKUP")
p.pricing.create_price_rule(vctx, ticket_type_id=tt_child["id"], amount_minor=to_minor(675), currency="THB",
                            code="CHILD-ONLINE", priority=10, channel="ONLINE")


def build_cart(adult=2, child=2, codes=(), payment_method=None):
    lines = []
    idx = 0
    if adult:
        lines.append(CartLine(index=idx, product_id=prod["id"], product_code="GA-DAY",
                              ticket_type_id=tt_adult["id"], ticket_type_code="GA-ADULT-INTL",
                              segment_id=tt_adult["segment_id"], segment_code="ADULT",
                              quantity=adult, unit_price_minor=to_minor(1251), currency="THB",
                              tax_rate_bp=700))
        idx += 1
    if child:
        lines.append(CartLine(index=idx, product_id=prod["id"], product_code="GA-DAY",
                              ticket_type_id=tt_child["id"], ticket_type_code="GA-CHILD-INTL",
                              segment_id=tt_child["segment_id"], segment_code="CHILD",
                              quantity=child, unit_price_minor=to_minor(675), currency="THB",
                              tax_rate_bp=700))
    return CartSnapshot(venue_id=vid, organization_id=org["id"], currency="THB", channel="ONLINE",
                        visit_date="2026-09-10", lines=lines, cart_id="cartP",
                        payment_method=payment_method, customer_key="cust-1",
                        tax_model="INCLUSIVE", tax_rate_bp=700)


p.promotions.create_promotion(vctx, internal_code="FAMILY-4", name={"en": "Family Package (2+2)"},
                              mechanic="FAMILY_PACKAGE",
                              config={"package_price_minor": to_minor(3580),
                                      "requires": [{"segment_code": "ADULT", "quantity": 2},
                                                   {"segment_code": "CHILD", "quantity": 2}]},
                              priority=50)
p.promotions.create_promotion(vctx, internal_code="EARLY10", name={"en": "Early Bird 10%"},
                              mechanic="EARLY_BIRD", config={"percent_bp": 1000},
                              rules={"days_before_visit_min": 7}, priority=20, stackable=True)
p.promotions.create_promotion(vctx, internal_code="WELCOME5", name={"en": "Welcome THB 100"},
                              mechanic="VOUCHER", config={"amount_minor": to_minor(100)},
                              code="WELCOME100", priority=10, stackable=True, usage_limit=1,
                              rules={"requires_code": True})

cart = build_cart()
outcome = p.promotions.evaluate(vctx, cart)
print("promo best ->", [(a["internal_code"], a["amount_minor"]) for a in outcome.applied],
      "discount", outcome.discount_minor, "gross", cart.gross_minor)
outcome2 = p.promotions.evaluate(vctx, build_cart(), codes=["WELCOME100"])
print("with code ->", [(a["internal_code"], a["amount_minor"]) for a in outcome2.applied],
      "total", outcome2.as_dict()["total_minor"])
bad = p.promotions.evaluate(vctx, build_cart(), codes=["NOPE"])
print("bad code ->", [r.as_dict() for r in bad.rejected])

# deterministic: same cart, repeated evaluation gives the identical combination
keys = {p.promotions.evaluate(vctx, build_cart()).combination_key for _ in range(8)}
assert len(keys) == 1, f"non-deterministic promotion selection: {keys}"
print("deterministic combination ->", keys.pop())

# Usage cap: two carts evaluated BEFORE either commits, so both believe the
# single-use voucher is available. Only one may actually redeem it (R13.6).
race_a = p.promotions.evaluate(vctx, build_cart(), codes=["WELCOME100"])
race_b = p.promotions.evaluate(vctx, build_cart(), codes=["WELCOME100"])
assert any(a["internal_code"] == "WELCOME5" for a in race_a.applied)
assert any(a["internal_code"] == "WELCOME5" for a in race_b.applied)
fail_a = p.promotions.commit_redemptions(vctx, booking_id="bk-1", outcome=race_a)
fail_b = p.promotions.commit_redemptions(vctx, booking_id="bk-2", outcome=race_b)
print("first commit failures ->", [f["reason_code"] for f in fail_a])
print("second commit blocked ->", [f["reason_code"] for f in fail_b])
assert not fail_a and [f["reason_code"] for f in fail_b] == ["usage_limit"], (fail_a, fail_b)
# once exhausted, the code is refused with a specific reason instead of being offered
after = p.promotions.evaluate(vctx, build_cart(), codes=["WELCOME100"])
print("exhausted code ->", [r.as_dict()["reason_code"] for r in after.rejected])
print("usage report ->", p.promotions.usage_report(vctx, p.db.query_one(
    "SELECT id FROM promotions WHERE internal_code='WELCOME5'")["id"]))
print("restore ->", p.promotions.restore_redemptions(vctx, booking_id="bk-1"))

# ---- PDPA ---------------------------------------------------------------- #
p.consent.publish_notice(vctx, version="2026.1", consent_text_version="ct-2026.1", language="en",
                         controller={"name": "Aquawalk (Thailand) Co., Ltd.", "contact": "privacy@aquaria.test",
                                     "address": "B1 Central Phuket Floresta"},
                         purposes=[{"code": "BOOKING_SERVICE", "description": "Deliver the booking"}],
                         retention={"bookings_years": 10, "consent_years": 10},
                         recipients=[{"name": "Payment provider", "role": "processor"},
                                     {"name": "Email provider", "role": "processor"}],
                         cross_border={"transfers": True, "countries": ["SG"], "safeguard": "SCC"},
                         rights=["access", "rectification", "erasure", "restriction", "portability",
                                 "objection", "withdraw"],
                         dpo_contact="dpo@aquaria.test", notice_url="https://aquaria.test/privacy")
dialog = p.consent.dialog(vctx, language="en", channel="ONLINE")
print("dialog items ->", [(i["code"], i["required"], i["granted"]) for i in dialog["items"]])
assert all(i["granted"] is False for i in dialog["items"]), "consent must never be pre-ticked"
assert dialog["submit_enabled"] is False and dialog["separate_from_terms"] is True

from utp.services.consent import ConsentCapture
try:
    p.consent.capture(vctx, ConsentCapture(items={"MARKETING": True}, notice_version="2026.1",
                                           consent_text_version="ct-2026.1", language="en",
                                           contact="guest@example.com"))
except ConsentRequired as e:
    print("required consent missing ->", e.code, e.details["missing_required_items"],
          "retained:", e.details["personal_data_retained"])

rec = p.consent.capture(vctx, ConsentCapture(items={"BOOKING_SERVICE": True, "MARKETING": False,
                                                    "ANALYTICS": True},
                                             notice_version="2026.1", consent_text_version="ct-2026.1",
                                             language="en", contact="guest@example.com"),
                        venue_id=vid, venue_timezone="Asia/Bangkok")
print("consent recorded ->", rec["id"][:12], rec["items"], "local", rec["created_at_local"])

cust = p.customers.upsert(vctx, consent_record_id=rec["id"], email="guest@example.com",
                          full_name="Somchai Jaidee", phone="+66811234567", language="th")
print("customer ->", cust["id"][:12], "email(masked for system? no VIEW_PII check for SYSTEM)", cust.get("email"))

# masked read without VIEW_PII
gate_role = p.staff.seed_role_templates(ctx, codes=["GATE_STAFF"])
gs = p.staff.invite_staff(ctx, email="gate@aquaria.test", first_name="Gate", last_name="One",
                          organization_id=org["id"])
p.staff.complete_enrolment(ctx, staff_id=gs["id"], token=gs["enrolment_token"], credential="Gate-Pass-2026")
p.staff.assign_role(ctx, staff_id=gs["id"], role_id=gate_role["GATE_STAFF"], scope_type="VENUE", scope_id=vid)
from utp.core.context import RequestContext, Principal
gctx = RequestContext(tenant_id=tid, principal=Principal(kind="STAFF", id=gs["id"]), channel="GATE", venue_id=vid)
masked = p.customers.get(gctx, cust["id"])
print("masked ->", masked.get("email"), masked.get("full_name"), masked.get("phone"))
assert "•••" in str(masked.get("email")), "PII must be masked without VIEW_PII"

# withdrawal requires informed confirmation, does not cancel booking
try:
    p.consent.withdraw(vctx, contact="guest@example.com", item_code="ANALYTICS")
except ConfirmationRequired as e:
    print("withdrawal needs confirmation ->", e.details["effective_within_days"], "days;",
          "booking valid:", e.details["booking_remains_valid"])
w = p.consent.withdraw(vctx, contact="guest@example.com", item_code="ANALYTICS", confirmed=True)
print("withdrawn ->", w["item_code"], "state now", p.consent.effective_state(vctx, "guest@example.com"))
try:
    p.consent.withdraw(vctx, contact="guest@example.com", item_code="BOOKING_SERVICE", confirmed=True)
except ValidationError as e:
    print("required consent not separately withdrawable ->", e.code)

# re-consent after version bump
p.consent.publish_notice(vctx, version="2026.2", consent_text_version="ct-2026.2", language="en",
                         controller={"name": "Aquawalk (Thailand) Co., Ltd.", "contact": "privacy@aquaria.test"},
                         purposes=[], retention={}, recipients=[], rights=[],
                         dpo_contact="dpo@aquaria.test", notice_url="https://aquaria.test/privacy")
print("needs reconsent ->", p.consent.needs_reconsent(vctx, "guest@example.com"))

# partner attestation required
partner_ctx = vctx.with_channel("PARTNER")
try:
    p.consent.capture(partner_ctx, ConsentCapture(items={"BOOKING_SERVICE": True}, notice_version="2026.2",
                                                  consent_text_version="ct-2026.2", language="en",
                                                  contact="hotelguest@example.com"), venue_id=vid)
except ValidationError as e:
    print("partner attestation ->", e.code)

# DSAR + erasure keeping financial records
dsar = p.consent.record_dsar(vctx, kind="ERASURE", contact="guest@example.com", customer_id=cust["id"])
print("dsar due ->", dsar["due_at"])
anon = p.customers.anonymize(vctx, cust["id"], reason="erasure_request")
print("anonymized ->", anon["retained_records"], anon["retention_justification"][:40])
p.consent.complete_dsar(vctx, dsar["id"], outcome="ANONYMIZED", justification="Financial records retained")

# breach
inc = p.consent.record_breach(vctx, scope="Email export misdirected", data_categories=["email", "name"],
                             affected_count=12)
print("breach due (72h) ->", inc["due_at"])

# consent record immutability
try:
    p.db.execute("UPDATE consent_records SET items_json='{}' WHERE id = ?", (rec["id"],))
except Exception as e:
    print("consent immutable ->", type(e).__name__, str(e)[:50])

print("\nPROMOTIONS + PDPA CHECKS PASSED")
