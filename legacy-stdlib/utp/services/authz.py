"""Authorization: effective permissions, venue scope, masking, escalation guards.

The whole point of this module is that hiding a button is a convenience and this
is the control (R42.1). Every protected operation in every other service calls
:meth:`AuthorizationService.require_page` or
:meth:`AuthorizationService.require_action` before it touches data.

Evaluation order is fixed by R42.2: authenticated and active principal → tenant
match → organization scope → venue scope → page permission → action permission.
A failure at any step produces the same generic ``AuthorizationDenied`` and an
audit event (R42.3).

Permission caching
------------------
Permissions are resolved from the database per request and cached only inside the
:class:`~utp.core.context.RequestContext`. There is deliberately no cross-request
cache, so a role change takes effect on the principal's very next request — well
inside the 60-second bound of R44.7 — without any invalidation machinery to get
wrong. ``perm_epoch`` on the staff row lets the API tell a signed-in user that
their access changed rather than failing obscurely (R44.8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.audit import AuditLog
from ..core.clock import Clock
from ..core.context import Principal, RequestContext
from ..core.db import Database
from ..core.errors import AuthenticationRequired, AuthorizationDenied, NotFound, ValidationError
from ..core import i18n
from ..domain import permission_labels as plabels
from ..domain import permissions as perms

#: Fields that require ``VIEW_PII`` to be returned unmasked (R42.9, R12.24).
PII_FIELDS: frozenset[str] = frozenset(
    {
        "email",
        "phone",
        "full_name",
        "customer_name",
        "first_name",
        "last_name",
        "address",
        "recipient",
        "contact",
        "tax_address",
        "guardian_name",
    }
)

#: Fields that require ``VIEW_COST`` (R41.8).
COST_FIELDS: frozenset[str] = frozenset(
    {"cost_minor", "margin_minor", "margin_bp", "net_cost_minor", "commission_minor", "commission_bp"}
)

#: Fields a non-super-admin principal may never set through any payload (R42.12).
IMMUTABLE_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "organization_id",
        "authority_level",
        "perm_epoch",
        "permission_key",
        "granted",
        "role_id",
        "confirmed",
        "usage_count",
        "budget_used_minor",
        "credit_used_minor",
        "sequence_no",
    }
)

MASK_TOKEN = "•••"

#: Scripts that do not separate words with spaces, so a regex word boundary tells
#: you nothing useful about where a term begins. Thai, CJK ideographs, hiragana,
#: katakana and the Hangul block.
_UNSPACED_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0E00, 0x0E7F),  # Thai
    (0x3040, 0x30FF),  # hiragana + katakana
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
)


def _is_unspaced_script(needle: str) -> bool:
    return any(
        low <= ord(ch) <= high for ch in needle for low, high in _UNSPACED_SCRIPT_RANGES
    )


def _text_matcher(needle: str):
    """Build a predicate that decides whether ``needle`` occurs in a haystack.

    Word-anchored for spaced scripts, plain substring otherwise. Returned as a
    closure so the regex is compiled once per search rather than once per page.
    """
    if _is_unspaced_script(needle):
        return lambda haystack: needle in haystack.casefold()
    pattern = re.compile(rf"\b{re.escape(needle)}", re.IGNORECASE)
    return lambda haystack: pattern.search(haystack) is not None


@dataclass(slots=True)
class EffectivePermissions:
    """Resolved authority for one principal against one venue scope."""

    granted: frozenset[str]
    authority_level: int
    venue_ids: frozenset[str] | None  # None == every venue in the tenant
    organization_ids: frozenset[str]
    roles: tuple[str, ...] = ()
    operating_points: frozenset[str] = frozenset()
    tenant_wide: bool = False
    matched_assignments: int = 0
    warnings: list[str] = field(default_factory=list)

    def has_page(self, page: str, verb: str) -> bool:
        return f"{page}.{verb}" in self.granted

    def has_action(self, action: str) -> bool:
        return f"{perms.ACTION_PREFIX}{action}" in self.granted

    def covers_venue(self, venue_id: str | None) -> bool:
        if self.tenant_wide or self.venue_ids is None:
            return True
        if venue_id is None:
            return bool(self.venue_ids)
        return venue_id in self.venue_ids


class AuthorizationService:
    """Permission and scope enforcement."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog, config: Any = None) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.config = config

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #

    def effective_permissions(self, ctx: RequestContext) -> EffectivePermissions:
        """Union of permissions from assignments whose scope covers the target venue.

        Assignments outside the request's venue scope contribute nothing (R43.3).
        """
        cache_key = f"perms::{ctx.venue_id or '-'}"
        if ctx._permission_cache is not None and cache_key in ctx._permission_cache:
            return ctx._permission_cache[cache_key]

        principal = ctx.principal
        if principal.is_system:
            result = EffectivePermissions(
                granted=frozenset(perms.ALL_PERMISSION_KEYS),
                authority_level=100,
                venue_ids=None,
                organization_ids=frozenset(),
                roles=("SYSTEM",),
                tenant_wide=True,
            )
            self._cache(ctx, cache_key, result)
            return result

        if not principal.is_staff or not principal.id:
            result = EffectivePermissions(
                granted=frozenset(), authority_level=0, venue_ids=frozenset(), organization_ids=frozenset()
            )
            self._cache(ctx, cache_key, result)
            return result

        staff = self.db.query_one(
            "SELECT id, status, organization_id, perm_epoch FROM staff WHERE id = ? AND tenant_id = ?",
            (principal.id, ctx.tenant_id),
        )
        if staff is None or staff["status"] != "ACTIVE":
            # Suspended/deactivated principals hold nothing, immediately (R38.5, R44.9).
            result = EffectivePermissions(
                granted=frozenset(), authority_level=0, venue_ids=frozenset(), organization_ids=frozenset()
            )
            self._cache(ctx, cache_key, result)
            return result

        rows = self.db.query(
            """
            SELECT ra.scope_type, ra.scope_id, ra.operating_point,
                   r.id AS role_id, r.code AS role_code, r.authority_level, r.status AS role_status
            FROM role_assignments ra
            JOIN roles r ON r.id = ra.role_id AND r.tenant_id = ra.tenant_id
            WHERE ra.tenant_id = ? AND ra.staff_id = ? AND ra.status = 'ACTIVE'
              AND ra.revoked_at IS NULL AND r.status = 'ACTIVE'
            """,
            (ctx.tenant_id, principal.id),
        )

        venue_org = self._venue_organizations(ctx.tenant_id)
        target_org = venue_org.get(ctx.venue_id) if ctx.venue_id else None

        granted: set[str] = set()
        venue_ids: set[str] = set()
        organization_ids: set[str] = set()
        operating_points: set[str] = set()
        role_codes: list[str] = []
        authority = 0
        tenant_wide = False
        matched = 0

        for row in rows:
            scope_type = row["scope_type"]
            scope_id = row["scope_id"]
            in_scope = False
            if scope_type == "TENANT":
                in_scope = True
                tenant_wide = True
            elif scope_type == "ORGANIZATION":
                organization_ids.add(scope_id)
                in_scope = ctx.venue_id is None or target_org == scope_id
            elif scope_type == "VENUE":
                in_scope = ctx.venue_id is None or ctx.venue_id == scope_id
            elif scope_type == "OPERATING_POINT":
                in_scope = ctx.venue_id is None or ctx.venue_id == scope_id
            if not in_scope:
                continue
            matched += 1
            role_codes.append(row["role_code"])
            authority = max(authority, int(row["authority_level"] or 0))
            granted.update(self._role_permission_keys(ctx.tenant_id, row["role_id"]))
            if scope_type == "TENANT":
                pass  # covers every venue
            elif scope_type == "ORGANIZATION":
                venue_ids.update(v for v, org in venue_org.items() if org == scope_id)
            else:
                venue_ids.add(scope_id)
                if row["operating_point"]:
                    operating_points.add(row["operating_point"])

        result = EffectivePermissions(
            granted=frozenset(granted),
            authority_level=authority,
            venue_ids=None if tenant_wide else frozenset(venue_ids),
            organization_ids=frozenset(organization_ids),
            roles=tuple(sorted(set(role_codes))),
            operating_points=frozenset(operating_points),
            tenant_wide=tenant_wide,
            matched_assignments=matched,
            warnings=perms.combination_warnings(set(granted)),
        )
        self._cache(ctx, cache_key, result)
        return result

    def _cache(self, ctx: RequestContext, key: str, value: EffectivePermissions) -> None:
        if ctx._permission_cache is None:
            ctx._permission_cache = {}
        ctx._permission_cache[key] = value

    def _role_permission_keys(self, tenant_id: str, role_id: str) -> set[str]:
        rows = self.db.query(
            "SELECT permission_key FROM role_permissions "
            "WHERE tenant_id = ? AND role_id = ? AND granted = 1",
            (tenant_id, role_id),
        )
        return {r["permission_key"] for r in rows}

    def _venue_organizations(self, tenant_id: str) -> dict[str, str]:
        rows = self.db.query("SELECT id, organization_id FROM venues WHERE tenant_id = ?", (tenant_id,))
        return {r["id"]: r["organization_id"] for r in rows}

    # ------------------------------------------------------------------ #
    # Enforcement
    # ------------------------------------------------------------------ #

    def require_authenticated(self, ctx: RequestContext) -> str:
        if ctx.principal.is_system:
            return "system"
        staff_id = ctx.principal.id
        if not ctx.principal.is_staff or not staff_id:
            raise AuthenticationRequired()
        row = self.db.query_one(
            "SELECT status FROM staff WHERE id = ? AND tenant_id = ?", (staff_id, ctx.tenant_id)
        )
        if row is None or row["status"] != "ACTIVE":
            raise AuthenticationRequired()
        return staff_id

    def can_page(self, ctx: RequestContext, page: str, verb: str) -> bool:
        perms.page_key(page, verb)  # validates page/verb pair
        effective = self.effective_permissions(ctx)
        return effective.has_page(page, verb) and effective.covers_venue(ctx.venue_id)

    def can_action(self, ctx: RequestContext, action: str) -> bool:
        perms.action_key(action)
        effective = self.effective_permissions(ctx)
        return effective.has_action(action) and effective.covers_venue(ctx.venue_id)

    def require_page(
        self,
        ctx: RequestContext,
        page: str,
        verb: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> EffectivePermissions:
        """Enforce a page permission in the order mandated by R42.2."""
        key = perms.page_key(page, verb)
        self.require_authenticated(ctx)
        effective = self.effective_permissions(ctx)
        if not effective.covers_venue(ctx.venue_id):
            self._deny(ctx, key, reason="venue_scope", target_type=target_type, target_id=target_id)
        if key not in effective.granted:
            self._deny(ctx, key, reason="page_permission", target_type=target_type, target_id=target_id)
        return effective

    def require_action(
        self,
        ctx: RequestContext,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        reason: str | None = None,
    ) -> EffectivePermissions:
        """Enforce an action permission, including its mandatory-reason rule (R67.4)."""
        key = perms.action_key(action)
        self.require_authenticated(ctx)
        effective = self.effective_permissions(ctx)
        if not effective.covers_venue(ctx.venue_id):
            self._deny(ctx, key, reason="venue_scope", target_type=target_type, target_id=target_id)
        if key not in effective.granted:
            self._deny(ctx, key, reason="action_permission", target_type=target_type, target_id=target_id)
        definition = perms.ACTIONS_BY_KEY[action]
        if definition.requires_reason and not (reason or "").strip():
            raise ValidationError(
                {"reason": f"A reason is required for {definition.label}."},
                message=f"{definition.label} requires a reason.",
            )
        return effective

    def require_any_action(self, ctx: RequestContext, actions: Iterable[str]) -> str:
        """Allow when the principal holds at least one of ``actions``."""
        self.require_authenticated(ctx)
        effective = self.effective_permissions(ctx)
        for action in actions:
            if effective.has_action(action) and effective.covers_venue(ctx.venue_id):
                return action
        self._deny(ctx, ",".join(f"{perms.ACTION_PREFIX}{a}" for a in actions), reason="action_permission")
        raise AssertionError("unreachable")  # pragma: no cover

    def require_venue(self, ctx: RequestContext, venue_id: str | None) -> str:
        """Every write must name an unambiguous, in-scope venue (R43.8)."""
        if not venue_id:
            raise ValidationError(
                {"venue_id": "Select the venue this action applies to."},
                message="The target venue must be unambiguous.",
            )
        scoped = ctx.for_venue(venue_id)
        effective = self.effective_permissions(scoped)
        if not effective.covers_venue(venue_id):
            self._deny(scoped, "venue_scope", reason="venue_scope", target_type="venue", target_id=venue_id)
        return venue_id

    def scoped_venue_ids(self, ctx: RequestContext) -> list[str] | None:
        """Venues whose data may contribute to a list, aggregate or export (R43.7).

        ``None`` means "every venue in the tenant" and is returned only for
        tenant-wide assignments.
        """
        effective = self.effective_permissions(ctx.for_venue(None))
        if effective.tenant_wide or effective.venue_ids is None:
            return None
        return sorted(effective.venue_ids)

    def authority_level(self, ctx: RequestContext) -> int:
        return self.effective_permissions(ctx.for_venue(None)).authority_level

    def _deny(
        self,
        ctx: RequestContext,
        required: str,
        *,
        reason: str,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None:
        """Emit the audit event, then raise the generic denial (R42.3, R45.8)."""
        self.audit.security(
            ctx,
            "AUTHORIZATION_DENIED",
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            detail={"required": required, "channel": ctx.channel},
        )
        raise AuthorizationDenied(required=required, correlation_id=ctx.correlation_id)

    # ------------------------------------------------------------------ #
    # Tenant isolation
    # ------------------------------------------------------------------ #

    def assert_same_tenant(
        self,
        ctx: RequestContext,
        record: Any,
        *,
        entity: str,
        record_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the record or raise ``NotFound`` for a foreign tenant (R1.2).

        The response is identical for "does not exist" and "belongs to another
        tenant", so existence is never disclosed.
        """
        if record is None:
            raise NotFound(details={"entity": entity})
        data = dict(record)
        if data.get("tenant_id") and data["tenant_id"] != ctx.tenant_id:
            self.audit.security(
                ctx,
                "CROSS_TENANT_ATTEMPT",
                target_type=entity,
                target_id=record_id or data.get("id"),
                reason="tenant_mismatch",
                detail={"entity": entity},
            )
            raise NotFound(details={"entity": entity})
        return data

    def load_scoped(
        self,
        ctx: RequestContext,
        table: str,
        record_id: str,
        *,
        entity: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a row by id inside the caller's tenant, or raise ``NotFound``."""
        row = self.db.query_one(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
        return self.assert_same_tenant(ctx, row, entity=entity or table, record_id=record_id)

    # ------------------------------------------------------------------ #
    # Field-level protection
    # ------------------------------------------------------------------ #

    def mask_record(
        self,
        ctx: RequestContext,
        record: dict[str, Any],
        *,
        pii_fields: Iterable[str] | None = None,
        cost_fields: Iterable[str] | None = None,
        audit_pii_access: bool = True,
        entity: str | None = None,
    ) -> dict[str, Any]:
        """Mask or omit fields the principal may not see, at the data level (R42.9).

        Reading unmasked personal data is itself audited (R12.24).
        """
        effective = self.effective_permissions(ctx)
        may_see_pii = effective.has_action("VIEW_PII")
        may_see_cost = effective.has_action("VIEW_COST")
        pii = frozenset(pii_fields) if pii_fields is not None else PII_FIELDS
        cost = frozenset(cost_fields) if cost_fields is not None else COST_FIELDS

        out: dict[str, Any] = {}
        touched_pii = False
        for key, value in record.items():
            if key in pii:
                if may_see_pii:
                    out[key] = value
                    if value not in (None, ""):
                        touched_pii = True
                else:
                    out[key] = mask_value(key, value)
            elif key in cost:
                if may_see_cost:
                    out[key] = value
                # omitted entirely when not permitted: absence leaks less than a mask
            else:
                out[key] = value
        if touched_pii and audit_pii_access and not ctx.principal.is_system:
            self.audit.record(
                ctx,
                "PII_ACCESS",
                target_type=entity or "record",
                target_id=str(record.get("id") or ""),
                new={"fields": sorted(f for f in record if f in pii)},
            )
        return out

    def assert_no_forbidden_fields(
        self,
        ctx: RequestContext,
        payload: dict[str, Any],
        *,
        extra: Iterable[str] = (),
    ) -> None:
        """Reject attempts to set fields the principal may not modify (R42.12, R44.11)."""
        forbidden = IMMUTABLE_REQUEST_FIELDS | frozenset(extra)
        offending = sorted(k for k in payload if k in forbidden)
        if not offending:
            return
        self.audit.security(
            ctx,
            "AUTHORIZATION_DENIED",
            reason="forbidden_fields",
            detail={"fields": offending},
        )
        raise AuthorizationDenied(
            required="protected_fields",
            log_detail=f"attempt to set protected fields: {offending}",
            correlation_id=ctx.correlation_id,
        )

    # ------------------------------------------------------------------ #
    # Escalation guards (R44)
    # ------------------------------------------------------------------ #

    def assert_may_grant(
        self,
        ctx: RequestContext,
        *,
        permission_keys: Iterable[str],
        target_authority_level: int,
        scope_venue_id: str | None,
    ) -> None:
        """Block privilege escalation before any grant is written.

        Three rules, all from R44:

        * a principal may not grant a permission they do not themselves hold in
          the target scope (R44.3);
        * a principal may not assign a role whose authority level exceeds their
          own (R44.4);
        * scope cannot be widened by manipulating the request (R44.11) — the check
          runs against the *target* scope, not the caller's convenient scope.
        """
        probe = ctx.for_venue(scope_venue_id) if scope_venue_id else ctx.for_venue(None)
        effective = self.effective_permissions(probe)
        if target_authority_level > effective.authority_level:
            self.audit.security(
                ctx,
                "AUTHORIZATION_DENIED",
                reason="authority_level_exceeded",
                detail={"target_level": target_authority_level, "actor_level": effective.authority_level},
            )
            raise AuthorizationDenied(
                required="authority_level",
                log_detail=(
                    f"actor level {effective.authority_level} cannot assign level {target_authority_level}"
                ),
                correlation_id=ctx.correlation_id,
            )
        missing = sorted(set(permission_keys) - set(effective.granted))
        if missing:
            self.audit.security(
                ctx,
                "AUTHORIZATION_DENIED",
                reason="grant_exceeds_own_permissions",
                detail={"missing": missing[:20], "missing_count": len(missing)},
            )
            raise AuthorizationDenied(
                required="grant_subset",
                log_detail=f"cannot grant permissions not held: {missing[:20]}",
                correlation_id=ctx.correlation_id,
            )

    def assert_not_self(self, ctx: RequestContext, target_staff_id: str, *, what: str) -> None:
        """A staff member may never modify their own authority (R44.2)."""
        if ctx.principal.is_staff and ctx.principal.id == target_staff_id:
            self.audit.security(
                ctx,
                "AUTHORIZATION_DENIED",
                target_type="staff",
                target_id=target_staff_id,
                reason="self_modification",
                detail={"what": what},
            )
            raise AuthorizationDenied(
                required="not_self",
                log_detail=f"self-modification of {what} is not permitted",
                correlation_id=ctx.correlation_id,
            )

    def permission_summary(
        self, ctx: RequestContext, staff_id: str, *, language: str | None = None
    ) -> dict[str, Any]:
        """Effective permissions for a staff member across pages and scopes (R40.12).

        This is the Effective Permission Viewer of §36: the *result* of combining
        role, scope and any additional grants, resolved by probing the same code
        path that enforces live requests. It is deliberately not a re-implementation
        of the resolution rules — a viewer that computed permissions its own way
        would eventually disagree with the enforcement, and the disagreement would
        be invisible until someone was wrongly admitted or wrongly refused.
        """
        self.require_page(ctx, "Staff", "VIEW")
        lang = language or ctx.language or i18n.DEFAULT_LANGUAGE
        staff = self.load_scoped(ctx, "staff", staff_id, entity="staff")
        assignments = self.db.query(
            """
            SELECT ra.id, ra.scope_type, ra.scope_id, ra.operating_point, ra.status,
                   r.code AS role_code, r.name AS role_name, r.authority_level
            FROM role_assignments ra
            JOIN roles r ON r.id = ra.role_id AND r.tenant_id = ra.tenant_id
            WHERE ra.tenant_id = ? AND ra.staff_id = ? AND ra.revoked_at IS NULL
            ORDER BY r.authority_level DESC
            """,
            (ctx.tenant_id, staff_id),
        )
        venues = self.db.query(
            "SELECT id, code, name_json FROM venues WHERE tenant_id = ?", (ctx.tenant_id,)
        )
        # Probe as the target staff member so the summary reflects *their* effective
        # authority, resolved by the same code path that enforces requests.
        target_principal = Principal(
            kind="STAFF",
            id=staff_id,
            display_name=staff.get("display_name"),
            organization_id=staff.get("organization_id"),
            perm_epoch=int(staff.get("perm_epoch") or 1),
        )
        per_venue: dict[str, Any] = {}
        for venue in venues:
            probe = ctx.with_principal(target_principal).for_venue(venue["id"])
            effective = self.effective_permissions(probe)
            if not effective.granted:
                continue
            per_venue[venue["id"]] = {
                "venue_code": venue["code"],
                "roles": list(effective.roles),
                "authority_level": effective.authority_level,
                "pages": {
                    page.key: {
                        verb: (f"{page.key}.{verb}" in effective.granted) for verb in page.verbs
                    }
                    for page in perms.PAGES
                },
                "actions": sorted(
                    a.key for a in perms.ACTIONS if a.permission_key in effective.granted
                ),
                # §36 renders "Payment Types: VIEW", "Promotions: VIEW / ADD / EDIT",
                # "Staff: No Access" per venue — so the summary is computed per venue,
                # not once for the account. A staff member can legitimately hold
                # different authority at two venues (§35) and a single blended figure
                # would describe neither.
                "summary": self.grant_summary(effective.granted, language=lang),
                "settings": perms.settings_tree(effective.granted),
            }
        tenant_wide = ctx.with_principal(target_principal).for_venue(None)
        overall = self.effective_permissions(tenant_wide)
        return {
            "staff_id": staff_id,
            "display_name": staff.get("display_name"),
            "status": staff.get("status"),
            "authority_level": overall.authority_level,
            "assignments": [dict(a) for a in assignments],
            "tenant_wide": overall.tenant_wide,
            "warnings": overall.warnings,
            "by_venue": per_venue,
            "language": lang,
            "overall_summary": self.grant_summary(overall.granted, language=lang),
        }

    def navigation(self, ctx: RequestContext, *, language: str | None = None) -> list[dict[str, Any]]:
        """Pages the principal may see. Others are hidden from navigation (R42.7).

        Navigation is built from permissions, never from role names (§45, §48):
        two people holding the same role code at different scopes can legitimately
        see different menus, and a tenant that edits a role must see the menu move
        with it. ``label`` is localized for display; ``page`` stays the internal
        key the client passes back and the server enforces (§49).
        """
        effective = self.effective_permissions(ctx)
        lang = language or ctx.language or i18n.DEFAULT_LANGUAGE
        items: list[dict[str, Any]] = []
        for page in perms.PAGES:
            if f"{page.key}.VIEW" not in effective.granted:
                continue
            items.append(
                {
                    "page": page.key,
                    "label": plabels.page_label(page.key, lang),
                    "group": page.group,
                    "group_label": plabels.group_label(page.group, lang),
                    "settings_category": perms.SETTINGS_CATEGORY_BY_PAGE.get(page.key),
                    "can_add": f"{page.key}.ADD" in effective.granted,
                    "can_edit": f"{page.key}.EDIT" in effective.granted,
                    "can_delete": f"{page.key}.DELETE" in effective.granted,
                    "delete_semantics": page.delete_semantics,
                    "delete_semantics_label": plabels.delete_semantics_label(page.delete_semantics, lang),
                }
            )
        return items

    def settings_home(self, ctx: RequestContext, *, language: str | None = None) -> list[dict[str, Any]]:
        """Settings categories and pages this principal may VIEW (§11, §26, §71).

        A category with nothing viewable inside it is not returned at all, so the
        Settings home draws no dead cards and reveals no structure the principal is
        not trusted with. The filter runs against the same ``granted`` set that
        ``require_page`` enforces, so the menu and the door cannot disagree.
        """
        effective = self.effective_permissions(ctx)
        lang = language or ctx.language or i18n.DEFAULT_LANGUAGE
        tree = perms.settings_tree(effective.granted)
        for category in tree:
            key = str(category["category"])
            category["label"] = plabels.category_label(key, lang)
            category["description"] = plabels.category_description(key, lang)
            for page in category["pages"]:  # type: ignore[union-attr]
                page_key = str(page["page"])
                page["label"] = plabels.page_label(page_key, lang)
                page["group_label"] = plabels.group_label(str(page["group"]), lang)
                page["delete_semantics_label"] = plabels.delete_semantics_label(
                    page.get("delete_semantics"), lang  # type: ignore[arg-type]
                )
        return tree

    def permission_matrix(self, ctx: RequestContext, *, language: str | None = None) -> dict[str, Any]:
        """The full registry, localized, for the role editor (§19, §20, §50).

        Requires ``Roles.VIEW`` or ``Permissions.VIEW``. The list of everything that
        *can* be granted is a map of the back office, so it is not public — but two
        different jobs need it: editing a role, and reading what a permission means
        while reviewing someone's access. Either page's VIEW is sufficient; neither
        implies the other.

        It returns the registry, not any principal's grants, so the response is
        identical for every caller who is allowed it. What differs is whether they
        are allowed it at all.
        """
        self.require_authenticated(ctx)
        effective = self.effective_permissions(ctx)
        if not effective.covers_venue(ctx.venue_id) or not (
            "Roles.VIEW" in effective.granted or "Permissions.VIEW" in effective.granted
        ):
            self._deny(ctx, "Roles.VIEW", reason="page_permission")
        lang = language or ctx.language or i18n.DEFAULT_LANGUAGE
        rows: list[dict[str, Any]] = []
        for page in perms.PAGES:
            rows.append(
                {
                    "page": page.key,
                    "label": plabels.page_label(page.key, lang),
                    "group": page.group,
                    "group_label": plabels.group_label(page.group, lang),
                    "settings_category": perms.SETTINGS_CATEGORY_BY_PAGE.get(page.key),
                    # False means "does not logically apply" — the spec's "-" cell.
                    # The UI must render it as not-applicable, not as an empty box a
                    # user can tick, because no grant of it would ever be honoured.
                    "verbs": {verb: (verb in page.verbs) for verb in perms.ALL_VERBS},
                    "keys": {verb: f"{page.key}.{verb}" for verb in page.verbs},
                    "delete_semantics": page.delete_semantics,
                    "delete_semantics_label": plabels.delete_semantics_label(page.delete_semantics, lang),
                    "protected": page.protected,
                    "description": page.description,
                }
            )
        actions: list[dict[str, Any]] = [
            {
                "key": action.key,
                "permission_key": action.permission_key,
                "label": plabels.action_label(action.key, lang),
                "group": action.group,
                "group_label": plabels.group_label(action.group, lang),
                "requires_reason": action.requires_reason,
                "requires_approval": action.requires_approval,
                "revenue_affecting": action.revenue_affecting,
                "description": action.description,
            }
            for action in perms.ACTIONS
        ]
        return {
            "language": lang,
            "verbs": [
                {"verb": verb, "label": plabels.verb_label(verb, lang)} for verb in perms.ALL_VERBS
            ],
            "pages": rows,
            "actions": actions,
            "categories": [
                {
                    "category": category.key,
                    "label": plabels.category_label(category.key, lang),
                    "description": plabels.category_description(category.key, lang),
                    "icon": category.icon,
                    "pages": list(category.pages),
                }
                for category in perms.SETTINGS_CATEGORIES
            ],
        }

    def grant_summary(
        self, granted: Iterable[str], *, language: str | None = None
    ) -> dict[str, Any]:
        """Plain-language summary of a permission set, for the pre-save review (§21).

        The administrator about to save a role should not have to read 291 checkbox
        states to know what they just built. This answers the four questions §76
        says the editor must make easy — what can they see, create, change, remove —
        and then calls out the pages where getting it wrong is expensive.

        ``sensitive`` deliberately reports "No access" as a distinct state from
        "Read only". Those are different decisions and conflating them is how a role
        quietly acquires authority nobody chose.
        """
        keys = set(granted)
        lang = language or i18n.DEFAULT_LANGUAGE
        settings_pages = [p for p in perms.PAGES if p.key in perms.SETTINGS_PAGE_KEYS]

        def _count(verb: str, pages: Iterable[perms.Page]) -> int:
            return sum(1 for p in pages if verb in p.verbs and f"{p.key}.{verb}" in keys)

        def _level(page_name: str) -> str:
            page = perms.PAGES_BY_KEY[page_name]
            if f"{page_name}.VIEW" not in keys:
                return "NONE"
            if "DELETE" in page.verbs and f"{page_name}.DELETE" in keys:
                return "FULL"
            if "EDIT" in page.verbs and f"{page_name}.EDIT" in keys:
                return "EDIT"
            if "ADD" in page.verbs and f"{page_name}.ADD" in keys:
                return "ADD"
            return "READ_ONLY"

        # The pages a reviewer should always be told about explicitly, because a
        # mistake on them moves money, opens gates or hands out authority.
        sensitive_pages = (
            "VAT Settings",
            "Service Charge Settings",
            "Exchange Rates",
            "Payment Type",
            "Payment Providers",
            "Staff",
            "Roles",
            "Permissions",
            "Login Security",
            "Audit Logs",
            "API Configuration",
        )
        granted_actions = [a for a in perms.ACTIONS if a.permission_key in keys]
        return {
            "language": lang,
            "total_keys": len(keys & perms.ALL_PERMISSION_KEY_SET),
            "pages_viewable": _count("VIEW", perms.PAGES),
            "settings_pages_viewable": _count("VIEW", settings_pages),
            "can_add": _count("ADD", perms.PAGES),
            "can_edit": _count("EDIT", perms.PAGES),
            "can_delete": _count("DELETE", perms.PAGES),
            "actions": [
                {"key": a.key, "label": plabels.action_label(a.key, lang), "group": a.group}
                for a in granted_actions
            ],
            "revenue_affecting_actions": [
                {"key": a.key, "label": plabels.action_label(a.key, lang)}
                for a in granted_actions
                if a.revenue_affecting
            ],
            "sensitive": [
                {
                    "page": page_name,
                    "label": plabels.page_label(page_name, lang),
                    "level": _level(page_name),
                }
                for page_name in sensitive_pages
            ],
            "by_category": [
                {
                    "category": category.key,
                    "label": plabels.category_label(category.key, lang),
                    "viewable": sum(1 for p in category.pages if f"{p}.VIEW" in keys),
                    "total": len(category.pages),
                }
                for category in perms.SETTINGS_CATEGORIES
            ],
            "warnings": perms.combination_warnings(keys),
        }

    def settings_search(
        self, ctx: RequestContext, query: str, *, language: str | None = None, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Settings pages matching ``query`` that this principal may VIEW (§27, §32).

        Search is filtered by the same permission as navigation, so a page the
        principal cannot open never appears as a result. Returning it and then
        refusing the click would confirm the page exists and teach the user exactly
        what they are missing.

        Matching is on the localized label *and* the internal key, so an operator
        working in Thai can still find a page by the English name they saw in
        documentation.

        Matching is anchored to the **start of a word**, not to any substring. A
        plain substring search for "VAT" also returns every page whose text contains
        "deacti*vat*es" or "reser*vat*ion", which buries the page actually called VAT
        under pages that have nothing to do with tax. Anchoring still matches while
        the user is mid-word ("exch" finds Exchange Rates), which is what a search
        box needs.

        Scripts written without spaces get plain substring matching instead, because
        a word boundary is not a meaningful concept in Thai, Chinese or Japanese and
        anchoring there would make the box useless in three of the five supported
        languages.

        Name hits are returned before description hits, so the page named VAT is
        never pushed below a page that merely mentions it.
        """
        needle = (query or "").strip().casefold()
        if not needle:
            return []
        lang = language or ctx.language or i18n.DEFAULT_LANGUAGE
        effective = self.effective_permissions(ctx)
        matches = _text_matcher(needle)
        by_name: list[dict[str, Any]] = []
        by_description: list[dict[str, Any]] = []
        for category in perms.SETTINGS_CATEGORIES:
            for page_name in category.pages:
                if f"{page_name}.VIEW" not in effective.granted:
                    continue
                page = perms.PAGES_BY_KEY[page_name]
                label = plabels.page_label(page_name, lang)
                names = (label, page_name, page.label)
                hit = "name" if any(matches(n) for n in names if n) else None
                if hit is None and page.description and matches(page.description):
                    hit = "description"
                if hit is None:
                    continue
                record = {
                    "page": page_name,
                    "label": label,
                    "category": category.key,
                    "category_label": plabels.category_label(category.key, lang),
                    "description": page.description,
                    "matched_on": hit,
                    "can_edit": f"{page_name}.EDIT" in effective.granted,
                }
                (by_name if hit == "name" else by_description).append(record)
        return (by_name + by_description)[:limit]

    def permission_changed(self, ctx: RequestContext) -> bool:
        """True when the principal's authority changed since their session began (R44.8)."""
        if not ctx.principal.is_staff or not ctx.principal.session_id:
            return False
        row = self.db.query_one(
            """
            SELECT s.perm_epoch AS session_epoch, st.perm_epoch AS staff_epoch
            FROM auth_sessions s
            JOIN staff st ON st.id = s.staff_id AND st.tenant_id = s.tenant_id
            WHERE s.id = ? AND s.tenant_id = ?
            """,
            (ctx.principal.session_id, ctx.tenant_id),
        )
        if row is None:
            return False
        return int(row["session_epoch"]) != int(row["staff_epoch"])


def mask_value(field_name: str, value: Any) -> Any:
    """Mask a personal-data value while keeping it recognisable to its owner.

    An operator without ``VIEW_PII`` still needs to match a guest to a booking at
    the counter, so masking preserves shape rather than deleting the field: enough
    to confirm identity with the guest present, not enough to build a contact list.
    """
    if value in (None, ""):
        return value
    text = str(value)
    if "@" in text and field_name in ("email", "recipient", "contact"):
        local, _, domain = text.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}{MASK_TOKEN}@{domain}"
    digits = [c for c in text if c.isdigit()]
    if len(digits) >= 6 and field_name in ("phone", "contact"):
        return f"{MASK_TOKEN}{text[-4:]}"
    if len(text) <= 2:
        return MASK_TOKEN
    parts = text.split()
    if len(parts) > 1:
        return " ".join([parts[0], *(f"{p[:1]}{MASK_TOKEN}" for p in parts[1:])])
    return f"{text[:1]}{MASK_TOKEN}"


__all__ = [
    "COST_FIELDS",
    "EffectivePermissions",
    "IMMUTABLE_REQUEST_FIELDS",
    "MASK_TOKEN",
    "PII_FIELDS",
    "AuthorizationService",
    "mask_value",
]
