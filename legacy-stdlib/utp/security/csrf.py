"""A01: CSRF protection.

Signed double-submit. The token is an HMAC over the session identifier plus an
expiry, delivered both in a ``SameSite=Strict`` cookie and in a header that script
must set explicitly. An attacker's page can cause the cookie to be sent but cannot
read it to populate the header, and cannot forge the signature without the server
key — so a state-changing request needs something only the real origin has.

Binding to the session is what makes this more than a plain double-submit: a token
minted for one session cannot be replayed into another, which closes the
cookie-injection variant where an attacker fixes a known token in the victim's
browser.

Safe methods are exempt. Any endpoint authenticated purely by a bearer token in an
``Authorization`` header (the partner API) is also exempt, because a browser will
never attach that header automatically and CSRF does not apply.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Iterable

from ..core.clock import Clock, SystemClock, to_iso
from ..core.errors import AuthorizationDenied
from ..core.ids import platform_signing_key, secure_token, sign_payload

#: Methods that must not change state and therefore need no token.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE_NAME = "utp_csrf"
DEFAULT_TTL_SECONDS = 8 * 3600


@dataclass(slots=True)
class CsrfProtection:
    """Mint and verify CSRF tokens."""

    clock: Clock = None  # type: ignore[assignment]
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    secret: bytes = b""

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = SystemClock()
        if not self.secret:
            self.secret = platform_signing_key()

    def issue(self, *, session_id: str) -> str:
        """Mint a token bound to ``session_id``.

        Shape: ``<random>.<expires_epoch>.<signature>``. The random part means two
        tokens for the same session differ, so a token captured from a log or a
        referrer cannot be recognised as belonging to that session.
        """
        nonce = secure_token(16)
        expires = int(self.clock.now().timestamp()) + int(self.ttl_seconds)
        body = f"{nonce}.{expires}.{session_id}"
        return f"{nonce}.{expires}.{sign_payload(self.secret, body)}"

    def verify(
        self,
        *,
        method: str,
        session_id: str | None,
        header_token: str | None,
        cookie_token: str | None,
        has_bearer_auth: bool = False,
    ) -> bool:
        """Return ``True`` when the request may proceed."""
        if method.upper() in SAFE_METHODS:
            return True
        if has_bearer_auth:
            # Not browser-driven; a cross-site page cannot set Authorization.
            return True
        if not session_id or not header_token or not cookie_token:
            return False
        # Constant-time comparison of the two submissions before touching the MAC.
        if not hmac.compare_digest(str(header_token), str(cookie_token)):
            return False
        parts = str(header_token).split(".")
        if len(parts) != 3:
            return False
        nonce, expires_text, signature = parts
        try:
            expires = int(expires_text)
        except ValueError:
            return False
        if int(self.clock.now().timestamp()) > expires:
            return False
        body = f"{nonce}.{expires_text}.{session_id}"
        return hmac.compare_digest(sign_payload(self.secret, body), signature)

    def require(
        self,
        *,
        method: str,
        session_id: str | None,
        header_token: str | None,
        cookie_token: str | None,
        has_bearer_auth: bool = False,
        correlation_id: str | None = None,
    ) -> None:
        """Raise :class:`AuthorizationDenied` when verification fails."""
        if self.verify(
            method=method,
            session_id=session_id,
            header_token=header_token,
            cookie_token=cookie_token,
            has_bearer_auth=has_bearer_auth,
        ):
            return
        raise AuthorizationDenied(
            required="csrf_token",
            log_detail=(
                f"csrf verification failed method={method} "
                f"session_present={bool(session_id)} header_present={bool(header_token)} "
                f"cookie_present={bool(cookie_token)}"
            ),
            correlation_id=correlation_id,
        )


def origin_allowed(origin: str | None, referer: str | None, allowed: Iterable[str]) -> bool:
    """Defence in depth alongside the token: check Origin, falling back to Referer.

    Some legacy clients omit ``Origin``; where both are absent the caller should rely
    on the token alone rather than failing closed on a header that was never
    guaranteed.
    """
    permitted = set(allowed)
    if origin:
        return origin in permitted
    if referer:
        for candidate in permitted:
            if referer.startswith(candidate.rstrip("/") + "/") or referer == candidate:
                return True
        return False
    return True


__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER",
    "DEFAULT_TTL_SECONDS",
    "SAFE_METHODS",
    "CsrfProtection",
    "origin_allowed",
]
