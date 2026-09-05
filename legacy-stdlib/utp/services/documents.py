"""Receipts, tax invoices and credit notes.

Number allocation is the interesting part. A Thai tax invoice sequence must be
gap-controlled and free of duplicates, including under concurrent issuance
(R72.3). Allocation therefore happens inside ``BEGIN IMMEDIATE`` as a conditional
increment of ``document_sequences.next_no``, and the resulting number carries a
UNIQUE constraint. Two cashiers pressing "tax invoice" at the same instant get
consecutive numbers, never the same one.

An issued tax invoice is never deleted or edited. A correction is a credit note
that references the original (R72.5), and the ``tax_invoices`` table blocks
DELETE at the data layer.
"""

from __future__ import annotations

from typing import Any

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, local_iso, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.i18n import text as i18n_text
from ..core.ids import new_id, secure_token
from ..core.money import apply_rounding, split_tax, to_major
from .authz import AuthorizationService

DOC_TYPES: tuple[str, ...] = ("RECEIPT", "TAX_INVOICE", "CREDIT_NOTE")


class DocumentService:
    """Financial document issuance."""

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
    # Sequence allocation (R72.3)
    # ------------------------------------------------------------------ #

    def _allocate_number(
        self, ctx: RequestContext, *, organization_id: str, doc_type: str, prefix: str | None = None
    ) -> tuple[int, str]:
        """Allocate the next number atomically. Returns ``(sequence_no, number)``."""
        with self.db.transaction(immediate=True):
            row = self.db.query_one(
                "SELECT * FROM document_sequences WHERE tenant_id = ? AND organization_id = ? AND doc_type = ?",
                (ctx.tenant_id, organization_id, doc_type),
            )
            if row is None:
                sequence_id = new_id("dsq")
                resolved_prefix = prefix if prefix is not None else self._default_prefix(doc_type)
                self.db.insert(
                    "document_sequences",
                    {
                        "id": sequence_id,
                        "tenant_id": ctx.tenant_id,
                        "organization_id": organization_id,
                        "doc_type": doc_type,
                        "prefix": resolved_prefix,
                        "next_no": 2,
                    },
                )
                return 1, self._format_number(resolved_prefix, 1)
            sequence_no = int(row["next_no"])
            granted = self.db.compare_and_increment(
                "document_sequences",
                row["id"],
                counter="next_no",
                delta=1,
                tenant_id=ctx.tenant_id,
                extra_predicate="next_no = :_expected",
                extra_params={"_expected": sequence_no},
            )
            if not granted:  # pragma: no cover - guarded by the write lock
                raise ConflictError("Could not allocate a document number. Please retry.")
            return sequence_no, self._format_number(row["prefix"], sequence_no)

    @staticmethod
    def _default_prefix(doc_type: str) -> str:
        return {"RECEIPT": "RC", "TAX_INVOICE": "INV", "CREDIT_NOTE": "CN"}.get(doc_type, "DOC")

    def _format_number(self, prefix: str, sequence_no: int) -> str:
        year = to_iso(self.clock.now())[:4]
        return f"{prefix}{year}-{sequence_no:06d}"

    # ------------------------------------------------------------------ #
    # Receipts (R72.1)
    # ------------------------------------------------------------------ #

    def issue_receipt(self, ctx: RequestContext, *, booking_id: str) -> dict[str, Any]:
        """Issue a receipt for a completed sale. Idempotent per booking."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        existing = self.db.query_one(
            "SELECT * FROM receipts WHERE tenant_id = ? AND booking_id = ?", (ctx.tenant_id, booking_id)
        )
        if existing is not None:
            record = dict(existing)
            record["payload"] = decode(record.pop("payload_json"), {})
            return record
        sequence_no, number = self._allocate_number(
            ctx, organization_id=booking["organization_id"], doc_type="RECEIPT"
        )
        payload = self._document_payload(ctx, booking=booking, doc_type="RECEIPT", number=number)
        receipt_id = new_id("rcp")
        self.db.insert(
            "receipts",
            {
                "id": receipt_id,
                "tenant_id": ctx.tenant_id,
                "booking_id": booking_id,
                "number": number,
                "issued_at": to_iso(self.clock.now()),
                "payload_json": payload,
            },
        )
        return {
            "id": receipt_id,
            "number": number,
            "sequence_no": sequence_no,
            "booking_id": booking_id,
            "payload": payload,
        }

    def _document_payload(
        self, ctx: RequestContext, *, booking: dict[str, Any], doc_type: str, number: str
    ) -> dict[str, Any]:
        """Snapshot everything a printed document must show.

        Frozen at issue time rather than resolved at print time: a receipt reprinted
        next year must show the venue name, prices and tax split as they were when the
        sale happened (R5.3, R34.13).
        """
        organization = self.authz.load_scoped(
            ctx, "organizations", booking["organization_id"], entity="organization"
        )
        venue = self.authz.load_scoped(ctx, "venues", booking["venue_id"], entity="venue")
        language = booking.get("language") or "en"
        lines, tax_base, tax_amount, total = self._invoice_lines(ctx, booking=booking, venue=venue)
        payments = [
            {
                "method": row["method"],
                "amount_minor": int(row["amount_minor"]),
                "tendered_minor": row["tendered_minor"],
                "change_minor": row["change_minor"],
                "provider_ref": row["provider_ref"],
                "captured_at": row["captured_at"] or row["authorized_at"],
            }
            for row in self.db.query(
                "SELECT * FROM payments WHERE tenant_id = ? AND booking_id = ? "
                "AND status IN ('AUTHORIZED','CAPTURED') ORDER BY created_at",
                (ctx.tenant_id, booking["id"]),
            )
        ]
        return {
            "doc_type": doc_type,
            "number": number,
            "issued_at": to_iso(self.clock.now()),
            "issued_at_local": local_iso(self.clock.now(), venue["timezone"]),
            "organization": {
                "name": organization["name"],
                "legal_name": organization["legal_name"] or organization["name"],
                "tax_id": organization["tax_id"],
                "address": organization["address"],
            },
            "venue": {
                "code": venue["code"],
                "name": i18n_text(decode(venue["name_json"], {}), language, fallback=venue["code"]),
                "timezone": venue["timezone"],
                "tax_model": venue["tax_model"],
                "tax_rate_bp": int(venue["tax_rate_bp"] or 0),
            },
            "booking": {
                "booking_number": booking["booking_number"],
                "visit_date": booking["visit_date"],
                "channel": booking["channel"],
                "currency": booking["currency"],
            },
            "lines": lines,
            "tax_base_minor": tax_base,
            "tax_minor": tax_amount,
            "total_minor": total,
            "payments": payments,
            "tax_note": (
                "Prices include VAT." if venue["tax_model"] == "INCLUSIVE" else "VAT added at payment."
            ),
        }

    def reprint_receipt(self, ctx: RequestContext, *, receipt_id: str, reason: str) -> dict[str, Any]:
        """Reprint requires ``REPRINT`` and is audited (R34.13, R41.1)."""
        receipt = self.authz.load_scoped(ctx, "receipts", receipt_id, entity="receipt")
        booking = self.authz.load_scoped(ctx, "bookings", receipt["booking_id"], entity="booking")
        scoped = ctx.for_venue(booking["venue_id"])
        self.authz.require_action(scoped, "REPRINT", target_type="receipt", target_id=receipt_id)
        self.db.update(
            "receipts",
            receipt_id,
            {"reprint_count": int(receipt["reprint_count"]) + 1},
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            scoped,
            "REPRINT",
            target_type="receipt",
            target_id=receipt_id,
            new={"reprint_count": int(receipt["reprint_count"]) + 1},
            reason=reason,
        )
        record = dict(receipt)
        record["payload"] = decode(record.pop("payload_json"), {})
        record["reprint_count"] = int(receipt["reprint_count"]) + 1
        return record

    # ------------------------------------------------------------------ #
    # Tax invoices (R72.2 - R72.7)
    # ------------------------------------------------------------------ #

    def issue_tax_invoice(
        self,
        ctx: RequestContext,
        *,
        booking_id: str,
        customer_tax: dict[str, Any],
        reissue: bool = False,
    ) -> dict[str, Any]:
        """Issue a tax invoice for a booking (R72.2 - R72.4)."""
        booking = self.authz.load_scoped(ctx, "bookings", booking_id, entity="booking")
        scoped = ctx.for_venue(booking["venue_id"])
        self.authz.require_action(scoped, "ISSUE_TAX_INVOICE", target_type="booking", target_id=booking_id)
        for field in ("name",):
            if not str(customer_tax.get(field) or "").strip():
                raise ValidationError(
                    {f"customer_tax.{field}": "The customer's tax name is required."},
                    message="Please provide the details required for a tax invoice.",
                )
        if booking["status"] not in ("CONFIRMED", "PARTIALLY_REFUNDED"):
            raise ConflictError(
                f"A tax invoice cannot be issued for a {booking['status'].lower()} booking."
            )
        existing = self.db.query_one(
            "SELECT * FROM tax_invoices WHERE tenant_id = ? AND booking_id = ? AND doc_type = 'TAX_INVOICE' "
            "AND status = 'ISSUED'",
            (ctx.tenant_id, booking_id),
        )
        if existing is not None and not reissue:
            record = dict(existing)
            record["customer_tax"] = decode(record.pop("customer_tax_json"), {})
            record["lines"] = decode(record.pop("lines_json"), [])
            record["already_issued"] = True
            return record

        organization = self.authz.load_scoped(
            ctx, "organizations", booking["organization_id"], entity="organization"
        )
        venue = self.authz.load_scoped(ctx, "venues", booking["venue_id"], entity="venue")
        sequence_no, number = self._allocate_number(
            ctx, organization_id=booking["organization_id"], doc_type="TAX_INVOICE"
        )
        lines, tax_base, tax_amount, total = self._invoice_lines(ctx, booking=booking, venue=venue)
        invoice_id = new_id("inv")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.insert(
                "tax_invoices",
                {
                    "id": invoice_id,
                    "tenant_id": ctx.tenant_id,
                    "organization_id": booking["organization_id"],
                    "booking_id": booking_id,
                    "number": number,
                    "sequence_no": sequence_no,
                    "doc_type": "TAX_INVOICE",
                    "issued_at": now,
                    "customer_tax_json": customer_tax,
                    "lines_json": lines,
                    "tax_base_minor": tax_base,
                    "tax_minor": tax_amount,
                    "total_minor": total,
                    "status": "ISSUED",
                    "actor_id": ctx.principal.id,
                },
            )
            self.audit.record(
                scoped,
                "TAX_INVOICE_ISSUE",
                target_type="tax_invoice",
                target_id=invoice_id,
                new={
                    "number": number,
                    "sequence_no": sequence_no,
                    "total_minor": total,
                    "booking_number": booking["booking_number"],
                    "reissue": reissue,
                },
                severity="WARNING",
                venue_timezone=venue["timezone"],
            )
        return {
            "id": invoice_id,
            "number": number,
            "sequence_no": sequence_no,
            "booking_id": booking_id,
            "issued_at": now,
            "organization": {
                "legal_name": organization["legal_name"] or organization["name"],
                "tax_id": organization["tax_id"],
                "address": organization["address"],
            },
            "customer_tax": customer_tax,
            "lines": lines,
            "tax_base_minor": tax_base,
            "tax_minor": tax_amount,
            "total_minor": total,
            "currency": booking["currency"],
            "receipt_reference": self.db.scalar(
                "SELECT number FROM receipts WHERE tenant_id = ? AND booking_id = ?",
                (ctx.tenant_id, booking_id),
            ),
            "download": self.secure_link(ctx, entity="tax_invoice", entity_id=invoice_id),
        }

    def _invoice_lines(
        self, ctx: RequestContext, *, booking: dict[str, Any], venue: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """Build invoice lines with tax split consistent with the cart (R72.8)."""
        rows = self.db.query(
            """
            SELECT bi.*, tt.name_json AS tt_name, s.name_json AS seg_name
            FROM booking_items bi
            JOIN ticket_types tt ON tt.id = bi.ticket_type_id AND tt.tenant_id = bi.tenant_id
            JOIN customer_segments s ON s.id = bi.segment_id AND s.tenant_id = bi.tenant_id
            WHERE bi.tenant_id = ? AND bi.booking_id = ? AND bi.state = 'ACTIVE'
            ORDER BY bi.created_at
            """,
            (ctx.tenant_id, booking["id"]),
        )
        lines: list[dict[str, Any]] = []
        tax_base = 0
        tax_amount = 0
        for row in rows:
            split = split_tax(
                int(row["net_minor"]), rate_bp=int(venue["tax_rate_bp"] or 0), model=venue["tax_model"]
            )
            tax_base += split.net_minor
            tax_amount += split.tax_minor
            lines.append(
                {
                    "description": i18n_text(decode(row["tt_name"], {}), booking["language"]),
                    "segment": i18n_text(decode(row["seg_name"], {}), booking["language"]),
                    "quantity": int(row["quantity"]),
                    "unit_price_minor": int(row["unit_price_minor"]),
                    "discount_minor": int(row["discount_minor"]),
                    "net_minor": split.net_minor,
                    "tax_minor": split.tax_minor,
                    "gross_minor": split.gross_minor,
                    "unit_price": str(to_major(int(row["unit_price_minor"]), booking["currency"])),
                }
            )
        total = apply_rounding(tax_base + tax_amount, venue["rounding_mode"], venue["currency"])
        return lines, tax_base, tax_amount, total

    def issue_credit_note(
        self, ctx: RequestContext, *, invoice_id: str, reason: str
    ) -> dict[str, Any]:
        """Correct an issued tax invoice by credit note, never by deletion (R72.5)."""
        invoice = self.authz.load_scoped(ctx, "tax_invoices", invoice_id, entity="tax_invoice")
        booking = self.authz.load_scoped(ctx, "bookings", invoice["booking_id"], entity="booking")
        scoped = ctx.for_venue(booking["venue_id"])
        self.authz.require_action(
            scoped, "ISSUE_TAX_INVOICE", target_type="tax_invoice", target_id=invoice_id
        )
        if invoice["doc_type"] != "TAX_INVOICE":
            raise ConflictError("A credit note can only reference a tax invoice.")
        if invoice["status"] != "ISSUED":
            raise ConflictError("That invoice has already been credited.")
        sequence_no, number = self._allocate_number(
            ctx, organization_id=invoice["organization_id"], doc_type="CREDIT_NOTE"
        )
        note_id = new_id("cn")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.insert(
                "tax_invoices",
                {
                    "id": note_id,
                    "tenant_id": ctx.tenant_id,
                    "organization_id": invoice["organization_id"],
                    "booking_id": invoice["booking_id"],
                    "number": number,
                    "sequence_no": sequence_no,
                    "doc_type": "CREDIT_NOTE",
                    "issued_at": now,
                    "customer_tax_json": decode(invoice["customer_tax_json"], {}),
                    "lines_json": decode(invoice["lines_json"], []),
                    "tax_base_minor": -int(invoice["tax_base_minor"]),
                    "tax_minor": -int(invoice["tax_minor"]),
                    "total_minor": -int(invoice["total_minor"]),
                    "status": "ISSUED",
                    "credit_note_of": invoice_id,
                    "actor_id": ctx.principal.id,
                },
            )
            self.db.update(
                "tax_invoices", invoice_id, {"status": "CREDITED"}, tenant_id=ctx.tenant_id
            )
            self.audit.record(
                scoped,
                "TAX_INVOICE_ISSUE",
                target_type="tax_invoice",
                target_id=note_id,
                previous={"invoice_number": invoice["number"], "status": "ISSUED"},
                new={"credit_note_number": number, "credits": invoice["number"]},
                reason=reason,
                severity="WARNING",
            )
        return {
            "id": note_id,
            "number": number,
            "credit_note_of": invoice["number"],
            "total_minor": -int(invoice["total_minor"]),
        }

    def delete_invoice(self, ctx: RequestContext, invoice_id: str, *, reason: str) -> dict[str, Any]:
        """DELETE on a tax invoice always executes as a credit note (R46.2, R67.6)."""
        self.authz.require_page(ctx, "Tax Invoices", "DELETE", target_type="tax_invoice", target_id=invoice_id)
        note = self.issue_credit_note(ctx, invoice_id=invoice_id, reason=reason)
        return {
            "requested": "DELETE",
            "performed": "CREDIT_NOTE",
            "explanation": "An issued tax invoice is never deleted; a credit note references the original.",
            **note,
        }

    def get_invoice(self, ctx: RequestContext, invoice_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "tax_invoices", invoice_id, entity="tax_invoice")
        record["customer_tax"] = decode(record.pop("customer_tax_json"), {})
        record["lines"] = decode(record.pop("lines_json"), [])
        return record

    def list_invoices(
        self, ctx: RequestContext, *, booking_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.authz.require_page(ctx, "Tax Invoices", "VIEW")
        sql = "SELECT id FROM tax_invoices WHERE tenant_id = ?"
        params: list[Any] = [ctx.tenant_id]
        if booking_id:
            sql += " AND booking_id = ?"
            params.append(booking_id)
        sql += " ORDER BY issued_at DESC LIMIT ?"
        params.append(int(limit))
        return [self.get_invoice(ctx, row["id"]) for row in self.db.query(sql, params)]

    def sequence_integrity(self, ctx: RequestContext, *, organization_id: str, doc_type: str) -> dict[str, Any]:
        """Prove the sequence has no gaps and no duplicates (R72.3)."""
        rows = self.db.query(
            "SELECT sequence_no FROM tax_invoices WHERE tenant_id = ? AND organization_id = ? "
            "AND doc_type = ? ORDER BY sequence_no",
            (ctx.tenant_id, organization_id, doc_type),
        )
        numbers = [int(r["sequence_no"]) for r in rows]
        expected = list(range(1, len(numbers) + 1))
        return {
            "doc_type": doc_type,
            "issued_count": len(numbers),
            "duplicates": sorted({n for n in numbers if numbers.count(n) > 1}),
            "gaps": sorted(set(expected) - set(numbers)),
            "contiguous": numbers == expected,
        }

    # ------------------------------------------------------------------ #
    # Secure delivery (R37.14, R72.7)
    # ------------------------------------------------------------------ #

    def secure_link(
        self, ctx: RequestContext, *, entity: str, entity_id: str, ttl_minutes: int = 60
    ) -> dict[str, Any]:
        """Expiring token for a document containing personal or financial data."""
        token = secure_token(24)
        expires = add_minutes(self.clock.now(), ttl_minutes)
        self.db.insert(
            "verification_challenges",
            {
                "id": new_id("vch"),
                "tenant_id": ctx.tenant_id,
                "booking_id": None,
                "purpose": f"DOWNLOAD:{entity}:{entity_id}",
                "contact_hash": entity_id,
                "code_hash": token,
                "issued_at": to_iso(self.clock.now()),
                "expires_at": to_iso(expires),
            },
        )
        return {
            "url": f"https://book.example/documents/{entity}/{token}",
            "expires_at": to_iso(expires),
            "single_use": True,
        }

    def resolve_secure_link(self, ctx: RequestContext, *, token: str) -> dict[str, Any]:
        now = to_iso(self.clock.now())
        row = self.db.query_one(
            "SELECT * FROM verification_challenges WHERE tenant_id = ? AND code_hash = ? "
            "AND consumed_at IS NULL AND expires_at > ? AND purpose LIKE 'DOWNLOAD:%'",
            (ctx.tenant_id, token, now),
        )
        if row is None:
            raise NotFound(details={"entity": "document_link"})
        self.db.update("verification_challenges", row["id"], {"consumed_at": now}, tenant_id=ctx.tenant_id)
        _, entity, entity_id = row["purpose"].split(":", 2)
        return {"entity": entity, "entity_id": entity_id}


__all__ = ["DOC_TYPES", "DocumentService"]
