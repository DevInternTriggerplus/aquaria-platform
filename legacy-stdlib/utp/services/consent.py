"""Thailand PDPA consent, customer records and data-subject rights.

Consent and customer data live in one module because R12.2 makes them
structurally dependent: personal data must not be persisted until the required
consent exists. :meth:`CustomerService.upsert` therefore *requires* a consent
record id and refuses to write without one. That is the requirement expressed as
a signature, not as a comment.

The design follows the resolution recorded in the requirements analysis (B.3):
the dialog does two jobs at once. It **delivers the privacy notice and captures
acknowledgement** for the processing necessary to deliver the booking, and it
**captures separate, freely given consent** for genuinely optional purposes.
Optional consents can never block or degrade a purchase (R12.6), which is why
:meth:`ConsentService.assert_required_accepted` looks only at required items.

Everything about a captured consent is immutable. ``consent_records`` and
``privacy_notice_versions`` both carry BEFORE UPDATE and BEFORE DELETE triggers
that abort, so evidence of consent cannot be rewritten later (R12.11, R12.12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..core.audit import AuditLog
from ..core.clock import Clock, add_minutes, local_iso, to_iso
from ..core.config import ConfigStore
from ..core.context import RequestContext
from ..core.db import Database, decode
from ..core.errors import (
    ConflictError,
    ConsentRequired,
    NotFound,
    ValidationError,
)
from ..core.i18n import text as i18n_text
from ..core.ids import hash_identifier, new_id
from .authz import AuthorizationService

#: Consent item codes required by R12.4. ``REQUIRED`` items gate submission;
#: optional ones never do.
CONSENT_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "code": "BOOKING_SERVICE",
        "required": True,
        "purpose": {
            "en": (
                "Create your booking, issue your tickets, take payment, deliver your "
                "e-tickets and validate your entry."
            ),
            "th": "สร้างการจอง ออกบัตรเข้าชม รับชำระเงิน ส่งอีบัตร และตรวจสอบการเข้าชม",
        },
        "lawful_basis": {
            "en": "Necessary to perform the contract you are entering into.",
            "th": "จำเป็นเพื่อปฏิบัติตามสัญญาที่ท่านกำลังทำ",
        },
        "data_categories": ["name", "email", "phone", "booking details", "payment reference"],
    },
    {
        "code": "MARKETING",
        "required": False,
        "purpose": {
            "en": "Send offers, news and promotional messages about the venue.",
            "th": "ส่งข้อเสนอ ข่าวสาร และข้อความส่งเสริมการขายเกี่ยวกับสถานที่",
        },
        "lawful_basis": {"en": "Your consent.", "th": "ความยินยอมของท่าน"},
        "data_categories": ["name", "email", "phone"],
    },
    {
        "code": "ANALYTICS",
        "required": False,
        "purpose": {
            "en": "Analytics, personalization and improving our service.",
            "th": "การวิเคราะห์ การปรับให้เหมาะกับท่าน และการปรับปรุงบริการ",
        },
        "lawful_basis": {"en": "Your consent.", "th": "ความยินยอมของท่าน"},
        "data_categories": ["usage data", "booking history"],
    },
    {
        "code": "PARTNER_SHARING",
        "required": False,
        "config_key": "consent.partner_sharing_enabled",
        "purpose": {
            "en": "Share your details with selected partners or affiliates.",
            "th": "แบ่งปันข้อมูลของท่านกับพันธมิตรหรือบริษัทในเครือที่คัดเลือก",
        },
        "lawful_basis": {"en": "Your consent.", "th": "ความยินยอมของท่าน"},
        "data_categories": ["name", "email"],
    },
    {
        "code": "SENSITIVE",
        "required": False,
        "config_key": "consent.sensitive_data_collected",
        "standalone": True,
        "purpose": {
            "en": "Process sensitive personal data you choose to provide, such as accessibility needs.",
            "th": "ประมวลผลข้อมูลส่วนบุคคลที่มีความละเอียดอ่อนที่ท่านให้ เช่น ความต้องการด้านการเข้าถึง",
        },
        "lawful_basis": {
            "en": "Your explicit consent.",
            "th": "ความยินยอมโดยชัดแจ้งของท่าน",
        },
        "data_categories": ["health or accessibility information"],
    },
)

CONSENT_ITEMS_BY_CODE: dict[str, dict[str, Any]] = {item["code"]: item for item in CONSENT_ITEMS}

REQUIRED_ITEM_CODES: frozenset[str] = frozenset(
    item["code"] for item in CONSENT_ITEMS if item["required"]
)

#: Data-subject request kinds required by R12.21.
DSAR_KINDS: tuple[str, ...] = (
    "ACCESS",
    "RECTIFICATION",
    "ERASURE",
    "RESTRICTION",
    "PORTABILITY",
    "OBJECTION",
    "WITHDRAWAL",
)

#: Fields the platform may collect, each with the purpose it serves (R12.23).
#: A field absent from this map is never persisted.
FIELD_PURPOSES: dict[str, str] = {
    "email": "Deliver the e-ticket, booking confirmation and manage-booking access.",
    "full_name": "Identify the booking at the counter and on the ticket.",
    "phone": "Contact the guest about a schedule change or cancellation.",
    "language": "Send messages and render screens in the guest's language.",
    "accessibility_notes": "Provide requested assistance during the visit.",
    "tax_name": "Issue a tax invoice when requested.",
    "tax_id": "Issue a tax invoice when requested.",
    "tax_address": "Issue a tax invoice when requested.",
}


@dataclass(slots=True)
class ConsentCapture:
    """A captured consent decision set, ready to be recorded."""

    items: dict[str, bool]
    notice_version: str
    consent_text_version: str
    language: str
    contact: str
    guardian_attestation: str | None = None
    authority_attestation: str | None = None
    capture_method: str = "DIALOG"

    def granted(self, code: str) -> bool:
        return bool(self.items.get(code))


class ConsentService:
    """Privacy notices, the consent gate, withdrawal, DSARs and breach records."""

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
    # Privacy notice versions (R12.3, R12.12)
    # ------------------------------------------------------------------ #

    def publish_notice(
        self,
        ctx: RequestContext,
        *,
        version: str,
        consent_text_version: str,
        language: str,
        controller: dict[str, Any],
        purposes: Iterable[dict[str, Any]],
        retention: dict[str, Any],
        recipients: Iterable[dict[str, Any]],
        rights: Iterable[str],
        dpo_contact: str,
        notice_url: str,
        cross_border: dict[str, Any] | None = None,
        items: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Publish an immutable privacy notice version.

        Re-publishing the same version is refused rather than silently updated: a
        published version can never be mutated (R12.12).
        """
        existing = self.db.query_one(
            "SELECT id FROM privacy_notice_versions WHERE tenant_id = ? AND version = ? AND language = ?",
            (ctx.tenant_id, version, language),
        )
        if existing is not None:
            raise ConflictError(
                f"Privacy notice version {version} already exists for {language}. "
                "Publish a new version instead.",
                code="notice_version_immutable",
            )
        for field in ("name", "contact"):
            if not controller.get(field):
                raise ValidationError(
                    {f"controller.{field}": "The data controller's identity and contact are mandatory."}
                )
        if not dpo_contact:
            raise ValidationError({"dpo_contact": "A Data Protection Officer contact channel is mandatory."})
        notice_id = new_id("pnv")
        item_codes = list(items) if items is not None else [i["code"] for i in CONSENT_ITEMS]
        self.db.insert(
            "privacy_notice_versions",
            {
                "id": notice_id,
                "tenant_id": ctx.tenant_id,
                "version": version,
                "consent_text_version": consent_text_version,
                "language": language,
                "controller_json": controller,
                "purposes_json": list(purposes),
                "retention_json": retention,
                "recipients_json": list(recipients),
                "cross_border_json": cross_border or {"transfers": False},
                "rights_json": list(rights),
                "dpo_contact": dpo_contact,
                "notice_url": notice_url,
                "items_json": item_codes,
                "published_at": to_iso(self.clock.now()),
            },
        )
        self.audit.record(
            ctx,
            "CONFIG_CHANGE",
            target_type="privacy_notice",
            target_id=notice_id,
            new={"version": version, "consent_text_version": consent_text_version, "language": language},
            severity="WARNING",
        )
        return self.current_notice(ctx, language=language)

    def current_notice(self, ctx: RequestContext, *, language: str) -> dict[str, Any]:
        """Latest published notice for a language, falling back to the tenant default."""
        # ``rowid`` breaks ties: two versions can share a publication timestamp, and
        # "current" must always mean the most recently inserted one rather than
        # whichever row the planner happens to return first.
        select = (
            "SELECT * FROM privacy_notice_versions WHERE tenant_id = ? AND language = ? "
            "ORDER BY published_at DESC, rowid DESC LIMIT 1"
        )
        row = self.db.query_one(select, (ctx.tenant_id, language))
        if row is None:
            default_language = self.db.scalar(
                "SELECT default_language FROM tenants WHERE id = ?", (ctx.tenant_id,), default="en"
            )
            row = self.db.query_one(select, (ctx.tenant_id, default_language))
        if row is None:
            raise NotFound(details={"entity": "privacy_notice"})
        record = dict(row)
        for field in (
            "controller",
            "purposes",
            "retention",
            "recipients",
            "cross_border",
            "rights",
            "items",
        ):
            record[field] = decode(record.pop(f"{field}_json"), None)
        return record

    # ------------------------------------------------------------------ #
    # The consent dialog (R12.1, R12.3 - R12.5, R12.18, R12.27)
    # ------------------------------------------------------------------ #

    def dialog(
        self,
        ctx: RequestContext,
        *,
        language: str | None = None,
        channel: str | None = None,
        is_minor: bool = False,
        booking_for_others: bool = False,
    ) -> dict[str, Any]:
        """Build the blocking consent dialog payload.

        Every checkbox is returned ``granted=False``. There is no code path that can
        return a pre-ticked item, which is how R12.5 is guaranteed rather than
        merely intended.
        """
        lang = language or ctx.language
        chan = channel or ctx.channel
        notice = self.current_notice(ctx, language=lang)
        enabled_codes = set(notice.get("items") or [])

        items: list[dict[str, Any]] = []
        for definition in CONSENT_ITEMS:
            code = definition["code"]
            if code not in enabled_codes:
                continue
            config_key = definition.get("config_key")
            if config_key and not self.config.get_bool(ctx, config_key, default=False):
                # Not configured for this tenant, so the item is not shown at all
                # rather than shown and ignored (R12.4 "WHERE such sharing is
                # configured").
                continue
            items.append(
                {
                    "code": code,
                    "required": bool(definition["required"]),
                    "standalone": bool(definition.get("standalone")),
                    "granted": False,  # never pre-ticked (R12.5)
                    "label": _localized(definition["purpose"], lang),
                    "lawful_basis": _localized(definition["lawful_basis"], lang),
                    "data_categories": definition["data_categories"],
                }
            )

        is_kiosk = chan == "KIOSK"
        return {
            "blocking": True,
            "channel": chan,
            "language": lang,
            "notice_version": notice["version"],
            "consent_text_version": notice["consent_text_version"],
            "notice_url": notice["notice_url"],
            "controller": notice["controller"],
            "dpo_contact": notice["dpo_contact"],
            "purposes": notice["purposes"],
            "retention": notice["retention"],
            "recipients": notice["recipients"],
            "cross_border": notice["cross_border"],
            "rights": notice["rights"],
            "items": items,
            "required_item_codes": sorted(i["code"] for i in items if i["required"]),
            "submit_enabled": False,
            "submit_disabled_reason": _localized(
                {
                    "en": (
                        "Please accept the processing needed to create your booking, "
                        "issue your tickets and take payment."
                    ),
                    "th": "กรุณายอมรับการประมวลผลข้อมูลที่จำเป็นสำหรับการจอง การออกบัตร และการชำระเงิน",
                },
                lang,
            ),
            "separate_from_terms": True,  # R12.9
            "requires_guardian_consent": bool(is_minor),
            "guardian_prompt": _localized(
                {
                    "en": "A parent or legal guardian must give this consent.",
                    "th": "ผู้ปกครองหรือผู้แทนโดยชอบธรรมต้องให้ความยินยอมนี้",
                },
                lang,
            )
            if is_minor
            else None,
            "requires_authority_attestation": bool(booking_for_others),  # R12.17
            "authority_prompt": _localized(
                {
                    "en": "Confirm you are authorised to provide the other visitors' details.",
                    "th": "ยืนยันว่าท่านได้รับอนุญาตให้ให้ข้อมูลของผู้เข้าชมท่านอื่น",
                },
                lang,
            )
            if booking_for_others
            else None,
            "staff_must_present_notice": chan in ("COUNTER", "STAFF"),  # R12.19
            # Accessibility contract for the dialog (R12.27, R68.7).
            "accessibility": {
                "trap_focus": True,
                "dismissible_only_by_choice": True,
                "labels_programmatically_associated": True,
                "keyboard_operable": True,
            },
            "presentation": {
                "single_screen": True,  # analysis B.7 — never split across screens
                "kiosk_scale": is_kiosk,
                "language_selector_inside_dialog": is_kiosk,  # R12.18
                "min_touch_target_px": self.config.get_int(
                    ctx, "ui.kiosk_touch_target_min_px" if is_kiosk else "ui.touch_target_min_px"
                ),
            },
        }

    # ------------------------------------------------------------------ #
    # Capture (R12.10)
    # ------------------------------------------------------------------ #

    def capture(
        self,
        ctx: RequestContext,
        capture: ConsentCapture,
        *,
        venue_id: str | None = None,
        booking_id: str | None = None,
        customer_id: str | None = None,
        venue_timezone: str = "UTC",
        partner_attestation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an immutable consent record (R12.10).

        ``booking_id`` is accepted here rather than written later, because the record
        can never be updated. The booking service reserves its identifier before
        capturing consent so R12.10's "booking reference (once created)" is satisfied
        without mutating evidence of consent.
        """
        self.check_required(ctx, capture.items)
        unknown = sorted(set(capture.items) - set(CONSENT_ITEMS_BY_CODE))
        if unknown:
            raise ValidationError({"items": f"Unknown consent item(s): {', '.join(unknown)}."})
        if ctx.channel in ("COUNTER", "STAFF") and not ctx.principal.id:
            # R12.19 — staff-assisted consent must be attributable to a staff actor.
            raise ValidationError(
                {"staff_actor_id": "Staff-assisted consent must record the staff member."},
                message="Sign in before recording consent on a guest's behalf.",
            )
        if ctx.channel == "PARTNER":
            self._validate_partner_attestation(partner_attestation)
        now = self.clock.now()
        record_id = new_id("con")
        self.db.insert(
            "consent_records",
            {
                "id": record_id,
                "tenant_id": ctx.tenant_id,
                "venue_id": venue_id or ctx.venue_id,
                "channel": ctx.channel,
                "device_id": ctx.device_id,
                "booking_id": booking_id,
                "customer_id": customer_id,
                "contact_hash": hash_identifier(capture.contact),
                "items_json": {code: bool(value) for code, value in capture.items.items()},
                "notice_version": capture.notice_version,
                "consent_text_version": capture.consent_text_version,
                "language": capture.language,
                "created_at_utc": to_iso(now),
                "created_at_local": local_iso(now, venue_timezone),
                "ip_address": ctx.ip_address,
                "user_agent": ctx.user_agent,
                "staff_actor_id": ctx.principal.id if ctx.channel in ("COUNTER", "STAFF") else None,
                "guardian_attestation": capture.guardian_attestation,
                "authority_attestation": capture.authority_attestation,
                "partner_attestation_json": partner_attestation,
                "capture_method": capture.capture_method,
            },
        )
        self.audit.record(
            ctx,
            "CONSENT_CAPTURED",
            target_type="consent_record",
            target_id=record_id,
            new={
                "items": {code: bool(v) for code, v in capture.items.items()},
                "notice_version": capture.notice_version,
                "consent_text_version": capture.consent_text_version,
                "channel": ctx.channel,
                "language": capture.language,
            },
            venue_timezone=venue_timezone,
        )
        return self.get_record(ctx, record_id)

    def check_required(self, ctx: RequestContext, items: dict[str, bool]) -> None:
        """Refuse to proceed without the required consent (R12.7, R12.8).

        The caller must not have persisted any personal data before this passes;
        :class:`CustomerService` enforces that by requiring a consent record id.
        """
        missing = sorted(code for code in REQUIRED_ITEM_CODES if not items.get(code))
        if missing:
            raise ConsentRequired(
                details={
                    "missing_required_items": missing,
                    "personal_data_retained": False,
                    "alternatives": ["Purchase at the ticket counter"],
                }
            )

    def _validate_partner_attestation(self, attestation: dict[str, Any] | None) -> None:
        """R12.20 — a partner booking without a valid attestation is rejected."""
        if not attestation:
            raise ValidationError(
                {"consent_attestation": "A consent attestation is required for partner bookings."},
                message="Partner bookings must transmit a consent attestation.",
                code="consent_attestation_required",
            )
        required = ("consent_text_version", "captured_at", "capture_method")
        missing = [field for field in required if not attestation.get(field)]
        if missing:
            raise ValidationError(
                {"consent_attestation": f"Attestation is missing: {', '.join(missing)}."},
                message="The consent attestation is incomplete.",
                code="consent_attestation_invalid",
            )

    def get_record(self, ctx: RequestContext, record_id: str) -> dict[str, Any]:
        record = self.authz.load_scoped(ctx, "consent_records", record_id, entity="consent_record")
        record["items"] = decode(record.pop("items_json"), {})
        record["partner_attestation"] = decode(record.pop("partner_attestation_json"), None)
        return record

    def latest_for_contact(self, ctx: RequestContext, contact: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT id FROM consent_records WHERE tenant_id = ? AND contact_hash = ? "
            "ORDER BY created_at_utc DESC, rowid DESC LIMIT 1",
            (ctx.tenant_id, hash_identifier(contact)),
        )
        return self.get_record(ctx, row["id"]) if row else None

    def needs_reconsent(self, ctx: RequestContext, contact: str, *, language: str | None = None) -> bool:
        """Has the notice or consent text moved on since this person last consented? (R12.13)"""
        latest = self.latest_for_contact(ctx, contact)
        if latest is None:
            return True
        notice = self.current_notice(ctx, language=language or ctx.language)
        return (
            latest["notice_version"] != notice["version"]
            or latest["consent_text_version"] != notice["consent_text_version"]
        )

    def effective_state(self, ctx: RequestContext, contact: str) -> dict[str, bool]:
        """Current consent per item, accounting for later withdrawals (R12.14)."""
        latest = self.latest_for_contact(ctx, contact)
        if latest is None:
            return {code: False for code in CONSENT_ITEMS_BY_CODE}
        state = {code: bool(value) for code, value in (latest["items"] or {}).items()}
        withdrawals = self.db.query(
            "SELECT item_code FROM consent_withdrawals WHERE tenant_id = ? AND consent_record_id = ?",
            (ctx.tenant_id, latest["id"]),
        )
        for row in withdrawals:
            state[row["item_code"]] = False
        return state

    def has_consent(self, ctx: RequestContext, contact: str, item_code: str) -> bool:
        return bool(self.effective_state(ctx, contact).get(item_code))

    # ------------------------------------------------------------------ #
    # Withdrawal (R12.14, R12.15)
    # ------------------------------------------------------------------ #

    def withdrawal_consequences(self, ctx: RequestContext, item_code: str, *, language: str | None = None) -> dict[str, Any]:
        """What the customer must be told *before* the withdrawal is confirmed."""
        lang = language or ctx.language
        if item_code in REQUIRED_ITEM_CODES:
            raise ValidationError(
                {"item_code": "The processing needed to deliver your booking cannot be withdrawn "
                              "while the booking is active."},
                message="This consent cannot be withdrawn separately.",
            )
        consequences = {
            "MARKETING": {
                "en": "You will stop receiving offers and news. Your booking and tickets are unaffected, "
                      "and we will still send messages needed to deliver your visit.",
                "th": "ท่านจะไม่ได้รับข้อเสนอและข่าวสารอีก การจองและบัตรของท่านไม่ได้รับผลกระทบ "
                      "และเราจะยังส่งข้อความที่จำเป็นสำหรับการเข้าชม",
            },
            "ANALYTICS": {
                "en": "We will stop using your data to personalize and improve the service.",
                "th": "เราจะหยุดใช้ข้อมูลของท่านเพื่อปรับให้เหมาะและปรับปรุงบริการ",
            },
            "PARTNER_SHARING": {
                "en": "We will stop sharing your details with partners.",
                "th": "เราจะหยุดแบ่งปันข้อมูลของท่านกับพันธมิตร",
            },
            "SENSITIVE": {
                "en": "We will stop processing the sensitive information you provided. "
                      "Assistance you requested may no longer be arranged automatically.",
                "th": "เราจะหยุดประมวลผลข้อมูลที่มีความละเอียดอ่อนที่ท่านให้ "
                      "ความช่วยเหลือที่ท่านขออาจไม่ถูกจัดเตรียมโดยอัตโนมัติ",
            },
        }
        days = self.config.get_int(ctx, "consent.withdrawal_effective_days")
        return {
            "item_code": item_code,
            "message": _localized(consequences.get(item_code, {"en": "This processing will stop."}), lang),
            "effective_within_days": days,
            "booking_remains_valid": True,  # R12.15
            "transactional_messages_continue": True,
        }

    def withdraw(
        self,
        ctx: RequestContext,
        *,
        contact: str,
        item_code: str,
        confirmed: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Withdraw one optional consent (R12.14).

        Never cancels the booking and never stops transactional messages (R12.15).
        """
        consequences = self.withdrawal_consequences(ctx, item_code)
        if not confirmed:
            from ..core.errors import ConfirmationRequired

            raise ConfirmationRequired(
                consequences["message"],
                details={"requires_confirmation": True, **consequences},
            )
        latest = self.latest_for_contact(ctx, contact)
        if latest is None:
            raise NotFound(details={"entity": "consent_record"})
        now = self.clock.now()
        days = int(consequences["effective_within_days"])
        withdrawal_id = new_id("cwd")
        with self.db.transaction():
            self.db.insert(
                "consent_withdrawals",
                {
                    "id": withdrawal_id,
                    "tenant_id": ctx.tenant_id,
                    "consent_record_id": latest["id"],
                    "customer_id": latest.get("customer_id"),
                    "item_code": item_code,
                    "withdrawn_at": to_iso(now),
                    "effective_by": to_iso(add_minutes(now, days * 24 * 60)),
                    "channel": ctx.channel,
                    "actor_id": actor_id or ctx.principal.id,
                    "acknowledged": 1,
                },
            )
            # The consent record is append-only, so the withdrawal is a new row and
            # the customer's derived opt-in flag is what gets cleared. Resolution is
            # by contact hash because a first-time guest's consent predates their
            # customer record (R12.2).
            customer_id = latest.get("customer_id") or self.db.scalar(
                "SELECT id FROM customers WHERE tenant_id = ? AND email_hash = ?",
                (ctx.tenant_id, latest["contact_hash"]),
            )
            if customer_id:
                flag = {
                    "MARKETING": "marketing_opt_in",
                    "ANALYTICS": "analytics_opt_in",
                    "PARTNER_SHARING": "partner_share_opt_in",
                }.get(item_code)
                if flag:
                    self.db.update(
                        "customers", customer_id, {flag: 0, "updated_at": to_iso(now)}, tenant_id=ctx.tenant_id
                    )
            self.audit.record(
                ctx,
                "CONSENT_WITHDRAWN",
                target_type="consent_record",
                target_id=latest["id"],
                previous={"item": item_code, "granted": True},
                new={"item": item_code, "granted": False, "effective_by": to_iso(add_minutes(now, days * 24 * 60))},
                severity="WARNING",
            )
        return {
            "withdrawal_id": withdrawal_id,
            "item_code": item_code,
            "withdrawn_at": to_iso(now),
            "effective_by": to_iso(add_minutes(now, days * 24 * 60)),
            "booking_remains_valid": True,
            "transactional_messages_continue": True,
        }

    # ------------------------------------------------------------------ #
    # Data subject requests (R12.21, R12.22)
    # ------------------------------------------------------------------ #

    def record_dsar(
        self,
        ctx: RequestContext,
        *,
        kind: str,
        contact: str,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Log a data-subject request against its statutory deadline (R12.21)."""
        if kind not in DSAR_KINDS:
            raise ValidationError({"kind": f"Kind must be one of {', '.join(DSAR_KINDS)}."})
        now = self.clock.now()
        days = self.config.get_int(ctx, "privacy.dsar_response_days")
        request_id = new_id("dsr")
        self.db.insert(
            "dsar_requests",
            {
                "id": request_id,
                "tenant_id": ctx.tenant_id,
                "customer_id": customer_id,
                "contact_hash": hash_identifier(contact),
                "kind": kind,
                "status": "RECEIVED",
                "received_at": to_iso(now),
                "due_at": to_iso(add_minutes(now, days * 24 * 60)),
                "actor_id": ctx.principal.id,
            },
        )
        self.audit.record(
            ctx,
            "DSAR_RECEIVED",
            target_type="dsar_request",
            target_id=request_id,
            new={"kind": kind, "due_at": to_iso(add_minutes(now, days * 24 * 60))},
            severity="WARNING",
        )
        return dict(self.db.query_one("SELECT * FROM dsar_requests WHERE id = ?", (request_id,)))

    def complete_dsar(
        self,
        ctx: RequestContext,
        request_id: str,
        *,
        outcome: str,
        justification: str | None = None,
    ) -> dict[str, Any]:
        request = self.authz.load_scoped(ctx, "dsar_requests", request_id, entity="dsar_request")
        now = to_iso(self.clock.now())
        self.db.update(
            "dsar_requests",
            request_id,
            {"status": "COMPLETED", "completed_at": now, "outcome": outcome, "justification": justification},
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            ctx,
            "DSAR_COMPLETED",
            target_type="dsar_request",
            target_id=request_id,
            previous={"status": request["status"]},
            new={"status": "COMPLETED", "outcome": outcome},
            reason=justification,
            severity="WARNING",
        )
        return dict(self.db.query_one("SELECT * FROM dsar_requests WHERE id = ?", (request_id,)))

    def overdue_dsars(self, ctx: RequestContext) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM dsar_requests WHERE tenant_id = ? AND status <> 'COMPLETED' AND due_at < ? "
            "ORDER BY due_at",
            (ctx.tenant_id, to_iso(self.clock.now())),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Breach handling (R12.26)
    # ------------------------------------------------------------------ #

    def record_breach(
        self,
        ctx: RequestContext,
        *,
        scope: str,
        data_categories: Sequence[str],
        affected_count: int,
        remediation: str | None = None,
    ) -> dict[str, Any]:
        """Open a breach incident with its 72-hour reporting deadline."""
        now = self.clock.now()
        hours = self.config.get_int(ctx, "privacy.breach_report_hours")
        incident_id = new_id("brc")
        self.db.insert(
            "breach_incidents",
            {
                "id": incident_id,
                "tenant_id": ctx.tenant_id,
                "detected_at": to_iso(now),
                "due_at": to_iso(add_minutes(now, hours * 60)),
                "scope": scope,
                "data_categories_json": list(data_categories),
                "affected_count": int(affected_count),
                "remediation": remediation,
                "status": "OPEN",
                "actor_id": ctx.principal.id,
            },
        )
        self.audit.record(
            ctx,
            "BREACH_RECORDED",
            target_type="breach_incident",
            target_id=incident_id,
            new={
                "scope": scope,
                "affected_count": int(affected_count),
                "due_at": to_iso(add_minutes(now, hours * 60)),
            },
            severity="WARNING",
        )
        return dict(self.db.query_one("SELECT * FROM breach_incidents WHERE id = ?", (incident_id,)))

    def report_breach(self, ctx: RequestContext, incident_id: str, *, remediation: str) -> dict[str, Any]:
        incident = self.authz.load_scoped(ctx, "breach_incidents", incident_id, entity="breach_incident")
        now = to_iso(self.clock.now())
        self.db.update(
            "breach_incidents",
            incident_id,
            {"status": "REPORTED", "reported_at": now, "remediation": remediation},
            tenant_id=ctx.tenant_id,
        )
        self.audit.record(
            ctx,
            "BREACH_RECORDED",
            target_type="breach_incident",
            target_id=incident_id,
            previous={"status": incident["status"]},
            new={"status": "REPORTED", "within_deadline": now <= incident["due_at"]},
            severity="WARNING",
        )
        return dict(self.db.query_one("SELECT * FROM breach_incidents WHERE id = ?", (incident_id,)))

    # ------------------------------------------------------------------ #
    # Evidence export (R12.11)
    # ------------------------------------------------------------------ #

    def export_evidence(self, ctx: RequestContext, *, contact: str) -> dict[str, Any]:
        """Exportable proof of consent for one data subject."""
        self.authz.require_action(ctx, "EXPORT")
        self.authz.require_action(ctx, "VIEW_PII")
        contact_hash = hash_identifier(contact)
        records = self.db.query(
            "SELECT * FROM consent_records WHERE tenant_id = ? AND contact_hash = ? ORDER BY created_at_utc",
            (ctx.tenant_id, contact_hash),
        )
        withdrawals = self.db.query(
            """
            SELECT w.* FROM consent_withdrawals w
            JOIN consent_records c ON c.id = w.consent_record_id AND c.tenant_id = w.tenant_id
            WHERE w.tenant_id = ? AND c.contact_hash = ? ORDER BY w.withdrawn_at
            """,
            (ctx.tenant_id, contact_hash),
        )
        self.audit.record(
            ctx,
            "EXPORT",
            target_type="consent_evidence",
            target_id=contact_hash[:16],
            new={"record_count": len(records), "withdrawal_count": len(withdrawals)},
            severity="WARNING",
        )
        out = []
        for row in records:
            item = dict(row)
            item["items"] = decode(item.pop("items_json"), {})
            item["partner_attestation"] = decode(item.pop("partner_attestation_json"), None)
            out.append(item)
        return {
            "contact_hash": contact_hash,
            "consent_records": out,
            "withdrawals": [dict(w) for w in withdrawals],
            "generated_at": to_iso(self.clock.now()),
        }


def _localized(mapping: dict[str, Any], language: str) -> str:
    """Resolve a bilingual literal from this module's constants (R66.7, R69.4)."""
    return i18n_text(mapping, language)


__all__ = [
    "CONSENT_ITEMS",
    "CONSENT_ITEMS_BY_CODE",
    "DSAR_KINDS",
    "FIELD_PURPOSES",
    "REQUIRED_ITEM_CODES",
    "ConsentCapture",
    "ConsentService",
]
