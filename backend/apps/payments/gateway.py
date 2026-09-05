"""Payment gateway abstraction.

The platform never sees raw card data — a real provider tokenizes it client-side and
returns a reference (R14.2). This interface reflects that: ``authorize`` takes an
amount and an idempotency key, and hands back a provider reference and status.

The simulated provider is what the tests and the demo run against. It is
deterministic and idempotent: the same idempotency key always yields the same
reference, so a retried request cannot create a second charge (R14.3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    provider: str
    provider_ref: str
    status: str  # AUTHORIZED | FAILED
    failure_code: str = ""
    failure_message: str = ""


class PaymentGateway(Protocol):
    def authorize(self, *, amount_minor: int, currency: str, idempotency_key: str,
                  method: str) -> AuthorizationResult: ...

    def sign_webhook(self, body: str) -> str: ...


class SimulatedGateway:
    """A deterministic stand-in for a real PSP.

    Authorizes everything except a sentinel amount, so the failure path is testable
    without special-casing the caller. The reference is derived from the idempotency
    key, which is exactly the property that makes replays safe.
    """

    name = "simulated"

    #: An amount ending in this many satang is treated as a declined card, so a test
    #: can exercise the failure branch deterministically.
    DECLINE_SENTINEL = 13

    def __init__(self, signing_key: bytes = b"simulated-webhook-key") -> None:
        self._signing_key = signing_key

    def authorize(self, *, amount_minor: int, currency: str, idempotency_key: str,
                  method: str) -> AuthorizationResult:
        ref = "auth_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
        if amount_minor % 100 == self.DECLINE_SENTINEL:
            return AuthorizationResult(
                provider=self.name,
                provider_ref=ref,
                status="FAILED",
                failure_code="card_declined",
                failure_message="The card was declined.",
            )
        return AuthorizationResult(provider=self.name, provider_ref=ref, status="AUTHORIZED")

    def sign_webhook(self, body: str) -> str:
        import hmac

        return hmac.new(self._signing_key, body.encode(), hashlib.sha256).hexdigest()
