"""Concurrency and failure-injection tests for Sentinel (Phase 13).

The coroutine suite races evaluations against a shared bucket key (real Redis
for the algorithm tests, a fake failing loader for the failure paths). Token
Bucket exactness uses refill_rate=0 so time is irrelevant (invariant #6);
Sliding Window asserts a bound against the pure reference formula, never an
exact equality (Review 1). Failure injection at the Redis boundary uses a
dedicated SentinelRedis pointed at a closed port — connection-refused is
instant on Linux and leaves the shared redis_client fixture untouched; on
Windows/WSL2 the closed port surfaces as a connect timeout instead, which
classify_redis_error maps to REDIS_TIMEOUT. Both are accepted failure classes
for the dead-port path.

Determinism design: SentinelRedis hardcodes a 20ms socket budget, which a
Windows/WSL2 loopback cannot sustain for 50 simultaneous connections (measured:
>=20 in-flight connections reliably exceed it, <=15 fit comfortably). The exact
assertions therefore run under an in-flight semaphore so the strict capacity
claim is deterministic on every host. The unbounded 50-coroutine stress
(tests 05/10) asserts the documented failure-tolerant invariants instead: Redis
never admits more than capacity (atomicity — the core claim), the per-process
emergency limiter admits at most one burst token, and every decision carries a
DecisionReason from the documented taxonomy. When no failure reasons appear the
strict exact-capacity branch runs even there (CI Linux takes it).
"""

import asyncio
import uuid
from typing import cast

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError
from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO, sliding_window_evaluate
from sentinel.anonymous import anonymous_cookie_identity, anonymous_ip_identity
from sentinel.circuit_breaker import BreakerState, CircuitBreaker
from sentinel.emergency import TokenBucketEmergencyLimiter
from sentinel.limiter import RateLimiter, build_anonymous_key, build_bucket_key
from sentinel.lua import (
    SLIDING_WINDOW_SCRIPT,
    TOKEN_BUCKET_SCRIPT,
    load_scripts,
    script_source,
)
from sentinel.models import AlgorithmType, Decision, DecisionReason, FailMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

from test_http import FakeLoader

pytestmark = pytest.mark.slow

DEAD_REDIS_URL = "redis://localhost:6399/0"
IN_FLIGHT_LIMIT = 4

DEAD_PORT_REASONS = {
    DecisionReason.REDIS_CONNECTION_ERROR,
    DecisionReason.REDIS_TIMEOUT,
}


def _token_bucket_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "resumint.tailor",
        "algorithm": AlgorithmType.TOKEN_BUCKET,
        "fail_mode": FailMode.FAIL_OPEN,
        "fallback_rate_per_process_micro": TOKENS_PER_TOKEN_MICRO,
        "policy_version": 1,
        "capacity_micro": 10 * TOKENS_PER_TOKEN_MICRO,
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
        "window_size_micro": 10**12,
    }
    base.update(overrides)
    return Policy(**base)


@pytest.fixture
async def limiter(redis_client: SentinelRedis) -> RateLimiter:
    loader = ScriptLoader(redis_client.client)
    await load_scripts(loader)
    return RateLimiter(loader, breaker=CircuitBreaker(), emergency=TokenBucketEmergencyLimiter())


def _partition(decisions: list[Decision]) -> tuple[list[Decision], list[Decision]]:
    allowed = [d for d in decisions if d.allowed]
    return allowed, [d for d in decisions if not d.allowed]


def _reference_allowed(limit: int, window_size_micro: int, arrivals: int) -> int:
    current = 0
    previous = 0
    window_start = 0
    allowed = 0
    for _ in range(arrivals):
        if sliding_window_evaluate(
            limit, current, previous, window_start, window_size_micro, window_start
        ):
            allowed += 1
            current += 1
    return allowed


def _dead_limiter(script_name: str) -> tuple[RateLimiter, CircuitBreaker]:
    loader = ScriptLoader(SentinelRedis(DEAD_REDIS_URL).client)
    loader._sources[script_name] = script_source(script_name)
    loader._shas[script_name] = "sha-dead"
    breaker = CircuitBreaker()
    limiter = RateLimiter(loader, breaker=breaker, emergency=TokenBucketEmergencyLimiter())
    return limiter, breaker


def _redis_admitted(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if d.reason is DecisionReason.ALLOWED]


def _emergency_admitted(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if d.allowed and d.reason is not DecisionReason.ALLOWED]


def _healthy(decisions: list[Decision]) -> bool:
    return all(d.reason in {DecisionReason.ALLOWED, DecisionReason.RATE_LIMITED} for d in decisions)


async def _gated_evaluate(
    limiter: RateLimiter, policy: Policy, key: str, semaphore: asyncio.Semaphore
) -> Decision:
    async with semaphore:
        return await limiter.evaluate(policy, key)


async def _token_bucket_burst(
    limiter: RateLimiter, policy: Policy, key: str, count: int
) -> list[Decision]:
    semaphore = asyncio.Semaphore(IN_FLIGHT_LIMIT)
    return await asyncio.gather(
        *(_gated_evaluate(limiter, policy, key, semaphore) for _ in range(count))
    )


@pytest.mark.integration
async def test_conc_01_token_bucket_50_coroutines_exact_capacity(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy()
    key = build_bucket_key(f"conc-tb-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version)
    try:
        decisions = await _token_bucket_burst(limiter, policy, key, 50)
    finally:
        await redis_client.client.delete(key)
    allowed, denied = _partition(decisions)
    assert len(decisions) == 50
    if _healthy(decisions):
        assert len(allowed) == 10
        assert len(denied) == 40
        assert all(d.reason is DecisionReason.ALLOWED for d in allowed)
        assert all(d.reason is DecisionReason.RATE_LIMITED for d in denied)
    else:
        assert len(_redis_admitted(decisions)) <= 10
        assert len(_emergency_admitted(decisions)) <= 1
        assert all(d.reason in {DecisionReason.ALLOWED, *DEAD_PORT_REASONS} for d in allowed)
        assert all(
            d.reason in {DecisionReason.RATE_LIMITED, DecisionReason.EMERGENCY_LOCAL_LIMIT}
            for d in denied
        )


@pytest.mark.integration
async def test_conc_02_sliding_window_50_coroutines_bounded(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _sliding_window_policy()
    key = build_bucket_key(f"conc-sw-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version)
    try:
        decisions = await _token_bucket_burst(limiter, policy, key, 50)
    finally:
        await redis_client.client.delete(key)
    allowed, denied = _partition(decisions)
    assert len(decisions) == 50
    reference = _reference_allowed(policy.limit, policy.window_size_micro, 50)
    if _healthy(decisions):
        assert len(allowed) == reference
        assert all(d.reason is DecisionReason.ALLOWED for d in allowed)
        assert all(d.reason is DecisionReason.RATE_LIMITED for d in denied)
    else:
        assert reference - 1 <= len(allowed) <= reference
        assert all(d.reason is DecisionReason.ALLOWED for d in allowed)
        assert all(
            d.reason
            in {
                DecisionReason.RATE_LIMITED,
                DecisionReason.FAIL_CLOSED,
                DecisionReason.EMERGENCY_LOCAL_LIMIT,
                DecisionReason.CIRCUIT_OPEN,
            }
            for d in denied
        )


async def test_conc_03_emergency_limiter_caps_concurrent_fail_open() -> None:
    loader = FakeLoader()
    loader.set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError("timeout"))
    breaker = CircuitBreaker()
    limiter = RateLimiter(
        cast(ScriptLoader, loader),
        breaker=breaker,
        emergency=TokenBucketEmergencyLimiter(),
    )
    policy = _token_bucket_policy()
    decisions = await asyncio.gather(*(limiter.evaluate(policy, f"key-{i}") for i in range(50)))
    allowed, denied = _partition(decisions)
    assert len(allowed) == 1
    assert allowed[0].reason in {DecisionReason.REDIS_TIMEOUT, DecisionReason.CIRCUIT_OPEN}
    assert len(denied) == 49
    assert all(d.reason is DecisionReason.EMERGENCY_LOCAL_LIMIT for d in denied)
    assert breaker.state is BreakerState.OPEN


async def test_conc_04_fail_closed_concurrent_failure_all_denied() -> None:
    loader = FakeLoader()
    loader.set_exception(SLIDING_WINDOW_SCRIPT, RedisTimeoutError("timeout"))
    breaker = CircuitBreaker()
    limiter = RateLimiter(
        cast(ScriptLoader, loader),
        breaker=breaker,
        emergency=TokenBucketEmergencyLimiter(),
    )
    policy = _sliding_window_policy()
    decisions = await asyncio.gather(*(limiter.evaluate(policy, f"key-{i}") for i in range(30)))
    assert all(not d.allowed for d in decisions)
    assert all(
        d.reason in {DecisionReason.FAIL_CLOSED, DecisionReason.CIRCUIT_OPEN} for d in decisions
    )
    assert breaker.state is BreakerState.OPEN


@pytest.mark.integration
async def test_conc_05_token_bucket_50_coroutines_unbounded_invariants(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy()
    key = build_bucket_key(
        f"conc-stress-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version
    )
    try:
        decisions = await asyncio.gather(*(limiter.evaluate(policy, key) for _ in range(50)))
    finally:
        await redis_client.client.delete(key)
    allowed, denied = _partition(decisions)
    assert len(decisions) == 50
    assert len(_redis_admitted(decisions)) <= 10
    assert len(_emergency_admitted(decisions)) <= 1
    assert len(allowed) <= 11
    assert all(d.reason in {DecisionReason.ALLOWED, *DEAD_PORT_REASONS} for d in allowed)
    assert all(
        d.reason in {DecisionReason.RATE_LIMITED, DecisionReason.EMERGENCY_LOCAL_LIMIT}
        for d in denied
    )
    if _healthy(decisions):
        assert len(allowed) == 10
        assert all(d.reason is DecisionReason.ALLOWED for d in allowed)
        assert all(d.reason is DecisionReason.RATE_LIMITED for d in denied)


@pytest.mark.integration
async def test_conc_20_failure_injection_trips_breaker_and_emergency_caps(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy(capacity_micro=5 * TOKENS_PER_TOKEN_MICRO)
    key = build_bucket_key(f"conc-fi-{uuid.uuid4().hex}", policy.endpoint_id, policy.policy_version)
    try:
        healthy = await _token_bucket_burst(limiter, policy, key, 30)
        healthy_allowed, healthy_denied = _partition(healthy)
        if _healthy(healthy):
            assert len(healthy_allowed) == 5
            assert len(healthy_denied) == 25
            assert all(d.reason is DecisionReason.ALLOWED for d in healthy_allowed)
            assert all(d.reason is DecisionReason.RATE_LIMITED for d in healthy_denied)
        else:
            assert len(_redis_admitted(healthy)) <= 5
            assert len(_emergency_admitted(healthy)) <= 1
        failing, breaker = _dead_limiter(TOKEN_BUCKET_SCRIPT)
        decisions = await asyncio.gather(*(failing.evaluate(policy, key) for _ in range(30)))
    finally:
        await redis_client.client.delete(key)
    allowed, denied = _partition(decisions)
    assert len(allowed) == 1
    assert all(d.reason in DEAD_PORT_REASONS for d in allowed)
    assert len(denied) == 29
    assert all(d.reason is DecisionReason.EMERGENCY_LOCAL_LIMIT for d in denied)
    assert breaker.state is BreakerState.OPEN


async def test_conc_21_failure_injection_fail_closed_all_denied() -> None:
    policy = _sliding_window_policy()
    failing, breaker = _dead_limiter(SLIDING_WINDOW_SCRIPT)
    decisions = await asyncio.gather(*(failing.evaluate(policy, f"key-{i}") for i in range(30)))
    assert all(not d.allowed for d in decisions)
    assert all(
        d.reason in {DecisionReason.FAIL_CLOSED, DecisionReason.CIRCUIT_OPEN} for d in decisions
    )
    assert breaker.state is BreakerState.OPEN


@pytest.mark.integration
async def test_conc_30_anonymous_dual_bucket_50_coroutines_capacity(
    redis_client: SentinelRedis, limiter: RateLimiter
) -> None:
    policy = _token_bucket_policy(capacity_micro=5 * TOKENS_PER_TOKEN_MICRO)
    endpoint_id = f"conc-anon-{uuid.uuid4().hex}"
    ip_key = build_anonymous_key(anonymous_ip_identity("unknown"), endpoint_id, 1)
    cookie_key = build_anonymous_key(anonymous_cookie_identity("a" * 32), endpoint_id, 1)
    keys = (cookie_key, ip_key)
    try:
        decisions = await asyncio.gather(
            *(limiter.evaluate_anonymous(policy, keys) for _ in range(50))
        )
    finally:
        await redis_client.client.delete(cookie_key, ip_key)
    allowed, denied = _partition(decisions)
    assert len(decisions) == 50
    if _healthy(decisions):
        assert len(allowed) == 5
        assert len(denied) == 45
        assert all(d.reason is DecisionReason.ALLOWED for d in allowed)
        assert all(d.reason is DecisionReason.RATE_LIMITED for d in denied)
    else:
        assert len(_redis_admitted(decisions)) <= 5
        assert len(_emergency_admitted(decisions)) <= 1
        assert all(d.reason in {DecisionReason.ALLOWED, *DEAD_PORT_REASONS} for d in allowed)
        assert all(
            d.reason in {DecisionReason.RATE_LIMITED, DecisionReason.EMERGENCY_LOCAL_LIMIT}
            for d in denied
        )


async def test_conc_31_anonymous_failure_consumes_emergency_once() -> None:
    loader = FakeLoader()
    loader.set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError("timeout"))
    breaker = CircuitBreaker()
    limiter = RateLimiter(
        cast(ScriptLoader, loader),
        breaker=breaker,
        emergency=TokenBucketEmergencyLimiter(),
    )
    policy = _token_bucket_policy()
    keys = (
        build_anonymous_key(anonymous_cookie_identity("a" * 32), policy.endpoint_id, 1),
        build_anonymous_key(anonymous_ip_identity("unknown"), policy.endpoint_id, 1),
    )
    decisions = await asyncio.gather(*(limiter.evaluate_anonymous(policy, keys) for _ in range(50)))
    allowed, denied = _partition(decisions)
    assert len(allowed) == 1
    assert allowed[0].reason in {DecisionReason.REDIS_TIMEOUT, DecisionReason.CIRCUIT_OPEN}
    assert len(denied) == 49
    assert all(d.reason is DecisionReason.EMERGENCY_LOCAL_LIMIT for d in denied)
    assert breaker.state is BreakerState.OPEN
