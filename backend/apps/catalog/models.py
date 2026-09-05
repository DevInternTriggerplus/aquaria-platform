"""What the venue sells: segments, experiences, products and ticket types.

Nothing here names a venue type. "Adult", "Child" and "Senior" are
``CustomerSegment`` rows for Aquaria Phuket, not enum members (R4.2), and a
mermaid show is an ``Experience`` whose ``kind`` is ``SHOW`` (R18.2).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TenantScopedModel

STATUS_CHOICES = [
    ("ACTIVE", "Active"),
    ("INACTIVE", "Inactive"),
    ("SCHEDULED", "Scheduled for future availability"),
    ("ARCHIVED", "Archived"),
]

#: Channels a product or ticket type may be sold through.
CHANNELS = ["ONLINE", "KIOSK", "COUNTER", "PARTNER", "STAFF", "API"]


class CustomerSegment(TenantScopedModel):
    """A configurable buyer class (R4.1).

    Qualification rules are data, and they surface both to the customer before
    purchase and to gate staff at validation (R4.4, R4.5).
    """

    id_prefix = "seg"

    code = models.SlugField(max_length=40)
    name = models.JSONField(default=dict, help_text='Translatable, e.g. {"en": "Adult"}')
    description = models.JSONField(default=dict, blank=True)
    display_order = models.IntegerField(default=0)
    #: e.g. {"age_min": 12, "age_max": null, "height_min_cm": 141, "proof": "Thai ID"}
    qualification = models.JSONField(default=dict, blank=True)
    proof_required_at_entry = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_segment_code_per_tenant"),
        ]
        ordering = ["display_order", "code"]

    def __str__(self) -> str:
        return self.name.get("en", self.code) if isinstance(self.name, dict) else str(self.name)


class Experience(TenantScopedModel):
    """A sellable or schedulable thing a guest does.

    General Admission, a mermaid show, a yoga class and a parade are all
    Experiences differing only by ``kind`` and configuration (R18.3).
    """

    id_prefix = "exp"

    KIND_CHOICES = [
        ("ADMISSION", "Admission"),
        ("SHOW", "Show or performance"),
        ("CLASS", "Class or workshop"),
        ("TOUR", "Guided tour"),
        ("RESOURCE", "Bookable resource"),
        ("EVENT", "Special event"),
    ]

    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="experiences")
    area = models.ForeignKey(
        "tenancy.Area", null=True, blank=True, on_delete=models.PROTECT, related_name="experiences"
    )
    code = models.SlugField(max_length=60)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default="ADMISSION")
    name = models.JSONField(default=dict)
    short_name = models.CharField(max_length=80, blank=True)
    description = models.JSONField(default=dict, blank=True)
    customer_instructions = models.JSONField(default=dict, blank=True)
    cancellation_message = models.JSONField(default=dict, blank=True)
    cover_image_url = models.URLField(blank=True)
    icon = models.CharField(max_length=60, blank=True)
    category = models.CharField(max_length=60, blank=True, db_index=True)
    default_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    recommended_audience = models.CharField(max_length=120, blank=True)
    languages = models.JSONField(default=list, blank=True)
    display_priority = models.IntegerField(default=0)
    customer_visible = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "code"], name="uniq_experience_code_per_venue"
            ),
        ]
        ordering = ["display_priority", "code"]


class Product(TenantScopedModel):
    """A commercial offer built from one or more experiences (R3).

    ``session_requirement`` is per product and independent of every other product
    (R3.3), which is what lets one venue sell timed entry and open-dated
    admission side by side.
    """

    id_prefix = "prd"

    SESSION_REQUIREMENT_CHOICES = [
        ("NOT_USED", "Sessions not used"),
        ("OPTIONAL", "Session optional"),
        ("REQUIRED", "Session required"),
    ]
    SEAT_REQUIREMENT_CHOICES = [
        ("NOT_USED", "No seat assignment"),
        ("OPTIONAL", "Seat optional"),
        ("REQUIRED", "Seat required"),
    ]

    venue = models.ForeignKey("tenancy.Venue", on_delete=models.PROTECT, related_name="products")
    experience = models.ForeignKey(
        Experience, null=True, blank=True, on_delete=models.PROTECT, related_name="products"
    )
    code = models.SlugField(max_length=60)
    name = models.JSONField(default=dict)
    description = models.JSONField(default=dict, blank=True)
    admission_model = models.CharField(
        max_length=30,
        default="GENERAL_ADMISSION",
        help_text="Configurable archetype, e.g. GENERAL_ADMISSION, TIMED_ENTRY, DAY_PASS (R3.1).",
    )
    session_requirement = models.CharField(
        max_length=10, choices=SESSION_REQUIREMENT_CHOICES, default="NOT_USED"
    )
    seat_requirement = models.CharField(
        max_length=10, choices=SEAT_REQUIREMENT_CHOICES, default="NOT_USED"
    )
    channels = models.JSONField(
        default=list, blank=True, help_text="Empty means every channel."
    )
    min_per_booking = models.PositiveSmallIntegerField(default=1)
    max_per_booking = models.PositiveSmallIntegerField(default=10)
    display_order = models.IntegerField(default=0)
    customer_visible = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")
    available_from = models.DateField(null=True, blank=True)
    available_until = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["venue", "code"], name="uniq_product_code_per_venue"),
        ]
        ordering = ["display_order", "code"]

    def __str__(self) -> str:
        return self.name.get("en", self.code) if isinstance(self.name, dict) else str(self.name)

    def allows_channel(self, channel: str) -> bool:
        return not self.channels or channel in self.channels


class ProductRelation(TenantScopedModel):
    """Bundle children and add-ons attached to a parent product (R3.4)."""

    id_prefix = "prl"

    RELATION_CHOICES = [("BUNDLE_ITEM", "Bundle item"), ("ADDON", "Add-on")]

    parent = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="relations")
    child = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="parent_relations")
    relation = models.CharField(max_length=12, choices=RELATION_CHOICES)
    min_quantity = models.PositiveSmallIntegerField(default=0)
    max_quantity = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "child", "relation"], name="uniq_product_relation"
            ),
        ]

    def clean(self) -> None:
        if self.parent_id and self.parent_id == self.child_id:
            raise ValidationError({"child": "A product cannot contain itself."})


class TicketType(TenantScopedModel):
    """A priced variant of a product for one customer segment (R3.5)."""

    id_prefix = "tkt"

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="ticket_types")
    segment = models.ForeignKey(
        CustomerSegment, on_delete=models.PROTECT, related_name="ticket_types"
    )
    code = models.SlugField(max_length=60)
    name = models.JSONField(default=dict)
    description = models.JSONField(default=dict, blank=True)
    admission_model = models.CharField(max_length=30, blank=True)
    channels = models.JSONField(default=list, blank=True)
    min_quantity = models.PositiveSmallIntegerField(default=0)
    max_quantity = models.PositiveSmallIntegerField(null=True, blank=True)
    entry_allowance = models.PositiveSmallIntegerField(
        default=1, help_text="0 means unlimited entries."
    )
    reentry_allowed = models.BooleanField(default=False)
    #: Surfaced to the customer before purchase and to gate staff at scan (R3.6).
    eligibility = models.JSONField(default=dict, blank=True)
    transferable = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)
    customer_visible = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uniq_ticket_type_code_per_tenant"
            ),
        ]
        ordering = ["display_order", "code"]

    def __str__(self) -> str:
        return self.name.get("en", self.code) if isinstance(self.name, dict) else str(self.name)

    def allows_channel(self, channel: str) -> bool:
        if self.channels:
            return channel in self.channels
        return self.product.allows_channel(channel)
