"""Rate limiting orchestration for Sentinel (Phases 6, 8, 9).

The RateLimiter maps a resolved Policy and a bucket key to a Decision by
delegating to the matching algorithm strategy, which executes the Phase 4 Lua
scripts through the ScriptLoader. Happy-path decisions carry ALLOWED or
RATE_LIMITED. The circuit breaker is consulted before Redis is called: an
OPEN breaker short-circuits with CIRCUIT_OPEN and never touches Redis (Phase
9). Redis failures are classified (Phase 8): fail-closed policies deny with
FAIL_CLOSED, fail-open policies delegate to the emergency limiter, which
either allows with the underlying failure reason or denies with
EMERGENCY_LOCAL_LIMIT. Only genuine Redis successes reset the breaker.
Programming errors (KeyError, RuntimeError, ...) are not caught and
propagate unchanged.
"""

import hashlib
import time
from typing import Protocol

from redis.exceptions import RedisError

from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO
from sentinel.circuit_breaker import CircuitBreaker
from sentinel.emergency import EmergencyLimiter
from sentinel.errors import classify_redis_error
from sentinel.lua import SLIDING_WINDOW_SCRIPT, TOKEN_BUCKET_SCRIPT
from sentinel.models import (
    AlgorithmType,
    Decision,
    DecisionReason,
    FailMode,
    Policy,
)
from sentinel.redis import ScriptLoader


def build_bucket_key(
    tenant_id: str,
    endpoint_id: str,
    policy_version: int,
) -> str:
    tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return f"sentinel:v1:{tenant_hash}:{endpoint_id}:{policy_version}"


class RateLimitStrategy(Protocol):
    async def evaluate(
        self,
        policy: Policy,
        key: str,
    ) -> Decision: ...


class TokenBucketStrategy:
    def __init__(self, loader: ScriptLoader) -> None:
        self._loader = loader

    async def evaluate(self, policy: Policy, key: str) -> Decision:
        assert policy.algorithm is AlgorithmType.TOKEN_BUCKET
        assert policy.capacity_micro is not None
        assert policy.refill_rate_micro_per_sec is not None
        result = await self._loader.execute(
            TOKEN_BUCKET_SCRIPT,
            keys=[key],
            args=[str(policy.capacity_micro), str(policy.refill_rate_micro_per_sec)],
        )
        assert isinstance(result, list)
        allowed, tokens_after, _, _ = result
        retry_after_seconds: float | None = None
        if not allowed and policy.refill_rate_micro_per_sec > 0:
            retry_after_seconds = (
                TOKENS_PER_TOKEN_MICRO - tokens_after
            ) / policy.refill_rate_micro_per_sec
        return Decision(
            allowed=bool(allowed),
            reason=DecisionReason.ALLOWED if allowed else DecisionReason.RATE_LIMITED,
            remaining_micro=tokens_after,
            # Python wall clock → observability timestamp
            # Redis TIME()     → rate-limit algorithm clock
            decision_time_micro=time.time_ns() // 1_000,
            retry_after_seconds=retry_after_seconds,
        )


class SlidingWindowStrategy:
    def __init__(self, loader: ScriptLoader) -> None:
        self._loader = loader

    async def evaluate(self, policy: Policy, key: str) -> Decision:
        assert policy.algorithm is AlgorithmType.SLIDING_WINDOW
        assert policy.limit is not None
        result = await self._loader.execute(
            SLIDING_WINDOW_SCRIPT,
            keys=[key],
            args=[str(policy.limit), str(policy.window_size_micro)],
        )
        assert isinstance(result, list)
        allowed, current_after, _, _, _ = result
        # Sliding Window returns no Retry-After: the Lua result does not expose
        # enough timing information (e.g. remaining window time) for a precise
        # Retry-After value.
        return Decision(
            allowed=bool(allowed),
            reason=DecisionReason.ALLOWED if allowed else DecisionReason.RATE_LIMITED,
            remaining_micro=max(0, policy.limit - current_after) * TOKENS_PER_TOKEN_MICRO,
            # Python wall clock → observability timestamp
            # Redis TIME()     → rate-limit algorithm clock
            decision_time_micro=time.time_ns() // 1_000,
            retry_after_seconds=None,
        )


def _denied(reason: DecisionReason) -> Decision:
    return Decision(
        allowed=False,
        reason=reason,
        remaining_micro=0,
        # Python wall clock → observability timestamp
        decision_time_micro=time.time_ns() // 1_000,
    )


class RateLimiter:
    def __init__(
        self,
        loader: ScriptLoader,
        *,
        breaker: CircuitBreaker,
        emergency: EmergencyLimiter,
    ) -> None:
        self._strategies: dict[AlgorithmType, RateLimitStrategy] = {
            AlgorithmType.TOKEN_BUCKET: TokenBucketStrategy(loader),
            AlgorithmType.SLIDING_WINDOW: SlidingWindowStrategy(loader),
        }
        self._breaker = breaker
        self._emergency = emergency

    async def evaluate(self, policy: Policy, key: str) -> Decision:
        if self._breaker.is_open():
            return await self._on_circuit_open(policy)
        try:
            decision = await self._strategies[policy.algorithm].evaluate(policy, key)
        except RedisError as exc:
            self._breaker.record_failure()
            reason = classify_redis_error(exc)
            if policy.fail_mode is FailMode.FAIL_CLOSED:
                return _denied(DecisionReason.FAIL_CLOSED)
            return await self._fail_open(policy, reason)
        self._breaker.record_success()
        return decision

    async def _on_circuit_open(self, policy: Policy) -> Decision:
        if policy.fail_mode is FailMode.FAIL_CLOSED:
            return _denied(DecisionReason.CIRCUIT_OPEN)
        return await self._fail_open(policy, DecisionReason.CIRCUIT_OPEN)

    async def _fail_open(self, policy: Policy, cause: DecisionReason) -> Decision:
        outcome = await self._emergency.evaluate(
            policy.endpoint_id, policy.fallback_rate_per_process_micro
        )
        if not outcome.allowed:
            return Decision(
                allowed=False,
                reason=DecisionReason.EMERGENCY_LOCAL_LIMIT,
                remaining_micro=outcome.remaining_micro,
                decision_time_micro=time.time_ns() // 1_000,
                retry_after_seconds=outcome.retry_after_seconds,
            )
        return Decision(
            allowed=True,
            reason=cause,
            remaining_micro=outcome.remaining_micro,
            decision_time_micro=time.time_ns() // 1_000,
        )
