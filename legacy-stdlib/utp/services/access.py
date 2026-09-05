"""Gate and access validation (R32).

A scan turns a QR payload into exactly one decision from the closed set in
:data:`utp.domain.enums.ACCESS_DECISIONS`, and records that decision in the
append-only ``scan_events`` table (R32.2, R32.5). The decision is deterministic
and evaluated in a fixed order so that two guests presenting equivalent tickets
always get the same outcome.

Why the order matters
---------------------
The checks run cheapest-first and most-disqualifying-first, which is both correct
and fast (R32.1 targets a decision inside 500 ms):

1. **Signature** — ``parse_qr_payload`` verifies the HMAC before any database
   lookup, so a forged or garbage code is rejected as ``REJECT_UNKNOWN_CODE``
   without touching storage.
2. **Existence and tenant** — an unknown token, or one minted for another tenant,
   is ``REJECT_UNKNOWN_CODE`` and never discloses that the code belongs elsewhere.
3. **Venue / gate** — a ticket for a different venue is ``REJECT_WRONG_VENUE_OR_GATE``.
4. **Terminal state** — cancelled/refunded/voided/blocked/expired states map to
   their specific rejection so the operator sees *why*.
5. **Validity window** — before ``valid_from`` is ``REJECT_NOT_YET_VALID``; after
   ``valid_until`` (plus grace) is ``REJECT_EXPIRED``. The window was snapshotted
   in the venue timezone at issue, so a later timezone change cannot move it
   (settings spec §14).
6. **Date / session** — a mismatch against the ticket's visit date or bound
   session is ``REJECT_WRONG_DATE`` / ``REJECT_WRONG_SESSION``.
7. **Entry allowance** — the actual admit consumes an entry through the ticket
   service's atomic counter, so two simultaneous scans of the last allowed entry
   cannot both succeed (R32.3); the loser is ``REJECT_ALREADY_USED`` and is shown
   the time and gate of the previous admission.

Offline operation
-----------------
A device with no connectivity still validates against a signed local dataset and
queues its scans; :meth:`AccessService.sync_offline_scans` ingests the queue when
connectivity returns, and if the same single-entry ticket was admitted at more
than one access point it retains both records, flags the conflict and reports it
rather than silently dropping either (R32.6–R32.8).
"""

from __future__ import annotations

from typing import Any

from ..core.audit import AuditLog
from ..core.clock import Clock, local_iso, parse_instant, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.ids import hash_identifier, new_id
from ..domain import enums
from .authz import AuthorizationService

#: States that map straight to a specific rejection before the window is even
#: considered, so the operator is told exactly why (R32.2).
_STATE_REJECTIONS: dict[str, str] = {
    "CANCELLED": "REJECT_CANCELLED",
    "REFUNDED": "REJECT_REFUNDED",
    "VOIDED": "REJECT_VOIDED",
    "BLOCKED": "REJECT_BLOCKED",
    "EXPIRED": "REJECT_EXPIRED",
    "TRANSFERRED": "REJECT_UNKNOWN_CODE",
}

#: Human-facing message per decision. Never leaks a token error or internal id
#: to the operator or the guest (R32.2, R66.4).
_DECISION_MESSAGES: dict[str, str] = {
    "ADMIT": "Admit",
    "ADMIT_WITH_CHECK": "Admit — check proof",
    "REJECT_ALREADY_USED": "Already used",
    "REJECT_WRONG_DATE": "Wrong date",
    "REJECT_WRONG_SESSION": "Wrong session",
    "REJECT_WRONG_VENUE_OR_GATE": "Wrong venue or gate",
    "REJECT_CANCELLED": "Booking cancelled",
    "REJECT_REFUNDED": "Ticket refunded",
    "REJECT_VOIDED": "Ticket voided",
    "REJECT_BLOCKED": "Ticket blocked",
    "REJECT_NOT_YET_VALID": "Not yet valid",
    "REJECT_EXPIRED": "Ticket expired",
    "REJECT_UNKNOWN_CODE": "Unknown code",
}


class AccessService:
    """Evaluate and record admission at an access point."""

    #: Injected by :class:`utp.app.Platform` after construction.
    tickets: Any = None
    tenancy: Any = None

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
    # Scan
    # ------------------------------------------------------------------ #

    def scan(
        self,
        ctx: RequestContext,
        *,
        qr_payload: str,
        access_point_id: str | None = None,
        device_id: str | None = None,
        device_code: str | None = None,
        device_secret: str | None = None,
        offline_captured: bool = False,
        at_utc: str | None = None,
    ) -> dict[str, Any]:
        """Validate a scanned QR and record the decision (R32.1, R32.2, R32.5).

        ``device_code``/``device_secret`` authenticate the scanner when supplied;
        an unregistered or deactivated device is refused before any ticket lookup
        (R32.12). ``at_utc`` lets an offline-captured scan carry the moment it was
        actually taken rather than the moment it synced.
        """
        device = None
        if device_code:
            # Rejects unregistered/deactivated devices (raises NotFound → the caller
            # maps it to a generic refusal; existence is never disclosed).
            device = self.tenancy.authenticate_device(ctx, device_code, device_secret or "")
            device_id = device["id"]
            if access_point_id is None:
                access_point_id = device.get("access_point_id")

        moment = parse_instant(at_utc) if at_utc else self.clock.now()

        from .tickets import parse_qr_payload

        verified = parse_qr_payload(qr_payload)
        if not verified["valid"] or verified.get("tenant_id") != ctx.tenant_id:
            return self._record_and_return(
                ctx,
                decision="REJECT_UNKNOWN_CODE",
                ticket=None,
                qr_payload=qr_payload,
                access_point_id=access_point_id,
                device_id=device_id,
                moment=moment,
                offline_captured=offline_captured,
            )

        ticket = self.tickets.find_by_qr(ctx, qr_payload)
        if ticket is None:
            return self._record_and_return(
                ctx,
                decision="REJECT_UNKNOWN_CODE",
                ticket=None,
                qr_payload=qr_payload,
                access_point_id=access_point_id,
                device_id=device_id,
                moment=moment,
                offline_captured=offline_captured,
            )

        decision, admit_extra = self._evaluate(
            ctx, ticket=ticket, access_point_id=access_point_id, moment=moment
        )
        return self._record_and_return(
            ctx,
            decision=decision,
            ticket=ticket,
            qr_payload=qr_payload,
            access_point_id=access_point_id,
            device_id=device_id,
            moment=moment,
            offline_captured=offline_captured,
            extra=admit_extra,
        )

    def _evaluate(
        self,
        ctx: RequestContext,
        *,
        ticket: dict[str, Any],
        access_point_id: str | None,
        moment: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Return ``(decision, extra)`` for a resolved ticket at ``moment``.

        ``extra`` carries the previous-admission detail on a duplicate, and the
        proof note on an ADMIT_WITH_CHECK, so the operator sees the specifics.
        """
        # Venue / gate (R32.2). The access point, when known, must belong to the
        # ticket's venue; a ticket for another venue never admits here.
        if access_point_id is not None:
            ap = self.db.query_one(
                "SELECT venue_id FROM access_points WHERE id = ? AND tenant_id = ?",
                (access_point_id, ctx.tenant_id),
            )
            if ap is None or ap["venue_id"] != ticket["venue_id"]:
                return "REJECT_WRONG_VENUE_OR_GATE", {}

        state = ticket["state"]
        if state in _STATE_REJECTIONS:
            return _STATE_REJECTIONS[state], {}

        # A fully used single/multi-entry ticket is already spent.
        if state == "USED":
            return "REJECT_ALREADY_USED", self._previous_admission(ctx, ticket)

        if state not in ("ISSUED", "VALID", "PARTIALLY_USED"):
            # Any other non-admissible state is treated as unknown rather than
            # guessed at, so a new state can never silently admit.
            return "REJECT_UNKNOWN_CODE", {}

        # Validity window (venue-local, snapshotted at issue — settings §14).
        grace = int((ticket.get("validity_policy_json") or {}).get("grace_minutes") or 0) if isinstance(
            ticket.get("validity_policy_json"), dict
        ) else 0
        valid_from = ticket.get("valid_from")
        valid_until = ticket.get("valid_until")
        if valid_from and moment < parse_instant(valid_from):
            return "REJECT_NOT_YET_VALID", {}
        if valid_until:
            from ..core.clock import add_minutes

            hard_expiry = add_minutes(parse_instant(valid_until), grace)
            if moment > hard_expiry:
                return "REJECT_EXPIRED", {}

        # Entry allowance and re-entry window.
        entries_used = int(ticket["entries_used"])
        allowance = int(ticket["entry_allowance"])
        unlimited = allowance < 0
        if not unlimited and entries_used >= allowance:
            return "REJECT_ALREADY_USED", self._previous_admission(ctx, ticket)

        # A repeat entry is only permitted inside the configured re-entry window
        # (R32.4). Outside it, the earlier admission has "closed".
        if entries_used > 0:
            window = ticket.get("reentry_window_minutes")
            last_entry = ticket.get("last_entry_at")
            if window and last_entry:
                from ..core.clock import minutes_between

                elapsed = minutes_between(parse_instant(last_entry), moment)
                if elapsed > float(window):
                    return "REJECT_ALREADY_USED", self._previous_admission(ctx, ticket)
            elif not window:
                # No re-entry configured and an entry already recorded: spent.
                return "REJECT_ALREADY_USED", self._previous_admission(ctx, ticket)

        # Proof requirement flags the admit, it does not block it (R3.6, R4.5).
        if int(ticket.get("proof_required") or 0):
            return "ADMIT_WITH_CHECK", {"proof_required": True}
        return "ADMIT", {}

    def _previous_admission(self, ctx: RequestContext, ticket: dict[str, Any]) -> dict[str, Any]:
        """Time and gate of the last admit for this ticket (R32.3)."""
        row = self.db.query_one(
            "SELECT at_utc, at_local, access_point_id FROM scan_events "
            "WHERE tenant_id = ? AND ticket_id = ? AND decision IN ('ADMIT','ADMIT_WITH_CHECK') "
            "ORDER BY at_utc DESC LIMIT 1",
            (ctx.tenant_id, ticket["id"]),
        )
        if row is None:
            return {
                "previous_admission": {
                    "at_utc": ticket.get("last_entry_at"),
                    "at_local": None,
                    "access_point_id": None,
                }
            }
        ap_name = None
        if row["access_point_id"]:
            ap = self.db.query_one(
                "SELECT code FROM access_points WHERE id = ? AND tenant_id = ?",
                (row["access_point_id"], ctx.tenant_id),
            )
            ap_name = ap["code"] if ap else None
        return {
            "previous_admission": {
                "at_utc": row["at_utc"],
                "at_local": row["at_local"],
                "access_point": ap_name,
            }
        }

    def _record_and_return(
        self,
        ctx: RequestContext,
        *,
        decision: str,
        ticket: dict[str, Any] | None,
        qr_payload: str,
        access_point_id: str | None,
        device_id: str | None,
        moment: Any,
        offline_captured: bool,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume the entry for an admit, then persist and shape the response.

        The entry is consumed *before* the scan row is written so that if the
        atomic increment loses a concurrent race the decision is corrected to
        REJECT_ALREADY_USED and the correct outcome is what gets recorded (R32.3).
        """
        extra = dict(extra or {})
        venue_id = ticket["venue_id"] if ticket else ctx.venue_id
        if decision in enums.ADMIT_DECISIONS and ticket is not None:
            try:
                updated = self.tickets.record_entry(ctx, ticket["id"])
                extra["entries_used"] = int(updated["entries_used"])
                extra["entries_remaining"] = updated["entries_remaining"]
            except ConflictError:
                # Lost the race for the last entry — someone else got in first.
                decision = "REJECT_ALREADY_USED"
                extra = self._previous_admission(ctx, ticket)

        tz = None
        if venue_id:
            tz = self.db.scalar(
                "SELECT timezone FROM venues WHERE id = ? AND tenant_id = ?",
                (venue_id, ctx.tenant_id),
            )
        at_utc = to_iso(moment)
        at_local = local_iso(moment, tz) if tz else at_utc

        event_id = new_id("scn")
        self.db.insert(
            "scan_events",
            {
                "id": event_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id or "",
                "access_point_id": access_point_id,
                "device_id": device_id or ctx.device_id,
                "ticket_id": ticket["id"] if ticket else None,
                "booking_id": ticket["booking_id"] if ticket else None,
                # Never store the raw QR — only a non-reversible hash, so the scan
                # log cannot be used to reconstruct a working code (R15.2, R45.9).
                "qr_token_hash": hash_identifier(qr_payload) if qr_payload else None,
                "decision": decision,
                "reason": _DECISION_MESSAGES.get(decision),
                "at_utc": at_utc,
                "at_local": at_local,
                "operator_id": ctx.principal.id,
                "offline_captured": 1 if offline_captured else 0,
                "correlation_id": ctx.correlation_id,
            },
        )
        return self._response(event_id, decision, ticket, extra)

    def _response(
        self,
        event_id: str,
        decision: str,
        ticket: dict[str, Any] | None,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "scan_id": event_id,
            "decision": decision,
            "admit": decision in enums.ADMIT_DECISIONS,
            "message": _DECISION_MESSAGES.get(decision, decision),
        }
        if ticket is not None:
            # Only what a gate operator operationally needs; full personal data is
            # never surfaced to a device (R32.11). Masking is enforced at the API.
            result["ticket"] = {
                "ticket_number": ticket["ticket_number"],
                "ticket_type_id": ticket["ticket_type_id"],
                "visit_date": ticket.get("visit_date"),
                "proof_required": bool(ticket.get("proof_required")),
            }
        result.update(extra)
        return result

    # ------------------------------------------------------------------ #
    # Override (R32.9)
    # ------------------------------------------------------------------ #

    def override_admit(
        self,
        ctx: RequestContext,
        *,
        scan_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Admit a guest whose scan was rejected, on an operator's authority.

        Requires ``OVERRIDE_ACCESS`` and a mandatory reason; the override is
        audited with the original rejection reason so the review report can see
        both (R32.9, residual-risk D.2).
        """
        self.authz.require_action(
            ctx, "OVERRIDE_ACCESS", reason=reason, target_type="scan_event", target_id=scan_id
        )
        original = self.db.query_one(
            "SELECT * FROM scan_events WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, scan_id)
        )
        if original is None:
            raise NotFound(details={"entity": "scan_event"})
        if original["decision"] in enums.ADMIT_DECISIONS:
            raise ConflictError("That scan already admitted the guest.")
        vctx = ctx.for_venue(original["venue_id"]) if original["venue_id"] else ctx
        moment = self.clock.now()
        tz = self.db.scalar(
            "SELECT timezone FROM venues WHERE id = ? AND tenant_id = ?",
            (original["venue_id"], ctx.tenant_id),
        )
        override_id = new_id("scn")
        # A distinct scan row records the override admission; the original
        # rejection stays intact and immutable (R32.8 guard).
        self.db.insert(
            "scan_events",
            {
                "id": override_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": original["venue_id"],
                "access_point_id": original["access_point_id"],
                "device_id": original["device_id"],
                "ticket_id": original["ticket_id"],
                "booking_id": original["booking_id"],
                "qr_token_hash": original["qr_token_hash"],
                "decision": "ADMIT",
                "reason": "Override admit",
                "at_utc": to_iso(moment),
                "at_local": local_iso(moment, tz) if tz else to_iso(moment),
                "operator_id": ctx.principal.id,
                "override_actor_id": ctx.principal.id,
                "override_reason": reason,
                "correlation_id": ctx.correlation_id,
            },
        )
        if original["ticket_id"]:
            try:
                self.tickets.record_entry(ctx, original["ticket_id"])
            except ConflictError:
                pass  # allowance already spent; the override still admits the person
        self.audit.record(
            vctx,
            "OVERRIDE_ACCESS",
            target_type="scan_event",
            target_id=override_id,
            previous={"original_decision": original["decision"]},
            new={"decision": "ADMIT", "original_scan_id": scan_id},
            reason=reason,
            severity="WARNING",
            venue_timezone=tz,
        )
        return {"scan_id": override_id, "decision": "ADMIT", "admit": True, "overridden": scan_id}

    # ------------------------------------------------------------------ #
    # Manual lookup (R32.10)
    # ------------------------------------------------------------------ #

    def manual_lookup(
        self, ctx: RequestContext, *, booking_number: str, venue_id: str | None = None
    ) -> dict[str, Any]:
        """Find a booking's tickets when the QR cannot be scanned (R32.10).

        Gated by ``Tickets.VIEW``; personal data stays masked unless the operator
        holds ``VIEW_PII`` (R32.11), which the API response layer enforces.
        """
        self.authz.require_page(ctx, "Tickets", "VIEW")
        booking = self.db.query_one(
            "SELECT id, venue_id, status FROM bookings WHERE tenant_id = ? AND booking_number = ?",
            (ctx.tenant_id, (booking_number or "").strip().upper()),
        )
        if booking is None:
            raise NotFound(details={"entity": "booking"})
        self.authz.require_venue(ctx.for_venue(booking["venue_id"]), booking["venue_id"])
        tickets = self.tickets.list_for_booking(ctx, booking["id"])
        return {
            "booking_number": booking_number,
            "status": booking["status"],
            "tickets": [
                {
                    "ticket_id": t["id"],
                    "ticket_number": t["ticket_number"],
                    "state": t["state"],
                    "visit_date": t.get("visit_date"),
                    "entries_used": int(t["entries_used"]),
                    "entries_remaining": t["entries_remaining"],
                    "proof_required": bool(t.get("proof_required")),
                }
                for t in tickets
            ],
        }

    # ------------------------------------------------------------------ #
    # Offline synchronisation (R32.6 - R32.8)
    # ------------------------------------------------------------------ #

    def sync_offline_scans(
        self, ctx: RequestContext, *, scans: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Ingest scans a device captured while offline (R32.6, R32.8).

        Each device decided its own outcome against a signed local dataset while
        offline, so the queued scan carries the decision it *made at the gate* — the
        sync records that decision as taken, it does not re-evaluate it against the
        current database (re-evaluating would rewrite history and hide the very
        conflict we must surface). The captured decision defaults to ADMIT when the
        device does not state one.

        After ingestion, any single-entry ticket that admitted at more than one
        access point is flagged as a conflict and reported. Every record is
        retained; neither admission is discarded (R32.8).
        """
        results: list[dict[str, Any]] = []
        touched_tickets: set[str] = set()
        for item in scans:
            payload = item.get("qr_payload", "")
            token = self._token_of(payload)
            ticket_row = (
                self.db.query_one(
                    "SELECT id, booking_id, venue_id FROM tickets WHERE qr_token = ? AND tenant_id = ?",
                    (token, ctx.tenant_id),
                )
                if token
                else None
            )
            decision = item.get("decision") or ("ADMIT" if ticket_row is not None else "REJECT_UNKNOWN_CODE")
            if decision not in enums.ACCESS_DECISIONS:
                raise ValidationError({"decision": "Unknown offline scan decision."})
            outcome = self._store_offline_scan(
                ctx,
                decision=decision,
                ticket_row=ticket_row,
                payload=payload,
                access_point_id=item.get("access_point_id"),
                device_id=item.get("device_id"),
                at_utc=item.get("at_utc"),
            )
            results.append(outcome)
            if ticket_row is not None:
                touched_tickets.add(ticket_row["id"])

        conflicts = self._detect_offline_conflicts(ctx, touched_tickets)
        return {"ingested": len(results), "results": results, "conflicts": conflicts}

    def _store_offline_scan(
        self,
        ctx: RequestContext,
        *,
        decision: str,
        ticket_row: dict[str, Any] | None,
        payload: str,
        access_point_id: str | None,
        device_id: str | None,
        at_utc: str | None,
    ) -> dict[str, Any]:
        """Persist one offline-captured scan exactly as the device decided it."""
        moment = parse_instant(at_utc) if at_utc else self.clock.now()
        venue_id = ticket_row["venue_id"] if ticket_row else ctx.venue_id
        tz = None
        if venue_id:
            tz = self.db.scalar(
                "SELECT timezone FROM venues WHERE id = ? AND tenant_id = ?",
                (venue_id, ctx.tenant_id),
            )
        # An offline admit still consumes an entry so the online allowance stays
        # consistent; if the allowance was already spent the increment simply fails
        # and the conflict pass below catches the double admission.
        if decision in enums.ADMIT_DECISIONS and ticket_row is not None:
            try:
                self.tickets.record_entry(ctx, ticket_row["id"])
            except ConflictError:
                pass
        event_id = new_id("scn")
        self.db.insert(
            "scan_events",
            {
                "id": event_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id or "",
                "access_point_id": access_point_id,
                "device_id": device_id or ctx.device_id,
                "ticket_id": ticket_row["id"] if ticket_row else None,
                "booking_id": ticket_row["booking_id"] if ticket_row else None,
                "qr_token_hash": hash_identifier(payload) if payload else None,
                "decision": decision,
                "reason": _DECISION_MESSAGES.get(decision),
                "at_utc": to_iso(moment),
                "at_local": local_iso(moment, tz) if tz else to_iso(moment),
                "operator_id": ctx.principal.id,
                "offline_captured": 1,
                "synced_at": to_iso(self.clock.now()),
                "correlation_id": ctx.correlation_id,
            },
        )
        return {"scan_id": event_id, "decision": decision, "admit": decision in enums.ADMIT_DECISIONS}

    @staticmethod
    def _token_of(qr_payload: str) -> str:
        from .tickets import parse_qr_payload

        parsed = parse_qr_payload(qr_payload)
        return parsed.get("token", "") if parsed.get("valid") else ""

    def _detect_offline_conflicts(
        self, ctx: RequestContext, ticket_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Flag single-entry tickets admitted at more than one access point (R32.8)."""
        conflicts: list[dict[str, Any]] = []
        for ticket_id in ticket_ids:
            ticket = self.db.query_one(
                "SELECT id, entry_allowance FROM tickets WHERE id = ? AND tenant_id = ?",
                (ticket_id, ctx.tenant_id),
            )
            if ticket is None or int(ticket["entry_allowance"]) != 1:
                continue
            admits = self.db.query(
                "SELECT id, access_point_id, at_utc FROM scan_events "
                "WHERE tenant_id = ? AND ticket_id = ? AND decision IN ('ADMIT','ADMIT_WITH_CHECK') "
                "ORDER BY at_utc",
                (ctx.tenant_id, ticket_id),
            )
            distinct_points = {a["access_point_id"] for a in admits if a["access_point_id"]}
            if len(admits) > 1 and len(distinct_points) > 1:
                # Retain every record; flag them; report it. Never discard either.
                for a in admits:
                    self.db.update(
                        "scan_events", a["id"], {"conflict_flag": 1}, tenant_id=ctx.tenant_id
                    )
                self.audit.record(
                    ctx,
                    "OFFLINE_SCAN_CONFLICT",
                    target_type="ticket",
                    target_id=ticket_id,
                    new={
                        "admissions": len(admits),
                        "access_points": sorted(p for p in distinct_points),
                    },
                    severity="WARNING",
                )
                conflicts.append(
                    {
                        "ticket_id": ticket_id,
                        "admissions": len(admits),
                        "access_points": sorted(p for p in distinct_points),
                    }
                )
        return conflicts

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def scan_history(
        self, ctx: RequestContext, *, ticket_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent scan events, tenant- and venue-scoped (R32.5)."""
        self.authz.require_page(ctx, "Tickets", "VIEW")
        scoped = self.authz.scoped_venue_ids(ctx)
        sql = ["SELECT * FROM scan_events WHERE tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if ticket_id:
            sql.append("AND ticket_id = ?")
            params.append(ticket_id)
        if scoped is not None:
            if not scoped:
                return []
            placeholders = ", ".join("?" for _ in scoped)
            sql.append(f"AND venue_id IN ({placeholders})")
            params.extend(scoped)
        sql.append("ORDER BY at_utc DESC LIMIT ?")
        params.append(int(limit))
        return [dict(r) for r in self.db.query(" ".join(sql), params)]


__all__ = ["AccessService"]
