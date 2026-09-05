"""Identifier and token generation.

Two distinct needs:

* **Internal identifiers** — opaque, sortable-by-creation, prefixed so that a
  log line or audit row is self-describing.
* **Customer-facing references** — booking numbers and ticket numbers that a
  human can read over the phone, plus QR payloads that must be *unguessable*
  and carry no personal data (R15.2).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

# Crockford-style alphabet: no I, L, O or U, so nothing is misread aloud.
_HUMAN_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base32_time() -> str:
    """Millisecond timestamp encoded so identifiers sort by creation order."""
    value = int(time.time() * 1000)
    out: list[str] = []
    for _ in range(10):
        out.append(_ID_ALPHABET[value % 32])
        value //= 32
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return a prefixed, time-ordered, collision-resistant identifier.

    >>> new_id("bkg").startswith("bkg_")
    True
    """
    if not prefix or not prefix.isalnum():
        raise ValueError("prefix must be alphanumeric")
    random_part = "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))
    return f"{prefix}_{_base32_time()}{random_part}"


def human_code(length: int = 8) -> str:
    """Random human-readable code (booking numbers, one-time codes)."""
    return "".join(secrets.choice(_HUMAN_ALPHABET) for _ in range(length))


def booking_number(venue_short_code: str, sequence_hint: int | None = None) -> str:
    """Booking number of the shape ``AQP-7K2M-4QX9``.

    The venue short code makes counter and gate conversations unambiguous when a
    tenant runs several sites. The remainder is random rather than sequential so
    that booking numbers cannot be enumerated (R16.3).
    """
    code = (venue_short_code or "GEN").upper()[:4]
    body = human_code(4)
    tail = human_code(4) if sequence_hint is None else f"{sequence_hint % 10000:04d}"
    return f"{code}-{body}-{tail}"


def ticket_number(booking_no: str, index: int) -> str:
    """Deterministic per-booking ticket number, e.g. ``AQP-7K2M-4QX9-03``."""
    return f"{booking_no}-{index:02d}"


def secure_token(nbytes: int = 24) -> str:
    """URL-safe, unguessable token (QR payloads, verification links)."""
    return secrets.token_urlsafe(nbytes)


def sign_payload(secret: bytes, payload: str) -> str:
    """Detached HMAC-SHA256 signature, truncated to 24 characters.

    Used for QR payloads and offline gate caches. The signature proves the
    payload was minted by this platform; the payload itself is an opaque
    reference, so no personal data is ever encoded in a QR code (R15.2).
    """
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:24]


def verify_signature(secret: bytes, payload: str, signature: str) -> bool:
    """Constant-time signature check."""
    return hmac.compare_digest(sign_payload(secret, payload), signature or "")


def hash_identifier(value: str, *, salt: bytes = b"utp-identifier") -> str:
    """Stable, non-reversible hash for lookup keys (e.g. email lookup index).

    Lets the platform find a customer by email without the index itself being a
    readable directory of email addresses.
    """
    normalized = (value or "").strip().lower().encode("utf-8")
    return hashlib.blake2b(normalized, key=salt, digest_size=20).hexdigest()


def new_correlation_id() -> str:
    """Correlation id shared by every audit event of one logical operation (R45.7)."""
    return f"cor_{secrets.token_hex(10)}"


def new_secret(nbytes: int = 32) -> str:
    """Fresh device/partner credential. Stored only as a hash."""
    return secrets.token_urlsafe(nbytes)


def hash_secret(secret: str, *, salt: str | None = None) -> str:
    """Hash a credential for storage. Returns ``salt$digest``."""
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${digest.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    """Verify a credential against ``salt$digest``."""
    if not stored or "$" not in stored:
        return False
    salt, _, expected = stored.partition("$")
    return hmac.compare_digest(hash_secret(secret, salt=salt), f"{salt}${expected}")


def platform_signing_key() -> bytes:
    """Signing key for QR payloads and offline caches.

    Read from the environment so that production supplies it from a managed
    secret store (R73.9). The development fallback is fixed only so that tests
    are deterministic; it is never used when the variable is set.
    """
    env = os.environ.get("UTP_SIGNING_KEY")
    if env:
        return env.encode("utf-8")
    return b"utp-development-signing-key-not-for-production"


__all__ = [
    "booking_number",
    "hash_identifier",
    "hash_secret",
    "human_code",
    "new_correlation_id",
    "new_id",
    "new_secret",
    "platform_signing_key",
    "secure_token",
    "sign_payload",
    "ticket_number",
    "verify_secret",
    "verify_signature",
]
