"""Provision Aquaria Phuket as configuration data.

Nothing in this file is aquarium-specific *code* — it is a sequence of ordinary
configuration calls against the generic platform (R1.6, R77.3). The same script shape
provisions a water park, a theatre or a gym by changing the values.

Figures come from the research report: THB1,251 online adult / THB621 Thai-resident
adult, operating hours 10:30–19:00 with last admission 18:00, and the nine zones.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from utp.app import Platform
from utp.core.clock import to_iso
from utp.core.ids import hash_secret, new_id
from utp.core.money import to_minor

TENANT_CODE = "aquaria"
VENUE_CODE = "AQP"

#: The shared demo credential. Every seeded account signs in with this.
DEMO_CREDENTIAL = "Aquaria-Demo-2026"

#: The durable owner/administrator account. It is reconciled on every startup so it can
#: always sign in with full access, even on an already-provisioned database (this is
#: what makes "I need to access it anytime" true rather than a one-time seed).
OWNER_EMAIL = "nisachol.la@triggersplus.com"

#: The nine zones described by the venue's current public material.
ZONES: tuple[tuple[str, str, str], ...] = (
    ("MYSTIC-FOREST", "Mystic Forest", "Freshwater and mythical forest environment"),
    ("CANOPY-WALK", "Canopy Walk", "Freshwater ecosystem walkthrough"),
    ("JEWELS-JUNGLE", "Jewels of the Jungle", "Reptiles and non-aquatic species"),
    ("RIVER-CAVES", "River Caves", "Small-clawed otters"),
    ("COASTAL-HAVEN", "Coastal Haven", "Penguins and sharks"),
    ("STINGRAY-BAY", "Stingray Bay", "Corals and stingrays"),
    ("LARGATO", "Largato", "Sunken ship narrative"),
    ("SOUTH-CHINA-SEA", "South China Sea", "Main marine exhibit, over 3.5 million litres"),
    ("STATION-AQUARIUS", "Station Aquarius", "Touch pool and jellyfish"),
)

#: (code, English, Thai, qualification)
SEGMENTS: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    # (code, display-name-per-language, qualification). The English form uses the
    # count-agnostic wording the venue asked for — Adult(s) / Child(ren) / Senior(s)
    # — and every supported language is provided so the label translates.
    (
        "ADULT",
        {"en": "Adult(s)", "th": "ผู้ใหญ่", "zh": "成人", "ja": "大人", "ru": "Взрослый(-е)"},
        {"height_min_cm": 141},
    ),
    (
        "CHILD",
        {"en": "Child(ren)", "th": "เด็ก", "zh": "儿童", "ja": "子ども", "ru": "Ребёнок (дети)"},
        {"height_min_cm": 91, "height_max_cm": 140},
    ),
    (
        "SENIOR",
        {"en": "Senior(s)", "th": "ผู้สูงอายุ", "zh": "长者", "ja": "シニア", "ru": "Пожилой(-е)"},
        {"age_min": 60, "documents": ["Photo ID showing date of birth"]},
    ),
)

#: International and Thai-resident pricing, walk-up and online (research report).
PRICES: dict[str, dict[str, int]] = {
    "ADULT": {"intl_walkup": 1390, "intl_online": 1251, "local_walkup": 690, "local_online": 621},
    "CHILD": {"intl_walkup": 750, "intl_online": 675, "local_walkup": 410, "local_online": 369},
    "SENIOR": {"intl_walkup": 750, "intl_online": 675, "local_walkup": 410, "local_online": 369},
}


def ensure_owner_access(
    platform: Platform,
    *,
    tenant_id: str,
    venue_id: str,
    email: str = OWNER_EMAIL,
    credential: str = DEMO_CREDENTIAL,
    role_code: str = "OWNER",
    first_name: str = "Nisachol",
    last_name: str = "La",
) -> dict[str, Any]:
    """Guarantee an account can sign in with a given role. Idempotent.

    Defaults reconcile the durable owner account; the standalone ``reset_access.py``
    CLI reuses it with other values to restore or reset any account from the terminal.

    Reconciles four things directly against the database, so it works no matter what
    state the account is in (missing, suspended, wrong password, or holding no role):

    1. the OWNER role template exists for the tenant;
    2. the staff row exists, is ACTIVE and has the known password;
    3. an ACTIVE tenant-scoped OWNER role assignment exists;
    4. the permission epoch is bumped so any stale session re-resolves at once.

    Written with plain SQL rather than the staff service because the service layer
    guards against exactly the operations a recovery needs (self-service limits,
    second-approval, "not the last super admin"); recovery must not be blockable by
    them. It is only ever called by seed/CLI with a system context.
    """
    db = platform.db
    ctx = platform.system_context(tenant_id)
    now = to_iso(platform.clock.now())
    email_norm = email.strip().lower()

    # 1. the role template for this tenant (seed_role_templates is idempotent).
    roles = platform.staff.seed_role_templates(ctx, codes=[role_code])
    role_id = roles[role_code]
    # A tenant-wide role covers every venue; a narrower role is scoped to the venue.
    tenant_scoped = role_code in ("PLATFORM_SUPER_ADMIN", "OWNER", "ORGANIZATION_ADMIN")

    # 2. staff row.
    org = db.query_one("SELECT id FROM organizations WHERE tenant_id = ? ORDER BY code LIMIT 1", (tenant_id,))
    org_id = org["id"] if org else None
    staff = db.query_one(
        "SELECT id, status FROM staff WHERE tenant_id = ? AND email = ?", (tenant_id, email_norm)
    )
    if staff is None:
        staff_id = new_id("stf")
        db.insert("staff", {
            "id": staff_id, "tenant_id": tenant_id, "organization_id": org_id,
            "first_name": first_name, "last_name": last_name,
            "display_name": f"{first_name} {last_name}".strip(),
            "email": email_norm, "status": "ACTIVE",
            "credential_hash": hash_secret(credential), "mfa_required": 0,
            "perm_epoch": 1, "created_at": now,
        })
    else:
        staff_id = staff["id"]
        db.execute(
            "UPDATE staff SET status = 'ACTIVE', credential_hash = ?, mfa_required = 0, "
            "failed_logins = 0, locked_until = NULL, perm_epoch = perm_epoch + 1, updated_at = ? "
            "WHERE tenant_id = ? AND id = ?",
            (hash_secret(credential), now, tenant_id, staff_id),
        )

    # 3. an ACTIVE role assignment at the right scope.
    scope_type = "TENANT" if tenant_scoped else "VENUE"
    scope_id = None if tenant_scoped else venue_id
    assignment = db.query_one(
        "SELECT id, status FROM role_assignments WHERE tenant_id = ? AND staff_id = ? AND role_id = ? "
        "AND scope_type = ?",
        (tenant_id, staff_id, role_id, scope_type),
    )
    if assignment is None:
        db.insert("role_assignments", {
            "id": new_id("rsa"), "tenant_id": tenant_id, "staff_id": staff_id,
            "role_id": role_id, "scope_type": scope_type, "scope_id": scope_id,
            "status": "ACTIVE", "created_at": now,
        })
    elif assignment["status"] != "ACTIVE":
        db.execute(
            "UPDATE role_assignments SET status = 'ACTIVE', revoked_at = NULL WHERE tenant_id = ? AND id = ?",
            (tenant_id, assignment["id"]),
        )

    platform.audit.record(
        ctx, "STAFF_EDIT", target_type="staff", target_id=staff_id,
        new={"reconciled": "access", "email": email_norm, "role": role_code},
    )
    return {"staff_id": staff_id, "email": email_norm, "role": role_code}


def provision(platform: Platform, *, today: str | None = None) -> dict[str, Any]:
    """Create the tenant and everything Aquaria Phuket needs to sell."""
    p = platform
    existing = p.db.query_one("SELECT id FROM tenants WHERE code = ?", (TENANT_CODE,))
    if existing is not None:
        ctx = p.system_context(existing["id"])
        venue = p.tenancy.venue_by_code(ctx, VENUE_CODE)
        # Self-heal the owner account on every startup so it can always sign in with
        # full access — even if the database predates the OWNER role, the account lost
        # its assignment, or its password drifted. This is what makes the account
        # durable across restarts rather than a one-shot seed.
        ensure_owner_access(p, tenant_id=existing["id"], venue_id=venue["id"])
        return {"tenant_id": existing["id"], "venue_id": venue["id"], "created": False}

    tenant = p.tenancy.create_tenant(
        code=TENANT_CODE,
        name="Aquawalk (Thailand) Co., Ltd.",
        default_language="en",
        languages=["en", "th", "zh", "ru"],
    )
    tenant_id = tenant["id"]
    ctx = p.system_context(tenant_id)

    organization = p.tenancy.create_organization(
        ctx,
        code="AQW-TH",
        name="Aquawalk Thailand",
        legal_name="Aquawalk (Thailand) Co., Ltd.",
        tax_id="0105558000000",
        address="B1, Central Phuket Floresta, Wichit, Mueang Phuket, Phuket 83000",
        country="TH",
    )

    # The venue type carries the defaults; the venue may override any of them (R1.5).
    p.tenancy.create_venue_type(
        None,
        code="AQUARIUM",
        name="Aquarium",
        platform_level=True,
        terminology={"session": "Show time", "area": "Zone", "experience": "Exhibit"},
        template={
            "tax_model": "INCLUSIVE",
            "tax_rate_bp": 700,
            "rounding_mode": "NEAREST_1",
            "operating_hours": {"default": {"open": "10:30", "close": "19:00", "last_admission": "18:00"}},
            "config": {
                "booking.max_days_in_advance": 90,
                "hold.duration_minutes": 10,
                "calendar.limited_availability_threshold": {"mode": "PERCENT", "value": 20},
                "notification.reminder_offsets_hours": [24],
            },
        },
    )

    venue = p.tenancy.create_venue(
        ctx,
        organization_id=organization["id"],
        venue_type_code="AQUARIUM",
        code=VENUE_CODE,
        short_code="AQP",
        name={"en": "Aquaria Phuket", "th": "อควาเรีย ภูเก็ต"},
        timezone="Asia/Bangkok",
        currency="THB",
        tax_registration="0105558000000",
        address={"line1": "B1, Central Phuket Floresta", "city": "Phuket", "country": "TH"},
        contact={"phone": "+66 76 000 000", "email": "hello@aquaria.test"},
        operating_hours={"default": {"open": "10:30", "close": "19:00", "last_admission": "18:00"}},
    )
    venue_id = venue["id"]
    vctx = ctx.for_venue(venue_id)

    # Configure Thailand's 7% inclusive VAT as an explicit, effective-dated setting
    # rather than relying on the venue-type template's legacy tax_model/tax_rate_bp
    # (settings spec §1, §34). Effective from the venue's operating start so every
    # sale resolves it. The system context bypasses MANAGE_TAX_SETTINGS for seeding.
    p.settings.set_vat(
        ctx,
        venue_id=venue_id,
        enabled=True,
        rate_bp=700,
        mode="INCLUSIVE",
        effective_from="2019-08-24",
        display_name="VAT",
        tax_registration="0105558000000",
        reason="Initial provisioning of Aquaria Phuket",
    )

    areas: dict[str, str] = {}
    for order, (code, english, description) in enumerate(ZONES):
        area = p.tenancy.create_area(
            vctx,
            venue_id=venue_id,
            code=code,
            name={"en": english},
            description={"en": description},
            kind="ZONE",
            floor="B1",
            map_ref=f"map:{code.lower()}",
            display_order=order * 10,
        )
        areas[code] = area["id"]

    gate = p.tenancy.create_access_point(
        vctx, venue_id=venue_id, code="MAIN-GATE", name={"en": "Main Entrance"}, kind="GATE"
    )

    for order, (code, names, qualification) in enumerate(SEGMENTS):
        p.catalog.create_segment(
            vctx,
            code=code,
            name=dict(names),
            qualification=qualification,
            proof_required=code == "SENIOR",
            proof={"en": "Photo ID showing date of birth"} if code == "SENIOR" else {},
            display_order=order * 10,
        )

    # --- general admission ------------------------------------------------ #
    admission = p.catalog.create_experience(
        vctx,
        venue_id=venue_id,
        code="GENERAL-ADMISSION",
        name={"en": "General Admission", "th": "บัตรเข้าชมทั่วไป"},
        description={"en": "Nine zones including the South China Sea main tank."},
        area_id=areas["SOUTH-CHINA-SEA"],
        customer_visible=True,
    )
    product_intl = p.catalog.create_product(
        vctx,
        venue_id=venue_id,
        code="GA-INTL",
        name={"en": "General Admission", "th": "บัตรเข้าชม"},
        description={"en": "Standard admission for visitors."},
        admission_model="GENERAL_ADMISSION",
        experience_id=admission["id"],
        max_per_booking=10,
        display_order=10,
    )
    product_local = p.catalog.create_product(
        vctx,
        venue_id=venue_id,
        code="GA-LOCAL",
        name={"en": "General Admission — Thai resident / expatriate", "th": "บัตรเข้าชม — คนไทย/ผู้พำนักในไทย"},
        description={"en": "Requires proof of Thai residency at the gate."},
        admission_model="GENERAL_ADMISSION",
        experience_id=admission["id"],
        max_per_booking=10,
        display_order=20,
    )

    ticket_types: dict[str, str] = {}
    for product, suffix, walkup_key, online_key, residency in (
        (product_intl, "INTL", "intl_walkup", "intl_online", None),
        (product_local, "LOCAL", "local_walkup", "local_online", "Thai"),
    ):
        for segment_code, segment_names, _qual in SEGMENTS:
            eligibility: dict[str, Any] = {}
            if residency:
                eligibility["residency"] = residency
                eligibility["documents"] = ["Thai ID card, or passport with a valid Thai visa"]
            if segment_code == "SENIOR":
                eligibility.setdefault("documents", []).append("Photo ID showing date of birth")
                eligibility["age_min"] = 60
            code = f"GA-{suffix}-{segment_code}"
            ticket_type = p.catalog.create_ticket_type(
                vctx,
                product_id=product["id"],
                segment_code=segment_code,
                code=code,
                name=dict(segment_names),
                eligibility=eligibility,
                max_quantity=10,
                display_order=0 if segment_code == "ADULT" else 10,
            )
            ticket_types[code] = ticket_type["id"]
            prices = PRICES[segment_code]
            # Walk-up applies everywhere; the online rule has higher priority and
            # narrows to the ONLINE channel (R5.2).
            p.pricing.create_price_rule(
                vctx,
                ticket_type_id=ticket_type["id"],
                amount_minor=to_minor(prices[walkup_key]),
                currency="THB",
                code=f"{code}-WALKUP",
                priority=0,
            )
            p.pricing.create_price_rule(
                vctx,
                ticket_type_id=ticket_type["id"],
                amount_minor=to_minor(prices[online_key]),
                currency="THB",
                code=f"{code}-ONLINE",
                priority=10,
                channel="ONLINE",
            )

    p.calendar.set_booking_rules(
        vctx,
        scope_type="VENUE",
        scope_id=venue_id,
        settings={
            "max_days_in_advance": 90,
            "same_day_enabled": True,
            "available_weekdays": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            "max_per_booking": 20,
        },
    )

    # Payment methods per channel (R14.1). Online and kiosk take card, a
    # PromptPay-style bank transfer and e-wallets (Alipay / WeChat); the counter
    # additionally takes cash, which the cashier reconciles at shift close (R34.5,
    # R34.8). These must cover every method the customer-facing payment types below
    # use, or a guest could pick a type the confirm step then refuses.
    p.config.set(vctx, "payment.methods.ONLINE", ["CARD", "QR_BANK_TRANSFER", "EWALLET"], scope_type="VENUE", scope_id=venue_id)
    p.config.set(vctx, "payment.methods.KIOSK", ["CARD", "QR_BANK_TRANSFER", "EWALLET"], scope_type="VENUE", scope_id=venue_id)
    p.config.set(
        vctx, "payment.methods.COUNTER", ["CASH", "CARD", "QR_BANK_TRANSFER", "EWALLET"], scope_type="VENUE", scope_id=venue_id
    )

    # Customer-facing payment types (update spec §21, §50): PromptPay, Credit Card,
    # Alipay, WeChat Pay. Admin can enable/disable/reorder these in the back office.
    p.payment_types.seed_defaults(vctx, venue_id=venue_id)

    # --- shows ------------------------------------------------------------ #
    base_date = _dt.date.fromisoformat(today) if today else _dt.date.today()
    shows: dict[str, str] = {}
    for code, name, area_code, duration, category, mode, capacity in (
        ("MERMAID", "Mermaid Show", "SOUTH-CHINA-SEA", 20, "PERFORMANCE", "OPTIONAL", 80),
        ("SHARK-FEED", "Shark Feeding", "COASTAL-HAVEN", 15, "FEEDING", "NONE", None),
        ("PENGUIN-FEED", "Penguin Feeding", "COASTAL-HAVEN", 15, "FEEDING", "NONE", None),
        ("OTTER-TALK", "Otter Keeper Talk", "RIVER-CAVES", 15, "EDUCATIONAL", "NONE", None),
    ):
        show = p.catalog.create_experience(
            vctx,
            venue_id=venue_id,
            code=code,
            name={"en": name},
            kind="SHOW",
            description={"en": f"{name} at Aquaria Phuket."},
            area_id=areas[area_code],
            category=category,
            default_duration_minutes=duration,
            reservation_mode=mode,
            display_priority=100 if code == "MERMAID" else 50,
        )
        shows[code] = show["id"]

    # Recurring daily patterns, then published so customers can see them (R21, R31).
    patterns = [
        ("MERMAID", "13:00", 20, 80),
        ("MERMAID", "16:00", 20, 80),
        ("SHARK-FEED", "11:30", 15, None),
        ("PENGUIN-FEED", "14:30", 15, None),
        ("OTTER-TALK", "15:30", 15, None),
    ]
    for code, start_time, duration, capacity in patterns:
        p.shows.create_pattern(
            vctx,
            venue_id=venue_id,
            experience_id=shows[code],
            start_time=start_time,
            duration_minutes=duration,
            valid_from=base_date.isoformat(),
            valid_until=(base_date + _dt.timedelta(days=120)).isoformat(),
            recurrence={"kind": "EVERY_DAY"},
            area_id=None,
            capacity=capacity,
            reservation_mode="OPTIONAL" if capacity else "NONE",
        )
    p.shows.publish(
        vctx,
        venue_id=venue_id,
        date_from=base_date.isoformat(),
        date_to=(base_date + _dt.timedelta(days=120)).isoformat(),
    )

    # --- promotions ------------------------------------------------------- #
    p.promotions.create_promotion(
        vctx,
        internal_code="FAMILY-2A2C",
        name={"en": "Family Package (2 adults + 2 children)"},
        mechanic="FAMILY_PACKAGE",
        config={
            "package_price_minor": to_minor(3580),
            "requires": [
                {"segment_code": "ADULT", "quantity": 2},
                {"segment_code": "CHILD", "quantity": 2},
            ],
        },
        priority=60,
        rules={"channels": ["ONLINE", "KIOSK", "COUNTER"]},
    )
    p.promotions.create_promotion(
        vctx,
        internal_code="EARLY-BIRD-10",
        name={"en": "Early Bird 10% off"},
        mechanic="EARLY_BIRD",
        config={"percent_bp": 1000},
        rules={"days_before_visit_min": 7, "channels": ["ONLINE"]},
        priority=20,
        stackable=True,
    )
    p.promotions.create_promotion(
        vctx,
        internal_code="WELCOME-100",
        name={"en": "Welcome THB 100 off"},
        mechanic="VOUCHER",
        config={"amount_minor": to_minor(100)},
        code="WELCOME100",
        rules={"requires_code": True, "min_purchase_minor": to_minor(1000)},
        priority=10,
        stackable=True,
        usage_limit=500,
    )

    # --- privacy notice (required before any booking, R12.2) --------------- #
    for language in ("en", "th"):
        p.consent.publish_notice(
            vctx,
            version="2026.1",
            consent_text_version="ct-2026.1",
            language=language,
            controller={
                "name": "Aquawalk (Thailand) Co., Ltd.",
                "contact": "privacy@aquaria.test",
                "address": "B1, Central Phuket Floresta, Wichit, Mueang Phuket",
            },
            purposes=[
                {"code": "BOOKING_SERVICE", "description": "Create the booking, issue tickets, take payment"},
                {"code": "MARKETING", "description": "Offers and news, only with consent"},
                {"code": "ANALYTICS", "description": "Service improvement, only with consent"},
            ],
            retention={"bookings_years": 10, "consent_years": 10, "marketing_years": 3},
            recipients=[
                {"name": "Payment provider", "role": "processor"},
                {"name": "Email delivery provider", "role": "processor"},
            ],
            cross_border={
                "transfers": True,
                "countries": ["SG"],
                "safeguard": "Standard contractual clauses",
            },
            rights=[
                "access",
                "rectification",
                "erasure",
                "restriction",
                "portability",
                "objection",
                "withdraw_consent",
            ],
            dpo_contact="dpo@aquaria.test",
            notice_url="https://aquaria.test/privacy",
        )

    # --- notification templates ------------------------------------------- #
    p.notifications.create_template(
        vctx,
        event_type="BOOKING_CONFIRMATION",
        language="en",
        subject="Your Aquaria Phuket tickets — {{booking_number}}",
        header="Your booking is confirmed",
        body=(
            "Hello {{customer_name}},\n\n"
            "Your visit to {{venue_name}} on {{visit_date}} is confirmed.\n\n"
            "Booking number: {{booking_number}}\n"
            "Tickets: {{ticket_count}}\n"
            "Total paid: {{total_amount}}\n\n"
            "{{ticket_lines}}\n\n"
            "Show your QR code at {{entry_location}}.\n"
            "Download your tickets: {{ticket_download_url}}\n"
            "What's on during your visit: {{show_schedule_url}}\n"
            "Manage your booking: {{manage_booking_url}}\n"
        ),
        footer="Aquaria Phuket, B1 Central Phuket Floresta. Opening hours 10:30–19:00.",
        venue_id=venue_id,
    )

    # --- staff ------------------------------------------------------------- #
    roles = p.staff.seed_role_templates(ctx, organization_id=organization["id"])
    demo_staff: dict[str, Any] = {}
    for email, first, last, role_code in (
        ("admin@aquaria.test", "Platform", "Admin", "PLATFORM_SUPER_ADMIN"),
        # Real operator account with full authority that signs in with a password
        # alone (OWNER role — all pages + all actions, not MFA-gated). This is the
        # account to use for full back-office access without an authenticator.
        ("nisachol.la@triggersplus.com", "Nisachol", "La", "OWNER"),
        ("manager@aquaria.test", "Venue", "Manager", "VENUE_MANAGER"),
        ("cashier@aquaria.test", "Counter", "Cashier", "COUNTER_CASHIER"),
        ("gate@aquaria.test", "Gate", "Staff", "GATE_STAFF"),
        # Holds the dashboards and reports but neither VIEW_PII nor VIEW_COST nor
        # EXPORT, so the demo can actually show a report with personal data masked
        # and cost columns absent rather than only describing it.
        ("viewer@aquaria.test", "Report", "Viewer", "REPORT_VIEWER"),
        # Device fleet + integrations authority (MANAGE_INTEGRATION), so the
        # Integrations / API / Webhooks settings pages are demonstrable without the
        # MFA-gated super admin.
        ("tech@aquaria.test", "Tech", "Support", "TECHNICAL_SUPPORT"),
    ):
        invited = p.staff.invite_staff(
            ctx, email=email, first_name=first, last_name=last, organization_id=organization["id"]
        )
        p.staff.complete_enrolment(
            ctx, staff_id=invited["id"], token=invited["enrolment_token"], credential="Aquaria-Demo-2026"
        )
        tenant_scoped = role_code in ("PLATFORM_SUPER_ADMIN", "OWNER")
        p.staff.assign_role(
            ctx,
            staff_id=invited["id"],
            role_id=roles[role_code],
            scope_type="TENANT" if tenant_scoped else "VENUE",
            scope_id=None if tenant_scoped else venue_id,
        )
        demo_staff[role_code] = {"email": email, "staff_id": invited["id"]}

    device = p.tenancy.register_device(
        vctx,
        venue_id=venue_id,
        code="GATE-SCANNER-01",
        name="Main gate scanner",
        kind="SCANNER",
        channel="GATE",
        access_point_id=gate["id"],
    )

    return {
        "tenant_id": tenant_id,
        "organization_id": organization["id"],
        "venue_id": venue_id,
        "access_point_id": gate["id"],
        "device": {"code": device["code"], "secret": device["secret"]},
        "areas": areas,
        "shows": shows,
        "ticket_types": ticket_types,
        "staff": demo_staff,
        "created": True,
    }


if __name__ == "__main__":  # pragma: no cover
    import json

    with Platform() as platform:
        print(json.dumps(provision(platform), indent=2, default=str))
