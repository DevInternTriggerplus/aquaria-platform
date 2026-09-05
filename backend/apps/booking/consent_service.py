"""Consent capture and the required-consent gate.

Kept small and focused: publish a notice, describe the dialog, capture a record, and
refuse to proceed without the required item. The heavier DSAR/breach/withdrawal
surface from the reference implementation is a later port; this is the part the
booking flow depends on.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from apps.core.errors import ConsentRequired, ValidationError
from apps.core.ids import hash_identifier

from .consent_models import CONSENT_ITEMS, REQUIRED_ITEMS, ConsentRecord, PrivacyNotice


def current_notice(tenant, language: str = "en") -> PrivacyNotice | None:
    """The newest published notice for a language, or English as a fallback."""
    qs = PrivacyNotice.objects.filter(tenant=tenant, is_current=True)
    return qs.filter(language=language).first() or qs.filter(language="en").first()


def dialog(tenant, language: str = "en") -> dict:
    """Everything the consent dialog renders (R12.3, R12.4).

    Items come back unchecked; the client must not pre-tick anything (R12.5). The
    required item is flagged so the client can gate its submit control (R12.7).
    """
    notice = current_notice(tenant, language)
    return {
        "notice_version": notice.version if notice else None,
        "controller": {
            "name": notice.controller_name if notice else "",
            "contact": notice.controller_contact if notice else "",
        },
        "dpo_contact": notice.dpo_contact if notice else "",
        "notice_url": notice.notice_url if notice else "",
        "body": notice.body if notice else {},
        "items": [
            {"code": code, "required": required, "label": label, "granted": False}
            for code, required, label in CONSENT_ITEMS
        ],
        "submit_disabled_reason": "Accept the required item to continue.",
    }


def check_required(items: dict[str, bool]) -> None:
    """Refuse to proceed without the required consent (R12.7, R12.8).

    Raised *before* any personal data is persisted, so declining leaves nothing
    behind (R12.8).
    """
    missing = [code for code in REQUIRED_ITEMS if not items.get(code)]
    if missing:
        raise ConsentRequired(
            "We cannot continue until you accept the processing needed to create your "
            "booking, issue your tickets and take payment.",
            details={"required": sorted(missing)},
        )


@transaction.atomic
def capture(
    *,
    tenant,
    venue,
    items: dict[str, bool],
    language: str = "en",
    channel: str = "ONLINE",
    contact: str = "",
    booking=None,
    customer=None,
    device: str = "",
    staff_actor=None,
    source_ip: str | None = None,
    user_agent: str = "",
    now: dt.datetime | None = None,
) -> ConsentRecord:
    """Write an immutable consent record (R12.10).

    Only known item codes are accepted, so a client cannot smuggle an unknown consent
    key past the dialog.
    """
    notice = current_notice(tenant, language)
    if notice is None:
        raise ValidationError(
            {"notice": "No privacy notice is published for this tenant."},
            message="Consent cannot be captured yet.",
        )
    known = {code for code, _, _ in CONSENT_ITEMS}
    unknown = set(items) - known
    if unknown:
        raise ValidationError({"items": f"Unknown consent item(s): {', '.join(sorted(unknown))}."})

    moment = now or timezone.now()
    local = moment.astimezone(venue.tzinfo).isoformat()
    return ConsentRecord.objects.create(
        tenant=tenant,
        venue=venue,
        customer=customer,
        booking=booking,
        contact_hash=hash_identifier(contact.lower()) if contact else "",
        notice=notice,
        notice_version=notice.version,
        language=language,
        items={code: bool(items.get(code, False)) for code, _, _ in CONSENT_ITEMS},
        channel=channel,
        device=device,
        staff_actor=staff_actor,
        source_ip=source_ip,
        user_agent=user_agent[:400],
        captured_at_utc=moment,
        captured_at_local=local,
    )


def publish_notice(
    *,
    tenant,
    version: str,
    controller_name: str,
    controller_contact: str,
    dpo_contact: str,
    body: dict,
    language: str = "en",
    notice_url: str = "",
) -> PrivacyNotice:
    """Publish a new immutable notice version and make it current (R12.12)."""
    if not dpo_contact:
        raise ValidationError({"dpo_contact": "A Data Protection Officer contact is mandatory."})
    with transaction.atomic():
        PrivacyNotice.objects.filter(
            tenant=tenant, language=language, is_current=True
        ).update(is_current=False)
        return PrivacyNotice.objects.create(
            tenant=tenant,
            version=version,
            language=language,
            controller_name=controller_name,
            controller_contact=controller_contact,
            dpo_contact=dpo_contact,
            body=body,
            notice_url=notice_url,
            is_current=True,
        )
