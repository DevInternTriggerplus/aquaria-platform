"""A10 Server-Side Request Forgery.

The platform makes outbound requests in a few places — payment provider calls, media
fetches, map references, partner callbacks — and any of them could be pointed at
internal infrastructure if the destination is influenced by input.

:func:`assert_safe_url` fails closed. It requires HTTPS, requires the host to be on
an allow-list, resolves the host and rejects any address that is private, loopback,
link-local, multicast, reserved or the cloud metadata endpoint. Resolution matters:
an allow-listed hostname whose DNS answer points at ``169.254.169.254`` is exactly
the attack this prevents.

Because DNS can change between the check and the request (a rebinding attack), the
resolved address is returned so the caller can connect to *that* address and send the
hostname in the ``Host`` header, rather than resolving a second time.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from ..core.errors import ValidationError

#: Cloud instance metadata endpoints. Reaching these usually means credential theft.
METADATA_HOSTS: frozenset[str] = frozenset(
    {"169.254.169.254", "metadata.google.internal", "100.100.100.200", "fd00:ec2::254"}
)

ALLOWED_SCHEMES: frozenset[str] = frozenset({"https"})

#: Ports that are never legitimate targets for an application HTTP call.
BLOCKED_PORTS: frozenset[int] = frozenset({22, 23, 25, 445, 3306, 3389, 5432, 6379, 9200, 11211, 27017})


@dataclass(slots=True)
class OutboundPolicy:
    """Allow-list of destinations this deployment may call."""

    allowed_hosts: tuple[str, ...] = ()
    #: Permit plain HTTP and private addresses. Development only; never in production.
    allow_insecure: bool = False
    timeout_seconds: float = 5.0
    max_redirects: int = 0
    max_response_bytes: int = 5 * 1024 * 1024

    def host_allowed(self, host: str) -> bool:
        lowered = host.lower()
        for candidate in self.allowed_hosts:
            allowed = candidate.lower()
            if allowed.startswith("*."):
                # A wildcard matches subdomains but not the bare parent, so
                # "*.example.com" cannot be satisfied by "example.com".
                if lowered.endswith(allowed[1:]) and lowered != allowed[2:]:
                    return True
            elif lowered == allowed:
                return True
        return False


@dataclass(frozen=True, slots=True)
class SafeTarget:
    """A validated destination, pinned to the address that was actually checked."""

    url: str
    scheme: str
    host: str
    port: int
    resolved_ip: str
    path: str

    @property
    def connect_to(self) -> tuple[str, int]:
        """Address the caller should connect to, avoiding a second DNS lookup."""
        return self.resolved_ip, self.port


def _reject(detail: str, *, field_name: str = "url") -> None:
    # The public message never names the internal host that was blocked.
    raise ValidationError(
        {field_name: "That address is not permitted."},
        message="That address cannot be used.",
        code="ssrf_blocked",
        log_detail=detail,
    )


def is_private_address(address: str) -> bool:
    """True for anything that is not a routable public address."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True  # unparseable is not provably safe
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def assert_safe_url(
    url: str,
    policy: OutboundPolicy,
    *,
    resolver=socket.getaddrinfo,
    field_name: str = "url",
) -> SafeTarget:
    """Validate an outbound destination, or raise.

    ``resolver`` is injectable so the test suite can prove the private-address and
    rebinding branches without needing DNS.
    """
    text = (url or "").strip()
    if not text:
        _reject("empty url", field_name=field_name)
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES and not (policy.allow_insecure and scheme == "http"):
        _reject(f"scheme not permitted: {scheme!r}", field_name=field_name)
    if not parts.hostname:
        _reject("missing host", field_name=field_name)
    host = parts.hostname
    if "@" in (parts.netloc or ""):
        # Credentials in a URL are also a way to disguise the real host.
        _reject("credentials in url", field_name=field_name)
    if host.lower() in METADATA_HOSTS:
        _reject(f"metadata endpoint: {host}", field_name=field_name)
    port = parts.port or (443 if scheme == "https" else 80)
    if port in BLOCKED_PORTS:
        _reject(f"blocked port: {port}", field_name=field_name)
    if not policy.host_allowed(host):
        _reject(f"host not in allow-list: {host}", field_name=field_name)

    try:
        infos = resolver(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        _reject(f"resolution failed for {host}: {exc}", field_name=field_name)
        raise AssertionError("unreachable")  # pragma: no cover
    if not infos:
        _reject(f"no address for {host}", field_name=field_name)

    resolved: list[str] = []
    for info in infos:
        address = info[4][0]
        resolved.append(address)
        if address in METADATA_HOSTS:
            _reject(f"metadata address via {host}", field_name=field_name)
        if is_private_address(address) and not policy.allow_insecure:
            # Every answer must be public: one private answer is enough to abuse.
            _reject(f"private address {address} via {host}", field_name=field_name)
    return SafeTarget(
        url=text, scheme=scheme, host=host, port=port, resolved_ip=resolved[0], path=parts.path or "/"
    )


def safe_redirect_target(candidate: str, *, allowed_prefixes: Iterable[str]) -> str:
    """Validate a post-login/post-checkout redirect (open redirect).

    Only same-site relative paths are accepted. A protocol-relative ``//evil.test``
    is rejected explicitly because it looks relative but is not.
    """
    text = (candidate or "").strip()
    if not text:
        return "/"
    if text.startswith("//") or "://" in text or text.startswith("\\"):
        _reject(f"absolute redirect rejected: {text!r}", field_name="redirect")
    if not text.startswith("/"):
        _reject(f"non-absolute path rejected: {text!r}", field_name="redirect")
    prefixes = tuple(allowed_prefixes)
    if prefixes and not text.startswith(prefixes):
        _reject(f"redirect outside allow-list: {text!r}", field_name="redirect")
    return text


__all__ = [
    "ALLOWED_SCHEMES",
    "BLOCKED_PORTS",
    "METADATA_HOSTS",
    "OutboundPolicy",
    "SafeTarget",
    "assert_safe_url",
    "is_private_address",
    "safe_redirect_target",
]
