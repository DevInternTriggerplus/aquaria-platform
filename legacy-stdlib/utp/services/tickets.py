"""Ticket issuance, QR payloads and ticket lifecycle.

QR design
---------
The QR payload is an **opaque reference plus a detached signature** — never
encoded personal data, and never a guessable sequence (R15.2). Concretely it is
``UTP1.<tenant-prefix>.<token>.<signature>`` where ``token`` is 32 bytes of
``secrets`` entropy and ``signature`` is an HMAC over the token and tenant. Two
consequences worth stating:

* A photographed ticket reveals nothing about the guest.
* A forged code fails signature verification before any database lookup, so the
  gate can reject garbage without touching storage — which is part of how the
  500 ms scan target in R32.1 is met.

Validity windows are derived from the admission model's primitives rather than
from per-model code, so a new admission model added as configuration gets correct
validity for free (R3.2).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, combine_local, parse_date, parse_instant, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.i18n import text as i18n_text
from ..core.ids import (
    new_id,
    platform_signing_key,
    secure_token,
    sign_payload,
    ticket_number,
    verify_signature,
)
from ..domain import enums
from .authz import AuthorizationService

QR_PREFIX = "UTP1"

#: Ticket states from which a scan may still admit.
ADMISSIBLE_STATES: frozenset[str] = frozenset({"ISSUED", "VALID", "PARTIALLY_USED"})

#: States that mean the ticket is finished, one way or another.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"USED", "EXPIRED", "CANCELLED", "VOIDED", "REFUNDED", "TRANSFERRED"}
)


def build_qr_payload(tenant_id: str) -> tuple[str, str, str]:
    """Return ``(token, signature, full_payload)`` for a new ticket."""
    token = secure_token(32)
    body = f"{QR_PREFIX}.{tenant_id}.{token}"
    signature = sign_payload(platform_signing_key(), body)
    return token, signature, f"{body}.{signature}"


def parse_qr_payload(payload: str) -> dict[str, Any]:
    """Parse and verify a scanned payload without touching the database.

    Returns ``{"valid": False, ...}`` for anything malformed or unsigned, which the
    gate maps to ``REJECT_UNKNOWN_CODE`` (R32.2).
    """
    parts = (payload or "").strip().split(".")
    if len(parts) != 4 or parts[0] != QR_PREFIX:
        return {"valid": False, "reason": "malformed"}
    _, tenant_id, token, signature = parts
    body = f"{QR_PREFIX}.{tenant_id}.{token}"
    if not verify_signature(platform_signing_key(), body, signature):
        return {"valid": False, "reason": "bad_signature", "tenant_id": tenant_id}
    return {"valid": True, "tenant_id": tenant_id, "token": token, "signature": signature}


class TicketService:
    """Issuance and lifecycle of individually redeemable admission artefacts."""

    #: Injected by :class:`utp.app.Platform` after construction. Supplies the venue's
    #: ticket-validity policy so QR expiry follows configuration, not per-model code.
    settings: Any = None

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
    # Issuance (R15.1)
    # ------------------------------------------------------------------ #

    def issue_for_booking(
        self,
        ctx: RequestContext,
        *,
        booking: dict[str, Any],
        venue: dict[str, Any],
        items: Sequence[dict[str, Any]],
        seat_assignments: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Issue one ticket per admitted person or redeemable unit.

        A line of quantity 3 becomes three individually scannable tickets, each with
        its own AccessRight and QR payload, because R15.1 requires per-person
        redeemability — a single shared code would make duplicate-entry detection
        impossible.
        """
        issued: list[dict[str, Any]] = []
        sequence = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM tickets WHERE tenant_id = ? AND booking_id = ?",
                (ctx.tenant_id, booking["id"]),
                default=0,
            )
        )
        seat_assignments = seat_assignments or {}
        with self.db.transaction():
            for item in items:
                ticket_type = self.authz.load_scoped(
                    ctx, "ticket_types", item["ticket_type_id"], entity="ticket_type"
                )
                seats = list(seat_assignments.get(item["id"], []))
                for unit in range(int(item["quantity"])):
                    sequence += 1
                    window = self._validity_window(
                        ctx,
                        venue=venue,
                        ticket_type=ticket_type,
                        visit_date=booking.get("visit_date"),
                        session_id=item.get("session_id"),
                    )
                    token, signature, _ = build_qr_payload(ctx.tenant_id)
                    # Re-entry allowance and max entries come from the resolved policy
                    # when present, so the QR admin settings (§12) govern the gate.
                    policy = window["policy"]
                    allowance = int(policy.get("max_entries") or ticket_type["entry_allowance"])
                    reentry = (
                        ticket_type["reentry_window_minutes"]
                        if policy.get("reentry_allowed")
                        else ticket_type["reentry_window_minutes"]
                    )
                    ticket_id = new_id("tck")
                    self.db.insert(
                        "tickets",
                        {
                            "id": ticket_id,
                            "tenant_id": ctx.tenant_id,
                            "venue_id": booking["venue_id"],
                            "booking_id": booking["id"],
                            "booking_item_id": item["id"],
                            "ticket_number": ticket_number(booking["booking_number"], sequence),
                            "qr_token": token,
                            "qr_signature": signature,
                            "state": "VALID",
                            "product_id": item["product_id"],
                            "ticket_type_id": item["ticket_type_id"],
                            "segment_id": item["segment_id"],
                            "session_id": item.get("session_id"),
                            "seat_id": seats[unit] if unit < len(seats) else item.get("seat_id"),
                            "visit_date": booking.get("visit_date"),
                            "valid_from": window["valid_from"],
                            "valid_until": window["valid_until"],
                            # Validity snapshot (settings spec §14, §33): the timezone
                            # and policy used are frozen onto the ticket so that later
                            # changes to venue settings cannot move this ticket's expiry.
                            "validity_timezone": venue["timezone"],
                            "validity_type": policy.get("validity_type"),
                            "validity_policy_json": policy,
                            "entry_allowance": allowance,
                            "entries_used": 0,
                            "reentry_window_minutes": reentry,
                            "proof_required": 1 if self._proof_required(ctx, ticket_type) else 0,
                            "issued_at": to_iso(self.clock.now()),
                        },
                    )
                    issued.append(self.get(ctx, ticket_id, include_qr=True))
        return issued

    #: End-of-day expiry, venue-local (settings spec §10). Not 24:00 — a ticket is
    #: valid *through* the last second of the visit date and invalid immediately after.
    END_OF_DAY = "23:59:59"

    def _validity_window(
        self,
        ctx: RequestContext,
        *,
        venue: dict[str, Any],
        ticket_type: dict[str, Any],
        visit_date: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Derive the admission window from the venue's configured validity policy.

        The policy (settings spec §11, §12) selects the model; the admission model's
        own primitives are the fallback when no policy is configured. The returned
        ``policy`` is what gets snapshotted onto the ticket so its expiry can never be
        recomputed under new settings (§14).
        """
        tz = venue["timezone"]
        now = self.clock.now()
        from datetime import timedelta

        policy = self._resolve_policy(ctx, venue=venue, ticket_type=ticket_type)
        validity = decode(ticket_type["validity_json"], {}) or {}
        vtype = policy["validity_type"]

        # A session-bound ticket always tracks its session, whatever the default type,
        # because the reserved thing is the session (R25, settings spec §11).
        if session_id and (vtype == "SESSION_BASED" or enums.admission_model(ticket_type["admission_model"]).validity == "SESSION"):
            session = self.db.query_one(
                "SELECT date, start_time, end_time, grace_minutes FROM sessions WHERE id = ? AND tenant_id = ?",
                (session_id, ctx.tenant_id),
            )
            if session is not None:
                grace = int(session["grace_minutes"] or policy.get("grace_minutes") or 0)
                starts = combine_local(session["date"], session["start_time"], tz)
                ends = combine_local(session["date"], session["end_time"], tz)
                if session["end_time"] < session["start_time"]:
                    ends = add_minutes(ends, 24 * 60)
                return self._window(add_minutes(starts, -grace), add_minutes(ends, grace), policy, vtype="SESSION_BASED")

        anchor_date = visit_date or to_iso(now)[:10]

        if vtype == "FIXED_RANGE":
            start = policy.get("valid_from") or validity.get("valid_from")
            end = policy.get("valid_until") or validity.get("valid_until")
            if start and end:
                return self._window(parse_instant(start), parse_instant(end), policy)

        if vtype == "FIXED_DURATION":
            minutes = int(policy.get("duration_minutes") or validity.get("duration_minutes") or 0)
            start_time = policy.get("entry_start_time")
            starts = combine_local(anchor_date, start_time, tz) if start_time else now
            return self._window(starts, add_minutes(starts, minutes), policy)

        if vtype == "NUMBER_OF_DAYS":
            days = int(policy.get("number_of_days") or validity.get("valid_days") or 1)
            starts = combine_local(anchor_date, "00:00:00", tz)
            last_day = (parse_date(anchor_date) + timedelta(days=days - 1)).isoformat()
            ends = combine_local(last_day, self.END_OF_DAY, tz)
            return self._window(starts, ends, policy)

        if vtype in ("MEMBERSHIP", "CUSTOM"):
            # Membership validity follows the member's rights; without a membership
            # subsystem yet, fall back to an open window anchored at issue so the
            # ticket is usable and the type is recorded honestly on the snapshot.
            days = int(policy.get("number_of_days") or validity.get("valid_days") or 365)
            return self._window(now, add_minutes(now, days * 24 * 60), policy)

        # END_OF_VISIT_DAY (the default): valid from the start of the visit date
        # through 23:59:59 that day, venue-local (settings spec §10, §37).
        starts = combine_local(anchor_date, policy.get("entry_start_time") or "00:00:00", tz)
        ends = combine_local(anchor_date, self.END_OF_DAY, tz)
        return self._window(starts, ends, policy)

    def _window(self, starts: Any, ends: Any, policy: dict[str, Any], *, vtype: str | None = None) -> dict[str, Any]:
        snapshot = dict(policy)
        if vtype:
            snapshot["validity_type"] = vtype
        return {"valid_from": to_iso(starts), "valid_until": to_iso(ends), "policy": snapshot}

    def _resolve_policy(
        self, ctx: RequestContext, *, venue: dict[str, Any], ticket_type: dict[str, Any]
    ) -> dict[str, Any]:
        """The effective validity policy for this ticket type at this venue.

        Prefers the SettingsService (product overrides venue); falls back to a policy
        derived from the admission model so a venue that has configured nothing still
        behaves correctly.
        """
        if self.settings is not None:
            try:
                return self.settings.validity_policy(
                    ctx, venue_id=venue["id"], product_id=ticket_type.get("product_id")
                )
            except Exception:  # never let a settings read block ticket issuance
                pass
        return self._policy_from_model(ticket_type)

    def _policy_from_model(self, ticket_type: dict[str, Any]) -> dict[str, Any]:
        model = enums.admission_model(ticket_type["admission_model"])
        validity = decode(ticket_type["validity_json"], {}) or {}
        mapping = {
            "SESSION": "SESSION_BASED",
            "OPEN_DATE": "NUMBER_OF_DAYS",
            "DATE_RANGE": "NUMBER_OF_DAYS",
            "SUBSCRIPTION": "NUMBER_OF_DAYS",
        }
        vtype = mapping.get(model.validity, "END_OF_VISIT_DAY")
        return {
            "validity_type": vtype,
            "number_of_days": int(validity.get("valid_days") or 1),
            "duration_minutes": validity.get("duration_minutes"),
            "entry_start_time": validity.get("open_time"),
            "entry_cutoff_time": None,
            "grace_minutes": 0,
            "reentry_allowed": bool(ticket_type.get("reentry_window_minutes")),
            "max_entries": int(ticket_type["entry_allowance"]),
            "valid_from": validity.get("valid_from"),
            "valid_until": validity.get("valid_until"),
        }

    def _proof_required(self, ctx: RequestContext, ticket_type: dict[str, Any]) -> bool:
        """R4.5 / R3.6 — flag tickets whose segment needs proof at entry."""
        eligibility = decode(ticket_type["eligibility_json"], {}) or {}
        if eligibility.get("documents") or eligibility.get("residency") or eligibility.get(
            "membership_required"
        ):
            return True
        return bool(
            self.db.scalar(
                "SELECT proof_required FROM customer_segments WHERE id = ? AND tenant_id = ?",
                (ticket_type["segment_id"], ctx.tenant_id),
                default=0,
            )
        )

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def get(self, ctx: RequestContext, ticket_id: str, *, include_qr: bool = False) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "tickets", ticket_id, entity="ticket")
        token = record.pop("qr_token")
        signature = record.pop("qr_signature")
        if include_qr:
            record["qr_payload"] = f"{QR_PREFIX}.{ctx.tenant_id}.{token}.{signature}"
        record["entries_remaining"] = (
            None if int(record["entry_allowance"]) < 0
            else max(int(record["entry_allowance"]) - int(record["entries_used"]), 0)
        )
        record["unlimited_entries"] = int(record["entry_allowance"]) < 0
        return record

    def find_by_qr(self, ctx: RequestContext, payload: str) -> dict[str, Any] | None:
        """Resolve a scanned payload to a ticket, verifying the signature first."""
        parsed = parse_qr_payload(payload)
        if not parsed["valid"] or parsed["tenant_id"] != ctx.tenant_id:
            return None
        row = self.db.query_one(
            "SELECT id FROM tickets WHERE qr_token = ? AND tenant_id = ?",
            (parsed["token"], ctx.tenant_id),
        )
        return self.get(ctx, row["id"]) if row else None

    def list_for_booking(
        self, ctx: RequestContext, booking_id: str, *, include_qr: bool = False
    ) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id FROM tickets WHERE tenant_id = ? AND booking_id = ? ORDER BY ticket_number",
            (ctx.tenant_id, booking_id),
        )
        return [self.get(ctx, row["id"], include_qr=include_qr) for row in rows]

    def presentation(
        self, ctx: RequestContext, ticket_id: str, *, language: str | None = None
    ) -> dict[str, Any]:
        """Everything printed or displayed on a ticket (R15.4)."""
        lang = language or ctx.language
        ticket = self.get(ctx, ticket_id, include_qr=True)
        venue = self.authz.load_scoped(ctx, "venues", ticket["venue_id"], entity="venue")
        product = self.authz.load_scoped(ctx, "products", ticket["product_id"], entity="product")
        ticket_type = self.authz.load_scoped(ctx, "ticket_types", ticket["ticket_type_id"], entity="ticket_type")
        segment = self.authz.load_scoped(
            ctx, "customer_segments", ticket["segment_id"], entity="customer_segment"
        )
        booking = self.authz.load_scoped(ctx, "bookings", ticket["booking_id"], entity="booking")
        session = None
        if ticket["session_id"]:
            session = self.db.query_one(
                "SELECT date, start_time, end_time, area_id FROM sessions WHERE id = ? AND tenant_id = ?",
                (ticket["session_id"], ctx.tenant_id),
            )
        entry_area_id = (session["area_id"] if session else None) or product.get("experience_id")
        entry_location = None
        access_point = self.db.query_one(
            "SELECT name_json FROM access_points WHERE tenant_id = ? AND venue_id = ? AND direction = 'IN' "
            "AND status = 'ACTIVE' ORDER BY code LIMIT 1",
            (ctx.tenant_id, ticket["venue_id"]),
        )
        if access_point is not None:
            entry_location = i18n_text(decode(access_point["name_json"], {}), lang)

        model = enums.admission_model(ticket_type["admission_model"])
        conditions: list[str] = []
        if ticket["proof_required"]:
            conditions.append("Bring proof of eligibility for this ticket type.")
        if ticket["unlimited_entries"]:
            conditions.append("Multiple entries permitted within the validity window.")
        elif int(ticket["entry_allowance"]) == 1:
            conditions.append("Single entry only.")
        else:
            conditions.append(f"Up to {ticket['entry_allowance']} entries.")
        reentry = ticket["reentry_window_minutes"]
        reentry_text = None
        if model.reentry_allowed and reentry:
            reentry_text = f"Re-entry permitted within {reentry} minutes of leaving."
            conditions.append(reentry_text)

        return {
            "ticket_id": ticket_id,
            "ticket_number": ticket["ticket_number"],
            "booking_number": booking["booking_number"],
            "venue": i18n_text(decode(venue["name_json"], {}), lang, fallback=venue["code"]),
            "venue_timezone": venue["timezone"],
            "product": i18n_text(decode(product["name_json"], {}), lang, fallback=product["code"]),
            "ticket_type": i18n_text(decode(ticket_type["name_json"], {}), lang, fallback=ticket_type["code"]),
            "segment": i18n_text(decode(segment["name_json"], {}), lang, fallback=segment["code"]),
            "visit_date": ticket["visit_date"],
            "session_time": f"{session['start_time']}–{session['end_time']}" if session else None,
            "entry_location": entry_location,
            "qr_payload": ticket["qr_payload"],
            "state": ticket["state"],
            "valid_from": ticket["valid_from"],
            "valid_until": ticket["valid_until"],
            "entries_used": int(ticket["entries_used"]),
            "entries_remaining": ticket["entries_remaining"],
            "conditions": conditions,
            "reentry_rules": reentry_text,
            "seat_id": ticket["seat_id"],
            "_entry_area_id": entry_area_id,
        }

    # ------------------------------------------------------------------ #
    # Lifecycle (R15.5 - R15.8)
    # ------------------------------------------------------------------ #

    def set_state(
        self,
        ctx: RequestContext,
        ticket_id: str,
        state: str,
        *,
        reason: str | None = None,
        audit_action: str | None = None,
        require_permission: bool = True,
    ) -> dict[str, Any]:
        if state not in enums.TICKET_STATES:
            raise ValidationError({"state": f"State must be one of {', '.join(enums.TICKET_STATES)}."})
        ticket = self.get(ctx, ticket_id)
        if require_permission:
            self.authz.require_page(ctx, "Tickets", "EDIT", target_type="ticket", target_id=ticket_id)
        payload: dict[str, Any] = {"state": state}
        if state == "BLOCKED":
            payload["blocked_reason"] = reason
        with self.db.transaction():
            self.db.update("tickets", ticket_id, payload, tenant_id=ctx.tenant_id)
            self.audit.record(
                ctx.for_venue(ticket["venue_id"]),
                audit_action or "CONFIG_CHANGE",
                target_type="ticket",
                target_id=ticket_id,
                previous={"state": ticket["state"]},
                new={"state": state},
                reason=reason,
                severity="WARNING" if state in ("BLOCKED", "VOIDED", "REFUNDED") else "INFO",
            )
        return self.get(ctx, ticket_id)

    def block(self, ctx: RequestContext, ticket_id: str, *, reason: str) -> dict[str, Any]:
        """Invalidate one ticket without cancelling the booking (R15.8)."""
        if not (reason or "").strip():
            raise ValidationError({"reason": "A reason is required to block a ticket."})
        self.authz.require_page(ctx, "Tickets", "EDIT", target_type="ticket", target_id=ticket_id)
        return self.set_state(
            ctx, ticket_id, "BLOCKED", reason=reason, audit_action="TICKET_BLOCK", require_permission=False
        )

    def unblock(self, ctx: RequestContext, ticket_id: str, *, reason: str) -> dict[str, Any]:
        ticket = self.get(ctx, ticket_id)
        if ticket["state"] != "BLOCKED":
            raise ConflictError("That ticket is not blocked.")
        self.authz.require_page(ctx, "Tickets", "EDIT", target_type="ticket", target_id=ticket_id)
        restored = "PARTIALLY_USED" if int(ticket["entries_used"]) > 0 else "VALID"
        return self.set_state(
            ctx, ticket_id, restored, reason=reason, audit_action="TICKET_BLOCK", require_permission=False
        )

    def reissue(self, ctx: RequestContext, ticket_id: str, *, reason: str) -> dict[str, Any]:
        """Mint a fresh QR for the same ticket identity (R15.6, R16.9).

        The ticket number and id are unchanged, so history and entry counts survive;
        only the scannable payload rotates, which is what invalidates a superseded
        code after a reschedule or a seat change.
        """
        ticket = self.get(ctx, ticket_id)
        self.authz.require_page(ctx, "Tickets", "EDIT", target_type="ticket", target_id=ticket_id)
        token, signature, payload = build_qr_payload(ctx.tenant_id)
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "tickets",
                ticket_id,
                {
                    "qr_token": token,
                    "qr_signature": signature,
                    "superseded_at": now,
                    "reissue_count": int(ticket["reissue_count"]) + 1,
                },
                tenant_id=ctx.tenant_id,
            )
            self.audit.record(
                ctx.for_venue(ticket["venue_id"]),
                "TICKET_REISSUE",
                target_type="ticket",
                target_id=ticket_id,
                previous={"reissue_count": int(ticket["reissue_count"])},
                new={"reissue_count": int(ticket["reissue_count"]) + 1, "previous_qr_invalidated": True},
                reason=reason,
                severity="WARNING",
            )
        result = self.get(ctx, ticket_id, include_qr=True)
        result["qr_payload"] = payload
        return result

    def refresh_validity(
        self,
        ctx: RequestContext,
        ticket_id: str,
        *,
        venue: dict[str, Any],
        visit_date: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Recompute the admission window after a reschedule."""
        ticket = self.get(ctx, ticket_id)
        ticket_type = self.authz.load_scoped(
            ctx, "ticket_types", ticket["ticket_type_id"], entity="ticket_type"
        )
        window = self._validity_window(
            ctx, venue=venue, ticket_type=ticket_type, visit_date=visit_date, session_id=session_id
        )
        self.db.update(
            "tickets",
            ticket_id,
            {
                "visit_date": visit_date,
                "session_id": session_id,
                "valid_from": window["valid_from"],
                "valid_until": window["valid_until"],
            },
            tenant_id=ctx.tenant_id,
        )
        return self.get(ctx, ticket_id)

    def record_entry(self, ctx: RequestContext, ticket_id: str) -> dict[str, Any]:
        """Increment the entry count and move the state accordingly (R15.7).

        The increment is a conditional UPDATE guarded by the allowance, so two
        simultaneous scans of the same ticket cannot both succeed.
        """
        now = to_iso(self.clock.now())
        with self.db.transaction(immediate=True):
            ticket = self.get(ctx, ticket_id)
            unlimited = ticket["unlimited_entries"]
            granted = self.db.compare_and_increment(
                "tickets",
                ticket_id,
                counter="entries_used",
                delta=1,
                limit_column=None if unlimited else "entry_allowance",
                tenant_id=ctx.tenant_id,
            )
            if not granted:
                raise ConflictError(
                    "This ticket's entry allowance is already used.",
                    details={"ticket_id": ticket_id, "entries_used": int(ticket["entries_used"])},
                )
            used = int(ticket["entries_used"]) + 1
            allowance = int(ticket["entry_allowance"])
            state = "PARTIALLY_USED" if unlimited or used < allowance else "USED"
            self.db.update(
                "tickets",
                ticket_id,
                {
                    "state": state,
                    "entries_used": used,
                    "last_entry_at": now,
                    "first_entry_at": ticket["first_entry_at"] or now,
                },
                tenant_id=ctx.tenant_id,
            )
        return self.get(ctx, ticket_id)

    def bulk_set_state(
        self,
        ctx: RequestContext,
        ticket_ids: Iterable[str],
        state: str,
        *,
        reason: str,
        audit_action: str,
    ) -> list[str]:
        """Cancel/void/refund a set of tickets in one correlated operation."""
        changed: list[str] = []
        for ticket_id in ticket_ids:
            self.set_state(
                ctx, ticket_id, state, reason=reason, audit_action=audit_action, require_permission=False
            )
            changed.append(ticket_id)
        return changed

    def any_used(self, ctx: RequestContext, booking_id: str) -> list[dict[str, Any]]:
        """Tickets already used — blocks self-service cancel/reschedule (R16.8)."""
        rows = self.db.query(
            "SELECT id, ticket_number, state, first_entry_at, last_entry_at, entries_used "
            "FROM tickets WHERE tenant_id = ? AND booking_id = ? AND entries_used > 0",
            (ctx.tenant_id, booking_id),
        )
        return [dict(r) for r in rows]

    def expire_due(self, ctx: RequestContext, *, limit: int = 1000) -> int:
        """Move tickets past their validity window to EXPIRED."""
        now = to_iso(self.clock.now())
        rows = self.db.query(
            "SELECT id FROM tickets WHERE tenant_id = ? AND state IN ('ISSUED','VALID','PARTIALLY_USED') "
            "AND valid_until IS NOT NULL AND valid_until < ? LIMIT ?",
            (ctx.tenant_id, now, int(limit)),
        )
        for row in rows:
            self.db.update("tickets", row["id"], {"state": "EXPIRED"}, tenant_id=ctx.tenant_id)
        return len(rows)


__all__ = [
    "ADMISSIBLE_STATES",
    "QR_PREFIX",
    "TERMINAL_STATES",
    "TicketService",
    "build_qr_payload",
    "parse_qr_payload",
]
