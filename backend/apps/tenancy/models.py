"""Tenant → Organization → Brand → Venue → Area, plus access points and devices.

Names here are deliberately generic (R1.6): there is no aquarium-specific model,
field or enum anywhere in the platform. Aquaria Phuket is a ``Venue`` row whose
``venue_type`` happens to be ``AQUARIUM``, and every behaviour that makes it an
aquarium is configuration data.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import BaseModel, TenantScopedModel

STATUS_CHOICES = [
    ("ACTIVE", "Active"),
    ("INACTIVE", "Inactive"),
    ("ARCHIVED", "Archived"),
]

TAX_MODEL_CHOICES = [
    ("INCLUSIVE", "Tax included in the displayed price"),
    ("EXCLUSIVE", "Tax added to the displayed price"),
]

ROUNDING_CHOICES = [
    ("NONE", "No rounding"),
    ("NEAREST_1", "Nearest 1"),
    ("NEAREST_5", "Nearest 5"),
    ("NEAREST_10", "Nearest 10"),
    ("UP_1", "Always up to 1"),
    ("DOWN_1", "Always down to 1"),
]


def validate_iana_timezone(value: str) -> None:
    """Require a real IANA zone, never a bare UTC offset (settings spec §8).

    ``Asia/Bangkok`` carries the rules; ``UTC+07:00`` does not, and a venue that
    ever observes DST would silently drift.
    """
    if not value or "/" not in value:
        raise ValidationError(
            "Use an IANA time zone identifier such as Asia/Bangkok, not a UTC offset."
        )
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"Unknown time zone {value!r}.") from exc


class Tenant(BaseModel):
    """Top-level isolation boundary. All data belongs to exactly one tenant."""

    id_prefix = "ten"

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")
    #: Tenant-wide defaults, resolved by nearest-scope precedence (R1.7).
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Organization(TenantScopedModel):
    """A business entity that owns venues, tax identity and staff."""

    id_prefix = "org"

    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=160)
    legal_name = models.CharField(max_length=200, blank=True)
    tax_id = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_org_code_per_tenant"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Brand(TenantScopedModel):
    """Optional layer between organization and venue (R1.3)."""

    id_prefix = "brd"

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="brands")
    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_brand_code_per_tenant"),
        ]


class Venue(TenantScopedModel):
    """A physical or logical operating site.

    ``venue_type`` supplies configuration defaults and terminology. It is a plain
    string so a new archetype is a data change, not a migration (R1.5).
    """

    id_prefix = "ven"

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="venues")
    brand = models.ForeignKey(
        Brand, null=True, blank=True, on_delete=models.PROTECT, related_name="venues"
    )
    code = models.SlugField(max_length=40)
    name = models.JSONField(
        default=dict, help_text='Translatable: {"en": "Aquaria Phuket", "th": "..."}'
    )
    short_name = models.CharField(max_length=60, blank=True)
    venue_type = models.CharField(max_length=40, default="GENERIC")

    #: IANA identifier. Every operating date, cutoff, session time, report
    #: boundary and ticket expiry is evaluated in this zone (R1.9).
    timezone = models.CharField(
        max_length=64, default="Asia/Bangkok", validators=[validate_iana_timezone]
    )
    currency = models.CharField(max_length=3, default="THB")

    #: Legacy/fallback tax fields. The authoritative, effective-dated values live
    #: in ``venuesettings``; these remain as the venue-level default.
    tax_model = models.CharField(max_length=10, choices=TAX_MODEL_CHOICES, default="INCLUSIVE")
    tax_rate_bp = models.PositiveIntegerField(
        default=0, help_text="Basis points. Thai VAT of 7% is 700."
    )
    rounding_mode = models.CharField(max_length=12, choices=ROUNDING_CHOICES, default="NONE")

    address = models.TextField(blank=True)
    contact = models.JSONField(default=dict, blank=True)
    logo_url = models.URLField(blank=True)
    #: {"default": {"open": "10:30", "close": "19:00", "last_admission": "18:00"}, ...}
    operating_hours = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")
    customer_visible = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_venue_code_per_tenant"),
        ]
        ordering = ["code"]

    def __str__(self) -> str:
        return self.display_name()

    def display_name(self, language: str = "en") -> str:
        if isinstance(self.name, dict):
            return self.name.get(language) or self.name.get("en") or self.code
        return str(self.name or self.code)

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Area(TenantScopedModel):
    """Arbitrary-depth subdivision of a venue: zone, tank, stage, room, court."""

    id_prefix = "are"

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="areas")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    code = models.SlugField(max_length=40)
    name = models.JSONField(default=dict)
    description = models.JSONField(default=dict, blank=True)
    image_url = models.URLField(blank=True)
    icon = models.CharField(max_length=60, blank=True)
    floor = models.CharField(max_length=30, blank=True)
    map_ref = models.CharField(max_length=120, blank=True)
    directions = models.JSONField(default=dict, blank=True)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "code"], name="uniq_area_code_per_venue"
            ),
        ]
        ordering = ["display_order", "code"]

    def clean(self) -> None:
        if self.parent and self.parent.venue_id != self.venue_id:
            raise ValidationError({"parent": "The parent area belongs to a different venue."})


class AccessPoint(TenantScopedModel):
    """A gate, turnstile, door or scan station (R2.6)."""

    id_prefix = "acp"

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="access_points")
    area = models.ForeignKey(
        Area, null=True, blank=True, on_delete=models.PROTECT, related_name="access_points"
    )
    code = models.SlugField(max_length=40)
    name = models.JSONField(default=dict)
    direction = models.CharField(
        max_length=10,
        choices=[("IN", "Entry"), ("OUT", "Exit"), ("BOTH", "Both")],
        default="IN",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "code"], name="uniq_access_point_code_per_venue"
            ),
        ]


class Device(TenantScopedModel):
    """A registered kiosk, POS terminal, scanner or printer.

    The device identity is itself an access control: an unregistered or
    deactivated scanner is refused, and revocation is immediate (R32.12, R73.12).
    """

    id_prefix = "dev"

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="devices")
    access_point = models.ForeignKey(
        AccessPoint, null=True, blank=True, on_delete=models.PROTECT, related_name="devices"
    )
    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=120)
    kind = models.CharField(
        max_length=16,
        choices=[
            ("KIOSK", "Self-service kiosk"),
            ("POS", "Counter POS"),
            ("SCANNER", "Gate scanner"),
            ("PRINTER", "Printer"),
        ],
    )
    channel = models.CharField(max_length=12, default="ONLINE")
    #: Hash of the device credential. The credential itself is never stored.
    credential_hash = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_device_code_per_tenant"),
        ]

    @property
    def is_usable(self) -> bool:
        return self.status == "ACTIVE"
