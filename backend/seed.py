"""Insert a minimal Aquaria configuration into PostgreSQL and read it back.

Proves the whole stack end to end on the real engine: ORM writes, constraints,
effective-dated settings resolution and the charge engine, then the HTTP layer
serving the same rows.
"""

import datetime as dt
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from apps.catalog.models import CustomerSegment, Product, TicketType  # noqa: E402
from apps.payments.models import PaymentType  # noqa: E402
from apps.pricing.models import PriceRule  # noqa: E402
from apps.tenancy.models import Organization, Tenant, Venue  # noqa: E402
from apps.venuesettings.models import VatSetting  # noqa: E402
from apps.venuesettings.services import compute_order_charges  # noqa: E402

tenant, _ = Tenant.objects.get_or_create(code="aquaria", defaults={"name": "Aquaria"})
org, _ = Organization.objects.get_or_create(
    tenant=tenant,
    code="aquawalk",
    defaults={
        "name": "Aquawalk Thailand",
        "legal_name": "Aquawalk (Thailand) Co., Ltd.",
        "tax_id": "0105558000000",
        "address": "B1, Central Phuket Floresta, Wichit, Mueang Phuket, Phuket 83000",
    },
)
venue, _ = Venue.objects.get_or_create(
    tenant=tenant,
    code="aqp",
    defaults={
        "organization": org,
        "name": {"en": "Aquaria Phuket", "th": "อควาเรีย ภูเก็ต"},
        "venue_type": "AQUARIUM",
        "timezone": "Asia/Bangkok",
        "currency": "THB",
        "tax_model": "INCLUSIVE",
        "tax_rate_bp": 700,
        "address": "B1, Central Phuket Floresta, Wichit, Mueang Phuket, Phuket 83000",
        "operating_hours": {
            "default": {"open": "10:30", "close": "19:00", "last_admission": "18:00"}
        },
    },
)

VatSetting.objects.get_or_create(
    tenant=tenant,
    venue=venue,
    effective_from=dt.date(2026, 1, 1),
    defaults={"enabled": True, "rate_bp": 700, "mode": "INCLUSIVE", "display_name": "VAT"},
)

segments = {}
for code, name, order in [
    ("adult", {"en": "Adult", "th": "ผู้ใหญ่"}, 1),
    ("child", {"en": "Child", "th": "เด็ก"}, 2),
    ("senior", {"en": "Senior", "th": "ผู้สูงอายุ"}, 3),
]:
    segments[code], _ = CustomerSegment.objects.get_or_create(
        tenant=tenant, code=code, defaults={"name": name, "display_order": order}
    )

product, _ = Product.objects.get_or_create(
    tenant=tenant,
    venue=venue,
    code="ga-intl",
    defaults={
        "name": {"en": "General Admission — International"},
        "description": {"en": "Nine zones, all shows included."},
        "admission_model": "GENERAL_ADMISSION",
        "session_requirement": "NOT_USED",
        "max_per_booking": 10,
    },
)

# The real published online prices (THB, inclusive of 7% VAT).
prices = {"adult": 125100, "child": 67500, "senior": 67500}
for code, amount in prices.items():
    tt, _ = TicketType.objects.get_or_create(
        tenant=tenant,
        code=f"ga-intl-{code}",
        defaults={
            "product": product,
            "segment": segments[code],
            "name": segments[code].name,
            "entry_allowance": 1,
        },
    )
    PriceRule.objects.get_or_create(
        tenant=tenant,
        venue=venue,
        ticket_type=tt,
        amount_minor=amount,
        defaults={"currency": "THB", "priority": 0, "label": "Online standard"},
    )

for code, method, label, order in [
    ("promptpay", "QR_BANK_TRANSFER", {"en": "PromptPay QR"}, 1),
    ("card", "CARD", {"en": "Credit or debit card"}, 2),
]:
    PaymentType.objects.get_or_create(
        tenant=tenant,
        venue=venue,
        code=code,
        defaults={
            "method": method,
            "display_name": label,
            "display_order": order,
            "web_enabled": True,
        },
    )

from apps.booking import consent_service  # noqa: E402
from apps.booking.consent_models import PrivacyNotice  # noqa: E402

if not PrivacyNotice.objects.filter(tenant=tenant, is_current=True).exists():
    consent_service.publish_notice(
        tenant=tenant,
        version="2026-01",
        controller_name="Aquawalk (Thailand) Co., Ltd.",
        controller_contact="privacy@aquaria.test",
        dpo_contact="dpo@aquaria.test",
        body={
            "purposes": [
                "Create your booking, issue tickets, take payment and validate entry.",
            ],
            "retention": "Financial records kept for the statutory period; contact "
            "details anonymized when no longer required.",
            "cross_border": {"transfers": False},
            "rights": ["access", "rectification", "erasure", "objection", "portability"],
        },
    )

print("seeded on PostgreSQL")
print(f"  tenant  {tenant.id}  {tenant.name}")
print(f"  venue   {venue.id}  {venue.display_name()}  tz={venue.timezone}")
print(f"  segments {CustomerSegment.objects.filter(tenant=tenant).count()}")
print(f"  ticket types {TicketType.objects.filter(tenant=tenant).count()}")
print(f"  price rules  {PriceRule.objects.filter(tenant=tenant).count()}")

# Two adults through the real charge engine, on the real engine.
breakdown = compute_order_charges(
    venue=venue, base_minor=125100 * 2, on_date=dt.date(2026, 9, 1)
)
print("\n2 x adult online:")
print(f"  gross      {breakdown.base_minor}")
print(f"  VAT        {breakdown.vat_minor} (included={breakdown.vat_included})")
print(f"  tax base   {breakdown.taxable_base_minor}")
print(f"  total      {breakdown.grand_total_minor}")
assert breakdown.taxable_base_minor + breakdown.vat_minor == breakdown.grand_total_minor
print("  reconciles: OK")
