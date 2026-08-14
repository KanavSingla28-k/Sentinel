"""Emergency local rate limiting for fail-open paths (Phase 8, completed in Phase 10).

When Redis is unavailable, a fail-open policy falls back to this per-process
limiter so that fail-open never means unlimited traffic. It is a plain
in-memory token bucket reusing ``token_bucket_evaluate`` with both capacity
and refill rate equal to ``fallback_rate_per_process_micro``: a burst of one
second's worth of the fallback rate, then sustained at the fallback rate.
Buckets are keyed by ``endpoint_id`` only — tenant fairness is a V2 topic.
The limiter deliberately uses the local process clock (monotonic) as the
documented exception to the "Redis TIME() is the one clock" invariant,
because it operates precisely when Redis is unreachable.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO, token_bucket_evaluate


@dataclass(frozen=True)
class EmergencyOutcome:
    allowed: bool
    remaining_micro: int
    retry_after_seconds: float | None = None


class EmergencyLimiter(Protocol):
    async def evaluate(
        self,
        endpoint_id: str,
        fallback_rate_micro_per_sec: int,
    ) -> EmergencyOutcome: ...


class TokenBucketEmergencyLimiter:
    def __init__(
        self,
        now_micro: Callable[[], int] | None = None,
    ) -> None:
        self._now_micro = now_micro or (lambda: time.monotonic_ns() // 1_000)
        self._buckets: dict[str, tuple[int, int]] = {}

    async def evaluate(
        self,
        endpoint_id: str,
        fallback_rate_micro_per_sec: int,
    ) -> EmergencyOutcome:
        now = self._now_micro()
        tokens_micro, last_refill_micro = self._buckets.get(
            endpoint_id, (fallback_rate_micro_per_sec, now)
        )
        allowed, tokens_after, last_refill_after = token_bucket_evaluate(
            capacity_micro=fallback_rate_micro_per_sec,
            refill_rate_micro_per_sec=fallback_rate_micro_per_sec,
            tokens_micro=tokens_micro,
            last_refill_micro=last_refill_micro,
            now_micro=now,
        )
        self._buckets[endpoint_id] = (tokens_after, last_refill_after)
        retry_after_seconds: float | None = None
        if not allowed:
            retry_after_seconds = (
                TOKENS_PER_TOKEN_MICRO - tokens_after
            ) / fallback_rate_micro_per_sec
        return EmergencyOutcome(
            allowed=allowed,
            remaining_micro=tokens_after,
            retry_after_seconds=retry_after_seconds,
        )
