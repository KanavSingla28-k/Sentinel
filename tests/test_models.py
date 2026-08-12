"""Domain model contract tests (Phase 1)."""

import pytest
from pydantic import ValidationError
from sentinel.models import AlgorithmType, Decision, DecisionReason, FailMode, Policy

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
        "capacity_micro": 1_000_000,
        "refill_rate_micro_per_sec": 1_000_000,
        "algorithm": AlgorithmType.SLIDING_WINDOW,
        "fail_mode": FailMode.FAIL_CLOSED,
        "fallback_rate_per_process_micro": 100_000,
        "policy_version": 1,
    }
    base.update(overrides)
    return Policy(**base)


def test_valid_policy_constructs() -> None:
    policy = make_policy()
    assert policy.endpoint_id == "pdftalk.ingest"
    assert policy.capacity_micro == 1_000_000
    assert policy.refill_rate_micro_per_sec == 1_000_000
    assert policy.algorithm is AlgorithmType.SLIDING_WINDOW
    assert policy.fail_mode is FailMode.FAIL_CLOSED
    assert policy.policy_version == 1


def test_rejects_negative_capacity() -> None:
    with pytest.raises(ValidationError):
        make_policy(capacity_micro=-1)


def test_rejects_zero_capacity() -> None:
    with pytest.raises(ValidationError):
        make_policy(capacity_micro=0)


def test_accepts_zero_refill_rate() -> None:
    policy = make_policy(refill_rate_micro_per_sec=0)
    assert policy.refill_rate_micro_per_sec == 0


def test_rejects_negative_refill_rate() -> None:
    with pytest.raises(ValidationError):
        make_policy(refill_rate_micro_per_sec=-1)


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


def test_policy_fields_are_all_integer_microtokens() -> None:
    for field_name in (
        "capacity_micro",
        "refill_rate_micro_per_sec",
        "fallback_rate_per_process_micro",
    ):
        assert Policy.model_fields[field_name].annotation is int


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
