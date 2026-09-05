"""Base models shared by every app.

Two invariants live here rather than being restated in each model:

1. **Tenant scoping.** Every record belongs to exactly one tenant, and queries
   are expected to go through :meth:`TenantScopedQuerySet.for_tenant`. A missing
   tenant filter is a bug, so the manager makes the scoped path the easy one.
2. **Protected records are never physically deleted.** Financial, ticketing,
   consent and audit rows refuse ``delete()`` at the model layer, so no view,
   management command, admin action or bulk operation can erase history. DELETE
   authority means "remove from active use" — cancel, void, archive, deactivate.
"""

from __future__ import annotations

from django.db import models

from .ids import new_id


class SoftDeleteNotAllowed(Exception):
    """Raised when code tries to physically delete a protected record."""


class TenantScopedQuerySet(models.QuerySet):
    """Queryset that makes the tenant-filtered path the obvious one."""

    def for_tenant(self, tenant_id: str) -> "TenantScopedQuerySet":
        return self.filter(tenant_id=tenant_id)

    def active(self) -> "TenantScopedQuerySet":
        return self.filter(status="ACTIVE")


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):  # type: ignore[misc]
    pass


def _prefixed_id(prefix: str):
    """Default callable producing a sortable, unguessable primary key."""

    def _make() -> str:
        return new_id(prefix)

    return _make


class BaseModel(models.Model):
    """Timestamps and a string primary key.

    String ids carry a short type prefix (``bkg_``, ``tck_``) which makes logs
    and support conversations unambiguous, and they are unguessable so an id in a
    URL is not an enumeration vector.
    """

    id = models.CharField(primary_key=True, max_length=40, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: Overridden by each concrete model, e.g. ``"bkg"``.
    id_prefix: str = "rec"

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = new_id(self.id_prefix)
        return super().save(*args, **kwargs)


class TenantScopedModel(BaseModel):
    """A record owned by exactly one tenant (R1.1)."""

    tenant = models.ForeignKey(
        "tenancy.Tenant", on_delete=models.PROTECT, related_name="+", db_index=True
    )

    objects = TenantScopedManager()

    class Meta:
        abstract = True


class ProtectedModel(models.Model):
    """Refuses physical deletion (R46.1).

    Deleting is blocked at the lowest layer available in the ORM, so an admin
    action, a management command or a careless ``queryset.delete()`` all fail the
    same way. Callers must use the domain transition instead — cancel, void,
    refund, archive or deactivate.
    """

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):  # noqa: D102 - see class docstring
        raise SoftDeleteNotAllowed(
            f"{type(self).__name__} is retained for audit and cannot be deleted. "
            "Use the domain action (cancel, void, refund, archive or deactivate)."
        )


class ProtectedQuerySet(TenantScopedQuerySet):
    """Queryset counterpart to :class:`ProtectedModel`."""

    def delete(self):  # noqa: D102
        raise SoftDeleteNotAllowed(
            "These records are retained for audit and cannot be bulk deleted."
        )


class ProtectedManager(models.Manager.from_queryset(ProtectedQuerySet)):  # type: ignore[misc]
    pass


class AuditEvent(TenantScopedModel, ProtectedModel):
    """Append-only record of who changed what, when and from where (R45).

    Rows are never updated or deleted. ``previous_value``/``new_value`` hold safe
    structured context only: never a secret, card number, password or unmasked
    sensitive personal data (R45.9).
    """

    id_prefix = "aud"

    actor = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events"
    )
    actor_role = models.CharField(max_length=120, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=60, blank=True, db_index=True)
    target_id = models.CharField(max_length=40, blank=True, db_index=True)
    previous_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    organization = models.ForeignKey(
        "tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    venue = models.ForeignKey(
        "tenancy.Venue", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    channel = models.CharField(max_length=20, blank=True)
    device = models.CharField(max_length=40, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    correlation_id = models.CharField(max_length=40, blank=True, db_index=True)
    occurred_at_utc = models.DateTimeField(db_index=True)
    occurred_at_local = models.CharField(max_length=40, blank=True)

    objects = ProtectedManager()

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "action", "occurred_at_utc"]),
            models.Index(fields=["tenant", "target_type", "target_id"]),
        ]
        ordering = ["-occurred_at_utc"]

    def save(self, *args, **kwargs):
        # Append-only: an existing row may never be rewritten.
        if not self._state.adding:
            raise SoftDeleteNotAllowed("Audit events are append-only and cannot be modified.")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"
