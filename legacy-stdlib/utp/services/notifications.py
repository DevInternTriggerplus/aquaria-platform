"""Notification templates, queueing and delivery.

Template language
-----------------
Deliberately **not** an expression evaluator. A template may contain
``{{variable_name}}`` placeholders drawn from a per-event allow-list and nothing
else. Unknown placeholders are rejected at *save* time with the offending name,
and a message is never sent containing an unresolved placeholder (R37.3). This is
the concrete answer to residual risk D.7 in the requirements analysis: there is no
sandbox to escape because there is no evaluator.

Rendered values are HTML-escaped, so a customer name containing markup cannot
inject script into an email (R37.7).

Delivery
--------
Everything is queued. :meth:`NotificationService.enqueue` only writes a row;
:meth:`dispatch_due` performs the sends. Booking confirmation, payment processing
and ticket issuance therefore never wait on an email provider (R37.8), and a
provider outage delays messages without failing a sale.

Transactional messages ignore marketing consent; marketing messages require it
(R36.11, R12.15).
"""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, combine_local, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import ConflictError, NotFound, ValidationError
from ..core.i18n import text as i18n_text
from ..core.ids import hash_identifier, new_id
from ..core.money import format_amount
from ..domain import enums
from .authz import AuthorizationService

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _encode_inline_images(images: dict[str, bytes] | None) -> str | None:
    """Store inline images as ``{cid: base64}``.

    Base64 rather than a blob because the column sits in a row that is otherwise
    text, and because a stored message should be inspectable without a binary
    reader when someone is working out why a QR did not arrive.
    """
    if not images:
        return None
    return json.dumps(
        {cid: base64.b64encode(blob).decode("ascii") for cid, blob in images.items()},
        separators=(",", ":"),
    )


def _decode_inline_images(raw: str | None) -> dict[str, bytes] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    decoded: dict[str, bytes] = {}
    for cid, text in (payload or {}).items():
        try:
            decoded[cid] = base64.b64decode(text)
        except (ValueError, TypeError):
            continue
    return decoded or None


#: Variables always available (R37.2).
COMMON_VARIABLES: tuple[str, ...] = (
    "booking_number",
    "customer_name",
    "venue_name",
    "venue_address",
    "venue_phone",
    "venue_timezone",
    "visit_date",
    "session_time",
    "ticket_count",
    "total_amount",
    "currency",
    "manage_booking_url",
    "show_schedule_url",
    "language",
    "entry_location",
    "conditions",
    "ticket_lines",
    "qr_code",
)

#: Additional variables per event type. Documented and enforced (R37.2, R37.3).
EVENT_VARIABLES: dict[str, tuple[str, ...]] = {
    # R36.4 permits Booking Confirmation, Payment Confirmation and E-Ticket Delivery to
    # be combined into one message "without losing any required content". When they are
    # combined — the platform default — the confirmation *is* the e-ticket, so it must be
    # able to render ticket and payment content. Hence the union here rather than a
    # narrower list that would make the supported configuration unusable.
    "BOOKING_CONFIRMATION": (
        "payment_summary",
        "visitor_count",
        "payment_amount",
        "payment_method",
        "payment_reference",
        "payment_time",
        "ticket_download_url",
        "show_timetable",
    ),
    "PAYMENT_CONFIRMATION": ("payment_amount", "payment_method", "payment_reference", "payment_time"),
    "ETICKET_DELIVERY": ("qr_code", "ticket_download_url", "verification_code", "purpose"),
    "BOOKING_REMINDER": ("hours_before", "show_timetable", "ticket_download_url"),
    "BOOKING_RESCHEDULED": ("previous_visit_date", "previous_session_time", "new_visit_date"),
    "BOOKING_CANCELLED": ("reason", "refund_amount_minor", "refund_expectation", "remedy", "amount_minor"),
    "REFUND_COMPLETED": ("refund_amount_minor", "refund_method", "refund_reference", "settlement_timing"),
    "TAX_INVOICE_AVAILABLE": ("invoice_number", "invoice_download_url", "invoice_expires_at"),
    "SHOW_SCHEDULE_CHANGED": (
        "show_name",
        "previous_start_time",
        "new_start_time",
        "previous_location",
        "new_location",
        "change_reason",
    ),
    "SHOW_CANCELLED": ("show_name", "show_start_time", "change_reason", "remedy"),
    "WAITING_LIST_OFFER": ("session_id", "offer_expires_at", "quantity", "claim_url"),
    "CONSENT_WITHDRAWAL_CONFIRMATION": ("item_code", "effective_by"),
    "DSAR_ACKNOWLEDGEMENT": ("request_kind", "due_at"),
    "SEAT_CHANGED": ("previous_seat", "new_seat", "change_reason"),
}

#: Every notification event in this platform is transactional. Marketing content is
#: a separate concern with its own consent gate; nothing here is sent on the back of
#: marketing consent (R36.11).
TRANSACTIONAL_EVENTS: frozenset[str] = frozenset(enums.NOTIFICATION_EVENTS)


def allowed_variables(event_type: str) -> tuple[str, ...]:
    return tuple(sorted(set(COMMON_VARIABLES) | set(EVENT_VARIABLES.get(event_type, ()))))


class EmailProvider(Protocol):
    name: str

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str,
        html: str | None = None,
        inline_images: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Send one message. Returns ``{status, provider_message_id, failure_reason?}``.

        ``html`` is the optional rich alternative. ``body`` is always populated and
        remains the authoritative content, so a provider or client that ignores
        HTML still delivers a complete message.

        ``inline_images`` maps Content-ID to image bytes for the ``cid:``
        references in the HTML. A provider that supports it should compose
        ``multipart/alternative`` over ``multipart/related``; see
        :mod:`utp.services.mail_mime`.
        """


@dataclass
class SimulatedEmailProvider:
    """Deterministic provider for tests and local development."""

    name: str = "simulated-email"
    sender: str = "tickets@aquaria.test"
    sent: list[dict[str, Any]] = field(default_factory=list)
    #: Addresses that hard-bounce.
    hard_bounce: set[str] = field(default_factory=set)
    #: Addresses that fail transiently the first N times.
    transient_failures: dict[str, int] = field(default_factory=dict)

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str,
        html: str | None = None,
        inline_images: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        if to in self.hard_bounce:
            return {"status": "BOUNCED", "failure_reason": "hard_bounce", "permanent": True}
        remaining = self.transient_failures.get(to, 0)
        if remaining > 0:
            self.transient_failures[to] = remaining - 1
            return {"status": "FAILED", "failure_reason": "provider_unavailable", "permanent": False}
        # Compose the real MIME message even though nothing is transmitted. It is the
        # only way a simulated provider can prove the structure a real client will
        # receive — and it fails loudly here if a cid reference has no attachment,
        # which on a live send would be a blank QR at the gate.
        from .mail_mime import build_message, describe

        mime = build_message(
            sender=self.sender,
            to=to,
            subject=subject,
            text_body=body,
            html_body=html,
            inline_images=inline_images,
            message_id=message_id,
        )
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "html": html,
                "inline_images": dict(inline_images or {}),
                "message_id": message_id,
                "mime": mime,
                "structure": describe(mime),
            }
        )
        return {"status": "SENT", "provider_message_id": f"prov_{message_id[-12:]}"}


@dataclass
class SmtpEmailProvider:
    """Sends real email over SMTP, for a deployment that has a mail server.

    The demo runs on :class:`SimulatedEmailProvider`, which records messages to the
    in-app mailbox and transmits nothing. To actually deliver to a real inbox, a
    deployment provides SMTP settings and the platform uses this instead. The MIME
    is composed by the same :func:`utp.services.mail_mime.build_message` the
    simulated provider uses, so the message a recipient receives is structurally
    identical to the one the mailbox preview shows — the QR travels the same way.

    Configuration comes from the environment, never hard-coded:

        UTP_SMTP_HOST      mail server host (required to enable real sending)
        UTP_SMTP_PORT      port (default 587)
        UTP_SMTP_USER      username (optional)
        UTP_SMTP_PASSWORD  password (optional)
        UTP_SMTP_SENDER    From address (default tickets@<host domain>)
        UTP_SMTP_TLS       "starttls" (default), "ssl", or "none"

    On any SMTP error the send returns a transient FAILED so the notification
    service's existing retry path applies rather than losing the message.
    """

    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    sender: str = "tickets@aquaria.test"
    tls: str = "starttls"  # starttls | ssl | none
    name: str = "smtp-email"

    @classmethod
    def from_env(cls) -> "SmtpEmailProvider | None":
        """Build from environment, or ``None`` when SMTP is not configured."""
        import os

        host = os.environ.get("UTP_SMTP_HOST", "").strip()
        if not host:
            return None
        return cls(
            host=host,
            port=int(os.environ.get("UTP_SMTP_PORT", "587") or 587),
            username=os.environ.get("UTP_SMTP_USER") or None,
            password=os.environ.get("UTP_SMTP_PASSWORD") or None,
            sender=os.environ.get("UTP_SMTP_SENDER") or f"tickets@{host}",
            tls=(os.environ.get("UTP_SMTP_TLS") or "starttls").strip().lower(),
        )

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str,
        html: str | None = None,
        inline_images: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        import smtplib
        import ssl as _ssl

        from .mail_mime import build_message

        mime = build_message(
            sender=self.sender,
            to=to,
            subject=subject,
            text_body=body,
            html_body=html,
            inline_images=inline_images,
            message_id=message_id,
        )
        try:
            if self.tls == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=_ssl.create_default_context())
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=20)
            with server:
                server.ehlo()
                if self.tls == "starttls":
                    server.starttls(context=_ssl.create_default_context())
                    server.ehlo()
                if self.username:
                    server.login(self.username, self.password or "")
                server.sendmail(self.sender, [to], mime.as_string())
            return {"status": "SENT", "provider_message_id": f"smtp_{message_id[-12:]}"}
        except smtplib.SMTPRecipientsRefused:
            # The address is bad — permanent, so it will not be retried.
            return {"status": "BOUNCED", "failure_reason": "recipient_refused", "permanent": True}
        except (smtplib.SMTPException, OSError) as exc:
            # Anything else (auth, connection, greylisting) is transient: let the
            # notification service retry rather than drop the message.
            return {"status": "FAILED", "failure_reason": f"smtp_error: {exc}", "permanent": False}


class NotificationService:
    """Templates, queue, dispatch and delivery log."""

    #: Wired by :class:`utp.app.Platform`.
    booking: Any = None
    tickets: Any = None
    #: ``(ctx, booking_id, language) -> str | None``, wired by the composition root.
    #: Renders the designed HTML e-ticket for the ticket-bearing events. Kept as an
    #: injected callable rather than an import so this service does not depend on a
    #: presentation package, and so a tenant that wants text-only mail simply has no
    #: renderer wired.
    eticket_renderer: Any = None

    #: Events that carry a ticket, and therefore an HTML e-ticket where one can be
    #: rendered. Anything else stays plain text.
    ETICKET_EVENTS: tuple[str, ...] = (
        "BOOKING_CONFIRMATION",
        "ETICKET_DELIVERY",
        "BOOKING_RESCHEDULED",
    )

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        authz: AuthorizationService,
        config: ConfigStore,
        provider: EmailProvider | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.audit = audit
        self.authz = authz
        self.config = config
        self.provider: EmailProvider = provider or SimulatedEmailProvider()
        #: Resolved on first send; see :meth:`_send_via_provider`.
        self._provider_accepts_html: bool | None = None
        self._provider_accepts_images: bool = False

    # ------------------------------------------------------------------ #
    # Templates (R37.1 - R37.7)
    # ------------------------------------------------------------------ #

    def create_template(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        language: str,
        subject: str,
        body: str,
        header: str = "",
        footer: str = "",
        venue_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a new template version, validating every placeholder (R37.3, R37.6)."""
        self.authz.require_page(ctx, "Email Templates", "ADD")
        if event_type not in enums.NOTIFICATION_EVENTS:
            raise ValidationError(
                {"event_type": f"Unknown event. Choose from: {', '.join(enums.NOTIFICATION_EVENTS)}."}
            )
        self.validate_template(event_type, subject=subject, header=header, body=body, footer=footer)
        version = (
            int(
                self.db.scalar(
                    "SELECT COALESCE(MAX(version), 0) FROM notification_templates "
                    "WHERE tenant_id = ? AND IFNULL(venue_id,'') = IFNULL(?,'') AND event_type = ? "
                    "AND language = ?",
                    (ctx.tenant_id, venue_id, event_type, language),
                    default=0,
                )
            )
            + 1
        )
        template_id = new_id("tpl")
        with self.db.transaction():
            self.db.execute(
                "UPDATE notification_templates SET state = 'SUPERSEDED' WHERE tenant_id = ? "
                "AND IFNULL(venue_id,'') = IFNULL(?,'') AND event_type = ? AND language = ? AND state = 'ACTIVE'",
                (ctx.tenant_id, venue_id, event_type, language),
            )
            self.db.insert(
                "notification_templates",
                {
                    "id": template_id,
                    "tenant_id": ctx.tenant_id,
                    "venue_id": venue_id,
                    "event_type": event_type,
                    "language": language,
                    "version": version,
                    "subject": subject,
                    "header": header,
                    "body": body,
                    "footer": footer,
                    "state": "ACTIVE",
                    "created_at": to_iso(self.clock.now()),
                    "created_by": ctx.principal.id,
                },
            )
            self.audit.record(
                ctx,
                "CONFIG_CHANGE",
                target_type="notification_template",
                target_id=template_id,
                new={"event_type": event_type, "language": language, "version": version},
            )
        return self.get_template(ctx, template_id)

    def validate_template(
        self, event_type: str, *, subject: str, header: str = "", body: str = "", footer: str = ""
    ) -> list[str]:
        """Reject unknown placeholders by name (R37.3)."""
        allowed = set(allowed_variables(event_type))
        used: set[str] = set()
        for section in (subject, header, body, footer):
            used |= {m.group(1) for m in _PLACEHOLDER.finditer(section or "")}
        unknown = sorted(used - allowed)
        if unknown:
            raise ValidationError(
                {"body": f"Unknown variable(s): {', '.join('{{' + u + '}}' for u in unknown)}."},
                message=f"These variables are not available for {event_type}: {', '.join(unknown)}.",
                code="unknown_template_variable",
            )
        return sorted(used)

    def get_template(self, ctx: RequestContext, template_id: str) -> dict[str, Any]:
        return self.authz.load_scoped(ctx, "notification_templates", template_id, entity="notification_template")

    def resolve_template(
        self, ctx: RequestContext, *, event_type: str, language: str, venue_id: str | None
    ) -> dict[str, Any] | None:
        """Venue template beats tenant template; requested language beats fallback (R37.4)."""
        default_language = self.db.scalar(
            "SELECT default_language FROM tenants WHERE id = ?", (ctx.tenant_id,), default="en"
        )
        for venue_filter in (venue_id, None):
            for lang in (language, default_language):
                row = self.db.query_one(
                    "SELECT * FROM notification_templates WHERE tenant_id = ? "
                    "AND IFNULL(venue_id,'') = IFNULL(?,'') AND event_type = ? AND language = ? "
                    "AND state = 'ACTIVE' ORDER BY version DESC LIMIT 1",
                    (ctx.tenant_id, venue_filter, event_type, lang),
                )
                if row is not None:
                    return dict(row)
        return None

    def preview(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        language: str,
        venue_id: str | None = None,
        sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render a template with sample data (R37.5)."""
        self.authz.require_page(ctx, "Email Templates", "VIEW")
        template = self.resolve_template(ctx, event_type=event_type, language=language, venue_id=venue_id)
        if template is None:
            raise NotFound(details={"entity": "notification_template", "event_type": event_type})
        variables = {name: f"[{name}]" for name in allowed_variables(event_type)}
        variables.update(sample or {})
        return {
            "event_type": event_type,
            "language": language,
            "version": int(template["version"]),
            "subject": self._render(template["subject"], variables),
            "body": self._render(
                "\n".join(filter(None, [template["header"], template["body"], template["footer"]])), variables
            ),
            "available_variables": list(allowed_variables(event_type)),
        }

    def test_send(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        language: str,
        recipient: str,
        venue_id: str | None = None,
        sample: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a marked test message to a nominated address (R37.5)."""
        self.authz.require_page(ctx, "Email Templates", "EDIT")
        rendered = self.preview(
            ctx, event_type=event_type, language=language, venue_id=venue_id, sample=sample
        )
        message_id = self._queue_row(
            ctx,
            event_type=event_type,
            recipient=recipient,
            language=language,
            booking_id=None,
            subject=f"[TEST] {rendered['subject']}",
            body=rendered["body"],
            template_id=None,
            template_version=rendered["version"],
            is_test=True,
            dedupe_key=None,
        )
        self.dispatch_due(ctx, limit=50)
        return {"message_id": message_id, "test": True}

    def _render(self, text: str, variables: dict[str, Any]) -> str:
        """Substitute allow-listed placeholders, escaping every value (R37.7)."""

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            value = variables.get(name)
            if value is None:
                return ""
            return html.escape(str(value), quote=False)

        return _PLACEHOLDER.sub(replace, text or "")

    # ------------------------------------------------------------------ #
    # Queue (R36, R37.8)
    # ------------------------------------------------------------------ #

    def enqueue(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        recipient: str | None,
        language: str,
        booking_id: str | None = None,
        venue_id: str | None = None,
        extra_variables: dict[str, Any] | None = None,
        contact_hash: str | None = None,
        force_resend: bool = False,
        actor_id: str | None = None,
        reason: str | None = None,
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        """Queue a message. Never sends inline (R37.8)."""
        if event_type not in enums.NOTIFICATION_EVENTS:
            raise ValidationError({"event_type": f"Unknown notification event {event_type!r}."})
        if not recipient:
            return {"queued": False, "reason": "no_recipient"}
        if self.is_suppressed(ctx, recipient) and not force_resend:
            # R37.12 — stop sending to a hard-bounced address, and make it visible.
            return {"queued": False, "reason": "suppressed", "recipient_hash": hash_identifier(recipient)}

        dedupe_key = None if force_resend else f"{event_type}:{booking_id or contact_hash or recipient}"
        if dedupe_key and not force_resend:
            existing = self.db.query_one(
                "SELECT id FROM notification_messages WHERE tenant_id = ? AND dedupe_key = ? "
                "AND status IN ('QUEUED','SENT')",
                (ctx.tenant_id, dedupe_key),
            )
            if existing is not None:
                # R36.13 — one send per event per booking unless explicitly resent.
                return {"queued": False, "reason": "duplicate_suppressed", "message_id": existing["id"]}

        variables = self.build_variables(
            ctx, event_type=event_type, booking_id=booking_id, venue_id=venue_id, language=language
        )
        variables.update(extra_variables or {})
        template = self.resolve_template(
            ctx, event_type=event_type, language=language, venue_id=venue_id
        )
        if template is None:
            subject, body = self._fallback_content(event_type, variables, language)
            template_id = None
            template_version = None
        else:
            subject = self._render(template["subject"], variables)
            body = self._render(
                "\n".join(filter(None, [template["header"], template["body"], template["footer"]])), variables
            )
            template_id = template["id"]
            template_version = int(template["version"])

        eticket_html, eticket_images = self._render_eticket(
            ctx, event_type, booking_id, language
        )
        message_id = self._queue_row(
            ctx,
            event_type=event_type,
            recipient=recipient,
            language=language,
            booking_id=booking_id,
            subject=subject,
            body=body,
            html=eticket_html,
            inline_images=eticket_images,
            template_id=template_id,
            template_version=template_version,
            is_test=False,
            dedupe_key=dedupe_key,
            actor_id=actor_id,
            scheduled_at=scheduled_at,
        )
        if reason:
            self.audit.record(
                ctx,
                "TICKET_RESEND",
                target_type="notification_message",
                target_id=message_id,
                new={"event_type": event_type, "forced": force_resend},
                reason=reason,
            )
        return {"queued": True, "message_id": message_id, "event_type": event_type}

    def _queue_row(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        recipient: str,
        language: str,
        booking_id: str | None,
        subject: str,
        body: str,
        template_id: str | None,
        template_version: int | None,
        is_test: bool,
        dedupe_key: str | None,
        actor_id: str | None = None,
        scheduled_at: str | None = None,
        html: str | None = None,
        inline_images: dict[str, bytes] | None = None,
    ) -> str:
        message_id = new_id("msg")
        now = to_iso(self.clock.now())
        self.db.insert(
            "notification_messages",
            {
                "id": message_id,
                "tenant_id": ctx.tenant_id,
                "booking_id": booking_id,
                "event_type": event_type,
                "recipient": recipient,
                "recipient_hash": hash_identifier(recipient),
                "channel": "EMAIL",
                "template_id": template_id,
                "template_version": template_version,
                "language": language,
                "subject": subject,
                "rendered_body": body,
                "rendered_html": html,
                "inline_images_json": _encode_inline_images(inline_images),
                "status": "QUEUED",
                "queued_at": now,
                "retry_count": 0,
                "next_attempt_at": scheduled_at or now,
                "is_test": 1 if is_test else 0,
                "dedupe_key": dedupe_key,
                "correlation_id": ctx.correlation_id,
                "actor_id": actor_id or ctx.principal.id,
            },
        )
        return message_id

    def _render_eticket(
        self, ctx: RequestContext, event_type: str, booking_id: str | None, language: str
    ) -> tuple[str | None, dict[str, bytes] | None]:
        """Render the HTML e-ticket for a ticket-bearing event, if possible.

        Returns the HTML and any inline images its ``cid:`` references need.

        Failure here must never cost the guest their ticket: the plain-text body
        is already complete and the ticket is retrievable from Manage Booking
        (R37.13), so a rendering problem degrades the mail rather than blocking
        the send.
        """
        if event_type not in self.ETICKET_EVENTS or not booking_id or self.eticket_renderer is None:
            return None, None
        try:
            rendered = self.eticket_renderer(ctx, booking_id, language)
        except Exception:  # noqa: BLE001 - deliberately non-fatal, see docstring
            return None, None
        if rendered is None:
            return None, None
        if isinstance(rendered, tuple):
            html, images = rendered
            return html, images or None
        return rendered, None

    def _fallback_content(
        self, event_type: str, variables: dict[str, Any], language: str
    ) -> tuple[str, str]:
        """Built-in content so a tenant without templates still communicates."""
        titles = {
            "BOOKING_CONFIRMATION": {"en": "Your booking is confirmed", "th": "ยืนยันการจองของท่านแล้ว"},
            "PAYMENT_CONFIRMATION": {"en": "Payment received", "th": "ได้รับการชำระเงินแล้ว"},
            "ETICKET_DELIVERY": {"en": "Your e-ticket", "th": "อีบัตรของท่าน"},
            "BOOKING_REMINDER": {"en": "Your visit is coming up", "th": "ใกล้ถึงวันเข้าชมของท่าน"},
            "BOOKING_RESCHEDULED": {"en": "Your booking has been changed", "th": "การจองของท่านถูกเปลี่ยนแปลง"},
            "BOOKING_CANCELLED": {"en": "Your booking has been cancelled", "th": "การจองของท่านถูกยกเลิก"},
            "REFUND_COMPLETED": {"en": "Your refund is complete", "th": "การคืนเงินเสร็จสมบูรณ์"},
            "TAX_INVOICE_AVAILABLE": {"en": "Your tax invoice", "th": "ใบกำกับภาษีของท่าน"},
            "SHOW_SCHEDULE_CHANGED": {"en": "A show time has changed", "th": "เวลาการแสดงมีการเปลี่ยนแปลง"},
            "SHOW_CANCELLED": {"en": "A show has been cancelled", "th": "การแสดงถูกยกเลิก"},
            "WAITING_LIST_OFFER": {"en": "A place is available", "th": "มีที่ว่างสำหรับท่าน"},
            "CONSENT_WITHDRAWAL_CONFIRMATION": {
                "en": "Your preference has been updated",
                "th": "อัปเดตการตั้งค่าของท่านแล้ว",
            },
            "DSAR_ACKNOWLEDGEMENT": {"en": "We received your request", "th": "เราได้รับคำขอของท่านแล้ว"},
            "SEAT_CHANGED": {"en": "Your seat has changed", "th": "ที่นั่งของท่านเปลี่ยนแปลง"},
        }
        subject = i18n_text(titles.get(event_type, {"en": event_type}), language, fallback=event_type)
        booking_no = variables.get("booking_number") or ""
        lines = [
            subject,
            "",
            f"Booking: {booking_no}" if booking_no else "",
            f"Venue: {variables.get('venue_name', '')}",
            f"Visit date: {variables.get('visit_date', '')}",
            f"Session: {variables.get('session_time', '')}" if variables.get("session_time") else "",
            f"Tickets: {variables.get('ticket_count', '')}",
            f"Total: {variables.get('total_amount', '')}",
            "",
            str(variables.get("ticket_lines") or ""),
            "",
            f"Entry: {variables.get('entry_location', '')}" if variables.get("entry_location") else "",
            f"Manage your booking: {variables.get('manage_booking_url', '')}",
            f"Show schedule: {variables.get('show_schedule_url', '')}",
        ]
        return subject, "\n".join(line for line in lines if line != "")

    def build_variables(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        booking_id: str | None,
        venue_id: str | None,
        language: str,
    ) -> dict[str, Any]:
        """Assemble the substitution values for a message.

        Only what the message needs, and expiring links for anything containing
        personal or financial data (R37.14).
        """
        variables: dict[str, Any] = {"language": language}
        if venue_id:
            venue = self.db.query_one(
                "SELECT * FROM venues WHERE id = ? AND tenant_id = ?", (venue_id, ctx.tenant_id)
            )
            if venue is not None:
                variables["venue_name"] = i18n_text(decode(venue["name_json"], {}), language, fallback=venue["code"])
                variables["venue_timezone"] = venue["timezone"]
                address = decode(venue["address_json"], {}) or {}
                variables["venue_address"] = address.get("line1", "")
                contact = decode(venue["contact_json"], {}) or {}
                variables["venue_phone"] = contact.get("phone", "")
                variables["show_schedule_url"] = (
                    # A live link, resolved at view time, so later schedule changes
                    # are reflected (R28.3).
                    f"https://book.example/{venue['code']}/shows"
                )
        if not booking_id:
            return variables
        booking = self.db.query_one(
            "SELECT * FROM bookings WHERE id = ? AND tenant_id = ?", (booking_id, ctx.tenant_id)
        )
        if booking is None:
            return variables
        variables["booking_number"] = booking["booking_number"]
        variables["visit_date"] = booking["visit_date"]
        variables["currency"] = booking["currency"]
        variables["total_amount"] = format_amount(
            int(booking["net_minor"]), booking["currency"], locale=language
        )
        variables["manage_booking_url"] = f"https://book.example/manage/{booking['booking_number']}"
        if booking["session_id"]:
            session = self.db.query_one(
                "SELECT start_time, end_time FROM sessions WHERE id = ?", (booking["session_id"],)
            )
            if session is not None:
                variables["session_time"] = f"{session['start_time']}–{session['end_time']}"
        tickets = self.db.query(
            "SELECT t.ticket_number, t.qr_token, tt.name_json AS tt_name, s.name_json AS seg_name "
            "FROM tickets t "
            "JOIN ticket_types tt ON tt.id = t.ticket_type_id AND tt.tenant_id = t.tenant_id "
            "JOIN customer_segments s ON s.id = t.segment_id AND s.tenant_id = t.tenant_id "
            "WHERE t.tenant_id = ? AND t.booking_id = ? ORDER BY t.ticket_number",
            (ctx.tenant_id, booking_id),
        )
        variables["ticket_count"] = len(tickets)
        variables["visitor_count"] = len(tickets)
        variables["ticket_lines"] = "\n".join(
            f"{row['ticket_number']} — "
            f"{i18n_text(decode(row['tt_name'], {}), language)} "
            f"({i18n_text(decode(row['seg_name'], {}), language)})"
            for row in tickets
        )
        variables["ticket_download_url"] = f"https://book.example/manage/{booking['booking_number']}/tickets"
        if tickets:
            variables["qr_code"] = f"https://book.example/qr/{tickets[0]['qr_token'][:12]}"
        payments = self.db.query(
            "SELECT method, amount_minor, provider_ref, captured_at, authorized_at FROM payments "
            "WHERE tenant_id = ? AND booking_id = ? AND status IN ('AUTHORIZED','CAPTURED') "
            "ORDER BY created_at",
            (ctx.tenant_id, booking_id),
        )
        if payments:
            payment = payments[0]
            variables["payment_method"] = payment["method"]
            variables["payment_amount"] = format_amount(
                int(payment["amount_minor"]), booking["currency"], locale=language
            )
            variables["payment_reference"] = payment["provider_ref"]
            variables["payment_time"] = payment["captured_at"] or payment["authorized_at"]
            variables["payment_summary"] = (
                f"{variables['payment_amount']} paid by {payment['method']}"
            )
        return variables

    # ------------------------------------------------------------------ #
    # Dispatch (R37.8 - R37.13)
    # ------------------------------------------------------------------ #

    def dispatch_due(self, ctx: RequestContext, *, limit: int = 100) -> dict[str, Any]:
        """Send queued messages whose attempt time has arrived."""
        now = to_iso(self.clock.now())
        rows = self.db.query(
            "SELECT * FROM notification_messages WHERE tenant_id = ? AND status = 'QUEUED' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY queued_at LIMIT ?",
            (ctx.tenant_id, now, int(limit)),
        )
        sent = failed = bounced = 0
        max_retries = self.config.get_int(ctx, "notification.max_retries")
        base = self.config.get_int(ctx, "notification.retry_base_seconds")
        for row in rows:
            if not self._body_is_clean(row["subject"], row["rendered_body"]):
                # R37.3 — never send an unresolved placeholder.
                self.db.update(
                    "notification_messages",
                    row["id"],
                    {"status": "FAILED", "failure_reason": "unresolved_placeholder"},
                    tenant_id=ctx.tenant_id,
                )
                failed += 1
                self._raise_exception(ctx, row["id"], "NOTIFICATION_UNRESOLVED_PLACEHOLDER")
                continue
            outcome = self._send_via_provider(
                to=row["recipient"],
                subject=row["subject"] or "",
                body=row["rendered_body"] or "",
                html=self._row_html(row),
                inline_images=self._row_inline_images(row),
                message_id=row["id"],
            )
            status = outcome.get("status")
            if status == "SENT":
                self.db.update(
                    "notification_messages",
                    row["id"],
                    {
                        "status": "SENT",
                        "sent_at": to_iso(self.clock.now()),
                        "provider_message_id": outcome.get("provider_message_id"),
                        "failure_reason": None,
                    },
                    tenant_id=ctx.tenant_id,
                )
                sent += 1
                continue
            if status == "BOUNCED" or outcome.get("permanent"):
                self.record_bounce(ctx, message_id=row["id"], reason=outcome.get("failure_reason") or "bounce")
                bounced += 1
                continue
            retries = int(row["retry_count"]) + 1
            if retries > max_retries:
                self.db.update(
                    "notification_messages",
                    row["id"],
                    {
                        "status": "FAILED",
                        "retry_count": retries,
                        "failure_reason": outcome.get("failure_reason") or "send_failed",
                    },
                    tenant_id=ctx.tenant_id,
                )
                failed += 1
                self._raise_exception(ctx, row["id"], "NOTIFICATION_FAILED")
                continue
            # Exponential backoff (R37.10).
            delay_seconds = base * (2 ** (retries - 1))
            self.db.update(
                "notification_messages",
                row["id"],
                {
                    "retry_count": retries,
                    "failure_reason": outcome.get("failure_reason") or "send_failed",
                    "next_attempt_at": to_iso(add_minutes(self.clock.now(), delay_seconds / 60.0)),
                },
                tenant_id=ctx.tenant_id,
            )
        return {"processed": len(rows), "sent": sent, "failed": failed, "bounced": bounced}

    @staticmethod
    def _body_is_clean(subject: str | None, body: str | None) -> bool:
        return not _PLACEHOLDER.search((subject or "") + (body or ""))

    @staticmethod
    def _row_html(row: Any) -> str | None:
        """Read the HTML alternative, tolerating a row from an older schema."""
        try:
            return row["rendered_html"]
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _row_inline_images(row: Any) -> dict[str, bytes] | None:
        """Read the stored inline images, tolerating a row from an older schema."""
        try:
            raw = row["inline_images_json"]
        except (KeyError, IndexError, TypeError):
            return None
        return _decode_inline_images(raw)

    def _send_via_provider(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html: str | None,
        message_id: str,
        inline_images: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Hand the message to the provider, tolerating a text-only implementation.

        ``EmailProvider`` is a plug-in point, so ``html`` is passed only to
        providers whose signature accepts it. The alternative — requiring every
        existing provider to change — would break a deployment's custom sender on
        upgrade for a feature it does not use. The signature is inspected rather
        than catching ``TypeError``, which would also swallow a genuine
        ``TypeError`` raised inside ``send``.
        """
        if self._provider_accepts_html is None:
            import inspect

            try:
                parameters = inspect.signature(self.provider.send).parameters
                any_kwargs = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
                )
                self._provider_accepts_html = "html" in parameters or any_kwargs
                self._provider_accepts_images = "inline_images" in parameters or any_kwargs
            except (TypeError, ValueError):  # pragma: no cover - exotic callables
                self._provider_accepts_html = False
                self._provider_accepts_images = False
        extra: dict[str, Any] = {}
        if self._provider_accepts_html:
            extra["html"] = html
        if self._provider_accepts_images and inline_images:
            extra["inline_images"] = inline_images
        return self.provider.send(
            to=to, subject=subject, body=body, message_id=message_id, **extra
        )

    def record_bounce(self, ctx: RequestContext, *, message_id: str, reason: str) -> dict[str, Any]:
        """Suppress the address and flag the booking for staff follow-up (R37.12)."""
        row = self.authz.load_scoped(ctx, "notification_messages", message_id, entity="notification_message")
        now = to_iso(self.clock.now())
        with self.db.transaction():
            self.db.update(
                "notification_messages",
                message_id,
                {"status": "BOUNCED", "failure_reason": reason},
                tenant_id=ctx.tenant_id,
            )
            existing = self.db.query_one(
                "SELECT id FROM notification_suppressions WHERE tenant_id = ? AND address = ?",
                (ctx.tenant_id, row["recipient"]),
            )
            if existing is None:
                self.db.insert(
                    "notification_suppressions",
                    {
                        "id": new_id("sup"),
                        "tenant_id": ctx.tenant_id,
                        "address": row["recipient"],
                        "reason": reason,
                        "created_at": now,
                    },
                )
            self._raise_exception(ctx, message_id, "NOTIFICATION_HARD_BOUNCE", severity="ERROR")
        return {
            "message_id": message_id,
            "suppressed": True,
            "booking_id": row["booking_id"],
            # R37.13 — the ticket is still retrievable through Manage Booking.
            "ticket_still_retrievable": True,
            "staff_follow_up_required": True,
        }

    def is_suppressed(self, ctx: RequestContext, address: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 FROM notification_suppressions WHERE tenant_id = ? AND address = ? "
                "AND cleared_at IS NULL",
                (ctx.tenant_id, address),
            )
            is not None
        )

    def clear_suppression(
        self, ctx: RequestContext, *, address: str, reason: str
    ) -> dict[str, Any]:
        """Let staff correct an address and resume sending (R37.12)."""
        self.authz.require_page(ctx, "Email Templates", "EDIT")
        cursor = self.db.execute(
            "UPDATE notification_suppressions SET cleared_at = ? WHERE tenant_id = ? AND address = ? "
            "AND cleared_at IS NULL",
            (to_iso(self.clock.now()), ctx.tenant_id, address),
        )
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="notification_suppression",
            target_id=hash_identifier(address),
            new={"cleared": True},
            reason=reason,
        )
        return {"cleared": cursor.rowcount > 0}

    def resend(
        self, ctx: RequestContext, *, message_id: str, reason: str, recipient: str | None = None
    ) -> dict[str, Any]:
        """Manually resend a message, audited with actor and reason (R37.11)."""
        self.authz.require_page(ctx, "Email Templates", "VIEW")
        row = self.authz.load_scoped(ctx, "notification_messages", message_id, entity="notification_message")
        if not (reason or "").strip():
            raise ValidationError({"reason": "A reason is required to resend a message."})
        target = recipient or row["recipient"]
        new_message = self._queue_row(
            ctx,
            event_type=row["event_type"],
            recipient=target,
            language=row["language"],
            booking_id=row["booking_id"],
            subject=row["subject"],
            body=row["rendered_body"],
            template_id=row["template_id"],
            template_version=row["template_version"],
            is_test=bool(row["is_test"]),
            dedupe_key=None,
        )
        self.audit.record(
            ctx,
            "TICKET_RESEND",
            target_type="notification_message",
            target_id=new_message,
            previous={"original_message_id": message_id},
            new={"event_type": row["event_type"], "recipient_changed": bool(recipient)},
            reason=reason,
            severity="WARNING",
        )
        return {"message_id": new_message, "resent_from": message_id}

    def message_log(
        self,
        ctx: RequestContext,
        *,
        booking_id: str | None = None,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Delivery log (R37.9). Recipient is masked without ``VIEW_PII``."""
        self.authz.require_page(ctx, "Email Templates", "VIEW")
        sql = ["SELECT * FROM notification_messages WHERE tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if booking_id:
            sql.append("AND booking_id = ?")
            params.append(booking_id)
        if status:
            sql.append("AND status = ?")
            params.append(status)
        if event_type:
            sql.append("AND event_type = ?")
            params.append(event_type)
        sql.append("ORDER BY queued_at DESC LIMIT ?")
        params.append(int(limit))
        out = []
        for row in self.db.query(" ".join(sql), params):
            record = dict(row)
            record.pop("rendered_body", None)
            out.append(self.authz.mask_record(ctx, record, entity="notification_message", audit_pii_access=False))
        return out

    def _raise_exception(
        self, ctx: RequestContext, message_id: str, kind: str, *, severity: str = "WARNING"
    ) -> None:
        self.db.insert(
            "exceptions_log",
            {
                "id": new_id("exc"),
                "tenant_id": ctx.tenant_id,
                "venue_id": ctx.venue_id,
                "kind": kind,
                "severity": severity,
                "entity_type": "notification_message",
                "entity_id": message_id,
                "detail_json": {"message_id": message_id},
                "state": "OPEN",
                "created_at": to_iso(self.clock.now()),
            },
        )

    # ------------------------------------------------------------------ #
    # Reminders (R36.5, R36.6)
    # ------------------------------------------------------------------ #

    def schedule_reminders(
        self, ctx: RequestContext, *, booking_id: str, venue: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Queue reminders at each configured offset before the visit."""
        booking = self.db.query_one(
            "SELECT * FROM bookings WHERE id = ? AND tenant_id = ?", (booking_id, ctx.tenant_id)
        )
        if booking is None or not booking["visit_date"]:
            return []
        offsets = self.config.get(ctx, "notification.reminder_offsets_hours", venue_id=venue["id"]) or []
        hours = (venue.get("operating_hours") or {}).get("default", {})
        start_time = hours.get("open", "09:00")
        if booking["session_id"]:
            session = self.db.query_one(
                "SELECT start_time FROM sessions WHERE id = ?", (booking["session_id"],)
            )
            if session is not None:
                start_time = session["start_time"]
        visit_at = combine_local(booking["visit_date"], start_time, venue["timezone"])
        scheduled: list[dict[str, Any]] = []
        for offset in offsets:
            send_at = add_minutes(visit_at, -int(offset) * 60)
            if send_at <= self.clock.now():
                continue
            scheduled.append(
                {
                    "hours_before": int(offset),
                    "scheduled_at": to_iso(send_at),
                    "booking_id": booking_id,
                }
            )
            self.db.insert(
                "notification_messages",
                {
                    "id": new_id("msg"),
                    "tenant_id": ctx.tenant_id,
                    "booking_id": booking_id,
                    "event_type": "BOOKING_REMINDER",
                    "recipient": "",  # resolved at dispatch, after a validity re-check
                    "recipient_hash": "",
                    "channel": "EMAIL",
                    "language": booking["language"],
                    "status": "SCHEDULED",
                    "queued_at": to_iso(self.clock.now()),
                    "next_attempt_at": to_iso(send_at),
                    "dedupe_key": f"BOOKING_REMINDER:{booking_id}:{int(offset)}",
                    "correlation_id": ctx.correlation_id,
                },
            )
        return scheduled

    def dispatch_reminders(
        self, ctx: RequestContext, *, limit: int = 200, customers: Any = None
    ) -> dict[str, Any]:
        """Materialize due reminders, skipping bookings that are no longer valid (R36.6)."""
        now = to_iso(self.clock.now())
        rows = self.db.query(
            "SELECT * FROM notification_messages WHERE tenant_id = ? AND status = 'SCHEDULED' "
            "AND next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?",
            (ctx.tenant_id, now, int(limit)),
        )
        queued = skipped = 0
        for row in rows:
            booking = self.db.query_one(
                "SELECT * FROM bookings WHERE id = ? AND tenant_id = ?",
                (row["booking_id"], ctx.tenant_id),
            )
            if booking is None or booking["status"] != "CONFIRMED":
                self.db.update(
                    "notification_messages",
                    row["id"],
                    {"status": "CANCELLED", "failure_reason": "booking_not_valid"},
                    tenant_id=ctx.tenant_id,
                )
                skipped += 1
                continue
            recipient = None
            if customers is not None and booking["customer_id"]:
                recipient = customers.contact_email(ctx, booking["customer_id"])
            if not recipient:
                self.db.update(
                    "notification_messages",
                    row["id"],
                    {"status": "CANCELLED", "failure_reason": "no_recipient"},
                    tenant_id=ctx.tenant_id,
                )
                skipped += 1
                continue
            variables = self.build_variables(
                ctx,
                event_type="BOOKING_REMINDER",
                booking_id=booking["id"],
                venue_id=booking["venue_id"],
                language=booking["language"],
            )
            variables["show_timetable"] = variables.get("show_schedule_url", "")
            template = self.resolve_template(
                ctx,
                event_type="BOOKING_REMINDER",
                language=booking["language"],
                venue_id=booking["venue_id"],
            )
            if template is None:
                subject, body = self._fallback_content("BOOKING_REMINDER", variables, booking["language"])
            else:
                subject = self._render(template["subject"], variables)
                body = self._render(
                    "\n".join(filter(None, [template["header"], template["body"], template["footer"]])),
                    variables,
                )
            self.db.update(
                "notification_messages",
                row["id"],
                {
                    "status": "QUEUED",
                    "recipient": recipient,
                    "recipient_hash": hash_identifier(recipient),
                    "subject": subject,
                    "rendered_body": body,
                    "template_id": template["id"] if template else None,
                    "template_version": int(template["version"]) if template else None,
                    "next_attempt_at": now,
                },
                tenant_id=ctx.tenant_id,
            )
            queued += 1
        return {"due": len(rows), "queued": queued, "skipped": skipped}

    def cancel_scheduled(self, ctx: RequestContext, *, booking_id: str, reason: str) -> int:
        cursor = self.db.execute(
            "UPDATE notification_messages SET status = 'CANCELLED', failure_reason = ? "
            "WHERE tenant_id = ? AND booking_id = ? AND status = 'SCHEDULED'",
            (reason, ctx.tenant_id, booking_id),
        )
        return cursor.rowcount


__all__ = [
    "COMMON_VARIABLES",
    "EVENT_VARIABLES",
    "TRANSACTIONAL_EVENTS",
    "EmailProvider",
    "NotificationService",
    "SimulatedEmailProvider",
    "SmtpEmailProvider",
    "allowed_variables",
]
