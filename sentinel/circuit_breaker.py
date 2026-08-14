"""Per-process circuit breaker for the Redis boundary (Phase 9).

CLOSED calls reach Redis, failures count toward the OPEN threshold, and any
genuine Redis success resets the count. OPEN short-circuits without touching
Redis for the quarantine period, then lazily enters HALF_OPEN where each
arriving call is a probe: success closes the breaker, failure opens it with a
fresh quarantine. The breaker is per-process by design (ADR-007) and uses the
local monotonic clock — the deliberate exception to "Redis TIME() is the one
clock", since it operates precisely when Redis is unreachable.
"""

import enum
import time
from collections.abc import Callable

FAILURE_THRESHOLD = 5
OPEN_TIMEOUT_SECONDS = 30.0


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        open_timeout_seconds: float = OPEN_TIMEOUT_SECONDS,
    ) -> None:
        self._now = now
        self._failure_threshold = failure_threshold
        self._open_timeout_seconds = open_timeout_seconds
        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def is_open(self) -> bool:
        if self._state is BreakerState.OPEN:
            assert self._opened_at is not None
            if self._now() - self._opened_at >= self._open_timeout_seconds:
                self._state = BreakerState.HALF_OPEN
        return self._state is BreakerState.OPEN

    def record_failure(self) -> None:
        if self._state is BreakerState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = self._now()
        elif self._state is BreakerState.HALF_OPEN:
            self._state = BreakerState.OPEN
            self._opened_at = self._now()

    def record_success(self) -> None:
        if self._state is BreakerState.CLOSED:
            self._failure_count = 0
        elif self._state is BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._opened_at = None
