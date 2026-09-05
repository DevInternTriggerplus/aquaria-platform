"""Row-level report queries — the ledger behind every summary figure.

Separate from :mod:`metrics` because these are lists, not aggregates: a summary
answers "how much", these answer "which ones", and the drill-down path from the
first to the second is the whole point of the module (§16, §48).

Each function takes ``(db, scope)`` and nothing else. They cannot mask, audit or
check permission, and that is deliberate — the service does all three centrally,
so a new report added here cannot forget to. Every query is bounded by
``scope.venue_clause`` and the date window, so an out-of-scope row is
unreachable rather than merely unrendered (R43.7).

Personal fields are selected raw and masked afterwards by the service, which is
also what makes an unmasked read auditable in one place (R12.24).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..core.clock import local, parse_instant, timezone_for
from ..core.db import decode
from ..core.i18n import text as i18n_text
from .metrics import Scope, _safe_div, _share_bp

_ACTIVE = "bi.state = 'ACTIVE'"


def _lang(scope: Scope) -> str:
    return scope.filters.get("language") or "en"


def _today(scope: Scope) -> _dt.date:
    return _dt.datetime.now(timezone_for(scope.timezone)).date()


def _now_local(scope: Scope) -> _dt.datetime:
    return _dt.datetime.now(timezone_for(scope.timezone))


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def arrivals(db, scope: Scope) -> list[dict[str, Any]]:
    """Who is due today, and where each party has got to (§18).

    The four states are derived, not stored: a booking does not know it is late,
    it is late because its session has started and nobody has scanned. So the
    rule lives here and is explained rather than hidden behind a column.
    """
    clause, venue_params = scope.venue_clause("b")
    rows = db.query(
        f"""
        SELECT b.id, b.booking_number, b.visit_date, b.status, b.session_id,
               c.full_name AS customer_name,
               s.start_time, s.end_time,
               (SELECT COALESCE(SUM(bi.quantity), 0) FROM booking_items bi
                 WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id AND {_ACTIVE}) AS party_size,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.booking_id = b.id AND t.tenant_id = b.tenant_id) AS ticket_count,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.booking_id = b.id AND t.tenant_id = b.tenant_id AND t.entries_used > 0) AS admitted,
               (SELECT tt.name_json FROM booking_items bi
                  JOIN ticket_types tt ON tt.id = bi.ticket_type_id AND tt.tenant_id = bi.tenant_id
                 WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id AND {_ACTIVE}
                 LIMIT 1) AS tt_name
        FROM bookings b
        LEFT JOIN customer_pii c ON c.customer_id = b.customer_id AND c.tenant_id = b.tenant_id
        LEFT JOIN sessions s ON s.id = b.session_id AND s.tenant_id = b.tenant_id
        WHERE b.tenant_id = ?{clause} AND b.visit_date BETWEEN ? AND ?
          AND b.status IN ('CONFIRMED','PARTIALLY_REFUNDED')
        ORDER BY COALESCE(s.start_time, '99:99'), b.booking_number
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    now = _now_local(scope)
    today = now.date()
    language = _lang(scope)
    result = []
    for row in rows:
        admitted = int(row["admitted"] or 0)
        total = int(row["ticket_count"] or 0)
        visit = row["visit_date"]
        start = row["start_time"]

        if admitted and admitted >= total:
            code, label = "CHECKED_IN", "Checked in"
        elif admitted:
            code, label = "CHECKED_IN", f"Partly in ({admitted}/{total})"
        elif visit < today.isoformat():
            # The day is over and nobody scanned.
            code, label = "NO_SHOW", "No-show"
        elif visit == today.isoformat() and start and start < now.strftime("%H:%M"):
            code, label = "LATE", "Late"
        else:
            code, label = "ARRIVING", "Arriving soon"

        result.append(
            {
                "booking_id": row["id"],
                "visit_time": start or "",
                "booking_number": row["booking_number"],
                "customer": row["customer_name"] or "",
                "party_size": int(row["party_size"] or 0),
                "ticket_type": i18n_text(decode(row["tt_name"], {}), language),
                "session": f"{start}-{row['end_time']}" if start else "Any time",
                "state": label,
                "state_code": code,
            }
        )
    return result


def bookings(db, scope: Scope) -> list[dict[str, Any]]:
    """Orders with payment and check-in state (§19)."""
    where, params = scope.booking_where()
    rows = db.query(
        f"""
        SELECT b.id, b.booking_number, b.created_at, b.visit_date, b.status, b.channel,
               b.net_minor, b.base_currency_minor,
               c.full_name AS customer_name,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.booking_id = b.id AND t.tenant_id = b.tenant_id) AS tickets,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.booking_id = b.id AND t.tenant_id = b.tenant_id AND t.entries_used > 0) AS admitted
        FROM bookings b
        LEFT JOIN customer_pii c ON c.customer_id = b.customer_id AND c.tenant_id = b.tenant_id
        {where}
        ORDER BY b.created_at DESC
        LIMIT 500
        """,
        params,
    )
    result = []
    for row in rows:
        tickets = int(row["tickets"] or 0)
        admitted = int(row["admitted"] or 0)
        if not tickets:
            checkin = "No tickets"
        elif admitted >= tickets:
            checkin = "Fully admitted"
        elif admitted:
            checkin = f"Partly admitted ({admitted}/{tickets})"
        else:
            checkin = "Not arrived"
        result.append(
            {
                "booking_id": row["id"],
                "booking_number": row["booking_number"],
                "created_at": _local_stamp(row["created_at"], scope),
                "visit_date": row["visit_date"],
                "customer": row["customer_name"] or "",
                "tickets": tickets,
                "net_minor": int(row["base_currency_minor"] or row["net_minor"] or 0),
                "status": _title(row["status"]),
                "checkin_state": checkin,
                "channel": row["channel"],
            }
        )
    return result


def sales(db, scope: Scope) -> list[dict[str, Any]]:
    """Transaction-level sales, with the currency and rate each order used (§33)."""
    where, params = scope.booking_where()
    rows = db.query(
        f"""
        SELECT b.booking_number, b.created_at, b.visit_date, b.channel, b.status,
               b.gross_minor, b.discount_minor, b.service_charge_minor, b.tax_minor,
               b.net_minor, b.base_currency_minor, b.refunded_minor,
               b.transaction_currency, b.base_currency, b.exchange_rate_text,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.booking_id = b.id AND t.tenant_id = b.tenant_id) AS tickets
        FROM bookings b
        {where}
        ORDER BY b.created_at DESC
        LIMIT 1000
        """,
        params,
    )
    return [
        {
            "booking_number": row["booking_number"],
            "created_at": _local_stamp(row["created_at"], scope),
            "visit_date": row["visit_date"],
            "channel": row["channel"],
            "tickets": int(row["tickets"] or 0),
            "gross_minor": int(row["gross_minor"] or 0),
            "discount_minor": int(row["discount_minor"] or 0),
            "service_charge_minor": int(row["service_charge_minor"] or 0),
            "tax_minor": int(row["tax_minor"] or 0),
            # Net of anything refunded, matching the headline definition — a ledger
            # that disagrees with the summary it explains is worse than no ledger.
            "net_minor": int(row["base_currency_minor"] or row["net_minor"] or 0)
            - int(row["refunded_minor"] or 0),
            "transaction_currency": row["transaction_currency"] or row["base_currency"] or "",
            # Blank rather than "1.0" when no conversion happened: showing a rate
            # for a same-currency order invites the reader to look for a
            # conversion that never took place.
            "exchange_rate_text": row["exchange_rate_text"] or "",
            "status": _title(row["status"]),
        }
        for row in rows
    ]


def shifts(db, scope: Scope) -> list[dict[str, Any]]:
    """Cash reconciliation per shift (§22)."""
    clause, venue_params = scope.venue_clause("sh")
    rows = db.query(
        f"""
        SELECT sh.*, st.display_name AS staff_name, st.email AS staff_email
        FROM shift_sessions sh
        LEFT JOIN staff st ON st.id = sh.staff_id AND st.tenant_id = sh.tenant_id
        WHERE sh.tenant_id = ?{clause} AND date(sh.opened_at) BETWEEN ? AND ?
        ORDER BY sh.opened_at DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    result = []
    for row in rows:
        variance = int(row["variance_minor"] or 0)
        status = row["status"]
        if status == "OPEN":
            state = "Open"
        elif variance == 0:
            state = "Balanced"
        elif variance > 0:
            state = f"Over by {variance}"
        else:
            state = f"Short by {abs(variance)}"
        result.append(
            {
                "staff": row["staff_name"] or row["staff_email"] or "",
                "counter_code": row["counter_code"] or "",
                "opened_at": _local_stamp(row["opened_at"], scope),
                "closed_at": _local_stamp(row["closed_at"], scope),
                "opening_float_minor": int(row["opening_float_minor"] or 0),
                "expected_minor": int(row["expected_minor"] or 0),
                "counted_minor": int(row["counted_minor"] or 0),
                "variance_minor": variance,
                "state": state,
            }
        )
    return result


def counter_sales(db, scope: Scope) -> list[dict[str, Any]]:
    """Counter production grouped by counter and cashier (§21)."""
    clause, venue_params = scope.venue_clause("b")
    rows = db.query(
        f"""
        SELECT COALESCE(st.display_name, st.email, 'Unattributed') AS label,
               COUNT(b.id) AS transactions,
               COALESCE(SUM(b.gross_minor), 0) AS gross_minor,
               COALESCE(SUM(b.discount_minor), 0) AS discount_minor,
               COALESCE(SUM(CASE WHEN b.status IN ('CONFIRMED','PARTIALLY_REFUNDED')
                                 THEN b.net_minor ELSE 0 END), 0) AS net_minor,
               COALESCE(SUM(b.refunded_minor), 0) AS refund_minor,
               COALESCE(SUM(CASE WHEN b.status = 'VOIDED' THEN b.gross_minor ELSE 0 END), 0) AS void_minor,
               (SELECT COUNT(*) FROM tickets t JOIN bookings b2 ON b2.id = t.booking_id
                 WHERE b2.staff_actor_id = b.staff_actor_id AND t.tenant_id = b.tenant_id
                   AND t.visit_date BETWEEN ? AND ?) AS tickets
        FROM bookings b
        LEFT JOIN staff st ON st.id = b.staff_actor_id AND st.tenant_id = b.tenant_id
        WHERE b.tenant_id = ?{clause} AND b.visit_date BETWEEN ? AND ?
          AND b.channel IN ('COUNTER','STAFF')
        GROUP BY b.staff_actor_id
        ORDER BY net_minor DESC
        """,
        [scope.date_from, scope.date_to, scope.tenant_id, *venue_params,
         scope.date_from, scope.date_to],
    )
    return [
        {
            "label": row["label"],
            "transactions": int(row["transactions"] or 0),
            "tickets": int(row["tickets"] or 0),
            "gross_minor": int(row["gross_minor"] or 0),
            "discount_minor": int(row["discount_minor"] or 0),
            "net_minor": int(row["net_minor"] or 0),
            "refund_minor": int(row["refund_minor"] or 0),
            "void_minor": int(row["void_minor"] or 0),
        }
        for row in rows
    ]


def refunds_and_voids(db, scope: Scope) -> list[dict[str, Any]]:
    """Money reversed, and whether the ticket had already been used (§23)."""
    clause, venue_params = scope.venue_clause("b")
    rows = db.query(
        f"""
        SELECT r.id, r.kind, r.amount_minor, r.reason, r.created_at, r.status,
               b.booking_number, b.net_minor AS original_minor,
               pm.method,
               actor.display_name AS actor_name, actor.email AS actor_email,
               appr.display_name AS approver_name, appr.email AS approver_email,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.booking_id = b.id AND t.tenant_id = b.tenant_id
                   AND t.entries_used > 0) AS used_tickets
        FROM refunds r
        JOIN bookings b ON b.id = r.booking_id AND b.tenant_id = r.tenant_id
        LEFT JOIN payments pm ON pm.id = r.payment_id AND pm.tenant_id = r.tenant_id
        LEFT JOIN staff actor ON actor.id = r.actor_id AND actor.tenant_id = r.tenant_id
        LEFT JOIN staff appr ON appr.id = r.approver_id AND appr.tenant_id = r.tenant_id
        WHERE r.tenant_id = ?{clause} AND date(r.created_at) BETWEEN ? AND ?
        ORDER BY r.created_at DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    result = [
        {
            "kind": _title(row["kind"]),
            "reference": row["id"],
            "booking_number": row["booking_number"],
            "original_minor": int(row["original_minor"] or 0),
            "amount_minor": int(row["amount_minor"] or 0),
            "reason": row["reason"] or "",
            "method": row["method"] or "",
            "actor": row["actor_name"] or row["actor_email"] or "",
            "approver": row["approver_name"] or row["approver_email"] or "",
            # Refunding a used ticket is the case that needs a second look, so it
            # is stated rather than left to be inferred (R17.3).
            "ticket_used": "Yes" if int(row["used_tickets"] or 0) else "No",
            "created_at": _local_stamp(row["created_at"], scope),
        }
        for row in rows
    ]

    # Voids live on the booking, not in the refunds table, but finance reads them
    # together — a reversal is a reversal.
    void_rows = db.query(
        f"""
        SELECT b.booking_number, b.gross_minor, b.cancel_reason, b.cancelled_at, b.channel,
               st.display_name AS actor_name, st.email AS actor_email
        FROM bookings b
        LEFT JOIN staff st ON st.id = b.staff_actor_id AND st.tenant_id = b.tenant_id
        WHERE b.tenant_id = ?{clause} AND b.status = 'VOIDED'
          AND date(COALESCE(b.cancelled_at, b.created_at)) BETWEEN ? AND ?
        ORDER BY b.cancelled_at DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    result.extend(
        {
            "kind": "Void",
            "reference": row["booking_number"],
            "booking_number": row["booking_number"],
            "original_minor": int(row["gross_minor"] or 0),
            "amount_minor": int(row["gross_minor"] or 0),
            "reason": row["cancel_reason"] or "",
            "method": row["channel"] or "",
            "actor": row["actor_name"] or row["actor_email"] or "",
            "approver": "",
            "ticket_used": "No",
            "created_at": _local_stamp(row["cancelled_at"], scope),
        }
        for row in void_rows
    )
    result.sort(key=lambda entry: entry["created_at"], reverse=True)
    return result


# --------------------------------------------------------------------------- #
# Finance
# --------------------------------------------------------------------------- #


def discounts(db, scope: Scope) -> list[dict[str, Any]]:
    """Every discount, attributed to where it came from (§24).

    A promotion redemption and a cashier's manual reduction are both "discount"
    on the order total but they are different management problems, so the source
    is separated rather than summed.
    """
    clause, venue_params = scope.venue_clause("b")
    promo_rows = db.query(
        f"""
        SELECT b.booking_number, b.gross_minor, b.net_minor, rd.amount_minor, rd.created_at,
               pr.name_json, pr.code, pr.mechanic
        FROM promotion_redemptions rd
        JOIN bookings b ON b.id = rd.booking_id AND b.tenant_id = rd.tenant_id
        JOIN promotions pr ON pr.id = rd.promotion_id AND pr.tenant_id = rd.tenant_id
        WHERE rd.tenant_id = ?{clause} AND date(rd.created_at) BETWEEN ? AND ?
          AND rd.state = 'APPLIED'
        ORDER BY rd.created_at DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    language = _lang(scope)
    result = [
        {
            "source": _promotion_source(row["mechanic"]),
            "booking_number": row["booking_number"],
            "original_minor": int(row["gross_minor"] or 0),
            "discount_minor": int(row["amount_minor"] or 0),
            "net_minor": int(row["net_minor"] or 0),
            "reference": i18n_text(decode(row["name_json"], {}), language, fallback=row["code"]),
            "actor": "",
            "created_at": _local_stamp(row["created_at"], scope),
        }
        for row in promo_rows
    ]
    result.extend(manual_discounts(db, scope, as_discount_rows=True))
    result.sort(key=lambda entry: entry["created_at"], reverse=True)
    return result


def _promotion_source(mechanic: str | None) -> str:
    code = (mechanic or "").upper()
    if code in ("COUPON_CODE",):
        return "Coupon"
    if code in ("VOUCHER",):
        return "Voucher"
    if code in ("MEMBER_PROMOTION",):
        return "Member discount"
    if code in ("PARTNER_PROMOTION",):
        return "Partner discount"
    if code in ("SPECIAL_PRICE",):
        return "Special price"
    return "Promotion"


def manual_discounts(db, scope: Scope, *, as_discount_rows: bool = False) -> list[dict[str, Any]]:
    """Staff-applied discounts, from the audit trail (§25).

    Sourced from audit events rather than a discount column, because the thing
    under control is *who decided* to reduce a price — and that only exists in
    the audit record, together with the mandatory reason (R34.6).
    """
    clause, venue_params = scope.venue_clause("ae")
    rows = db.query(
        f"""
        SELECT ae.at_utc, ae.actor_id, ae.new_json, ae.reason, ae.target_id,
               st.display_name AS actor_name, st.email AS actor_email,
               b.booking_number, b.gross_minor, b.discount_minor, b.net_minor
        FROM audit_events ae
        LEFT JOIN staff st ON st.id = ae.actor_id AND st.tenant_id = ae.tenant_id
        LEFT JOIN bookings b ON b.id = ae.target_id AND b.tenant_id = ae.tenant_id
        WHERE ae.tenant_id = ?{clause} AND ae.action = 'MANUAL_DISCOUNT'
          AND date(ae.at_utc) BETWEEN ? AND ?
        ORDER BY ae.at_utc DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    threshold_bp = int(scope.filters.get("manual_discount_threshold_bp") or 2_000)
    out: list[dict[str, Any]] = []
    for row in rows:
        detail = decode(row["new_json"], {}) or {}
        amount = int(detail.get("discount_minor") or row["discount_minor"] or 0)
        original = int(detail.get("gross_minor") or row["gross_minor"] or 0)
        rate_bp = _share_bp(amount, original)
        actor = row["actor_name"] or row["actor_email"] or ""
        if as_discount_rows:
            out.append(
                {
                    "source": "Manual discount",
                    "booking_number": row["booking_number"] or "",
                    "original_minor": original,
                    "discount_minor": amount,
                    "net_minor": int(row["net_minor"] or 0),
                    "reference": row["reason"] or "",
                    "actor": actor,
                    "created_at": _local_stamp(row["at_utc"], scope),
                }
            )
            continue
        out.append(
            {
                "actor": actor,
                "booking_number": row["booking_number"] or "",
                "rate_bp": rate_bp,
                "discount_minor": amount,
                "reason": row["reason"] or "",
                "approver": detail.get("approver") or "",
                "created_at": _local_stamp(row["at_utc"], scope),
                "flagged": "Above threshold" if rate_bp > threshold_bp else "",
            }
        )
    return out


def complimentary(db, scope: Scope) -> list[dict[str, Any]]:
    """Tickets given away, their value and who authorised them (§26)."""
    clause, venue_params = scope.venue_clause("b")
    rows = db.query(
        f"""
        SELECT tt.name_json AS tt_name, tt.code AS tt_code,
               COUNT(t.id) AS quantity,
               COALESCE(SUM(bi.unit_price_minor), 0) AS value_minor,
               b.cancel_reason, b.notes, b.visit_date,
               c.full_name AS customer_name,
               st.display_name AS actor_name, st.email AS actor_email,
               COALESCE(SUM(CASE WHEN t.entries_used > 0 THEN 1 ELSE 0 END), 0) AS admitted
        FROM tickets t
        JOIN bookings b ON b.id = t.booking_id AND b.tenant_id = t.tenant_id
        JOIN payments pm ON pm.booking_id = b.id AND pm.tenant_id = b.tenant_id
        JOIN ticket_types tt ON tt.id = t.ticket_type_id AND tt.tenant_id = t.tenant_id
        LEFT JOIN booking_items bi ON bi.id = t.booking_item_id AND bi.tenant_id = t.tenant_id
        LEFT JOIN customer_pii c ON c.customer_id = b.customer_id AND c.tenant_id = b.tenant_id
        LEFT JOIN staff st ON st.id = b.staff_actor_id AND st.tenant_id = b.tenant_id
        WHERE t.tenant_id = ?{clause} AND t.visit_date BETWEEN ? AND ?
          AND pm.method = 'COMPLIMENTARY'
        GROUP BY t.ticket_type_id, b.id
        ORDER BY b.visit_date DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    language = _lang(scope)
    return [
        {
            "ticket_type": i18n_text(decode(row["tt_name"], {}), language, fallback=row["tt_code"]),
            "quantity": int(row["quantity"] or 0),
            "value_minor": int(row["value_minor"] or 0),
            "reason": row["cancel_reason"] or row["notes"] or "",
            "actor": row["actor_name"] or row["actor_email"] or "",
            "approver": "",
            "recipient": row["customer_name"] or "",
            "visit_date": row["visit_date"],
            "checkin_state": "Admitted" if int(row["admitted"] or 0) else "Not used",
        }
        for row in rows
    ]


def reconciliation(db, scope: Scope) -> list[dict[str, Any]]:
    """Platform payments against the provider record (§30)."""
    clause, venue_params = scope.venue_clause("b")
    rows = db.query(
        f"""
        SELECT pm.created_at, pm.provider_ref, pm.method, pm.provider, pm.currency,
               pm.amount_minor, pm.status, pm.reconciliation_state,
               b.booking_number
        FROM payments pm
        JOIN bookings b ON b.id = pm.booking_id AND b.tenant_id = pm.tenant_id
        WHERE pm.tenant_id = ?{clause} AND date(pm.created_at) BETWEEN ? AND ?
        ORDER BY pm.created_at DESC
        LIMIT 1000
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    return [
        {
            "created_at": _local_stamp(row["created_at"], scope),
            "booking_number": row["booking_number"],
            "provider_ref": row["provider_ref"] or "",
            "method": row["method"],
            "provider": row["provider"] or "",
            "currency": row["currency"],
            "amount_minor": int(row["amount_minor"] or 0),
            "state": _reconciliation_state(row["reconciliation_state"], row["status"]),
        }
        for row in rows
    ]


def _reconciliation_state(state: str | None, status: str | None) -> str:
    if state:
        return _title(state)
    if status in ("AUTHORIZED",):
        return "Pending"
    if status in ("CAPTURED",):
        return "Matched"
    if status in ("FAILED", "VOIDED"):
        return _title(status)
    return "Unmatched"


def tax_invoices(db, scope: Scope) -> list[dict[str, Any]]:
    """Issued invoices and credit notes (§32)."""
    clause, venue_params = scope.venue_clause("b")
    rows = db.query(
        f"""
        SELECT ti.number, ti.issued_at, ti.doc_type, ti.status,
               ti.tax_base_minor, ti.tax_minor, ti.total_minor, ti.customer_tax_json,
               b.booking_number
        FROM tax_invoices ti
        JOIN bookings b ON b.id = ti.booking_id AND b.tenant_id = ti.tenant_id
        WHERE ti.tenant_id = ?{clause} AND date(ti.issued_at) BETWEEN ? AND ?
        ORDER BY ti.sequence_no DESC
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    result = []
    for row in rows:
        tax_details = decode(row["customer_tax_json"], {}) or {}
        status = row["status"]
        if row["doc_type"] == "CN":
            status = "Credit note"
        result.append(
            {
                "number": row["number"],
                "issued_at": _local_stamp(row["issued_at"], scope),
                "booking_number": row["booking_number"],
                "customer": tax_details.get("name") or tax_details.get("company") or "",
                "tax_id": tax_details.get("tax_id") or "",
                "tax_base_minor": int(row["tax_base_minor"] or 0),
                "tax_minor": int(row["tax_minor"] or 0),
                "total_minor": int(row["total_minor"] or 0),
                "status": _title(status),
            }
        )
    return result


def exchange_rates(db, scope: Scope) -> list[dict[str, Any]]:
    """Configured rates and how many orders used each (§33)."""
    rows = db.query(
        """
        SELECT id, from_currency, to_currency, rate_text, effective_from, effective_until, status
        FROM exchange_rates WHERE tenant_id = ?
        ORDER BY from_currency, effective_from DESC
        """,
        (scope.tenant_id,),
    )
    usage = db.query(
        """
        SELECT transaction_currency, base_currency, COUNT(*) AS n
        FROM bookings WHERE tenant_id = ? AND transaction_currency IS NOT NULL
        GROUP BY transaction_currency, base_currency
        """,
        (scope.tenant_id,),
    )
    counts = {(row["transaction_currency"], row["base_currency"]): int(row["n"] or 0) for row in usage}
    return [
        {
            "pair": f"{row['from_currency']} -> {row['to_currency']}",
            "direction": f"1 {row['from_currency']} = {row['rate_text']} {row['to_currency']}",
            "effective_from": row["effective_from"],
            "effective_until": row["effective_until"] or "",
            "status": _title(row["status"]),
            "bookings": counts.get((row["from_currency"], row["to_currency"]), 0),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Devices, shows, seats
# --------------------------------------------------------------------------- #


def devices(db, scope: Scope) -> list[dict[str, Any]]:
    """Device health (§29). Status is derived from the heartbeat, not trusted."""
    clause, venue_params = scope.venue_clause("d")
    rows = db.query(
        f"""
        SELECT d.code, d.name, d.kind, d.status, d.last_seen_at, d.health_json,
               v.code AS venue_code,
               (SELECT MAX(pm.created_at) FROM payments pm
                 WHERE pm.device_id = d.id AND pm.tenant_id = d.tenant_id) AS last_payment,
               (SELECT MAX(s.at_utc) FROM scan_events s
                 WHERE s.device_id = d.id AND s.tenant_id = d.tenant_id) AS last_scan
        FROM devices d
        LEFT JOIN venues v ON v.id = d.venue_id AND v.tenant_id = d.tenant_id
        WHERE d.tenant_id = ?{clause}
        ORDER BY d.kind, d.code
        """,
        [scope.tenant_id, *venue_params],
    )
    result = []
    for row in rows:
        health = decode(row["health_json"], {}) or {}
        last_transaction = max(
            [stamp for stamp in (row["last_payment"], row["last_scan"]) if stamp], default=None
        )
        result.append(
            {
                "name": row["name"] or row["code"],
                "kind": _title(row["kind"]),
                "venue": row["venue_code"] or "",
                "state": _device_state(row["status"], row["last_seen_at"], health),
                "last_seen_at": _local_stamp(row["last_seen_at"], scope),
                "last_transaction_at": _local_stamp(last_transaction, scope),
                "last_error": health.get("last_error") or "",
                "app_version": health.get("app_version") or "",
            }
        )
    return result


def _device_state(status: str | None, last_seen: str | None, health: dict[str, Any]) -> str:
    """A device is only online if it said so recently.

    Trusting the stored status would report a kiosk as online after somebody
    pulled its plug, which is exactly the failure the dashboard exists to catch.
    """
    if status != "ACTIVE":
        return _title(status)
    reported = (health.get("state") or "").upper()
    if reported in ("PAPER_LOW", "PRINTER_ERROR", "PAYMENT_DEVICE_ERROR"):
        return _title(reported)
    if not last_seen:
        return "Never seen"
    minutes = _minutes_since(last_seen)
    if minutes is None:
        return "Unknown"
    if minutes > 15:
        return "Offline"
    return "Online"


def shows(db, scope: Scope) -> list[dict[str, Any]]:
    """Today's show timetable with reservations and attendance (§27)."""
    clause, venue_params = scope.venue_clause("s")
    rows = db.query(
        f"""
        SELECT s.id, s.date, s.start_time, s.end_time, s.capacity, s.confirmed, s.status,
               s.delayed_start_time,
               e.name_json AS show_name, e.code AS show_code,
               a.name_json AS area_name, a.code AS area_code
        FROM sessions s
        LEFT JOIN experiences e ON e.id = s.experience_id AND e.tenant_id = s.tenant_id
        LEFT JOIN areas a ON a.id = s.area_id AND a.tenant_id = s.tenant_id
        WHERE s.tenant_id = ?{clause} AND s.kind = 'SHOW' AND s.date BETWEEN ? AND ?
        ORDER BY s.date, s.start_time
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    language = _lang(scope)
    now = _now_local(scope).strftime("%H:%M")
    today = _today(scope).isoformat()
    result = []
    for row in rows:
        capacity = int(row["capacity"] or 0)
        reserved = int(row["confirmed"] or 0)
        result.append(
            {
                "session_id": row["id"],
                "show": i18n_text(decode(row["show_name"], {}), language, fallback=row["show_code"] or ""),
                "location": i18n_text(decode(row["area_name"], {}), language, fallback=row["area_code"] or ""),
                "start_time": row["delayed_start_time"] or row["start_time"],
                "capacity": capacity or None,
                "reserved": reserved,
                "remaining": max(capacity - reserved, 0) if capacity else None,
                "attendance": reserved,
                "state": _show_state(row, now=now, today=today),
            }
        )
    return result


def _show_state(row: Any, *, now: str, today: str) -> str:
    status = (row["status"] or "").upper()
    if status in ("CANCELLED", "COMPLETED", "DELAYED", "HIDDEN"):
        return _title(status)
    capacity = int(row["capacity"] or 0)
    if capacity and int(row["confirmed"] or 0) >= capacity:
        return "Full"
    if row["date"] != today:
        return "Scheduled"
    start = row["delayed_start_time"] or row["start_time"] or ""
    end = row["end_time"] or ""
    if end and end < now:
        return "Completed"
    if start and start <= now:
        return "Happening now"
    if start and _minutes_between(now, start) <= 30:
        return "Starting soon"
    return "Upcoming"


def seats(db, scope: Scope) -> list[dict[str, Any]]:
    """Seat inventory per session (§28).

    Returns nothing where the venue does not use reserved seating, which is the
    honest answer — an empty seat report is not an error.
    """
    clause, venue_params = scope.venue_clause("s")
    rows = db.query(
        f"""
        SELECT s.id, s.date, s.start_time,
               e.name_json AS experience_name, e.code AS experience_code,
               (SELECT COUNT(*) FROM seats st
                 WHERE st.layout_version_id = s.seat_layout_version_id
                   AND st.tenant_id = s.tenant_id) AS total,
               (SELECT COUNT(*) FROM seat_reservations sr
                 WHERE sr.session_id = s.id AND sr.tenant_id = s.tenant_id
                   AND sr.state = 'CONFIRMED') AS sold,
               (SELECT COUNT(*) FROM seat_holds sh
                 WHERE sh.session_id = s.id AND sh.tenant_id = s.tenant_id
                   AND sh.state = 'ACTIVE') AS held,
               (SELECT COUNT(*) FROM seat_blocks sb
                 WHERE sb.session_id = s.id AND sb.tenant_id = s.tenant_id) AS blocked
        FROM sessions s
        LEFT JOIN experiences e ON e.id = s.experience_id AND e.tenant_id = s.tenant_id
        WHERE s.tenant_id = ?{clause} AND s.date BETWEEN ? AND ?
          AND s.seat_layout_version_id IS NOT NULL
        ORDER BY s.date, s.start_time
        """,
        [scope.tenant_id, *venue_params, scope.date_from, scope.date_to],
    )
    language = _lang(scope)
    result = []
    for row in rows:
        total = int(row["total"] or 0)
        sold = int(row["sold"] or 0)
        held = int(row["held"] or 0)
        blocked = int(row["blocked"] or 0)
        label = i18n_text(decode(row["experience_name"], {}), language, fallback=row["experience_code"] or "")
        result.append(
            {
                "session": f"{label} {row['date']} {row['start_time']}".strip(),
                "total": total,
                "available": max(total - sold - held - blocked, 0),
                "held": held,
                "sold": sold,
                "blocked": blocked,
                "occupancy_bp": _share_bp(sold, total),
            }
        )
    return result


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _title(value: str | None) -> str:
    return (value or "").replace("_", " ").capitalize()


def _local_stamp(instant_text: str | None, scope: Scope) -> str:
    """Render a stored UTC instant in the venue's timezone, to the minute."""
    if not instant_text:
        return ""
    try:
        return local(parse_instant(instant_text), scope.timezone).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(instant_text)[:16].replace("T", " ")


def _minutes_since(instant_text: str | None) -> float | None:
    try:
        moment = parse_instant(instant_text)
    except (ValueError, TypeError):
        return None
    return (_dt.datetime.now(_dt.timezone.utc) - moment).total_seconds() / 60.0


def _minutes_between(now_hhmm: str, later_hhmm: str) -> int:
    def minutes(text: str) -> int:
        hours, _, mins = text.partition(":")
        return int(hours) * 60 + int(mins or 0)

    try:
        return minutes(later_hhmm) - minutes(now_hhmm)
    except ValueError:
        return 9_999


__all__ = [
    "arrivals",
    "bookings",
    "complimentary",
    "counter_sales",
    "devices",
    "discounts",
    "exchange_rates",
    "manual_discounts",
    "reconciliation",
    "refunds_and_voids",
    "sales",
    "seats",
    "shifts",
    "shows",
    "tax_invoices",
]
