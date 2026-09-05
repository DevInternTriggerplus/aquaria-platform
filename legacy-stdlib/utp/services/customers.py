"""Customer records, PII access control and retention.

Three requirements shape the whole module:

* **R12.2** — personal data is not persisted until the required consent exists.
  :meth:`CustomerService.upsert` takes ``consent_record_id`` as a *mandatory*
  argument and verifies the record grants the required item. There is no code path
  that writes ``customer_pii`` without one.
* **R12.23** — data minimization. A field absent from
  :data:`~utp.services.consent.FIELD_PURPOSES` is rejected, so nobody can quietly
  add a field with no documented purpose.
* **R12.24** — personal data is split into ``customers`` (non-identifying
  operational row) and ``customer_pii`` (the identifying values). Reads go through
  :meth:`get`, which masks unless the principal holds ``VIEW_PII`` and audits every
  unmasked read.

Erasure follows R12.22: identifying values are removed or irreversibly
anonymized, while the financial and audit records that the law requires are
retained, with the retention justification recorded.
"""

from __future__ import annotations

from typing import Any

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode, encode
from ..core.errors import ConflictError, ConsentRequired, ValidationError
from ..core.ids import hash_identifier, new_id
from .authz import AuthorizationService
from .consent import FIELD_PURPOSES, REQUIRED_ITEM_CODES, ConsentService

#: Columns of ``customer_pii`` that hold identifying values.
_PII_COLUMNS: tuple[str, ...] = ("email", "full_name", "phone")


class CustomerService:
    """Customer identity, PII access and retention."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
        consent: ConsentService | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config
        self.consent = consent

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def upsert(
        self,
        ctx: RequestContext,
        *,
        consent_record_id: str,
        email: str,
        full_name: str | None = None,
        phone: str | None = None,
        language: str | None = None,
        extra: dict[str, Any] | None = None,
        is_minor: bool = False,
    ) -> dict[str, Any]:
        """Create or update a customer, gated on a valid consent record (R12.2)."""
        email_norm = (email or "").strip().lower()
        if "@" not in email_norm:
            raise ValidationError({"email": "Enter a valid email address."})
        consent_record = self._verify_consent(ctx, consent_record_id, email_norm)

        extra = dict(extra or {})
        self._assert_documented_fields(extra)

        email_hash = hash_identifier(email_norm)
        now = to_iso(self.clock.now())
        existing = self.db.query_one(
            "SELECT * FROM customers WHERE tenant_id = ? AND email_hash = ?", (ctx.tenant_id, email_hash)
        )
        items = consent_record["items"] or {}
        flags = {
            "marketing_opt_in": 1 if items.get("MARKETING") else 0,
            "analytics_opt_in": 1 if items.get("ANALYTICS") else 0,
            "partner_share_opt_in": 1 if items.get("PARTNER_SHARING") else 0,
        }
        with self.db.transaction():
            if existing is None:
                customer_id = new_id("cus")
                self.db.insert(
                    "customers",
                    {
                        "id": customer_id,
                        "tenant_id": ctx.tenant_id,
                        "email_hash": email_hash,
                        "language": language or ctx.language,
                        "is_minor": 1 if is_minor else 0,
                        **flags,
                        "created_at": now,
                    },
                )
            else:
                customer_id = existing["id"]
                self.db.update(
                    "customers",
                    customer_id,
                    {
                        "language": language or existing["language"],
                        "is_minor": 1 if is_minor else int(existing["is_minor"] or 0),
                        **flags,
                        "updated_at": now,
                        "anonymized_at": None,
                    },
                    tenant_id=ctx.tenant_id,
                )
            pii_row = self.db.query_one(
                "SELECT customer_id FROM customer_pii WHERE customer_id = ? AND tenant_id = ?",
                (customer_id, ctx.tenant_id),
            )
            payload = {
                "email": email_norm,
                "full_name": full_name,
                "phone": phone,
                "extra_json": extra,
                "updated_at": now,
            }
            if pii_row is None:
                self.db.insert(
                    "customer_pii", {"customer_id": customer_id, "tenant_id": ctx.tenant_id, **payload}
                )
            else:
                # ``customer_pii`` is keyed by customer_id, not ``id``, so the generic
                # ``Database.update`` helper does not apply.
                assignments = ", ".join(f"{key} = :{key}" for key in payload)
                params = {key: encode(value) for key, value in payload.items()}
                params["cid"] = customer_id
                params["tid"] = ctx.tenant_id
                self.db.execute(
                    f"UPDATE customer_pii SET {assignments} WHERE customer_id = :cid AND tenant_id = :tid",
                    params,
                )
        # No back-link is written onto the consent record: it is append-only, and
        # for a first-time guest the customer legitimately does not exist yet when
        # consent is captured (R12.2). Both rows carry the same ``contact_hash``,
        # so the association is derivable in the immutable direction, and the
        # booking additionally stores ``consent_record_id``.
        return self.get(ctx, customer_id)

    def for_consent_record(self, ctx: RequestContext, consent_record_id: str) -> dict[str, Any] | None:
        """Resolve the customer a consent record belongs to, via ``contact_hash``."""
        contact_hash = self.db.scalar(
            "SELECT contact_hash FROM consent_records WHERE id = ? AND tenant_id = ?",
            (consent_record_id, ctx.tenant_id),
        )
        if not contact_hash:
            return None
        row = self.db.query_one(
            "SELECT id FROM customers WHERE tenant_id = ? AND email_hash = ?",
            (ctx.tenant_id, contact_hash),
        )
        return self.get(ctx, row["id"]) if row else None

    def id_for_contact_hash(self, ctx: RequestContext, contact_hash: str) -> str | None:
        return self.db.scalar(
            "SELECT id FROM customers WHERE tenant_id = ? AND email_hash = ?",
            (ctx.tenant_id, contact_hash),
        )

    def _verify_consent(self, ctx: RequestContext, consent_record_id: str, email: str) -> dict[str, Any]:
        """The consent must exist, be this tenant's, and cover this contact."""
        row = self.db.query_one(
            "SELECT * FROM consent_records WHERE id = ? AND tenant_id = ?",
            (consent_record_id, ctx.tenant_id),
        )
        record = self.authz.assert_same_tenant(
            ctx, row, entity="consent_record", record_id=consent_record_id
        )
        record["items"] = decode(record.pop("items_json"), {})
        if record["contact_hash"] != hash_identifier(email):
            raise ValidationError(
                {"consent_record_id": "The consent record does not match this contact."},
                message="We could not match your consent to these details.",
            )
        missing = sorted(code for code in REQUIRED_ITEM_CODES if not record["items"].get(code))
        if missing:
            raise ConsentRequired(details={"missing_required_items": missing, "personal_data_retained": False})
        return record

    def _assert_documented_fields(self, extra: dict[str, Any]) -> None:
        """R12.23 — every collected field must have a documented purpose."""
        undocumented = sorted(set(extra) - set(FIELD_PURPOSES))
        if undocumented:
            raise ValidationError(
                {"extra": f"No documented purpose for: {', '.join(undocumented)}."},
                message="These fields cannot be collected because no purpose is configured for them.",
                code="undocumented_field",
            )

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def get(self, ctx: RequestContext, customer_id: str, *, mask: bool = True) -> dict[str, Any]:
        """Fetch a customer. Masks personal data unless ``VIEW_PII`` is held (R12.24)."""
        record = self.authz.load_scoped(ctx, "customers", customer_id, entity="customer")
        pii = self.db.query_one(
            "SELECT * FROM customer_pii WHERE customer_id = ? AND tenant_id = ?",
            (customer_id, ctx.tenant_id),
        )
        merged = dict(record)
        if pii is not None:
            values = dict(pii)
            merged["email"] = values.get("email")
            merged["full_name"] = values.get("full_name")
            merged["phone"] = values.get("phone")
            merged["extra"] = decode(values.get("extra_json"), {})
        merged.pop("email_hash", None)
        if not mask:
            return merged
        return self.authz.mask_record(ctx, merged, entity="customer")

    def find_by_email(self, ctx: RequestContext, email: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT id FROM customers WHERE tenant_id = ? AND email_hash = ?",
            (ctx.tenant_id, hash_identifier(email)),
        )
        return self.get(ctx, row["id"]) if row else None

    def contact_email(self, ctx: RequestContext, customer_id: str) -> str | None:
        """Internal accessor for notification delivery.

        Deliberately not exposed through the API: the notification pipeline needs the
        address to send a transactional message, but that is not a staff PII read, so
        it must not trigger a ``PII_ACCESS`` audit event for a background job.
        """
        return self.db.scalar(
            "SELECT email FROM customer_pii WHERE customer_id = ? AND tenant_id = ?",
            (customer_id, ctx.tenant_id),
        )

    def list_customers(self, ctx: RequestContext, *, limit: int = 100) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Customers", "VIEW")
        rows = self.db.query(
            "SELECT id FROM customers WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (ctx.tenant_id, int(limit)),
        )
        return [self.get(ctx, row["id"]) for row in rows]

    def field_purposes(self) -> dict[str, str]:
        """The documented purpose of every collectable field (R12.23)."""
        return dict(FIELD_PURPOSES)

    # ------------------------------------------------------------------ #
    # Erasure and retention (R12.22, R12.25)
    # ------------------------------------------------------------------ #

    def anonymize(
        self,
        ctx: RequestContext,
        customer_id: str,
        *,
        reason: str = "erasure_request",
        justification: str | None = None,
    ) -> dict[str, Any]:
        """Irreversibly anonymize a customer while retaining mandatory records (R12.22)."""
        self.authz.require_page(ctx, "Customers", "DELETE", target_type="customer", target_id=customer_id)
        record = self.authz.load_scoped(ctx, "customers", customer_id, entity="customer")
        if int(record.get("legal_hold") or 0):
            raise ConflictError(
                "This record is under legal hold and cannot be anonymized.",
                details={"customer_id": customer_id},
            )
        retained = self._mandatory_records(ctx, customer_id)
        now = to_iso(self.clock.now())
        with self.db.transaction():
            # Remove identifying values but keep the row so bookings, tickets and
            # tax invoices remain referentially intact.
            self.db.execute(
                "UPDATE customer_pii SET email = NULL, full_name = NULL, phone = NULL, "
                "extra_json = '{}', updated_at = ? WHERE customer_id = ? AND tenant_id = ?",
                (now, customer_id, ctx.tenant_id),
            )
            self.db.update(
                "customers",
                customer_id,
                {
                    "anonymized_at": now,
                    "email_hash": f"anon:{customer_id}",
                    "marketing_opt_in": 0,
                    "analytics_opt_in": 0,
                    "partner_share_opt_in": 0,
                    "updated_at": now,
                },
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx,
                "DSAR_COMPLETED",
                target_type="customer",
                target_id=customer_id,
                previous={"anonymized": False},
                new={
                    "anonymized": True,
                    "retained_records": retained,
                    "retention_justification": justification
                    or "Financial, tax and audit records are retained under statutory obligation.",
                },
                reason=reason,
                severity="WARNING",
            )
        return {
            "customer_id": customer_id,
            "anonymized_at": now,
            "retained_records": retained,
            "retention_justification": justification
            or "Financial, tax and audit records are retained under statutory obligation.",
        }

    def _mandatory_records(self, ctx: RequestContext, customer_id: str) -> dict[str, int]:
        """What must be kept despite an erasure request, and how much of it."""
        counts: dict[str, int] = {}
        for label, sql in (
            ("bookings", "SELECT COUNT(*) FROM bookings WHERE tenant_id = ? AND customer_id = ?"),
            (
                "tax_invoices",
                "SELECT COUNT(*) FROM tax_invoices ti JOIN bookings b ON b.id = ti.booking_id "
                "WHERE ti.tenant_id = ? AND b.customer_id = ?",
            ),
            (
                "payments",
                "SELECT COUNT(*) FROM payments p JOIN bookings b ON b.id = p.booking_id "
                "WHERE p.tenant_id = ? AND b.customer_id = ?",
            ),
            (
                "consent_records",
                "SELECT COUNT(*) FROM consent_records WHERE tenant_id = ? AND customer_id = ?",
            ),
        ):
            count = int(self.db.scalar(sql, (ctx.tenant_id, customer_id), default=0))
            if count:
                counts[label] = count
        return counts

    def set_legal_hold(self, ctx: RequestContext, customer_id: str, *, on: bool, reason: str) -> dict[str, Any]:
        """Exclude a record from automated purge, e.g. during a dispute (R12.25)."""
        self.authz.require_page(ctx, "Customers", "EDIT", target_type="customer", target_id=customer_id)
        self.authz.load_scoped(ctx, "customers", customer_id, entity="customer")
        self.db.update(
            "customers",
            customer_id,
            {"legal_hold": 1 if on else 0, "updated_at": to_iso(self.clock.now())},
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="customer",
            target_id=customer_id,
            new={"legal_hold": on},
            reason=reason,
            severity="WARNING",
        )
        return {"customer_id": customer_id, "legal_hold": on}

    def apply_retention(self, ctx: RequestContext, *, dry_run: bool = False) -> dict[str, Any]:
        """Anonymize customers whose retention period has expired (R12.25).

        Records under legal hold are skipped, and a customer with a future-dated
        booking is never purged while the service is still to be delivered.
        """
        days = self.config.get_int(ctx, "retention.customer_pii_days")
        cutoff = to_iso(add_minutes(self.clock.now(), -days * 24 * 60))
        today = to_iso(self.clock.now())[:10]
        rows = self.db.query(
            """
            SELECT c.id FROM customers c
            WHERE c.tenant_id = ? AND c.anonymized_at IS NULL AND c.legal_hold = 0
              AND COALESCE(c.updated_at, c.created_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM bookings b
                  WHERE b.tenant_id = c.tenant_id AND b.customer_id = c.id
                    AND b.status IN ('CONFIRMED','PENDING','AWAITING_PAYMENT')
                    AND (b.visit_date IS NULL OR b.visit_date >= ?)
              )
            """,
            (ctx.tenant_id, cutoff, today),
        )
        candidates = [row["id"] for row in rows]
        if dry_run:
            return {"retention_days": days, "cutoff": cutoff, "candidates": candidates, "anonymized": 0}
        anonymized = 0
        system = ctx.system()
        for customer_id in candidates:
            self.anonymize(
                system,
                customer_id,
                reason="retention_expiry",
                justification=f"Retention period of {days} days elapsed.",
            )
            anonymized += 1
        return {
            "retention_days": days,
            "cutoff": cutoff,
            "candidates": candidates,
            "anonymized": anonymized,
        }


__all__ = ["CustomerService"]
