"""A05 Security Misconfiguration: response headers, cookies and CORS.

Content Security Policy is nonce-based rather than ``unsafe-inline``. That choice
drives a real constraint on the front end — no inline event handlers, no inline
``<style>`` without the nonce — which is the point: a CSP with ``unsafe-inline``
stops almost nothing, so the policy is written strictly and the UI is built to fit
it.

Two profiles exist because the channels differ. Customer-facing and back office pages
get the full policy. The kiosk profile additionally forbids navigation away from the
venue's own origin, so a guest cannot reach the open web from a kiosk browser
(R33.7's spirit applied at the transport layer).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Iterable, Literal

Profile = Literal["CUSTOMER", "BACKOFFICE", "KIOSK", "API"]


def new_nonce() -> str:
    """Per-response CSP nonce. Must never be reused across responses."""
    return secrets.token_urlsafe(16)


@dataclass(slots=True)
class SecurityHeaderPolicy:
    """Configured header values for one deployment."""

    #: Origins permitted to embed or call the platform. Empty means same-origin only.
    allowed_origins: tuple[str, ...] = ()
    #: Hosts the browser may load images from — the CDN in front of the S3 media bucket.
    image_hosts: tuple[str, ...] = ()
    #: Hosts the browser may connect to (payment SDK, analytics if consented).
    connect_hosts: tuple[str, ...] = ()
    #: Hosts permitted to supply framed content, e.g. a 3-D Secure step-up.
    frame_hosts: tuple[str, ...] = ()
    #: Endpoint that receives CSP violation reports.
    report_uri: str | None = None
    hsts_max_age_seconds: int = 63_072_000  # two years
    hsts_preload: bool = True
    enforce_https: bool = True

    def csp(self, *, nonce: str, profile: Profile = "CUSTOMER") -> str:
        """Build the Content-Security-Policy value."""
        images = " ".join(("'self'", "data:", *self.image_hosts))
        connects = " ".join(("'self'", *self.connect_hosts))
        frames = " ".join(("'none'",)) if not self.frame_hosts else " ".join(self.frame_hosts)
        directives = [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            # 'unsafe-inline' for style is a pragmatic exception: it enables no script
            # execution, and seat maps set per-element fill colours from configuration.
            f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline'",
            f"img-src {images}",
            "font-src 'self' data:",
            f"connect-src {connects}",
            f"frame-src {frames}",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "manifest-src 'self'",
            "worker-src 'self'",
            "upgrade-insecure-requests",
        ]
        if profile == "KIOSK":
            # A kiosk must not be a route to the open web.
            directives = [d for d in directives if not d.startswith("form-action")]
            directives.append("form-action 'self'")
            directives.append("navigate-to 'self'")
        if profile == "API":
            directives = ["default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"]
        if self.report_uri:
            directives.append(f"report-uri {self.report_uri}")
            directives.append("report-to csp-endpoint")
        return "; ".join(directives)

    def headers(self, *, nonce: str | None = None, profile: Profile = "CUSTOMER") -> dict[str, str]:
        """The complete header set for a response."""
        resolved_nonce = nonce or new_nonce()
        headers = {
            "Content-Security-Policy": self.csp(nonce=resolved_nonce, profile=profile),
            # Stops MIME sniffing turning an uploaded file into executable content.
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            # Deny by default; the booking flow needs none of these.
            "Permissions-Policy": (
                "accelerometer=(), ambient-light-sensor=(), autoplay=(), battery=(), camera=(), "
                "display-capture=(), document-domain=(), encrypted-media=(), fullscreen=(self), "
                "geolocation=(), gyroscope=(), magnetometer=(), microphone=(), midi=(), "
                "payment=(self), publickey-credentials-get=(self), screen-wake-lock=(), "
                "usb=(), xr-spatial-tracking=()"
            ),
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cross-Origin-Embedder-Policy": "credentialless",
            # Financial and personal data must never be cached by a shared proxy.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
        }
        if self.enforce_https:
            preload = "; preload" if self.hsts_preload else ""
            headers["Strict-Transport-Security"] = (
                f"max-age={self.hsts_max_age_seconds}; includeSubDomains{preload}"
            )
        if profile == "KIOSK":
            headers["Permissions-Policy"] = headers["Permissions-Policy"].replace(
                "fullscreen=(self)", "fullscreen=*"
            )
        return headers

    # ------------------------------------------------------------------ #
    # CORS
    # ------------------------------------------------------------------ #

    def cors_headers(self, origin: str | None, *, allow_credentials: bool = True) -> dict[str, str]:
        """Reflect an origin only if it is on the allow-list.

        Never returns ``Access-Control-Allow-Origin: *`` alongside credentials, and
        never echoes an unvetted origin — the two mistakes that make CORS a
        vulnerability rather than a control.
        """
        if not origin or origin not in self.allowed_origins:
            return {}
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": (
                "Content-Type, Authorization, X-CSRF-Token, X-Idempotency-Key, X-Correlation-Id"
            ),
            "Access-Control-Max-Age": "600",
        }
        if allow_credentials:
            headers["Access-Control-Allow-Credentials"] = "true"
        return headers

    def is_origin_allowed(self, origin: str | None) -> bool:
        return bool(origin) and origin in self.allowed_origins


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """Cookie attributes. Session cookies are host-only and inaccessible to script."""

    secure: bool = True
    http_only: bool = True
    same_site: Literal["Strict", "Lax", "None"] = "Lax"
    path: str = "/"
    #: Deliberately no ``Domain``: a host-only cookie is not shared with sibling
    #: subdomains, which limits the blast radius of one compromised host.
    domain: str | None = None

    def render(self, name: str, value: str, *, max_age_seconds: int | None = None) -> str:
        parts = [f"{name}={value}", f"Path={self.path}", f"SameSite={self.same_site}"]
        if self.domain:
            parts.append(f"Domain={self.domain}")
        if self.secure:
            parts.append("Secure")
        if self.http_only:
            parts.append("HttpOnly")
        if max_age_seconds is not None:
            parts.append(f"Max-Age={int(max_age_seconds)}")
        if self.same_site == "None" and not self.secure:  # pragma: no cover - guard
            raise ValueError("SameSite=None requires Secure")
        return "; ".join(parts)

    def expire(self, name: str) -> str:
        return self.render(name, "", max_age_seconds=0)


#: Session cookie: Strict SameSite, because no third-party context needs to send it.
SESSION_COOKIE = CookiePolicy(same_site="Strict")

#: CSRF cookie: readable by script by design (double-submit), so HttpOnly is off.
CSRF_COOKIE = CookiePolicy(same_site="Strict", http_only=False)


def default_policy(*, allowed_origins: Iterable[str] = (), cdn_host: str | None = None) -> SecurityHeaderPolicy:
    """A sensible starting policy for a deployment behind CloudFront."""
    image_hosts = tuple(h for h in (cdn_host,) if h)
    return SecurityHeaderPolicy(
        allowed_origins=tuple(allowed_origins),
        image_hosts=image_hosts,
        connect_hosts=(),
        frame_hosts=(),
    )


__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "CookiePolicy",
    "Profile",
    "SecurityHeaderPolicy",
    "default_policy",
    "new_nonce",
]
