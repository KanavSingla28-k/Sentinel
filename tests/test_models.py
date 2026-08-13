"""Domain model contract tests (Phase 1)."""

import pytest
from pydantic import ValidationError
from sentinel.models import (
    LUA_MAX_EXACT_INT,
    TOKEN_BUCKET_LUA_PRODUCT_LIMIT,
    AlgorithmType,
    Decision,
    DecisionReason,
    FailMode,
    Policy,
)

EXPECTED_DECISION_REASONS = frozenset(
    {
        "ALLOWED",
        "RATE_LIMITED",
        "REDIS_TIMEOUT",
        "REDIS_CONNECTION_ERROR",
        "REDIS_NOSCRIPT_RETRY",
        "CIRCUIT_OPEN",
        "EMERGENCY_LOCAL_LIMIT",
        "FAIL_CLOSED",
    }
)


def make_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "pdftalk.ingest",
        "algorithm": AlgorithmType.SLIDING_WINDOW,
        "fail_mode": FailMode.FAIL_CLOSED,
        "fallback_rate_per_process_micro": 100_000,
        "policy_version": 1,
        "limit": 10,
    }
    base.update(overrides)
    return Policy(**base)


def test_token_bucket_lua_product_limit_stays_within_exact_int() -> None:
    assert TOKEN_BUCKET_LUA_PRODUCT_LIMIT < LUA_MAX_EXACT_INT


def test_valid_policy_constructs() -> None:
    policy = make_policy()
    assert policy.endpoint_id == "pdftalk.ingest"
    assert policy.capacity_micro is None
    assert policy.refill_rate_micro_per_sec is None
    assert policy.algorithm is AlgorithmType.SLIDING_WINDOW
    assert policy.fail_mode is FailMode.FAIL_CLOSED
    assert policy.policy_version == 1


def test_rejects_negative_capacity() -> None:
    with pytest.raises(ValidationError):
        make_policy(algorithm=AlgorithmType.TOKEN_BUCKET, capacity_micro=-1, limit=None)


def test_rejects_zero_capacity() -> None:
    with pytest.raises(ValidationError):
        make_policy(algorithm=AlgorithmType.TOKEN_BUCKET, capacity_micro=0, limit=None)


def test_rejects_sub_token_capacity() -> None:
    with pytest.raises(ValidationError):
        make_policy(algorithm=AlgorithmType.TOKEN_BUCKET, capacity_micro=999_999, limit=None)


def test_accepts_zero_refill_rate() -> None:
    policy = make_policy(
        algorithm=AlgorithmType.TOKEN_BUCKET,
        capacity_micro=1_000_000,
        refill_rate_micro_per_sec=0,
        limit=None,
    )
    assert policy.refill_rate_micro_per_sec == 0


def test_rejects_negative_refill_rate() -> None:
    with pytest.raises(ValidationError):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            capacity_micro=1_000_000,
            refill_rate_micro_per_sec=-1,
            limit=None,
        )


def test_rejects_unknown_algorithm() -> None:
    with pytest.raises(ValidationError):
        make_policy(algorithm="token")


def test_rejects_unknown_fail_mode() -> None:
    with pytest.raises(ValidationError):
        make_policy(fail_mode="break_glass")


def test_rejects_endpoint_id_with_slash() -> None:
    with pytest.raises(ValidationError):
        make_policy(endpoint_id="pdftalk/ingest")


def test_rejects_endpoint_id_with_uppercase() -> None:
    with pytest.raises(ValidationError):
        make_policy(endpoint_id="PDFTalk.Ingest")


def test_rejects_empty_endpoint_id() -> None:
    with pytest.raises(ValidationError):
        make_policy(endpoint_id="")


def test_accepts_dotted_endpoint_id() -> None:
    policy = make_policy(endpoint_id="resumint.tailor")
    assert policy.endpoint_id == "resumint.tailor"


def test_rejects_missing_policy_version() -> None:
    with pytest.raises(ValidationError):
        make_policy(policy_version=None)


def test_rejects_zero_policy_version() -> None:
    with pytest.raises(ValidationError):
        make_policy(policy_version=0)


def test_rejects_zero_fallback_rate() -> None:
    with pytest.raises(ValidationError):
        make_policy(fallback_rate_per_process_micro=0)


def test_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        make_policy(cost=5)


def test_sliding_window_defaults_to_60_second_window() -> None:
    policy = make_policy()
    assert policy.limit == 10
    assert policy.window_size_micro == 60_000_000


def test_sliding_window_accepts_explicit_window_size() -> None:
    policy = make_policy(window_size_micro=1_000_000)
    assert policy.window_size_micro == 1_000_000


def test_sliding_window_requires_limit() -> None:
    with pytest.raises(ValidationError, match="limit is required"):
        make_policy(limit=None)


def test_valid_token_bucket_policy_constructs() -> None:
    policy = make_policy(
        algorithm=AlgorithmType.TOKEN_BUCKET,
        capacity_micro=2_000_000,
        refill_rate_micro_per_sec=1_000_000,
        limit=None,
    )
    assert policy.algorithm is AlgorithmType.TOKEN_BUCKET
    assert policy.limit is None
    assert policy.window_size_micro == 60_000_000


def test_sliding_window_rejects_capacity_micro() -> None:
    with pytest.raises(ValidationError, match="only valid when algorithm is token_bucket"):
        make_policy(capacity_micro=1_000_000)


def test_sliding_window_rejects_refill_rate() -> None:
    with pytest.raises(ValidationError, match="only valid when algorithm is token_bucket"):
        make_policy(refill_rate_micro_per_sec=1_000_000)


def test_token_bucket_requires_capacity_micro() -> None:
    with pytest.raises(ValidationError, match="capacity_micro is required"):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            refill_rate_micro_per_sec=1_000_000,
            limit=None,
            capacity_micro=None,
        )


def test_token_bucket_requires_refill_rate() -> None:
    with pytest.raises(ValidationError, match="refill_rate_micro_per_sec is required"):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            capacity_micro=1_000_000,
            limit=None,
            refill_rate_micro_per_sec=None,
        )


def test_sliding_window_rejects_zero_limit() -> None:
    with pytest.raises(ValidationError):
        make_policy(limit=0)


def test_sliding_window_rejects_sub_millisecond_window() -> None:
    with pytest.raises(ValidationError):
        make_policy(window_size_micro=999)


def test_sliding_window_rejects_product_above_lua_exactness_bound() -> None:
    with pytest.raises(ValidationError, match="Lua integer exactness"):
        make_policy(limit=10_000_000_000, window_size_micro=1_000_000)


def test_token_bucket_rejects_limit() -> None:
    with pytest.raises(ValidationError, match="only valid when algorithm is sliding_window"):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            capacity_micro=1_000_000,
            refill_rate_micro_per_sec=1_000_000,
            limit=10,
        )


def test_token_bucket_rejects_explicit_window_size() -> None:
    with pytest.raises(ValidationError, match="only valid when algorithm is sliding_window"):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            capacity_micro=1_000_000,
            refill_rate_micro_per_sec=1_000_000,
            limit=None,
            window_size_micro=1_000_000,
        )


def test_token_bucket_rejects_capacity_above_lua_exactness_bound() -> None:
    with pytest.raises(ValidationError, match="Lua integer exactness"):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            capacity_micro=2**30 + 1,
            refill_rate_micro_per_sec=1_000_000,
            limit=None,
        )


def test_token_bucket_rejects_rate_above_lua_exactness_bound() -> None:
    with pytest.raises(ValidationError, match="Lua integer exactness"):
        make_policy(
            algorithm=AlgorithmType.TOKEN_BUCKET,
            capacity_micro=1_000_000,
            refill_rate_micro_per_sec=2**30 + 1,
            limit=None,
        )


def test_policy_micro_fields_are_integers() -> None:
    assert Policy.model_fields["capacity_micro"].annotation == (int | None)
    assert Policy.model_fields["refill_rate_micro_per_sec"].annotation == (int | None)
    assert Policy.model_fields["fallback_rate_per_process_micro"].annotation is int


def test_decision_reason_has_exactly_eight_values() -> None:
    assert {member.name for member in DecisionReason} == EXPECTED_DECISION_REASONS


def test_decision_reason_values_are_snake_case() -> None:
    for member in DecisionReason:
        assert member.value == member.name.lower()


def test_allowed_decision_constructs() -> None:
    decision = Decision(
        allowed=True,
        reason=DecisionReason.ALLOWED,
        remaining_micro=500_000,
        decision_time_micro=1_700_000_000_000_000_000,
    )
    assert decision.allowed is True
    assert decision.retry_after_seconds is None


def test_rate_limited_decision_constructs_with_retry_after() -> None:
    decision = Decision(
        allowed=False,
        reason=DecisionReason.RATE_LIMITED,
        remaining_micro=0,
        decision_time_micro=1_700_000_000_000_000_000,
        retry_after_seconds=2.5,
    )
    assert decision.allowed is False
    assert decision.retry_after_seconds == 2.5


def test_decision_rejects_negative_remaining() -> None:
    with pytest.raises(ValidationError):
        Decision(
            allowed=False,
            reason=DecisionReason.RATE_LIMITED,
            remaining_micro=-1,
            decision_time_micro=1_700_000_000_000_000_000,
        )
