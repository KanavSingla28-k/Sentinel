"""Rate limiter integration tests against real Redis (Phase 6)."""

import uuid
from typing import cast

import pytest
from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO
from sentinel.limiter import RateLimiter, build_bucket_key
from sentinel.lua import load_scripts
from sentinel.models import AlgorithmType, DecisionReason, FailMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

pytestmark = pytest.mark.integration


async def _get_state(client: SentinelRedis, key: str) -> str:
    state = await client.client.get(key)
    assert state is not None
    return cast(str, state)


def _token_bucket_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "resumint.tailor",
        "algorithm": AlgorithmType.TOKEN_BUCKET,
        "fail_mode": FailMode.FAIL_OPEN,
        "fallback_rate_per_process_micro": 2_000,
        "policy_version": 1,
        "capacity_micro": 2_000_000,
        "refill_rate_micro_per_sec": 0,
    }
    base.update(overrides)
    return Policy(**base)


def _sliding_window_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "pdftalk.ingest",
        "algorithm": AlgorithmType.SLIDING_WINDOW,
        "fail_mode": FailMode.FAIL_CLOSED,
        "fallback_rate_per_process_micro": 5_000,
        "policy_version": 1,
        "limit": 5,
        "window_size_micro": 1_000_000,
    }
    base.update(overrides)
    return Policy(**base)


async def _now_micro(client: SentinelRedis) -> int:
    seconds, microseconds = await client.client.time()
    return seconds * 1_000_000 + microseconds


@pytest.fixture
async def limiter(redis_client: SentinelRedis) -> RateLimiter:
    loader = ScriptLoader(redis_client.client)
    await load_scripts(loader)
    return RateLimiter(loader)


async def test_token_bucket_fresh_tenant_allows(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy()
    key = build_bucket_key(
        f"tb-fresh-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version
    )
    decision = await limiter.evaluate(policy, key)
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert decision.remaining_micro == TOKENS_PER_TOKEN_MICRO
    state = await _get_state(redis_client, key)
    tokens, _ = state.split(":")
    assert int(tokens) == TOKENS_PER_TOKEN_MICRO


async def test_token_bucket_drain_then_deny(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy()
    key = build_bucket_key(
        f"tb-drain-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version
    )
    first = await limiter.evaluate(policy, key)
    second = await limiter.evaluate(policy, key)
    third = await limiter.evaluate(policy, key)
    assert first.allowed is True
    assert first.remaining_micro == TOKENS_PER_TOKEN_MICRO
    assert second.allowed is True
    assert second.remaining_micro == 0
    assert third.allowed is False
    assert third.reason is DecisionReason.RATE_LIMITED
    assert third.remaining_micro == 0


async def test_token_bucket_zero_rate_allows_exact_capacity_then_denies(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy(capacity_micro=TOKENS_PER_TOKEN_MICRO)
    key = build_bucket_key(
        f"tb-exact-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version
    )
    first = await limiter.evaluate(policy, key)
    second = await limiter.evaluate(policy, key)
    third = await limiter.evaluate(policy, key)
    assert first.allowed is True
    assert second.allowed is False
    assert third.allowed is False
    assert second.retry_after_seconds is None
    state = await _get_state(redis_client, key)
    tokens, _ = state.split(":")
    assert tokens == "0"


async def test_token_bucket_denied_retry_after_matches_oracle(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy(refill_rate_micro_per_sec=TOKENS_PER_TOKEN_MICRO)
    key = build_bucket_key(
        f"tb-retry-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version
    )
    now = await _now_micro(redis_client)
    await redis_client.client.set(key, f"500000:{now + TOKENS_PER_TOKEN_MICRO}")
    decision = await limiter.evaluate(policy, key)
    assert decision.allowed is False
    assert decision.reason is DecisionReason.RATE_LIMITED
    assert decision.retry_after_seconds == 0.5
    state = await _get_state(redis_client, key)
    assert state == f"500000:{now + TOKENS_PER_TOKEN_MICRO}"


async def test_sliding_window_fresh_tenant_allows(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _sliding_window_policy()
    key = build_bucket_key(
        f"sw-fresh-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version
    )
    decision = await limiter.evaluate(policy, key)
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert decision.remaining_micro == 4 * TOKENS_PER_TOKEN_MICRO
    state = await _get_state(redis_client, key)
    current, previous, _ = state.split(":")
    assert current == "1"
    assert previous == "0"


async def test_sliding_window_deny_persists_state(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _sliding_window_policy()
    key = build_bucket_key(f"sw-deny-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version)
    now = await _now_micro(redis_client)
    seed = f"5:0:{now}"
    await redis_client.client.set(key, seed)
    decision = await limiter.evaluate(policy, key)
    assert decision.allowed is False
    assert decision.reason is DecisionReason.RATE_LIMITED
    assert decision.remaining_micro == 0
    assert decision.retry_after_seconds is None
    state = await _get_state(redis_client, key)
    assert state == seed


async def test_sliding_window_rollover(redis_client: SentinelRedis, limiter: RateLimiter) -> None:
    policy = _sliding_window_policy()
    key = build_bucket_key(f"sw-roll-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version)
    now = await _now_micro(redis_client)
    window_start = now - 1_500_000
    await redis_client.client.set(key, f"4:9:{window_start}")
    decision = await limiter.evaluate(policy, key)
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert decision.remaining_micro == 4 * TOKENS_PER_TOKEN_MICRO
    state = await _get_state(redis_client, key)
    assert state == f"1:4:{window_start}"


async def test_tenant_isolation_same_endpoint(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy(capacity_micro=TOKENS_PER_TOKEN_MICRO)
    tenant_a = f"tenant-a-{uuid.uuid4().hex}"
    tenant_b = f"tenant-b-{uuid.uuid4().hex}"
    key_a = build_bucket_key(tenant_a, policy.endpoint_id, policy.policy_version)
    key_b = build_bucket_key(tenant_b, policy.endpoint_id, policy.policy_version)
    assert key_a != key_b
    first_a = await limiter.evaluate(policy, key_a)
    second_a = await limiter.evaluate(policy, key_a)
    first_b = await limiter.evaluate(policy, key_b)
    assert first_a.allowed is True
    assert second_a.allowed is False
    assert first_b.allowed is True
    await _get_state(redis_client, key_a)
    await _get_state(redis_client, key_b)
