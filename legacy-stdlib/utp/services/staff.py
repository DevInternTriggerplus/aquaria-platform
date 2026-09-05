"""Staff, roles, role assignments and authentication.

Design points that carry requirement weight:

* **History survives people.** A staff account with any transaction, scan, shift
  or approval is never hard-deleted; DELETE maps to deactivation and the
  confirmation dialog says so (R38.6, R38.7).
* **Suspension is immediate.** Suspending or deactivating revokes every active
  session and refresh token in the same transaction, so the next request from
  that principal fails authentication rather than waiting for a token to expire
  (R38.5, R44.9).
* **Grants can only shrink authority.** Every write that touches a role or an
  assignment goes through :meth:`AuthorizationService.assert_may_grant`, which
  refuses to grant a permission the actor does not hold and refuses to assign a
  role above the actor's authority level (R44.3, R44.4).
* **The platform cannot be locked out.** The last active Platform Super Admin
  cannot be suspended, deactivated, or stripped of their assignment (R44.5).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, to_iso
from ..core.config import ConfigStore
from ..core.context import Principal, RequestContext
from ..core.db import Database, decode
from ..core.errors import (
    AuthenticationRequired,
    ConflictError,
    NotFound,
    RateLimited,
    ValidationError,
)
from ..core.ids import hash_secret, new_id, new_secret, verify_secret
from ..domain import permissions as perms
from .authz import AuthorizationService

#: Tables scanned to decide whether a staff account carries history (R38.6).
_HISTORY_SOURCES: tuple[tuple[str, str], ...] = (
    ("audit_events", "actor_id"),
    ("bookings", "staff_actor_id"),
    ("payments", "actor_id"),
    ("refunds", "actor_id"),
    ("scan_events", "operator_id"),
    ("shift_sessions", "staff_id"),
    ("seat_blocks", "actor_id"),
)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
SESSION_IDLE_MINUTES = 30
SESSION_ABSOLUTE_MINUTES = 12 * 60


class StaffService:
    """Staff, role and session management."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config

    # ------------------------------------------------------------------ #
    # Roles
    # ------------------------------------------------------------------ #

    def seed_role_templates(
        self, ctx: RequestContext, *, organization_id: str | None = None, codes: Iterable[str] | None = None
    ) -> dict[str, str]:
        """Create tenant roles from platform templates.

        Copies are independent from the moment they are created, so a later
        platform change to a template never mutates a tenant role (R39.6).
        """
        wanted = tuple(codes) if codes is not None else tuple(t.code for t in perms.ROLE_TEMPLATES)
        created: dict[str, str] = {}
        now = to_iso(self.clock.now())
        with self.db.transaction():
            for code in wanted:
                template = perms.ROLE_TEMPLATES_BY_CODE.get(code)
                if template is None:
                    raise ValidationError({"code": f"Unknown role template {code!r}."})
                existing = self.db.query_one(
                    "SELECT id FROM roles WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)
                )
                if existing is not None:
                    created[code] = existing["id"]
                    continue
                role_id = new_id("rol")
                self.db.insert(
                    "roles",
                    {
                        "id": role_id,
                        "tenant_id": ctx.tenant_id,
                        "organization_id": organization_id,
                        "code": code,
                        "name": template.name,
                        "description": template.description,
                        "authority_level": template.authority_level,
                        "template_code": code,
                        "status": "ACTIVE",
                        "created_at": now,
                        "created_by": ctx.principal.id,
                    },
                )
                for key in template.resolve():
                    self.db.insert(
                        "role_permissions",
                        {
                            "id": new_id("rpm"),
                            "tenant_id": ctx.tenant_id,
                            "role_id": role_id,
                            "permission_key": key,
                            "granted": 1,
                        },
                    )
                created[code] = role_id
                self.audit.record(
                    ctx,
                    "ROLE_ADD",
                    target_type="role",
                    target_id=role_id,
                    new={"code": code, "authority_level": template.authority_level, "from_template": True},
                )
        return created

    def create_role(
        self,
        ctx: RequestContext,
        *,
        code: str,
        name: str,
        authority_level: int,
        permission_keys: Sequence[str] = (),
        organization_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a custom role. Starts with nothing granted unless keys are given (R44.1)."""
        self.authz.require_page(ctx, "Roles", "ADD")
        self.authz.require_action(ctx, "MANAGE_PERMISSION")
        keys = [perms.validate_permission_key(k) for k in permission_keys]
        self.authz.assert_may_grant(
            ctx, permission_keys=keys, target_authority_level=authority_level, scope_venue_id=None
        )
        if self.db.query_one("SELECT 1 FROM roles WHERE tenant_id = ? AND code = ?", (ctx.tenant_id, code)):
            raise ConflictError(f"A role with code {code!r} already exists.")
        role_id = new_id("rol")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.insert(
                "roles",
                {
                    "id": role_id,
                    "tenant_id": ctx.tenant_id,
                    "organization_id": organization_id or ctx.organization_id,
                    "code": code,
                    "name": name,
                    "description": description,
                    "authority_level": int(authority_level),
                    "status": "ACTIVE",
                    "created_at": now,
                    "created_by": ctx.principal.id,
                },
            )
            for key in keys:
                self.db.insert(
                    "role_permissions",
                    {
                        "id": new_id("rpm"),
                        "tenant_id": ctx.tenant_id,
                        "role_id": role_id,
                        "permission_key": key,
                        "granted": 1,
                    },
                )
            self.audit.record(
                ctx,
                "ROLE_ADD",
                target_type="role",
                target_id=role_id,
                new={"code": code, "name": name, "authority_level": authority_level, "permissions": keys},
            )
        return self.get_role(ctx, role_id)

    def clone_role(self, ctx: RequestContext, role_id: str, *, code: str, name: str) -> dict[str, Any]:
        """Duplicate a role including its grants (R39.3)."""
        source = self.get_role(ctx, role_id)
        return self.create_role(
            ctx,
            code=code,
            name=name,
            authority_level=int(source["authority_level"]),
            permission_keys=source["permissions"],
            organization_id=source.get("organization_id"),
            description=source.get("description"),
        )

    def set_role_permissions(
        self,
        ctx: RequestContext,
        role_id: str,
        changes: dict[str, bool],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Grant/revoke individual permission keys on a role.

        Each key is stored as its own row with its own boolean, which is what makes
        ADD/EDIT/DELETE genuinely independent (R40.4, R40.13). Bulk UI actions
        ("grant all in row") simply send more keys.
        """
        self.authz.require_page(ctx, "Roles", "EDIT", target_type="role", target_id=role_id)
        self.authz.require_action(ctx, "MANAGE_PERMISSION", target_type="role", target_id=role_id)
        role = self.get_role(ctx, role_id)
        newly_granted = [perms.validate_permission_key(k) for k, v in changes.items() if v]
        for key in changes:
            perms.validate_permission_key(key)
        self.authz.assert_may_grant(
            ctx,
            permission_keys=newly_granted,
            target_authority_level=int(role["authority_level"]),
            scope_venue_id=None,
        )
        before = set(role["permissions"])
        with self.db.transaction():
            for key, granted in changes.items():
                existing = self.db.query_one(
                    "SELECT id FROM role_permissions WHERE tenant_id = ? AND role_id = ? AND permission_key = ?",
                    (ctx.tenant_id, role_id, key),
                )
                if existing is None:
                    self.db.insert(
                        "role_permissions",
                        {
                            "id": new_id("rpm"),
                            "tenant_id": ctx.tenant_id,
                            "role_id": role_id,
                            "permission_key": key,
                            "granted": 1 if granted else 0,
                        },
                    )
                else:
                    self.db.update(
                        "role_permissions",
                        existing["id"],
                        {"granted": 1 if granted else 0},
                        tenant_id=ctx.tenant_id,
                    )
            after = set(self._role_permission_keys(ctx.tenant_id, role_id))
            self.audit.record(
                ctx,
                "PERMISSION_CHANGE",
                target_type="role",
                target_id=role_id,
                previous={"permissions": sorted(before)},
                new={"permissions": sorted(after), "changed": changes},
                reason=reason,
                severity="WARNING",
            )
            # Everyone holding this role must be re-evaluated on their next request.
            self._bump_epoch_for_role(ctx, role_id)
        updated = self.get_role(ctx, role_id)
        updated["warnings"] = perms.combination_warnings(set(updated["permissions"]))
        return updated

    def deactivate_role(self, ctx: RequestContext, role_id: str, *, reason: str | None = None) -> dict[str, Any]:
        self.authz.require_page(ctx, "Roles", "EDIT", target_type="role", target_id=role_id)
        self.authz.require_action(ctx, "MANAGE_PERMISSION")
        role = self.get_role(ctx, role_id)
        self._assert_not_last_super_admin_role(ctx, role)
        with self.db.transaction():
            self.db.update("roles", role_id, {"status": "INACTIVE"}, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx,
                "ROLE_EDIT",
                target_type="role",
                target_id=role_id,
                previous={"status": role["status"]},
                new={"status": "INACTIVE"},
                reason=reason,
            )
            self._bump_epoch_for_role(ctx, role_id)
        return self.get_role(ctx, role_id)

    def delete_role(self, ctx: RequestContext, role_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Delete a role. Refused while any staff member holds it (R39.4)."""
        self.authz.require_page(ctx, "Roles", "DELETE", target_type="role", target_id=role_id)
        self.authz.require_action(ctx, "MANAGE_PERMISSION")
        role = self.get_role(ctx, role_id)
        usage = self.role_usage(ctx, role_id)
        if usage["assigned_count"]:
            raise ConflictError(
                "Reassign the staff who hold this role before deleting it.",
                details={
                    "assigned_count": usage["assigned_count"],
                    "staff": [s["display_name"] for s in usage["staff"][:10]],
                },
            )
        self._assert_not_last_super_admin_role(ctx, role)
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM role_permissions WHERE tenant_id = ? AND role_id = ?", (ctx.tenant_id, role_id)
            )
            self.db.execute("DELETE FROM roles WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, role_id))
            self.audit.record(
                ctx,
                "ROLE_DELETE",
                target_type="role",
                target_id=role_id,
                previous={"code": role["code"], "name": role["name"]},
                reason=reason,
                severity="WARNING",
            )
        return {"deleted": True, "role_id": role_id, "code": role["code"]}

    def role_usage(self, ctx: RequestContext, role_id: str) -> dict[str, Any]:
        """Who holds this role — shown before any destructive change (R39.7)."""
        rows = self.db.query(
            """
            SELECT s.id, s.display_name, s.status, ra.scope_type, ra.scope_id
            FROM role_assignments ra
            JOIN staff s ON s.id = ra.staff_id AND s.tenant_id = ra.tenant_id
            WHERE ra.tenant_id = ? AND ra.role_id = ? AND ra.status = 'ACTIVE' AND ra.revoked_at IS NULL
            """,
            (ctx.tenant_id, role_id),
        )
        return {"role_id": role_id, "assigned_count": len(rows), "staff": [dict(r) for r in rows]}

    def get_role(self, ctx: RequestContext, role_id: str) -> dict[str, Any]:
        role = self.authz.load_scoped(ctx, "roles", role_id, entity="role")
        role["permissions"] = self._role_permission_keys(ctx.tenant_id, role_id)
        return role

    def list_roles(self, ctx: RequestContext) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Roles", "VIEW")
        rows = self.db.query(
            "SELECT * FROM roles WHERE tenant_id = ? ORDER BY authority_level DESC, name",
            (ctx.tenant_id,),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["permissions"] = self._role_permission_keys(ctx.tenant_id, row["id"])
            item["assigned_count"] = self.role_usage(ctx, row["id"])["assigned_count"]
            out.append(item)
        return out

    def permission_matrix(self, ctx: RequestContext, role_id: str) -> dict[str, Any]:
        """Matrix payload for the role editor UI (R40.3)."""
        self.authz.require_page(ctx, "Roles", "VIEW", target_type="role", target_id=role_id)
        role = self.get_role(ctx, role_id)
        granted = set(role["permissions"])
        rows = []
        for page in perms.PAGES:
            rows.append(
                {
                    "page": page.key,
                    "label": page.label,
                    "group": page.group,
                    "delete_semantics": page.delete_semantics,
                    "protected": page.protected,
                    "cells": {
                        verb: {
                            "available": verb in page.verbs,
                            "granted": f"{page.key}.{verb}" in granted,
                        }
                        for verb in perms.ALL_VERBS
                    },
                }
            )
        return {
            "role": {"id": role["id"], "code": role["code"], "name": role["name"]},
            "pages": rows,
            "actions": {
                group: [{**item, "granted": item["permission_key"] in granted} for item in items]
                for group, items in perms.action_groups().items()
            },
            "warnings": perms.combination_warnings(granted),
        }

    def _role_permission_keys(self, tenant_id: str, role_id: str) -> list[str]:
        rows = self.db.query(
            "SELECT permission_key FROM role_permissions "
            "WHERE tenant_id = ? AND role_id = ? AND granted = 1 ORDER BY permission_key",
            (tenant_id, role_id),
        )
        return [r["permission_key"] for r in rows]

    # ------------------------------------------------------------------ #
    # Staff
    # ------------------------------------------------------------------ #

    def invite_staff(
        self,
        ctx: RequestContext,
        *,
        email: str,
        first_name: str,
        last_name: str,
        organization_id: str,
        phone: str | None = None,
        employee_id: str | None = None,
        display_name: str | None = None,
        mfa_required: bool = False,
    ) -> dict[str, Any]:
        """Create an Invited staff account and return the single-use enrolment token (R38.4)."""
        self.authz.require_page(ctx, "Staff", "ADD")
        email_norm = (email or "").strip().lower()
        if "@" not in email_norm:
            raise ValidationError({"email": "Enter a valid email address."})
        if self.db.query_one(
            "SELECT 1 FROM staff WHERE tenant_id = ? AND email = ?", (ctx.tenant_id, email_norm)
        ):
            raise ConflictError(
                "A staff member with this email already exists.", details={"field": "email"}
            )
        staff_id = new_id("stf")
        token = new_secret(24)
        now = self.clock.now()
        with self.db.transaction():
            self.db.insert(
                "staff",
                {
                    "id": staff_id,
                    "tenant_id": ctx.tenant_id,
                    "organization_id": organization_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "display_name": display_name or f"{first_name} {last_name}".strip(),
                    "email": email_norm,
                    "phone": phone,
                    "employee_id": employee_id,
                    "status": "INVITED",
                    "mfa_required": 1 if mfa_required else 0,
                    "invite_token_hash": hash_secret(token),
                    "invite_expires_at": to_iso(add_minutes(now, 72 * 60)),
                    "perm_epoch": 1,
                    "created_at": to_iso(now),
                    "created_by": ctx.principal.id,
                },
            )
            self.audit.record(
                ctx,
                "STAFF_INVITE",
                target_type="staff",
                target_id=staff_id,
                new={"display_name": display_name or f"{first_name} {last_name}", "status": "INVITED"},
            )
        record = self.get_staff(ctx, staff_id)
        record["enrolment_token"] = token  # returned once, never stored in clear
        return record

    def complete_enrolment(
        self, ctx: RequestContext, *, staff_id: str, token: str, credential: str
    ) -> dict[str, Any]:
        """Consume an enrolment token and activate the account (R38.4)."""
        row = self.db.query_one(
            "SELECT * FROM staff WHERE id = ? AND tenant_id = ?", (staff_id, ctx.tenant_id)
        )
        staff = self.authz.assert_same_tenant(ctx, row, entity="staff", record_id=staff_id)
        if staff["status"] != "INVITED":
            raise ConflictError("This invitation has already been used.")
        if not staff["invite_token_hash"] or not verify_secret(token, staff["invite_token_hash"]):
            raise AuthenticationRequired()
        if staff["invite_expires_at"] and to_iso(self.clock.now()) > staff["invite_expires_at"]:
            raise ConflictError("This invitation has expired. Ask an administrator to resend it.")
        self._validate_credential(credential)
        with self.db.transaction():
            self.db.update(
                "staff",
                staff_id,
                {
                    "status": "ACTIVE",
                    "credential_hash": hash_secret(credential),
                    "invite_token_hash": None,
                    "invite_expires_at": None,
                    "updated_at": to_iso(self.clock.now()),
                },
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.with_principal(Principal(kind="STAFF", id=staff_id)),
                "STAFF_EDIT",
                target_type="staff",
                target_id=staff_id,
                previous={"status": "INVITED"},
                new={"status": "ACTIVE", "enrolment": "completed"},
            )
        return self.get_staff(ctx, staff_id)

    def update_staff(
        self, ctx: RequestContext, staff_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        self.authz.require_page(ctx, "Staff", "EDIT", target_type="staff", target_id=staff_id)
        self.authz.assert_no_forbidden_fields(ctx, changes, extra=("status", "credential_hash", "email"))
        before = self.authz.load_scoped(ctx, "staff", staff_id, entity="staff")
        allowed = {"first_name", "last_name", "display_name", "phone", "employee_id", "mfa_required"}
        payload = {k: v for k, v in changes.items() if k in allowed}
        if not payload:
            raise ValidationError({"changes": "No editable fields were supplied."})
        payload["updated_at"] = to_iso(self.clock.now())
        payload["updated_by"] = ctx.principal.id
        with self.db.transaction():
            self.db.update("staff", staff_id, payload, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx,
                "STAFF_EDIT",
                target_type="staff",
                target_id=staff_id,
                previous={k: before.get(k) for k in payload if k in before},
                new=payload,
            )
        return self.get_staff(ctx, staff_id)

    def set_staff_status(
        self,
        ctx: RequestContext,
        staff_id: str,
        status: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Activate, suspend or deactivate. Sessions die immediately on the latter two."""
        if status not in ("ACTIVE", "SUSPENDED", "INACTIVE"):
            raise ValidationError({"status": "Status must be ACTIVE, SUSPENDED or INACTIVE."})
        self.authz.require_page(ctx, "Staff", "EDIT", target_type="staff", target_id=staff_id)
        before = self.authz.load_scoped(ctx, "staff", staff_id, entity="staff")
        if status != "ACTIVE":
            self._assert_not_last_super_admin(ctx, staff_id)
        action = {
            "ACTIVE": "STAFF_REACTIVATE",
            "SUSPENDED": "STAFF_SUSPEND",
            "INACTIVE": "STAFF_DEACTIVATE",
        }[status]
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "staff",
                staff_id,
                {
                    "status": status,
                    "updated_at": now,
                    "updated_by": ctx.principal.id,
                    "deactivated_at": now if status == "INACTIVE" else None,
                    "perm_epoch": int(before.get("perm_epoch") or 1) + 1,
                },
                tenant_id=ctx.tenant_id,
            )
            revoked = 0
            if status != "ACTIVE":
                revoked = self.revoke_sessions(ctx, staff_id, reason=f"status:{status}")
            self.audit.record(
                ctx,
                action,
                target_type="staff",
                target_id=staff_id,
                previous={"status": before["status"]},
                new={"status": status, "sessions_revoked": revoked},
                reason=reason,
                severity="WARNING" if status != "ACTIVE" else "INFO",
            )
        return self.get_staff(ctx, staff_id)

    def delete_staff(self, ctx: RequestContext, staff_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """DELETE on a staff record. Maps to deactivation whenever history exists (R38.7)."""
        self.authz.require_page(ctx, "Staff", "DELETE", target_type="staff", target_id=staff_id)
        staff = self.authz.load_scoped(ctx, "staff", staff_id, entity="staff")
        has_history = self.staff_has_history(ctx, staff_id)
        if has_history:
            result = self.set_staff_status(ctx, staff_id, "INACTIVE", reason=reason)
            return {
                "requested": "DELETE",
                "performed": "DEACTIVATE",
                "reason": "This account has transaction or audit history, which is retained.",
                "staff": result,
            }
        self._assert_not_last_super_admin(ctx, staff_id)
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM role_assignments WHERE tenant_id = ? AND staff_id = ?",
                (ctx.tenant_id, staff_id),
            )
            self.db.execute(
                "DELETE FROM auth_sessions WHERE tenant_id = ? AND staff_id = ?", (ctx.tenant_id, staff_id)
            )
            self.db.execute("DELETE FROM staff WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, staff_id))
            self.audit.record(
                ctx,
                "STAFF_DEACTIVATE",
                target_type="staff",
                target_id=staff_id,
                previous={"display_name": staff["display_name"], "email_present": True},
                new={"performed": "DELETE", "had_history": False},
                reason=reason,
                severity="WARNING",
            )
        return {"requested": "DELETE", "performed": "DELETE", "staff_id": staff_id}

    def staff_has_history(self, ctx: RequestContext, staff_id: str) -> bool:
        for table, column in _HISTORY_SOURCES:
            found = self.db.query_one(
                f"SELECT 1 FROM {table} WHERE tenant_id = ? AND {column} = ? LIMIT 1",
                (ctx.tenant_id, staff_id),
            )
            if found is not None:
                return True
        return False

    def get_staff(self, ctx: RequestContext, staff_id: str, *, mask: bool = True) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "staff", staff_id, entity="staff")
        for secret in ("credential_hash", "invite_token_hash"):
            record.pop(secret, None)
        record["roles"] = [
            dict(r)
            for r in self.db.query(
                """
                SELECT ra.id AS assignment_id, ra.scope_type, ra.scope_id, ra.operating_point,
                       r.code AS role_code, r.name AS role_name, r.authority_level
                FROM role_assignments ra
                JOIN roles r ON r.id = ra.role_id AND r.tenant_id = ra.tenant_id
                WHERE ra.tenant_id = ? AND ra.staff_id = ? AND ra.status = 'ACTIVE' AND ra.revoked_at IS NULL
                """,
                (ctx.tenant_id, staff_id),
            )
        ]
        if mask:
            # Staff contact data is personal data too (R38.9).
            return self.authz.mask_record(ctx, record, entity="staff")
        return record

    def list_staff(self, ctx: RequestContext, *, status: str | None = None) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Staff", "VIEW")
        sql = "SELECT * FROM staff WHERE tenant_id = ?"
        params: list[Any] = [ctx.tenant_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY display_name"
        out = []
        for row in self.db.query(sql, params):
            record = dict(row)
            for secret in ("credential_hash", "invite_token_hash"):
                record.pop(secret, None)
            out.append(self.authz.mask_record(ctx, record, entity="staff", audit_pii_access=False))
        return out

    # ------------------------------------------------------------------ #
    # Role assignment (R43)
    # ------------------------------------------------------------------ #

    def assign_role(
        self,
        ctx: RequestContext,
        *,
        staff_id: str,
        role_id: str,
        scope_type: str = "VENUE",
        scope_id: str | None = None,
        operating_point: str | None = None,
        reason: str | None = None,
        approver_id: str | None = None,
    ) -> dict[str, Any]:
        """Assign ``(staff, role, scope)`` — the triple mandated by R43.1."""
        if scope_type not in ("TENANT", "ORGANIZATION", "VENUE", "OPERATING_POINT"):
            raise ValidationError({"scope_type": "Scope must be TENANT, ORGANIZATION, VENUE or OPERATING_POINT."})
        if scope_type != "TENANT" and not scope_id:
            raise ValidationError({"scope_id": "A scope identifier is required for this scope type."})
        self.authz.require_page(ctx, "Staff", "EDIT", target_type="staff", target_id=staff_id)
        self.authz.require_action(ctx, "MANAGE_PERMISSION", target_type="staff", target_id=staff_id)
        self.authz.assert_not_self(ctx, staff_id, what="role assignment")

        staff = self.authz.load_scoped(ctx, "staff", staff_id, entity="staff")
        role = self.get_role(ctx, role_id)
        scope_venue = scope_id if scope_type in ("VENUE", "OPERATING_POINT") else None
        self.authz.assert_may_grant(
            ctx,
            permission_keys=role["permissions"],
            target_authority_level=int(role["authority_level"]),
            scope_venue_id=scope_venue,
        )
        if role["code"] in perms.HIGH_AUTHORITY_ROLE_CODES:
            self._require_second_approval(ctx, role["code"], approver_id)

        assignment_id = new_id("rsa")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            existing = self.db.query_one(
                """
                SELECT id, status FROM role_assignments
                WHERE tenant_id = ? AND staff_id = ? AND role_id = ? AND scope_type = ?
                  AND IFNULL(scope_id,'') = IFNULL(?,'') AND IFNULL(operating_point,'') = IFNULL(?,'')
                """,
                (ctx.tenant_id, staff_id, role_id, scope_type, scope_id, operating_point),
            )
            if existing is not None:
                if existing["status"] == "ACTIVE":
                    raise ConflictError("This role is already assigned at that scope.")
                assignment_id = existing["id"]
                self.db.update(
                    "role_assignments",
                    assignment_id,
                    {"status": "ACTIVE", "revoked_at": None, "created_at": now, "created_by": ctx.principal.id},
                    tenant_id=ctx.tenant_id,
                )
            else:
                self.db.insert(
                    "role_assignments",
                    {
                        "id": assignment_id,
                        "tenant_id": ctx.tenant_id,
                        "staff_id": staff_id,
                        "role_id": role_id,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "operating_point": operating_point,
                        "status": "ACTIVE",
                        "created_at": now,
                        "created_by": ctx.principal.id,
                    },
                )
            self.db.update(
                "staff",
                staff_id,
                {"perm_epoch": int(staff.get("perm_epoch") or 1) + 1, "updated_at": now},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx,
                "ROLE_ASSIGN",
                target_type="staff",
                target_id=staff_id,
                new={
                    "role_code": role["code"],
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "operating_point": operating_point,
                    "approver_id": approver_id,
                },
                reason=reason,
                severity="WARNING",
            )
        return {"assignment_id": assignment_id, "staff_id": staff_id, "role_code": role["code"]}

    def remove_role_assignment(
        self, ctx: RequestContext, assignment_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        self.authz.require_action(ctx, "MANAGE_PERMISSION", target_type="role_assignment", target_id=assignment_id)
        assignment = self.authz.load_scoped(ctx, "role_assignments", assignment_id, entity="role_assignment")
        self.authz.require_page(ctx, "Staff", "EDIT", target_type="staff", target_id=assignment["staff_id"])
        self.authz.assert_not_self(ctx, assignment["staff_id"], what="role assignment")
        role = self.get_role(ctx, assignment["role_id"])
        if role["code"] == perms.SUPER_ADMIN_CODE:
            self._assert_not_last_super_admin(ctx, assignment["staff_id"])
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "role_assignments",
                assignment_id,
                {"status": "REVOKED", "revoked_at": now},
                tenant_id=ctx.tenant_id,
            )
            staff = self.db.query_one(
                "SELECT perm_epoch FROM staff WHERE id = ? AND tenant_id = ?",
                (assignment["staff_id"], ctx.tenant_id),
            )
            self.db.update(
                "staff",
                assignment["staff_id"],
                {"perm_epoch": int(staff["perm_epoch"] or 1) + 1, "updated_at": now},
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx,
                "ROLE_REMOVE",
                target_type="staff",
                target_id=assignment["staff_id"],
                previous={
                    "role_code": role["code"],
                    "scope_type": assignment["scope_type"],
                    "scope_id": assignment["scope_id"],
                },
                reason=reason,
                severity="WARNING",
            )
        return {"assignment_id": assignment_id, "removed": True}

    # ------------------------------------------------------------------ #
    # Authentication (R73.1, R73.3)
    # ------------------------------------------------------------------ #

    def login(
        self,
        ctx: RequestContext,
        *,
        email: str,
        credential: str,
        mfa_code: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Authenticate a staff member and open a bound session."""
        email_norm = (email or "").strip().lower()
        row = self.db.query_one(
            "SELECT * FROM staff WHERE tenant_id = ? AND email = ?", (ctx.tenant_id, email_norm)
        )
        now = self.clock.now()
        if row is None:
            self.audit.security(ctx, "LOGIN_FAILED", reason="unknown_principal", detail={"channel": ctx.channel})
            raise AuthenticationRequired()
        staff = dict(row)
        if staff["locked_until"] and to_iso(now) < staff["locked_until"]:
            self.audit.security(
                ctx, "LOGIN_FAILED", target_type="staff", target_id=staff["id"], reason="locked"
            )
            raise RateLimited(LOCKOUT_MINUTES * 60)
        if staff["status"] != "ACTIVE" or not staff["credential_hash"]:
            self.audit.security(
                ctx, "LOGIN_FAILED", target_type="staff", target_id=staff["id"], reason="inactive"
            )
            raise AuthenticationRequired()
        if not verify_secret(credential, staff["credential_hash"]):
            failures = int(staff["failed_logins"] or 0) + 1
            locked = failures >= MAX_FAILED_LOGINS
            with self.db.transaction():
                self.db.update(
                    "staff",
                    staff["id"],
                    {
                        "failed_logins": failures,
                        "locked_until": to_iso(add_minutes(now, LOCKOUT_MINUTES)) if locked else None,
                    },
                    tenant_id=ctx.tenant_id,
                )
                self.audit.security(
                    ctx,
                    "LOGIN_FAILED",
                    target_type="staff",
                    target_id=staff["id"],
                    reason="bad_credential",
                    detail={"failures": failures, "locked": locked},
                )
            raise AuthenticationRequired()

        effective = self.authz.effective_permissions(
            ctx.with_principal(Principal(kind="STAFF", id=staff["id"])).for_venue(None)
        )
        mfa_needed = bool(staff["mfa_required"]) or any(
            code in perms.HIGH_AUTHORITY_ROLE_CODES for code in effective.roles
        )
        if mfa_needed and not (mfa_code or "").strip():
            # R73.2: MFA is mandatory for Platform Super Admin and Organization Admin.
            raise ValidationError(
                {"mfa_code": "Enter your authenticator code."},
                message="Multi-factor authentication is required for this account.",
                code="mfa_required",
            )

        session_id = new_id("sess")
        token = new_secret(32)
        with self.db.transaction():
            idle = self.config.get_int(ctx, "auth.idle_timeout_minutes", default=SESSION_IDLE_MINUTES)
            absolute = self.config.get_int(
                ctx, "auth.absolute_timeout_minutes", default=SESSION_ABSOLUTE_MINUTES
            )
            self.db.insert(
                "auth_sessions",
                {
                    "id": session_id,
                    "tenant_id": ctx.tenant_id,
                    "staff_id": staff["id"],
                    "token_hash": hash_secret(token),
                    "channel": channel or ctx.channel,
                    "ip_address": ctx.ip_address,
                    "perm_epoch": int(staff["perm_epoch"] or 1),
                    "issued_at": to_iso(now),
                    "idle_expires_at": to_iso(add_minutes(now, idle)),
                    "absolute_expires_at": to_iso(add_minutes(now, absolute)),
                    "last_seen_at": to_iso(now),
                },
            )
            self.db.update(
                "staff",
                staff["id"],
                {
                    "failed_logins": 0,
                    "locked_until": None,
                    "last_login_at": to_iso(now),
                    "last_login_channel": channel or ctx.channel,
                    "last_login_ip": ctx.ip_address,
                },
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.with_principal(Principal(kind="STAFF", id=staff["id"], session_id=session_id)),
                "LOGIN",
                target_type="staff",
                target_id=staff["id"],
                new={"channel": channel or ctx.channel, "mfa": mfa_needed},
            )
        return {
            "session_id": session_id,
            "token": token,
            "staff_id": staff["id"],
            "display_name": staff["display_name"],
            "roles": list(effective.roles),
            "authority_level": effective.authority_level,
            "idle_expires_at": to_iso(add_minutes(now, SESSION_IDLE_MINUTES)),
        }

    def authenticate_token(self, ctx: RequestContext, token: str) -> Principal:
        """Resolve a session token to a principal, enforcing timeouts and revocation."""
        now = to_iso(self.clock.now())
        rows = self.db.query(
            """
            SELECT s.*, st.status AS staff_status, st.perm_epoch AS staff_epoch,
                   st.display_name, st.organization_id
            FROM auth_sessions s
            JOIN staff st ON st.id = s.staff_id AND st.tenant_id = s.tenant_id
            WHERE s.tenant_id = ? AND s.revoked_at IS NULL
              AND s.idle_expires_at > ? AND s.absolute_expires_at > ?
            """,
            (ctx.tenant_id, now, now),
        )
        for row in rows:
            if not verify_secret(token, row["token_hash"]):
                continue
            if row["staff_status"] != "ACTIVE":
                raise AuthenticationRequired()
            effective = self.authz.effective_permissions(
                ctx.with_principal(Principal(kind="STAFF", id=row["staff_id"])).for_venue(None)
            )
            idle = self.config.get_int(ctx, "auth.idle_timeout_minutes", default=SESSION_IDLE_MINUTES)
            self.db.update(
                "auth_sessions",
                row["id"],
                {"last_seen_at": now, "idle_expires_at": to_iso(add_minutes(self.clock.now(), idle))},
                tenant_id=ctx.tenant_id,
            )
            return Principal(
                kind="STAFF",
                id=row["staff_id"],
                display_name=row["display_name"],
                organization_id=row["organization_id"],
                authority_level=effective.authority_level,
                perm_epoch=int(row["staff_epoch"] or 1),
                session_id=row["id"],
            )
        raise AuthenticationRequired()

    def revoke_sessions(self, ctx: RequestContext, staff_id: str, *, reason: str = "manual") -> int:
        """Kill every live session for a staff member (R38.5)."""
        now = to_iso(self.clock.now())
        count = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM auth_sessions WHERE tenant_id = ? AND staff_id = ? AND revoked_at IS NULL",
                (ctx.tenant_id, staff_id),
                default=0,
            )
        )
        self.db.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE tenant_id = ? AND staff_id = ? AND revoked_at IS NULL",
            (now, ctx.tenant_id, staff_id),
        )
        if count:
            self.audit.security(
                ctx,
                "SESSION_TERMINATED",
                target_type="staff",
                target_id=staff_id,
                reason=reason,
                detail={"sessions": count},
            )
        return count

    def logout(self, ctx: RequestContext) -> dict[str, Any]:
        session_id = ctx.principal.session_id
        if not session_id:
            return {"logged_out": False}
        self.db.update(
            "auth_sessions", session_id, {"revoked_at": to_iso(self.clock.now())}, tenant_id=ctx.tenant_id
        )
        self.audit.record(ctx, "LOGOUT", target_type="staff", target_id=ctx.principal.id)
        return {"logged_out": True}

    # ------------------------------------------------------------------ #
    # Session profile (settings/reports spec §3)
    # ------------------------------------------------------------------ #

    def session_profile(self, ctx: RequestContext, *, language: str | None = None) -> dict[str, Any]:
        """Everything the back office needs to draw itself for this principal.

        §3 lists user, tenant, organization, assigned venues, role assignments,
        permissions, scope and preferences. Returning them in one call is not a
        convenience: a client that fetches identity and authority separately can
        render a menu from stale permissions, and the window between the two calls
        is exactly where a revoked permission keeps working.

        ``permissions`` is the raw key set. The client uses it for
        ``can(page, verb)`` (§48) so no screen ever branches on a role name — but it
        is advisory. Every one of those pages re-checks the same key server-side, so
        a tampered response buys nothing (§46, §75).
        """
        staff_id = self.authz.require_authenticated(ctx)
        lang = language or ctx.language or "en"
        row = self.db.query_one(
            "SELECT * FROM staff WHERE id = ? AND tenant_id = ?", (staff_id, ctx.tenant_id)
        )
        if row is None:
            raise AuthenticationRequired()
        staff = dict(row)

        # Resolved without a venue filter, so the answer describes the account
        # rather than whichever venue this request happened to name.
        overall = self.authz.effective_permissions(ctx.for_venue(None))
        scoped = self.authz.effective_permissions(ctx)

        tenant = self.db.query_one(
            "SELECT id, code, name, status, default_language, languages_json FROM tenants WHERE id = ?",
            (ctx.tenant_id,),
        )
        organization = None
        if staff.get("organization_id"):
            organization = self.db.query_one(
                "SELECT id, code, name FROM organizations WHERE id = ? AND tenant_id = ?",
                (staff["organization_id"], ctx.tenant_id),
            )

        venue_ids = self.authz.scoped_venue_ids(ctx)
        if venue_ids is None:
            venue_rows = self.db.query(
                "SELECT id, code, name_json, timezone, currency FROM venues "
                "WHERE tenant_id = ? AND status = 'ACTIVE' ORDER BY code",
                (ctx.tenant_id,),
            )
        elif venue_ids:
            placeholders = ",".join("?" for _ in venue_ids)
            venue_rows = self.db.query(
                f"SELECT id, code, name_json, timezone, currency FROM venues "
                f"WHERE tenant_id = ? AND id IN ({placeholders}) ORDER BY code",
                (ctx.tenant_id, *venue_ids),
            )
        else:
            venue_rows = []

        assignments = self.db.query(
            """
            SELECT ra.id, ra.scope_type, ra.scope_id, ra.operating_point, ra.status,
                   r.code AS role_code, r.name AS role_name, r.authority_level
            FROM role_assignments ra
            JOIN roles r ON r.id = ra.role_id AND r.tenant_id = ra.tenant_id
            WHERE ra.tenant_id = ? AND ra.staff_id = ? AND ra.status = 'ACTIVE'
              AND ra.revoked_at IS NULL AND r.status = 'ACTIVE'
            ORDER BY r.authority_level DESC
            """,
            (ctx.tenant_id, staff_id),
        )
        session: dict[str, Any] = {}
        if ctx.principal.session_id:
            row = self.db.query_one(
                "SELECT id, channel, issued_at, idle_expires_at, absolute_expires_at, last_seen_at "
                "FROM auth_sessions WHERE id = ? AND tenant_id = ?",
                (ctx.principal.session_id, ctx.tenant_id),
            )
            session = dict(row) if row is not None else {}

        return {
            "staff": {
                "id": staff["id"],
                "email": staff["email"],
                "display_name": staff["display_name"],
                "status": staff["status"],
                "employee_id": staff.get("employee_id"),
                "mfa_required": bool(staff.get("mfa_required")),
                "mfa_enrolled": bool(staff.get("mfa_enrolled")),
                "last_login_at": staff.get("last_login_at"),
                "last_login_channel": staff.get("last_login_channel"),
                # The language this response was rendered in, so the client can
                # confirm what it is showing rather than assume.
                "language": lang,
            },
            "tenant": (
                {
                    "id": tenant["id"],
                    "code": tenant["code"],
                    "name": tenant["name"],
                    "status": tenant["status"],
                    "default_language": tenant["default_language"],
                    "languages": decode(tenant["languages_json"], ["en"]),
                }
                if tenant
                else None
            ),
            "organization": dict(organization) if organization else None,
            "venues": [
                {
                    "id": v["id"],
                    "code": v["code"],
                    "name": decode(v["name_json"], {}),
                    "timezone": v["timezone"],
                    "currency": v["currency"],
                }
                for v in venue_rows
            ],
            "roles": [
                {
                    "code": a["role_code"],
                    "name": a["role_name"],
                    "authority_level": a["authority_level"],
                    "scope_type": a["scope_type"],
                    "scope_id": a["scope_id"],
                    "operating_point": a["operating_point"],
                }
                for a in assignments
            ],
            "authority_level": overall.authority_level,
            "scope": {
                "tenant_wide": overall.tenant_wide,
                # ``None`` means every venue in the tenant, which is not the same as
                # an empty list and must not be flattened into one.
                "venue_ids": venue_ids,
                "organization_ids": sorted(overall.organization_ids),
                "operating_points": sorted(overall.operating_points),
                "current_venue_id": ctx.venue_id,
            },
            "permissions": sorted(scoped.granted),
            "navigation": self.authz.navigation(ctx, language=lang),
            "settings": self.authz.settings_home(ctx, language=lang),
            "summary": self.authz.grant_summary(scoped.granted, language=lang),
            "session": {
                "id": ctx.principal.session_id,
                "channel": session.get("channel"),
                "issued_at": session.get("issued_at"),
                # The client shows "Your session has expired. Please sign in again."
                # (§57) rather than letting a request fail obscurely, so it needs to
                # know when that will be.
                "idle_expires_at": session.get("idle_expires_at"),
                "absolute_expires_at": session.get("absolute_expires_at"),
            },
            "permissions_changed": self.authz.permission_changed(ctx),
            "warnings": overall.warnings,
        }

    def reset_credential(
        self, ctx: RequestContext, staff_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Issue a fresh enrolment token and revoke sessions (R38.3)."""
        self.authz.require_page(ctx, "Staff", "EDIT", target_type="staff", target_id=staff_id)
        staff = self.authz.load_scoped(ctx, "staff", staff_id, entity="staff")
        token = new_secret(24)
        now = self.clock.now()
        with self.db.transaction():
            self.db.update(
                "staff",
                staff_id,
                {
                    "credential_hash": None,
                    "status": "INVITED",
                    "invite_token_hash": hash_secret(token),
                    "invite_expires_at": to_iso(add_minutes(now, 24 * 60)),
                    "failed_logins": 0,
                    "locked_until": None,
                    "perm_epoch": int(staff.get("perm_epoch") or 1) + 1,
                    "updated_at": to_iso(now),
                    "updated_by": ctx.principal.id,
                },
                tenant_id=ctx.tenant_id,
            )
            self.revoke_sessions(ctx, staff_id, reason="credential_reset")
            self.audit.record(
                ctx,
                "CREDENTIAL_RESET",
                target_type="staff",
                target_id=staff_id,
                reason=reason,
                severity="WARNING",
            )
        return {"staff_id": staff_id, "enrolment_token": token}

    # ------------------------------------------------------------------ #
    # Self-service password reset (R73.1, settings spec §1 "Recover / Reset")
    # ------------------------------------------------------------------ #

    def request_password_reset(self, ctx: RequestContext, *, email: str) -> dict[str, Any]:
        """Begin a self-service reset for a locked-out staff member.

        Deliberately unauthenticated: the whole point is to help someone who cannot
        sign in. It is **enumeration-safe** — the response is identical whether or not
        the email exists, so it cannot be used to discover which emails are staff
        accounts. A one-time token is minted only when the email matches an account,
        stored hashed, and returned to the caller (the API records it in the mailbox
        for local dev and would email it in production). It does not change the
        account's status, so an active user is not locked out by someone requesting a
        reset for their address.
        """
        email_norm = (email or "").strip().lower()
        generic = {
            "requested": True,
            "message": "If that email belongs to a staff account, a reset link has been sent.",
        }
        row = self.db.query_one(
            "SELECT id, status, perm_epoch FROM staff WHERE tenant_id = ? AND email = ?",
            (ctx.tenant_id, email_norm),
        )
        if row is None:
            # No account: same response, no token, nothing recorded.
            self.audit.security(ctx, "PASSWORD_RESET_REQUESTED", reason="unknown_email")
            return generic
        token = new_secret(24)
        now = self.clock.now()
        with self.db.transaction():
            self.db.update(
                "staff",
                row["id"],
                {
                    "invite_token_hash": hash_secret(token),
                    "invite_expires_at": to_iso(add_minutes(now, 60)),
                    "updated_at": to_iso(now),
                },
                tenant_id=ctx.tenant_id,
            )
            self.audit.security(
                ctx, "PASSWORD_RESET_REQUESTED", target_type="staff", target_id=row["id"]
            )
        # The token is returned so the caller (API) can deliver it; it is never stored
        # or logged in clear.
        return {**generic, "staff_id": row["id"], "reset_token": token, "email": email_norm}

    def complete_password_reset(
        self, ctx: RequestContext, *, email: str, token: str, credential: str
    ) -> dict[str, Any]:
        """Consume a reset token and set a new password, then reactivate the account.

        Verifies the token against the hash and its expiry, applies the password
        policy, sets the new credential, clears the token, unlocks the account and
        bumps the permission epoch so any stale session is invalidated. A wrong or
        expired token is refused without revealing whether the email exists.
        """
        email_norm = (email or "").strip().lower()
        row = self.db.query_one(
            "SELECT * FROM staff WHERE tenant_id = ? AND email = ?", (ctx.tenant_id, email_norm)
        )
        now = self.clock.now()
        if (
            row is None
            or not row["invite_token_hash"]
            or not verify_secret(token, row["invite_token_hash"])
            or (row["invite_expires_at"] and to_iso(now) > row["invite_expires_at"])
        ):
            self.audit.security(ctx, "PASSWORD_RESET_FAILED", reason="bad_or_expired_token")
            raise AuthenticationRequired()
        self._validate_credential(credential)
        with self.db.transaction():
            self.db.update(
                "staff",
                row["id"],
                {
                    "status": "ACTIVE",
                    "credential_hash": hash_secret(credential),
                    "invite_token_hash": None,
                    "invite_expires_at": None,
                    "failed_logins": 0,
                    "locked_until": None,
                    "perm_epoch": int(row["perm_epoch"] or 1) + 1,
                    "updated_at": to_iso(now),
                },
                tenant_id=ctx.tenant_id,
            )
            self.revoke_sessions(
                ctx.with_principal(Principal(kind="STAFF", id=row["id"])), row["id"], reason="password_reset"
            )
            self.audit.security(
                ctx, "PASSWORD_RESET_COMPLETED", target_type="staff", target_id=row["id"]
            )
        return {"reset": True, "staff_id": row["id"]}

    # ------------------------------------------------------------------ #
    # Guards
    # ------------------------------------------------------------------ #

    def _validate_credential(self, credential: str) -> None:
        """Password policy (R73.1). Deliberately about length and variety, not expiry."""
        problems: list[str] = []
        if len(credential or "") < 12:
            problems.append("at least 12 characters")
        if not any(c.isupper() for c in credential or ""):
            problems.append("an uppercase letter")
        if not any(c.islower() for c in credential or ""):
            problems.append("a lowercase letter")
        if not any(c.isdigit() for c in credential or ""):
            problems.append("a number")
        if problems:
            raise ValidationError(
                {"credential": "Your password needs " + ", ".join(problems) + "."},
                message="Please choose a stronger password.",
            )

    def _assert_not_last_super_admin(self, ctx: RequestContext, staff_id: str) -> None:
        """R44.5 — the platform must never be left without a super admin."""
        holds = self.db.query_one(
            """
            SELECT 1 FROM role_assignments ra
            JOIN roles r ON r.id = ra.role_id AND r.tenant_id = ra.tenant_id
            WHERE ra.tenant_id = ? AND ra.staff_id = ? AND ra.status = 'ACTIVE'
              AND ra.revoked_at IS NULL AND r.code = ?
            """,
            (ctx.tenant_id, staff_id, perms.SUPER_ADMIN_CODE),
        )
        if holds is None:
            return
        remaining = int(
            self.db.scalar(
                """
                SELECT COUNT(DISTINCT ra.staff_id) FROM role_assignments ra
                JOIN roles r ON r.id = ra.role_id AND r.tenant_id = ra.tenant_id
                JOIN staff s ON s.id = ra.staff_id AND s.tenant_id = ra.tenant_id
                WHERE ra.tenant_id = ? AND ra.status = 'ACTIVE' AND ra.revoked_at IS NULL
                  AND r.code = ? AND s.status = 'ACTIVE' AND ra.staff_id <> ?
                """,
                (ctx.tenant_id, perms.SUPER_ADMIN_CODE, staff_id),
                default=0,
            )
        )
        if remaining == 0:
            raise ConflictError(
                "This is the last active Platform Super Admin. Assign the role to "
                "another active staff member before removing it from this account.",
                code="last_super_admin",
                details={"role": perms.SUPER_ADMIN_CODE},
            )

    def _assert_not_last_super_admin_role(self, ctx: RequestContext, role: dict[str, Any]) -> None:
        if role.get("code") != perms.SUPER_ADMIN_CODE:
            return
        raise ConflictError(
            "The Platform Super Admin role cannot be deactivated or deleted.",
            code="last_super_admin",
            details={"role": perms.SUPER_ADMIN_CODE},
        )

    def _require_second_approval(
        self, ctx: RequestContext, role_code: str, approver_id: str | None
    ) -> None:
        """R44.10 — high-authority assignments may require a second approver."""
        required = self.config.get_bool(ctx, "authz.require_second_approval_for_high_authority", default=False)
        if not required:
            return
        if not approver_id:
            raise ValidationError(
                {"approver_id": "A second approver holding APPROVE is required."},
                message=f"Assigning {role_code} requires a second approval.",
            )
        if approver_id == ctx.principal.id:
            raise ValidationError(
                {"approver_id": "The approver must be a different staff member."},
                message="Self-approval is not permitted.",
            )
        approver = self.db.query_one(
            "SELECT id, status FROM staff WHERE id = ? AND tenant_id = ?", (approver_id, ctx.tenant_id)
        )
        if approver is None or approver["status"] != "ACTIVE":
            raise NotFound(details={"entity": "approver"})
        approver_ctx = ctx.with_principal(Principal(kind="STAFF", id=approver_id))
        if not self.authz.can_action(approver_ctx.for_venue(ctx.venue_id), "APPROVE"):
            raise ValidationError(
                {"approver_id": "The nominated approver does not hold APPROVE."},
                message="The nominated approver cannot authorize this change.",
            )
        self.audit.record(
            ctx,
            "APPROVAL_GRANTED",
            target_type="role_assignment",
            target_id=role_code,
            new={"approver_id": approver_id, "requested_by": ctx.principal.id},
            severity="WARNING",
        )

    def _bump_epoch_for_role(self, ctx: RequestContext, role_id: str) -> int:
        """Force re-evaluation for everyone holding a changed role (R44.7)."""
        rows = self.db.query(
            "SELECT DISTINCT staff_id FROM role_assignments "
            "WHERE tenant_id = ? AND role_id = ? AND status = 'ACTIVE'",
            (ctx.tenant_id, role_id),
        )
        now = to_iso(self.clock.now())
        for row in rows:
            self.db.execute(
                "UPDATE staff SET perm_epoch = perm_epoch + 1, updated_at = ? WHERE tenant_id = ? AND id = ?",
                (now, ctx.tenant_id, row["staff_id"]),
            )
        return len(rows)


__all__ = ["StaffService"]
