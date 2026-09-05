"""Staff, roles and scoped role assignments.

Three rules from the spec shape this module:

* **Default deny** — a new role holds nothing until a permission is granted, and
  anything not explicitly granted is denied (R44.1).
* **VIEW/ADD/EDIT/DELETE are fully independent** — granting ADD never implies
  EDIT, and none of them implies VIEW. Permissions are stored and enforced
  literally as configured (R40.4).
* **Staff with history are never hard-deleted** — a DELETE maps to deactivation
  so audit attribution survives (R38.6, R38.7).
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from apps.core.models import BaseModel, ProtectedModel, TenantScopedModel
from apps.core.ids import new_id

from .permissions import ACTIONS_BY_KEY, ALL_VERBS, PAGES_BY_KEY

STAFF_STATUS_CHOICES = [
    ("INVITED", "Invited"),
    ("ACTIVE", "Active"),
    ("SUSPENDED", "Suspended"),
    ("INACTIVE", "Inactive"),
]


class StaffManager(BaseUserManager):
    def create_user(self, email: str, password: str | None = None, **extra):
        if not email:
            raise ValueError("Staff require an email address.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra):
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_platform_admin", True)
        extra.setdefault("status", "ACTIVE")
        if not extra.get("is_superuser"):
            raise ValueError("A superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra)


class Staff(AbstractBaseUser, PermissionsMixin, ProtectedModel):
    """A staff principal.

    Not called ``User`` because the platform also has customers, who are a
    different thing entirely and never authenticate into the back office.
    """

    id = models.CharField(primary_key=True, max_length=40, editable=False)
    tenant = models.ForeignKey(
        "tenancy.Tenant", on_delete=models.PROTECT, related_name="staff", null=True, blank=True
    )
    organization = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="staff"
    )

    email = models.EmailField(max_length=254)
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    display_name = models.CharField(max_length=160, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    employee_ref = models.CharField(max_length=60, blank=True)

    status = models.CharField(max_length=12, choices=STAFF_STATUS_CHOICES, default="INVITED")
    is_platform_admin = models.BooleanField(
        default=False, help_text="Platform Super Admin. Requires MFA (R73.2)."
    )
    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=64, blank=True)

    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_channel = models.CharField(max_length=20, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Django admin plumbing. Back-office authority comes from RoleAssignment, not
    # from these flags.
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = StaffManager()

    class Meta:
        constraints = [
            # Email is unique within a tenant, not globally: two tenants may
            # legitimately employ the same person (R38.8).
            models.UniqueConstraint(fields=["tenant", "email"], name="uniq_staff_email_per_tenant"),
        ]
        ordering = ["email"]

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = new_id("stf")
        if self.email:
            self.email = self.email.lower()
        if not self.display_name:
            self.display_name = f"{self.first_name} {self.last_name}".strip() or self.email
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.display_name or self.email

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        """Django checks this on login. Suspension takes effect immediately (R38.5)."""
        return self.status == "ACTIVE"

    def deactivate(self) -> None:
        """The real meaning of DELETE for a staff record with history (R38.7)."""
        self.status = "INACTIVE"
        self.save(update_fields=["status", "updated_at"])


class Role(TenantScopedModel):
    """A named permission set.

    Default templates are seeded as ordinary rows; no permission is bound
    exclusively to a built-in role (R39.2).
    """

    id_prefix = "rol"

    code = models.SlugField(max_length=60)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    #: Guards escalation: a principal may not assign a role above their own level
    #: (R44.4). Platform Super Admin is 100.
    authority_level = models.PositiveSmallIntegerField(default=10)
    is_template = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12,
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")],
        default="ACTIVE",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_role_code_per_tenant"),
        ]
        ordering = ["-authority_level", "name"]

    def __str__(self) -> str:
        return self.name

    def grant_page(self, page: str, *verbs: str) -> None:
        """Grant page verbs. Each verb is stored independently (R40.4)."""
        if page not in PAGES_BY_KEY:
            raise ValueError(f"Unknown page {page!r}")
        for verb in verbs:
            if verb not in ALL_VERBS:
                raise ValueError(f"Unknown verb {verb!r}")
            RolePermission.objects.update_or_create(
                role=self, page=page, verb=verb, defaults={"granted": True}
            )

    def grant_action(self, action: str) -> None:
        """``action`` is the bare name, e.g. ``REFUND``."""
        key = action if action.startswith("ACTION:") else f"ACTION:{action}"
        if key not in ACTIONS_BY_KEY:
            raise ValueError(f"Unknown action permission {action!r}")
        RoleActionPermission.objects.update_or_create(
            role=self, action=key, defaults={"granted": True}
        )

    def has_page(self, page: str, verb: str) -> bool:
        return RolePermission.objects.filter(
            role=self, page=page, verb=verb, granted=True
        ).exists()

    def has_action(self, action: str) -> bool:
        key = action if action.startswith("ACTION:") else f"ACTION:{action}"
        return RoleActionPermission.objects.filter(
            role=self, action=key, granted=True
        ).exists()


class RolePermission(BaseModel):
    """One page verb for one role. Absence means denied."""

    id_prefix = "rpm"

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="page_permissions")
    page = models.CharField(max_length=60)
    verb = models.CharField(max_length=8, choices=[(v, v) for v in ALL_VERBS])
    granted = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "page", "verb"], name="uniq_role_page_verb"
            ),
        ]
        indexes = [models.Index(fields=["role", "page"])]


class RoleActionPermission(BaseModel):
    """One action permission for one role, independent of any CRUD grant (R41.3)."""

    id_prefix = "ram"

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="action_permissions")
    action = models.CharField(max_length=60)
    granted = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "action"], name="uniq_role_action"),
        ]


class RoleAssignment(TenantScopedModel):
    """(staff, role, scope) — the same person may hold different roles at
    different venues simultaneously (R43.1, R43.2)."""

    id_prefix = "ras"

    SCOPE_CHOICES = [
        ("TENANT", "Whole tenant"),
        ("ORGANIZATION", "One organization"),
        ("VENUE", "One venue"),
        ("OPERATING_POINT", "A counter or gate"),
    ]

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="assignments")
    scope_type = models.CharField(max_length=20, choices=SCOPE_CHOICES, default="VENUE")
    #: Null only for TENANT scope.
    scope_id = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "role", "scope_type", "scope_id"],
                name="uniq_role_assignment",
            ),
        ]

    def covers_venue(self, venue_id: str, organization_id: str | None = None) -> bool:
        """Whether this assignment's scope includes the request's target venue."""
        if self.scope_type == "TENANT":
            return True
        if self.scope_type == "ORGANIZATION":
            return bool(organization_id) and self.scope_id == organization_id
        return self.scope_id == venue_id
