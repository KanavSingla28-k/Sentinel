"""Rate limiter unit tests: key construction and Decision mapping (Phase 6)."""

import time
from typing import cast

import pytest
from pydantic import ValidationError
from sentinel.limiter import (
    RateLimiter,
    SlidingWindowStrategy,
    TokenBucketStrategy,
    build_bucket_key,
)
from sentinel.models import AlgorithmType, DecisionReason, FailMode, Policy
from sentinel.redis import ScriptLoader


class FakeLoader:
    def __init__(self, results: dict[str, int | list[int] | None]) -> None:
        self._results = results
        self.calls: list[tuple[str, list[str], list[str]]] = []

    async def execute(self, name: str, keys: list[str], args: list[str]) -> int | list[int] | None:
        self.calls.append((name, keys, args))
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
    rate_limiter = RateLimiter(cast(ScriptLoader, loader))
    tb_decision = await rate_limiter.evaluate(_token_bucket_policy(), "sentinel:v1:tb")
    assert tb_decision.allowed is True
    assert tb_decision.reason is DecisionReason.ALLOWED
    sw_decision = await rate_limiter.evaluate(_sliding_window_policy(), "sentinel:v1:sw")
    assert sw_decision.allowed is True
    assert sw_decision.reason is DecisionReason.ALLOWED
    assert [name for name, _, _ in loader.calls] == ["token_bucket", "sliding_window"]


def test_unknown_algorithm_follows_enum_contract() -> None:
    with pytest.raises(ValidationError):
        _token_bucket_policy(algorithm="bogus")
