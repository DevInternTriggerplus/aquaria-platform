"""Request context.

Every service method takes a :class:`RequestContext`. It carries who is asking,
which tenant and venue they are asking about, through which channel, and the
correlation id that ties the whole logical operation together in the audit log
(R45.7). Passing it explicitly — rather than reading thread-local state — is what
makes the enforcement path auditable and testable: a service cannot accidentally
run without a principal.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .errors import AuthenticationRequired
from .ids import new_correlation_id

PrincipalKind = Literal["STAFF", "CUSTOMER", "PARTNER", "DEVICE", "SYSTEM", "ANONYMOUS"]

Channel = Literal["ONLINE", "KIOSK", "COUNTER", "PARTNER", "STAFF", "API", "GATE", "SYSTEM"]

CUSTOMER_FACING_CHANNELS: frozenset[str] = frozenset({"ONLINE", "KIOSK"})
STAFF_CHANNELS: frozenset[str] = frozenset({"COUNTER", "STAFF", "GATE"})
ALL_CHANNELS: tuple[str, ...] = ("ONLINE", "KIOSK", "COUNTER", "PARTNER", "STAFF", "API", "GATE", "SYSTEM")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor.

    ``authority_level`` is the numeric rank used to make R44.4 enforceable: a
    principal may never assign a role whose level exceeds their own. Higher means
    more authority; Platform Super Admin is 100.
    """

    kind: PrincipalKind
    id: str | None = None
    display_name: str | None = None
    organization_id: str | None = None
    authority_level: int = 0
    perm_epoch: int = 0
    session_id: str | None = None
    partner_id: str | None = None
    device_id: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.kind == "STAFF"

    @property
    def is_system(self) -> bool:
        return self.kind == "SYSTEM"

    def require_staff(self) -> str:
        if self.kind != "STAFF" or not self.id:
            raise AuthenticationRequired()
        return self.id


ANONYMOUS = Principal(kind="ANONYMOUS")
SYSTEM = Principal(kind="SYSTEM", id="system", display_name="System", authority_level=100)


@dataclass(slots=True)
class RequestContext:
    """Scope and identity for one request or one unit of background work."""

    tenant_id: str
    principal: Principal = ANONYMOUS
    channel: str = "ONLINE"
    venue_id: str | None = None
    organization_id: str | None = None
    device_id: str | None = None
    partner_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    language: str = "en"
    correlation_id: str = field(default_factory=new_correlation_id)
    #: Populated by ``authz.effective_permissions`` and reused within a request.
    _permission_cache: dict[str, Any] | None = field(default=None, repr=False)
    #: Set when a caller has explicitly confirmed a sensitive action (R67).
    confirmations: frozenset[str] = frozenset()

    # ------------------------------------------------------------------ #

    @property
    def actor_id(self) -> str | None:
        return self.principal.id

    @property
    def is_staff_channel(self) -> bool:
        return self.channel in STAFF_CHANNELS

    def for_venue(self, venue_id: str | None) -> RequestContext:
        """Copy scoped to a venue, resetting the permission cache.

        Permissions are the union of assignments whose scope includes the target
        venue (R43.3), so the cache must not survive a venue change.
        """
        if venue_id == self.venue_id:
            return self
        clone = replace(self, venue_id=venue_id)
        clone._permission_cache = None
        return clone

    def with_principal(self, principal: Principal) -> RequestContext:
        clone = replace(self, principal=principal)
        clone._permission_cache = None
        return clone

    def with_channel(self, channel: str) -> RequestContext:
        return replace(self, channel=channel)

    def with_confirmation(self, *tokens: str) -> RequestContext:
        return replace(self, confirmations=self.confirmations | frozenset(tokens))

    def system(self) -> RequestContext:
        """Elevated context for scheduled/background work.

        Background work initiated by a staff action stays attributed to that
        principal (R42.11); this helper is only for platform-initiated jobs such
        as hold reclamation and session completion.
        """
        clone = replace(self, principal=SYSTEM, channel="SYSTEM")
        clone._permission_cache = None
        return clone

    def audit_fields(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "venue_id": self.venue_id,
            "actor_id": self.principal.id,
            "channel": self.channel,
            "device_id": self.device_id,
            "ip_address": self.ip_address,
            "correlation_id": self.correlation_id,
        }


def system_context(tenant_id: str, *, venue_id: str | None = None, correlation_id: str | None = None) -> RequestContext:
    """Context for platform-initiated background work."""
    return RequestContext(
        tenant_id=tenant_id,
        principal=SYSTEM,
        channel="SYSTEM",
        venue_id=venue_id,
        correlation_id=correlation_id or new_correlation_id(),
    )


def guest_context(
    tenant_id: str,
    *,
    venue_id: str | None = None,
    channel: str = "ONLINE",
    language: str = "en",
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> RequestContext:
    """Context for an unauthenticated customer. Guest checkout is required (R11.6)."""
    return RequestContext(
        tenant_id=tenant_id,
        principal=ANONYMOUS,
        channel=channel,
        venue_id=venue_id,
        language=language,
        ip_address=ip_address,
        user_agent=user_agent,
    )


__all__ = [
    "ALL_CHANNELS",
    "ANONYMOUS",
    "CUSTOMER_FACING_CHANNELS",
    "Channel",
    "Principal",
    "PrincipalKind",
    "RequestContext",
    "STAFF_CHANNELS",
    "SYSTEM",
    "guest_context",
    "system_context",
]
