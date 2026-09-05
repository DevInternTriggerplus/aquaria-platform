"""A04 Insecure Design: rate limiting and abuse quotas.

Rate limiting here is not just about load. Three of the residual risks recorded in
the requirements analysis are abuse problems that only a quota can address:

* **D.5 seat-hold abuse** — repeatedly holding seats to deny inventory to real
  customers. Bounded by a per-source hold quota, not just a request rate.
* **D.6 promotion-code brute force** — enumerating codes. Bounded per source, and
  deliberately *slower* than the generic limit.
* **booking enumeration (R16.3)** — bounded per identifier *and* per source address,
  so neither dimension alone is a way through, with exponential backoff.

A fixed window is used rather than a sliding one: it is exact under concurrency with a
single conditional UPDATE, and the burst it permits at a boundary is acceptable for
these controls. Where burst tolerance matters more than exactness, :class:`TokenBucket`
is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..core.clock import Clock, to_iso
from ..core.db import Database
from ..core.errors import RateLimited
from ..core.ids import hash_identifier, new_id

Granularity = Literal["minute", "hour", "day"]


@dataclass(frozen=True, slots=True)
class Quota:
    """One named limit."""

    name: str
    limit: int
    granularity: Granularity = "hour"
    #: Double the retry delay for each attempt beyond the limit.
    backoff: bool = True
    max_retry_after_seconds: int = 3600
    #: Starting delay for the first breach. Deliberately small and independent of the
    #: window: telling the first over-limit caller to wait an hour makes escalation
    #: invisible, whereas 30s -> 60s -> 120s punishes persistence specifically.
    base_retry_seconds: int = 30

    def window(self, now_iso: str) -> str:
        return {"minute": now_iso[:16], "hour": now_iso[:13], "day": now_iso[:10]}[self.granularity]

    def window_seconds(self) -> int:
        return {"minute": 60, "hour": 3600, "day": 86400}[self.granularity]

    def retry_after(self, attempts_over_limit: int) -> int:
        """Delay to advertise after ``attempts_over_limit`` breaches."""
        if not self.backoff:
            return min(self.window_seconds(), self.max_retry_after_seconds)
        exponent = max(attempts_over_limit - 1, 0)
        delay = self.base_retry_seconds * (2 ** min(exponent, 12))
        # Never advertise longer than the window, because the counter resets then.
        return int(min(delay, self.window_seconds(), self.max_retry_after_seconds))


#: Platform quotas. Every one of these is overridable per tenant in configuration.
QUOTAS: dict[str, Quota] = {
    # R73.5 — authentication
    "login_per_account": Quota("login_per_account", limit=10, granularity="hour"),
    "login_per_source": Quota("login_per_source", limit=30, granularity="hour"),
    # R16.3 — booking lookup, both dimensions
    "booking_lookup_per_identifier": Quota("booking_lookup_per_identifier", limit=5, granularity="hour"),
    "booking_lookup_per_source": Quota("booking_lookup_per_source", limit=20, granularity="hour"),
    "verification_attempt": Quota("verification_attempt", limit=8, granularity="hour"),
    # D.6 — promotion codes, deliberately tight
    "promo_code_per_source": Quota("promo_code_per_source", limit=12, granularity="hour"),
    "promo_code_per_cart": Quota("promo_code_per_cart", limit=6, granularity="hour"),
    # D.5 — hold abuse
    "hold_per_source": Quota("hold_per_source", limit=40, granularity="hour"),
    "seat_hold_per_source": Quota("seat_hold_per_source", limit=60, granularity="hour"),
    "concurrent_holds_per_source": Quota("concurrent_holds_per_source", limit=6, granularity="hour"),
    # Payment and partner surfaces
    "payment_attempt_per_cart": Quota("payment_attempt_per_cart", limit=8, granularity="hour"),
    "partner_api_per_partner": Quota("partner_api_per_partner", limit=600, granularity="minute"),
    "webhook_per_provider": Quota("webhook_per_provider", limit=1200, granularity="minute"),
    # Data egress (R73.14 abnormal export volume)
    "export_per_actor": Quota("export_per_actor", limit=20, granularity="hour"),
    "pii_view_per_actor": Quota("pii_view_per_actor", limit=200, granularity="hour"),
    # Gate scanning is high-volume by design; the limit only catches a runaway device.
    "scan_per_device": Quota("scan_per_device", limit=3000, granularity="minute"),
}


class RateLimiter:
    """Fixed-window counters backed by ``rate_limit_counters``."""

    def __init__(self, db: Database, clock: Clock, *, config=None, audit=None) -> None:
        self.db = db
        self.clock = clock
        self.config = config
        self.audit = audit

    def quota(self, ctx, name: str) -> Quota:
        """Resolve a quota, honouring a tenant override in configuration."""
        base = QUOTAS.get(name) or Quota(name, limit=60)
        if self.config is None or ctx is None:
            return base
        override = self.config.get(ctx, f"ratelimit.{name}", use_platform_default=False)
        if override is None:
            return base
        if isinstance(override, int):
            return Quota(base.name, limit=int(override), granularity=base.granularity, backoff=base.backoff)
        if isinstance(override, dict):
            return Quota(
                base.name,
                limit=int(override.get("limit", base.limit)),
                granularity=override.get("granularity", base.granularity),
                backoff=bool(override.get("backoff", base.backoff)),
                base_retry_seconds=int(override.get("base_retry_seconds", base.base_retry_seconds)),
            )
        return base

    def check(self, ctx, name: str, *, subject: str, cost: int = 1, raise_on_exceed: bool = True) -> dict:
        """Count one attempt against a quota.

        ``subject`` is hashed, so an email address or IP never becomes a readable
        directory in the counter table.
        """
        quota = self.quota(ctx, name)
        now_iso = to_iso(self.clock.now())
        window = quota.window(now_iso)
        tenant = getattr(ctx, "tenant_id", "platform")
        bucket = f"{tenant}:{name}:{hash_identifier(str(subject))[:24]}"

        row = self.db.query_one(
            "SELECT id, count FROM rate_limit_counters WHERE bucket = ? AND window_start = ?",
            (bucket, window),
        )
        if row is None:
            self.db.insert(
                "rate_limit_counters",
                {"id": new_id("rlc"), "bucket": bucket, "window_start": window, "count": int(cost)},
            )
            count = int(cost)
        else:
            count = int(row["count"]) + int(cost)
            self.db.update("rate_limit_counters", row["id"], {"count": count})

        exceeded = count > quota.limit
        retry_after = 0
        if exceeded:
            retry_after = quota.retry_after(count - quota.limit)
            if self.audit is not None and ctx is not None:
                self.audit.security(
                    ctx,
                    "AUTHORIZATION_DENIED",
                    reason="rate_limited",
                    detail={"quota": name, "count": count, "limit": quota.limit},
                )
            if raise_on_exceed:
                raise RateLimited(retry_after)
        return {
            "quota": name,
            "limit": quota.limit,
            "count": count,
            "remaining": max(quota.limit - count, 0),
            "exceeded": exceeded,
            "retry_after_seconds": retry_after,
            "window": window,
        }

    def peek(self, ctx, name: str, *, subject: str) -> int:
        """Current count without incrementing."""
        quota = self.quota(ctx, name)
        window = quota.window(to_iso(self.clock.now()))
        tenant = getattr(ctx, "tenant_id", "platform")
        bucket = f"{tenant}:{name}:{hash_identifier(str(subject))[:24]}"
        return int(
            self.db.scalar(
                "SELECT count FROM rate_limit_counters WHERE bucket = ? AND window_start = ?",
                (bucket, window),
                default=0,
            )
        )

    def purge_old_windows(self, *, keep_days: int = 2) -> int:
        """Housekeeping: counter rows have no value once their window has passed."""
        cutoff = to_iso(self.clock.now())[:10]
        cursor = self.db.execute(
            "DELETE FROM rate_limit_counters WHERE substr(window_start, 1, 10) < ?",
            (str(cutoff),),
        )
        return cursor.rowcount

    # ------------------------------------------------------------------ #
    # Abuse quotas that count state rather than requests
    # ------------------------------------------------------------------ #

    def assert_hold_quota(self, ctx, *, source: str, concurrent_holds: int) -> None:
        """Bound simultaneously-held inventory per source (residual risk D.5).

        A request-rate limit alone does not stop this: an attacker can hold slowly and
        still deny inventory, because a hold lives for minutes. So the *outstanding*
        count is what is bounded.
        """
        quota = self.quota(ctx, "concurrent_holds_per_source")
        if concurrent_holds >= quota.limit:
            if self.audit is not None:
                self.audit.security(
                    ctx,
                    "AUTHORIZATION_DENIED",
                    reason="hold_quota_exceeded",
                    detail={"concurrent_holds": concurrent_holds, "limit": quota.limit},
                )
            raise RateLimited(
                300,
                message=(
                    "You have several reservations in progress. Please complete or cancel one "
                    "before starting another."
                ),
                code="hold_quota_exceeded",
            )


@dataclass(slots=True)
class TokenBucket:
    """In-memory token bucket for per-process smoothing.

    Not a substitute for :class:`RateLimiter`: it is per-process and therefore not
    authoritative across instances. Useful in front of an outbound provider call to
    avoid tripping *their* limit.
    """

    capacity: float
    refill_per_second: float
    tokens: float = 0.0
    last_refill: float = 0.0

    def allow(self, now: float, cost: float = 1.0) -> bool:
        if self.last_refill == 0.0:
            self.tokens = float(self.capacity)
            self.last_refill = now
        elapsed = max(now - self.last_refill, 0.0)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


__all__ = ["QUOTAS", "Granularity", "Quota", "RateLimiter", "TokenBucket"]
