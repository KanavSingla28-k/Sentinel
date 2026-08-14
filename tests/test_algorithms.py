"""Deterministic unit tests for the Phase 3 reference algorithms."""

import pytest
from sentinel.algorithms import (
    TOKENS_PER_TOKEN_MICRO,
    micro_to_tokens,
    sliding_window_evaluate,
    token_bucket_evaluate,
    tokens_to_micro,
)

CAPACITY = 2 * TOKENS_PER_TOKEN_MICRO
RATE_ONE_TOKEN_PER_SEC = TOKENS_PER_TOKEN_MICRO


def test_tokens_to_micro() -> None:
    assert tokens_to_micro(1) == 1_000_000
    assert tokens_to_micro(5) == 5_000_000


def test_micro_to_tokens_floor_division() -> None:
    assert micro_to_tokens(1_000_000) == 1
    assert micro_to_tokens(1_500_000) == 1
    assert micro_to_tokens(999_999) == 0


def test_tokens_to_micro_rejects_negative() -> None:
    with pytest.raises(ValueError):
        tokens_to_micro(-1)


def test_micro_to_tokens_rejects_negative() -> None:
    with pytest.raises(ValueError):
        micro_to_tokens(-1)


def test_token_bucket_elapsed_zero() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(
        CAPACITY, RATE_ONE_TOKEN_PER_SEC, 1_000_000, 100, 100
    )
    assert allowed is True
    assert tokens_after == 0
    assert last_after == 100


def test_token_bucket_elapsed_zero_denied() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(
        CAPACITY, RATE_ONE_TOKEN_PER_SEC, 500_000, 100, 100
    )
    assert allowed is False
    assert tokens_after == 500_000
    assert last_after == 100


def test_token_bucket_small_elapsed_refills_exactly() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(
        CAPACITY, RATE_ONE_TOKEN_PER_SEC, 500_000, 100_000, 700_000
    )
    assert allowed is True
    assert tokens_after == 100_000
    assert last_after == 700_000


def test_token_bucket_floor_division_matters() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(CAPACITY, 500_000, 0, 0, 999_999)
    assert allowed is False
    assert tokens_after == 499_999
    assert last_after == 0


def test_token_bucket_huge_elapsed_caps_at_capacity() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(
        CAPACITY, RATE_ONE_TOKEN_PER_SEC, 0, 0, 10**12
    )
    assert allowed is True
    assert tokens_after == 1_000_000
    assert last_after == 10**12


def test_token_bucket_zero_refill_rate() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(CAPACITY, 0, 999_999, 0, 10**9)
    assert allowed is False
    assert tokens_after == 999_999
    assert last_after == 0


def test_token_bucket_exactly_one_token_allowed() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(CAPACITY, 0, 1_000_000, 5, 5)
    assert allowed is True
    assert tokens_after == 0
    assert last_after == 5


def test_token_bucket_one_microtoken_below_one_token_denied() -> None:
    allowed, tokens_after, _ = token_bucket_evaluate(CAPACITY, 0, 999_999, 5, 5)
    assert allowed is False
    assert tokens_after == 999_999


def test_token_bucket_denied_does_not_advance_last_refill() -> None:
    _, _, last_after = token_bucket_evaluate(CAPACITY, RATE_ONE_TOKEN_PER_SEC, 0, 5, 5)
    assert last_after == 5


def test_token_bucket_negative_clock_skew_clamps_to_zero() -> None:
    allowed, tokens_after, last_after = token_bucket_evaluate(
        CAPACITY, RATE_ONE_TOKEN_PER_SEC, 0, 1_000, 500
    )
    assert allowed is False
    assert tokens_after == 0
    assert last_after == 1_000


def test_token_bucket_empty_bucket_stays_non_negative() -> None:
    allowed, tokens_after, _ = token_bucket_evaluate(CAPACITY, 0, 0, 0, 0)
    assert allowed is False
    assert tokens_after == 0


def test_sliding_window_inside_window() -> None:
    assert sliding_window_evaluate(10, 5, 0, 0, 1_000_000, 400_000) is True


def test_sliding_window_elapsed_zero() -> None:
    assert sliding_window_evaluate(10, 5, 0, 0, 1_000_000, 0) is True


def test_sliding_window_elapsed_exactly_one_window_rolls_over() -> None:
    assert sliding_window_evaluate(10, 5, 2, 0, 1_000_000, 1_000_000) is True


def test_sliding_window_partial_rollover_counts_remaining_time() -> None:
    # window_size = 100, request at now = 160: the new window began at 100,
    # so remaining = 40 (2 * window_size - now), not a full window. The
    # post-shift previous count contributes 1 * 40/100 = 0.4 < limit 1.
    assert sliding_window_evaluate(1, 1, 0, 0, 100, 160) is True
    # The buggy oracle treated remaining as a full window (contribution
    # 1.0 == limit) and rejected this request.


def test_sliding_window_exact_boundary_rollover_counts_full_window() -> None:
    # Review case: limit=1, current=1, previous=0, window_size=1, now=1.
    # elapsed == window_size: the new window has just begun, so remaining is
    # the full window and previous=1 exactly fills limit 1 -> rejected.
    assert sliding_window_evaluate(1, 1, 0, 0, 1, 1) is False


def test_sliding_window_elapsed_exactly_two_windows_expires_both() -> None:
    assert sliding_window_evaluate(5, 5, 9, 0, 1_000_000, 2_000_000) is True


def test_sliding_window_elapsed_beyond_two_windows_expires_both() -> None:
    assert sliding_window_evaluate(5, 5, 9, 0, 1_000_000, 3_000_000) is True


def test_sliding_window_remaining_near_zero_still_counts_current() -> None:
    assert sliding_window_evaluate(10, 9, 5, 0, 1_000_000, 999_999) is True


def test_sliding_window_remaining_near_zero_full_current_rejects() -> None:
    assert sliding_window_evaluate(10, 10, 5, 0, 1_000_000, 999_999) is False


def test_sliding_window_remaining_full_counts_previous_fully() -> None:
    assert sliding_window_evaluate(10, 0, 7, 0, 1_000_000, 0) is True


def test_sliding_window_estimated_below_limit_allows() -> None:
    assert sliding_window_evaluate(10, 2, 7, 0, 1_000_000, 0) is True


def test_sliding_window_estimated_equal_limit_rejects() -> None:
    assert sliding_window_evaluate(10, 4, 6, 0, 1_000_000, 0) is False


def test_sliding_window_estimated_above_limit_rejects() -> None:
    assert sliding_window_evaluate(10, 9, 2, 0, 1_000_000, 0) is False


def test_sliding_window_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        sliding_window_evaluate(0, 0, 0, 0, 1_000_000, 0)


def test_sliding_window_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        sliding_window_evaluate(-1, 0, 0, 0, 1_000_000, 0)


def test_sliding_window_rejects_zero_window_size() -> None:
    with pytest.raises(ValueError):
        sliding_window_evaluate(10, 0, 0, 0, 0, 0)


def test_sliding_window_rejects_negative_window_size() -> None:
    with pytest.raises(ValueError):
        sliding_window_evaluate(10, 0, 0, 0, -5, 0)
