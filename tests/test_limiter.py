"""Rate limiter unit tests: key construction, Decision mapping, and failure
handling (Phases 6 and 8)."""

import time
from typing import cast

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO
from sentinel.anonymous import (
    anonymous_cookie_identity,
    anonymous_ip_identity,
    hash_identity,
)
from sentinel.circuit_breaker import (
    FAILURE_THRESHOLD,
    OPEN_TIMEOUT_SECONDS,
    BreakerState,
    CircuitBreaker,
)
from sentinel.emergency import (
    EmergencyLimiter,
    TokenBucketEmergencyLimiter,
)
from sentinel.errors import ScriptMissingError
from sentinel.limiter import (
    RateLimiter,
    SlidingWindowStrategy,
    TokenBucketStrategy,
    build_anonymous_key,
    build_bucket_key,
)
from sentinel.models import AlgorithmType, DecisionReason, FailMode, Policy
from sentinel.redis import ScriptLoader


class FakeLoader:
    def __init__(self, results: dict[str, int | list[int] | None]) -> None:
        self._results = results
        self._exceptions: dict[str, Exception | None] = {}
        self.calls: list[tuple[str, list[str], list[str]]] = []

    def set_exception(self, name: str, exc: Exception | None) -> None:
        self._exceptions[name] = exc

    async def execute(self, name: str, keys: list[str], args: list[str]) -> int | list[int] | None:
        self.calls.append((name, keys, args))
        exc = self._exceptions.get(name)
        if exc is not None:
            raise exc
        return self._results.get(name)


def _token_bucket_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "resumint.tailor",
        "algorithm": AlgorithmType.TOKEN_BUCKET,
        "fail_mode": FailMode.FAIL_OPEN,
        "fallback_rate_per_process_micro": 2_000,
        "policy_version": 1,
        "capacity_micro": 2_000_000,
        "refill_rate_micro_per_sec": 1_000_000,
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
        "window_size_micro": 60_000_000,
    }
    base.update(overrides)
    return Policy(**base)


def test_build_bucket_key_exact_format() -> None:
    key = build_bucket_key("tenant-a", "pdftalk.ingest", 1)
    prefix, version, tenant_hash, endpoint_id, policy_version = key.split(":")
    assert prefix == "sentinel"
    assert version == "v1"
    assert tenant_hash == "80a707af7dc77ee1228f9127180f3964835e5beb4c4ab0d812f0fe7593579b3a"
    assert endpoint_id == "pdftalk.ingest"
    assert policy_version == "1"
    assert len(tenant_hash) == 64


def test_build_bucket_key_is_deterministic() -> None:
    first = build_bucket_key("tenant-a", "pdftalk.ingest", 1)
    second = build_bucket_key("tenant-a", "pdftalk.ingest", 1)
    assert first == second


def test_build_bucket_key_different_tenants_differ() -> None:
    first = build_bucket_key("tenant-a", "pdftalk.ingest", 1)
    second = build_bucket_key("tenant-b", "pdftalk.ingest", 1)
    assert first != second


def test_build_bucket_key_includes_endpoint_id() -> None:
    first = build_bucket_key("tenant-a", "pdftalk.ingest", 1)
    second = build_bucket_key("tenant-a", "resumint.tailor", 1)
    assert first != second
    assert "pdftalk.ingest" in first
    assert "resumint.tailor" in second


def test_build_bucket_key_includes_policy_version() -> None:
    first = build_bucket_key("tenant-a", "pdftalk.ingest", 1)
    second = build_bucket_key("tenant-a", "pdftalk.ingest", 2)
    assert first != second
    assert first.endswith(":1")
    assert second.endswith(":2")


def test_build_anonymous_key_exact_format() -> None:
    identity = anonymous_ip_identity("203.0.113.9")
    key = build_anonymous_key(identity, "auth.login", 1)
    prefix, version, identity_hash, endpoint_id, policy_version = key.split(":")
    assert prefix == "sentinel"
    assert version == "v2"
    assert len(identity_hash) == 64
    assert identity_hash == hash_identity(identity)
    assert endpoint_id == "auth.login"
    assert policy_version == "1"
    assert "203.0.113.9" not in key


def test_build_anonymous_key_is_deterministic_and_scoped() -> None:
    cookie_identity = anonymous_cookie_identity("a" * 32)
    first = build_anonymous_key(cookie_identity, "auth.login", 1)
    second = build_anonymous_key(cookie_identity, "auth.login", 1)
    other_endpoint = build_anonymous_key(cookie_identity, "auth.signup", 1)
    other_version = build_anonymous_key(cookie_identity, "auth.login", 2)
    other_identity = build_anonymous_key(anonymous_cookie_identity("b" * 32), "auth.login", 1)
    assert first == second
    assert first != other_endpoint
    assert first != other_version
    assert first != other_identity


def test_build_anonymous_key_distinct_from_tenant_keys() -> None:
    anonymous = build_anonymous_key(anonymous_ip_identity("203.0.113.9"), "auth.login", 1)
    tenant = build_bucket_key("203.0.113.9", "auth.login", 1)
    assert anonymous != tenant


async def test_token_bucket_allowed_mapping() -> None:
    loader = FakeLoader({"token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60]})
    strategy = TokenBucketStrategy(cast(ScriptLoader, loader))
    decision = await strategy.evaluate(_token_bucket_policy(), "sentinel:v1:k")
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert decision.remaining_micro == 1_000_000
    assert decision.retry_after_seconds is None


async def test_token_bucket_denied_retry_after() -> None:
    loader = FakeLoader({"token_bucket": [0, 500_000, 1_700_000_000_000_000, -1]})
    strategy = TokenBucketStrategy(cast(ScriptLoader, loader))
    decision = await strategy.evaluate(_token_bucket_policy(), "sentinel:v1:k")
    assert decision.allowed is False
    assert decision.reason is DecisionReason.RATE_LIMITED
    assert decision.remaining_micro == 500_000
    assert decision.retry_after_seconds == 0.5


async def test_token_bucket_zero_refill_rate_no_retry_after() -> None:
    policy = _token_bucket_policy(refill_rate_micro_per_sec=0)
    loader = FakeLoader({"token_bucket": [0, 0, 1_700_000_000_000_000, -1]})
    strategy = TokenBucketStrategy(cast(ScriptLoader, loader))
    decision = await strategy.evaluate(policy, "sentinel:v1:k")
    assert decision.allowed is False
    assert decision.reason is DecisionReason.RATE_LIMITED
    assert decision.remaining_micro == 0
    assert decision.retry_after_seconds is None


async def test_token_bucket_passes_policy_values_and_key_to_script() -> None:
    policy = _token_bucket_policy(capacity_micro=3_000_000, refill_rate_micro_per_sec=500_000)
    loader = FakeLoader({"token_bucket": [1, 2_000_000, 1_700_000_000_000_000, 60]})
    strategy = TokenBucketStrategy(cast(ScriptLoader, loader))
    await strategy.evaluate(policy, "sentinel:v1:key")
    name, keys, args = loader.calls[0]
    assert name == "token_bucket"
    assert keys == ["sentinel:v1:key"]
    assert args == ["3000000", "500000"]


async def test_sliding_window_allowed_mapping() -> None:
    loader = FakeLoader({"sliding_window": [1, 1, 0, 1_700_000_000_000_000, 120]})
    strategy = SlidingWindowStrategy(cast(ScriptLoader, loader))
    decision = await strategy.evaluate(_sliding_window_policy(), "sentinel:v1:k")
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert decision.remaining_micro == 4_000_000
    assert decision.retry_after_seconds is None


async def test_sliding_window_denied_mapping() -> None:
    loader = FakeLoader({"sliding_window": [0, 5, 0, 1_700_000_000_000_000, -1]})
    strategy = SlidingWindowStrategy(cast(ScriptLoader, loader))
    decision = await strategy.evaluate(_sliding_window_policy(), "sentinel:v1:k")
    assert decision.allowed is False
    assert decision.reason is DecisionReason.RATE_LIMITED
    assert decision.remaining_micro == 0
    assert decision.retry_after_seconds is None


async def test_sliding_window_remaining_clamped_at_zero() -> None:
    loader = FakeLoader({"sliding_window": [0, 99, 0, 1_700_000_000_000_000, -1]})
    strategy = SlidingWindowStrategy(cast(ScriptLoader, loader))
    decision = await strategy.evaluate(_sliding_window_policy(), "sentinel:v1:k")
    assert decision.remaining_micro == 0


async def test_sliding_window_passes_policy_values_and_key_to_script() -> None:
    policy = _sliding_window_policy(limit=7, window_size_micro=30_000_000)
    loader = FakeLoader({"sliding_window": [1, 1, 0, 1_700_000_000_000_000, 60]})
    strategy = SlidingWindowStrategy(cast(ScriptLoader, loader))
    await strategy.evaluate(policy, "sentinel:v1:key")
    name, keys, args = loader.calls[0]
    assert name == "sliding_window"
    assert keys == ["sentinel:v1:key"]
    assert args == ["7", "30000000"]


async def test_decision_timestamp_is_reasonable() -> None:
    loader = FakeLoader({"token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60]})
    strategy = TokenBucketStrategy(cast(ScriptLoader, loader))
    before = time.time_ns() // 1_000
    decision = await strategy.evaluate(_token_bucket_policy(), "sentinel:v1:k")
    after = time.time_ns() // 1_000
    assert before <= decision.decision_time_micro <= after


async def test_rate_limiter_dispatches_to_correct_strategy() -> None:
    loader = FakeLoader(
        {
            "token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60],
            "sliding_window": [1, 1, 0, 1_700_000_000_000_000, 120],
        }
    )
    rate_limiter = RateLimiter(
        cast(ScriptLoader, loader),
        breaker=CircuitBreaker(),
        emergency=TokenBucketEmergencyLimiter(),
    )
    tb_decision = await rate_limiter.evaluate(_token_bucket_policy(), "sentinel:v1:tb")
    assert tb_decision.allowed is True
    assert tb_decision.reason is DecisionReason.ALLOWED
    sw_decision = await rate_limiter.evaluate(_sliding_window_policy(), "sentinel:v1:sw")
    assert sw_decision.allowed is True
    assert sw_decision.reason is DecisionReason.ALLOWED
    assert [name for name, _, _ in loader.calls] == ["token_bucket", "sliding_window"]


def _limiter(
    loader: FakeLoader,
    *,
    breaker: CircuitBreaker | None = None,
    emergency: EmergencyLimiter | None = None,
) -> RateLimiter:
    return RateLimiter(
        cast(ScriptLoader, loader),
        breaker=breaker or CircuitBreaker(),
        emergency=emergency or TokenBucketEmergencyLimiter(),
    )


async def test_fail_open_emergency_denial_produces_emergency_limit() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=2_000),
        "sentinel:v1:k",
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.EMERGENCY_LOCAL_LIMIT
    assert decision.remaining_micro == 2_000
    assert decision.retry_after_seconds == (TOKENS_PER_TOKEN_MICRO - 2_000) / 2_000


async def test_fail_closed_timeout_denies_with_fail_closed() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED), "sentinel:v1:k"
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.FAIL_CLOSED
    assert decision.remaining_micro == 0
    assert decision.retry_after_seconds is None


async def test_fail_closed_connection_error_denies_with_fail_closed() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisConnectionError("connection refused"))
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED), "sentinel:v1:k"
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.FAIL_CLOSED


async def test_fail_closed_noscript_exhaustion_denies_with_fail_closed() -> None:
    loader = FakeLoader({})
    loader.set_exception("sliding_window", ScriptMissingError("missing again"))
    decision = await _limiter(loader).evaluate(
        _sliding_window_policy(fail_mode=FailMode.FAIL_CLOSED), "sentinel:v1:k"
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.FAIL_CLOSED


async def test_fail_open_timeout_allows_with_failure_reason() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(
            fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO
        ),
        "sentinel:v1:k",
    )
    assert decision.allowed is True
    assert decision.reason is DecisionReason.REDIS_TIMEOUT
    assert decision.remaining_micro == 0
    assert decision.retry_after_seconds is None


async def test_fail_open_connection_error_allows_with_failure_reason() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisConnectionError("connection refused"))
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(
            fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO
        ),
        "sentinel:v1:k",
    )
    assert decision.allowed is True
    assert decision.reason is DecisionReason.REDIS_CONNECTION_ERROR


async def test_fail_open_noscript_exhaustion_allows_with_failure_reason() -> None:
    loader = FakeLoader({})
    loader.set_exception("sliding_window", ScriptMissingError("missing again"))
    decision = await _limiter(loader).evaluate(
        _sliding_window_policy(
            fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO
        ),
        "sentinel:v1:k",
    )
    assert decision.allowed is True
    assert decision.reason is DecisionReason.REDIS_NOSCRIPT_RETRY


async def test_fail_open_tiny_fallback_denies_with_emergency_limit() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=2_000),
        "sentinel:v1:k",
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.EMERGENCY_LOCAL_LIMIT
    assert decision.remaining_micro == 2_000
    assert decision.retry_after_seconds == (TOKENS_PER_TOKEN_MICRO - 2_000) / 2_000


async def test_fail_open_sustained_failure_matches_fallback_rate() -> None:
    """Phase 14 regression through the full fail-open journey.

    Sustained Redis failure with fallback rate = 1 token/s at a 100 ms cadence
    over 3 s must admit exactly the initial burst plus one token per elapsed
    second (steps 0/10/20/30). The pre-fix emergency limiter admitted at steps
    0/4/8/... (~2.3x the configured rate).
    """
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    clock = FakeMicroClock()
    limiter = _limiter(
        loader,
        emergency=TokenBucketEmergencyLimiter(now_micro=clock),
    )
    policy = _token_bucket_policy(
        fail_mode=FailMode.FAIL_OPEN,
        fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO,
    )
    allowed_steps: list[int] = []
    for step in range(31):
        if (await limiter.evaluate(policy, "sentinel:v1:k")).allowed:
            allowed_steps.append(step)
        clock.advance(100_000)
    assert allowed_steps == [0, 10, 20, 30]


async def test_non_redis_exceptions_propagate() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", KeyError("unloaded script"))
    with pytest.raises(KeyError):
        await _limiter(loader).evaluate(_token_bucket_policy(), "sentinel:v1:k")


async def test_failure_decision_timestamp_is_reasonable() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    before = time.time_ns() // 1_000
    decision = await _limiter(loader).evaluate(
        _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED), "sentinel:v1:k"
    )
    after = time.time_ns() // 1_000
    assert before <= decision.decision_time_micro <= after


def _tripped_breaker() -> CircuitBreaker:
    breaker = CircuitBreaker()
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    return breaker


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


class FakeMicroClock:
    def __init__(self, start: int = 1_700_000_000_000_000_000) -> None:
        self.value = start

    def advance(self, micro: int) -> None:
        self.value += micro

    def __call__(self) -> int:
        return self.value


async def test_open_breaker_fail_closed_denies_without_redis() -> None:
    breaker = _tripped_breaker()
    loader = FakeLoader({})
    limiter = _limiter(loader, breaker=breaker)
    decision = await limiter.evaluate(
        _sliding_window_policy(fail_mode=FailMode.FAIL_CLOSED), "sentinel:v1:k"
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.CIRCUIT_OPEN
    assert decision.remaining_micro == 0
    assert decision.retry_after_seconds is None
    assert loader.calls == []


async def test_open_breaker_fail_open_routes_to_emergency_without_redis() -> None:
    breaker = _tripped_breaker()
    loader = FakeLoader({})
    limiter = _limiter(loader, breaker=breaker)
    decision = await limiter.evaluate(
        _token_bucket_policy(
            fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO
        ),
        "sentinel:v1:k",
    )
    assert decision.allowed is True
    assert decision.reason is DecisionReason.CIRCUIT_OPEN
    assert decision.remaining_micro == 0
    assert loader.calls == []


async def test_failures_accumulate_toward_open_threshold() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    breaker = CircuitBreaker()
    limiter = _limiter(loader, breaker=breaker)
    policy = _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED)
    for _ in range(FAILURE_THRESHOLD - 1):
        await limiter.evaluate(policy, "sentinel:v1:k")
    assert breaker.state is BreakerState.CLOSED
    await limiter.evaluate(policy, "sentinel:v1:k")
    assert breaker.state is BreakerState.OPEN


async def test_genuine_redis_success_resets_failure_count() -> None:
    loader = FakeLoader({"token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60]})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    breaker = CircuitBreaker()
    limiter = _limiter(loader, breaker=breaker)
    policy = _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED)
    for _ in range(FAILURE_THRESHOLD - 2):
        await limiter.evaluate(policy, "sentinel:v1:k")
    assert breaker.failure_count == FAILURE_THRESHOLD - 2
    loader.set_exception("token_bucket", None)
    decision = await limiter.evaluate(policy, "sentinel:v1:k")
    assert decision.allowed is True
    assert breaker.failure_count == 0
    assert breaker.state is BreakerState.CLOSED


async def test_emergency_pass_through_does_not_reset_breaker() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    breaker = CircuitBreaker()
    limiter = _limiter(loader, breaker=breaker)
    decision = await limiter.evaluate(
        _token_bucket_policy(
            fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO
        ),
        "sentinel:v1:k",
    )
    assert decision.allowed is True
    assert breaker.failure_count == 1
    assert breaker.state is BreakerState.CLOSED


async def test_half_open_probe_failure_reopens_breaker() -> None:
    clock = FakeClock()
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    breaker = CircuitBreaker(now=clock)
    limiter = _limiter(loader, breaker=breaker)
    policy = _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED)
    for _ in range(FAILURE_THRESHOLD):
        await limiter.evaluate(policy, "sentinel:v1:k")
    assert breaker.state is BreakerState.OPEN
    clock.advance(OPEN_TIMEOUT_SECONDS)
    decision = await limiter.evaluate(policy, "sentinel:v1:k")
    assert decision.reason is DecisionReason.FAIL_CLOSED
    assert breaker.state is BreakerState.OPEN
    assert len(loader.calls) == FAILURE_THRESHOLD + 1


async def test_recovered_half_open_probe_success_closes_breaker() -> None:
    clock = FakeClock()
    loader = FakeLoader({"token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60]})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    breaker = CircuitBreaker(now=clock)
    limiter = _limiter(loader, breaker=breaker)
    policy = _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED)
    for _ in range(FAILURE_THRESHOLD):
        await limiter.evaluate(policy, "sentinel:v1:k")
    clock.advance(OPEN_TIMEOUT_SECONDS)
    loader.set_exception("token_bucket", None)
    decision = await limiter.evaluate(policy, "sentinel:v1:k")
    assert decision.allowed is True
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_count == 0


def test_unknown_algorithm_follows_enum_contract() -> None:
    with pytest.raises(ValidationError):
        _token_bucket_policy(algorithm="bogus")


async def test_evaluate_anonymous_allows_only_when_all_buckets_allow() -> None:
    loader = FakeLoader({"token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60]})
    limiter = _limiter(loader)
    decision = await limiter.evaluate_anonymous(
        _token_bucket_policy(), ("sentinel:v2:a", "sentinel:v2:b")
    )
    assert decision.allowed is True
    assert decision.reason is DecisionReason.ALLOWED
    assert [call[1][0] for call in loader.calls] == ["sentinel:v2:a", "sentinel:v2:b"]


async def test_evaluate_anonymous_first_denial_wins() -> None:
    loader = FakeLoader({"token_bucket": [0, 0, 1_700_000_000_000_000, -1]})
    limiter = _limiter(loader)
    decision = await limiter.evaluate_anonymous(
        _token_bucket_policy(), ("sentinel:v2:a", "sentinel:v2:b")
    )
    assert decision.allowed is False
    assert decision.reason is DecisionReason.RATE_LIMITED
    assert [call[1][0] for call in loader.calls] == ["sentinel:v2:a"]


async def test_evaluate_anonymous_single_key() -> None:
    loader = FakeLoader({"token_bucket": [1, 1_000_000, 1_700_000_000_000_000, 60]})
    limiter = _limiter(loader)
    decision = await limiter.evaluate_anonymous(_token_bucket_policy(), ("sentinel:v2:only",))
    assert decision.allowed is True
    assert len(loader.calls) == 1


async def test_evaluate_anonymous_empty_keys_raise() -> None:
    with pytest.raises(ValueError, match="at least one bucket key"):
        await _limiter(FakeLoader({})).evaluate_anonymous(_token_bucket_policy(), ())


async def test_evaluate_anonymous_failure_is_terminal_no_second_emergency_consumption() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    limiter = _limiter(
        loader,
        emergency=TokenBucketEmergencyLimiter(),
    )
    policy = _token_bucket_policy(
        fail_mode=FailMode.FAIL_OPEN, fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO
    )
    decision = await limiter.evaluate_anonymous(policy, ("sentinel:v2:a", "sentinel:v2:b"))
    assert decision.allowed is True
    assert decision.reason is DecisionReason.REDIS_TIMEOUT
    assert len(loader.calls) == 1
    breaker_limiter = _limiter(loader, breaker=_tripped_breaker())
    decision = await breaker_limiter.evaluate_anonymous(policy, ("sentinel:v2:a", "sentinel:v2:b"))
    assert decision.allowed is True
    assert decision.reason is DecisionReason.CIRCUIT_OPEN
    assert len(loader.calls) == 1


async def test_evaluate_anonymous_fail_closed_denies_on_first_failure() -> None:
    loader = FakeLoader({})
    loader.set_exception("token_bucket", RedisTimeoutError("timeout"))
    policy = _token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED)
    decision = await _limiter(loader).evaluate_anonymous(policy, ("sentinel:v2:a", "sentinel:v2:b"))
    assert decision.allowed is False
    assert decision.reason is DecisionReason.FAIL_CLOSED
    assert len(loader.calls) == 1
