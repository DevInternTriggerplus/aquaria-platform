"""Signed, expiring references to a ticket's QR image.

An e-ticket in email has to get its QR in front of the guest, and mail clients
disagree about how that is allowed to happen:

* ``cid:`` inline attachments are the most reliable, and are what
  :mod:`utp.services.mail_mime` builds — but they only exist inside a real MIME
  message.
* ``data:`` URLs render in a browser and in several desktop clients, but Gmail
  strips them.
* A remote ``https://`` image is what Gmail expects; it fetches through its own
  proxy and caches the result.

The remote option needs a URL that can be fetched with no session, which means it
cannot be ``/tickets/{id}/qr.svg`` — that would make a credential-bearing image
reachable by guessing an identifier. So a link carries its own capability: the
ticket reference and an expiry, HMAC-signed with the platform key, encoded
URL-safe. Guessing one is as hard as forging the signature, and it stops working
on its own.

The token is *not* the access credential. It only authorises rendering the image;
the credential lives inside the QR, and the gate verifies that separately.
"""

from __future__ import annotations

import base64
import datetime as _dt

from ..core.ids import platform_signing_key, sign_payload, verify_signature

#: How long a mailed QR link stays fetchable. Long enough for a guest to open the
#: message weeks later and for Gmail's proxy to re-fetch, short enough that a
#: forwarded mail does not grant an indefinite handle.
DEFAULT_TTL_DAYS = 120

_PREFIX = "QRL1"


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(text: str) -> str:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii")).decode("utf-8")


def sign_qr_token(
    ticket_id: str, *, expires_at: _dt.datetime | None = None, ttl_days: int = DEFAULT_TTL_DAYS
) -> str:
    """Mint a capability token for one ticket's QR image."""
    if expires_at is None:
        expires_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=ttl_days)
    expiry = int(expires_at.timestamp())
    claim = f"{_PREFIX}.{ticket_id}.{expiry}"
    return f"{_b64(claim)}.{sign_payload(platform_signing_key(), claim)}"


def verify_qr_token(token: str, *, now: _dt.datetime | None = None) -> str | None:
    """Return the ticket id for a valid, unexpired token, else ``None``.

    Every failure returns ``None`` rather than distinguishing "expired" from
    "forged": the caller answers both with a plain not-found, so the endpoint
    cannot be used to probe which tickets exist.
    """
    if not token or "." not in token:
        return None
    encoded, _, signature = token.rpartition(".")
    try:
        claim = _unb64(encoded)
    except (ValueError, UnicodeDecodeError):
        return None
    if not verify_signature(platform_signing_key(), claim, signature):
        return None
    parts = claim.split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    _, ticket_id, expiry_text = parts
    try:
        expiry = int(expiry_text)
    except ValueError:
        return None
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.timestamp() > expiry:
        return None
    return ticket_id or None


def qr_image_url(ticket_id: str, *, base_url: str = "", ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """The absolute (or root-relative) URL of a ticket's QR image."""
    token = sign_qr_token(ticket_id, ttl_days=ttl_days)
    return f"{base_url.rstrip('/')}/qr/{token}"
