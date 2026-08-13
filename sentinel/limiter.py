"""Rate limiting orchestration for Sentinel (Phase 6).

The RateLimiter maps a resolved Policy and a bucket key to a Decision by
delegating to the matching algorithm strategy, which executes the Phase 4 Lua
scripts through the ScriptLoader. This phase is happy-path only: exceptions
from Redis propagate unchanged, and the only DecisionReason values produced
are ALLOWED and RATE_LIMITED.
"""

import hashlib
import time
from typing import Protocol

from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO
from sentinel.lua import SLIDING_WINDOW_SCRIPT, TOKEN_BUCKET_SCRIPT
from sentinel.models import AlgorithmType, Decision, DecisionReason, Policy
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
            decision_time_micro=time.time_ns() // 1_000,
            retry_after_seconds=None,
        )


class RateLimiter:
    def __init__(self, loader: ScriptLoader) -> None:
        self._strategies: dict[AlgorithmType, RateLimitStrategy] = {
            AlgorithmType.TOKEN_BUCKET: TokenBucketStrategy(loader),
            AlgorithmType.SLIDING_WINDOW: SlidingWindowStrategy(loader),
        }

    async def evaluate(self, policy: Policy, key: str) -> Decision:
        return await self._strategies[policy.algorithm].evaluate(policy, key)
