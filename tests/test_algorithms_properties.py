"""Hypothesis-driven invariants for the Phase 3 reference algorithms.

These establish that the pure functions are trustworthy enough to serve as
the reference oracle for the Phase 4 Lua implementation.
"""

from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st
from sentinel.algorithms import (
    TOKENS_PER_TOKEN_MICRO,
    sliding_window_evaluate,
    token_bucket_evaluate,
)

MAX_TIME_MICRO = 10**12


@given(
    capacity=st.integers(min_value=TOKENS_PER_TOKEN_MICRO, max_value=100_000_000),
    rate=st.integers(min_value=0, max_value=100_000_000),
    tokens=st.integers(min_value=0, max_value=100_000_000),
    last=st.integers(min_value=0, max_value=MAX_TIME_MICRO),
    now=st.integers(min_value=0, max_value=MAX_TIME_MICRO),
)
def test_token_bucket_state_stays_bounded(
    capacity: int, rate: int, tokens: int, last: int, now: int
) -> None:
    _, tokens_after, _ = token_bucket_evaluate(capacity, rate, tokens, last, now)
    assert 0 <= tokens_after <= capacity


@given(
    capacity=st.integers(min_value=TOKENS_PER_TOKEN_MICRO, max_value=100_000_000),
    rate=st.integers(min_value=0, max_value=100_000_000),
    initial_tokens=st.integers(min_value=0, max_value=100_000_000),
    times=st.lists(st.integers(min_value=0, max_value=MAX_TIME_MICRO), min_size=1, max_size=200),
)
def test_token_bucket_stream_consumption_never_exceeds_input(
    capacity: int, rate: int, initial_tokens: int, times: list[int]
) -> None:
    sorted_times = sorted(times)
    first = sorted_times[0]
    tokens = min(capacity, initial_tokens)
    last_refill = first
    allowed_total = 0
    for now in sorted_times:
        allowed, tokens, last_refill = token_bucket_evaluate(
            capacity, rate, tokens, last_refill, now
        )
        if allowed:
            allowed_total += 1
        assert tokens >= 0
    duration = sorted_times[-1] - first
    assert allowed_total * TOKENS_PER_TOKEN_MICRO <= initial_tokens + duration * rate


def _reference_sliding_window(
    limit: int,
    current_count: int,
    previous_count: int,
    window_size_micro: int,
    now_micro: int,
) -> bool:
    if now_micro >= 2 * window_size_micro:
        current_count = 0
        previous_count = 0
        remaining_micro = window_size_micro
    elif now_micro >= window_size_micro:
        previous_count = current_count
        current_count = 0
        # Reference keeps window_start = 0, so now_micro doubles as elapsed:
        # partway into the new window, only 2 * window_size - now remains.
        remaining_micro = 2 * window_size_micro - now_micro
    else:
        remaining_micro = window_size_micro - now_micro
    estimated = Fraction(current_count) + Fraction(
        previous_count * remaining_micro, window_size_micro
    )
    return estimated < limit


@given(
    limit=st.integers(min_value=1, max_value=100),
    current=st.integers(min_value=0, max_value=300),
    previous=st.integers(min_value=0, max_value=300),
    size=st.integers(min_value=1, max_value=1_000_000),
    now=st.integers(min_value=0, max_value=4_000_000),
)
def test_sliding_window_matches_fraction_reference(
    limit: int, current: int, previous: int, size: int, now: int
) -> None:
    actual = sliding_window_evaluate(limit, current, previous, 0, size, now)
    reference = _reference_sliding_window(limit, current, previous, size, now)
    assert actual == reference


def test_sliding_window_partial_rollover_matches_fraction_reference() -> None:
    """Deterministic oracle parity across every rollover region.

    The exact-boundary case (now == size) hides the rollover-remaining bug
    because remaining is the full window there either way; the partway case
    (now == 160, remaining == 40) is where the buggy oracle diverged.
    """
    cases = [
        (2, 3, 1, 100, 60),  # inside current window
        (2, 2, 1, 100, 100),  # exactly one window boundary (hides the bug)
        (1, 1, 0, 100, 160),  # partway into the next window: remaining 40
        (1, 1, 0, 1, 1),  # review case at the exact boundary
        (5, 5, 9, 100, 200),  # exactly two windows
        (5, 5, 9, 100, 250),  # beyond two windows
    ]
    for limit, current, previous, size, now in cases:
        actual = sliding_window_evaluate(limit, current, previous, 0, size, now)
        reference = _reference_sliding_window(limit, current, previous, size, now)
        assert actual == reference
    # window_size=100, now=160: the new window started at 100, so 40 units
    # remain; previous=2 contributes 2 * 40/100 = 4/5 < limit 2, which admits
    # the request only under the corrected remaining-time semantics.
    assert sliding_window_evaluate(2, 2, 0, 0, 100, 160) is True


@given(
    limit=st.integers(min_value=1, max_value=50),
    size=st.integers(min_value=1_000, max_value=1_000_000),
    times=st.lists(st.integers(min_value=0, max_value=5_000_000), min_size=1, max_size=300),
)
def test_sliding_window_admission_bounds_hold(limit: int, size: int, times: list[int]) -> None:
    sorted_times = sorted(times)
    window_start = 0
    current = 0
    previous = 0
    bucket_counts: dict[int, int] = {}
    for now in sorted_times:
        next_start = now // size * size
        if next_start != window_start:
            if next_start - window_start >= 2 * size:
                current = 0
                previous = 0
            else:
                previous = current
                current = 0
            window_start = next_start
        if sliding_window_evaluate(limit, current, previous, window_start, size, now):
            current += 1
            bucket_counts[window_start] = bucket_counts.get(window_start, 0) + 1
    for count in bucket_counts.values():
        assert count <= limit
    for adjacent, count in zip(sorted(bucket_counts), sorted(bucket_counts)[1:], strict=False):
        assert bucket_counts[adjacent] + bucket_counts[count] <= 2 * limit
