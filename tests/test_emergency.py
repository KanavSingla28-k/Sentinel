"""Emergency local limiter unit tests with an injected clock (Phase 10)."""

import asyncio

from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO, token_bucket_evaluate
from sentinel.emergency import TokenBucketEmergencyLimiter


class FakeMicroClock:
    def __init__(self, start: int = 1_700_000_000_000_000_000) -> None:
        self.value = start

    def advance(self, micro: int) -> None:
        self.value += micro

    def __call__(self) -> int:
        return self.value


def _outcome(rate: int) -> tuple[TokenBucketEmergencyLimiter, FakeMicroClock]:
    clock = FakeMicroClock()
    limiter = TokenBucketEmergencyLimiter(now_micro=clock)
    return limiter, clock


async def test_first_use_bucket_starts_at_full_capacity() -> None:
    limiter, clock = _outcome(1_000_000)
    outcome = await limiter.evaluate("ep", 1_000_000)
    assert outcome.allowed is True


async def test_exact_exhaustion_at_capacity() -> None:
    limiter, clock = _outcome(3_000_000)
    results = [await limiter.evaluate("ep", 3_000_000) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].remaining_micro == 0


async def test_refill_after_elapsed_time() -> None:
    limiter, clock = _outcome(1_000_000)
    first = await limiter.evaluate("ep", 1_000_000)
    assert first.allowed is True
    clock.advance(999_999)
    second = await limiter.evaluate("ep", 1_000_000)
    assert second.allowed is False
    clock.advance(1)
    third = await limiter.evaluate("ep", 1_000_000)
    assert third.allowed is True


async def test_refill_is_clamped_at_capacity() -> None:
    limiter, clock = _outcome(1_000_000)
    first = await limiter.evaluate("ep", 1_000_000)
    assert first.allowed is True
    clock.advance(100 * TOKENS_PER_TOKEN_MICRO)
    second = await limiter.evaluate("ep", 1_000_000)
    assert second.allowed is True
    third = await limiter.evaluate("ep", 1_000_000)
    assert third.allowed is False


async def test_buckets_are_isolated_per_endpoint() -> None:
    limiter, clock = _outcome(1_000_000)
    assert (await limiter.evaluate("ep-a", 1_000_000)).allowed is True
    assert (await limiter.evaluate("ep-a", 1_000_000)).allowed is False
    assert (await limiter.evaluate("ep-b", 1_000_000)).allowed is True


async def test_denied_retry_after_matches_token_bucket_formula() -> None:
    limiter, clock = _outcome(1_000_000)
    assert (await limiter.evaluate("ep", 1_000_000)).allowed is True
    denied = await limiter.evaluate("ep", 1_000_000)
    assert denied.allowed is False
    assert denied.retry_after_seconds == 1.0


async def test_partial_remaining_retry_after() -> None:
    limiter, clock = _outcome(2_000_000)
    assert (await limiter.evaluate("ep", 2_000_000)).allowed is True
    assert (await limiter.evaluate("ep", 2_000_000)).allowed is True
    clock.advance(750_000)
    assert (await limiter.evaluate("ep", 2_000_000)).allowed is True
    denied = await limiter.evaluate("ep", 2_000_000)
    assert denied.allowed is False
    assert denied.remaining_micro == 500_000
    assert denied.retry_after_seconds == 0.25


async def test_tiny_fallback_rate_never_allows_and_stays_bounded() -> None:
    limiter, clock = _outcome(2_000)
    outcome = await limiter.evaluate("ep", 2_000)
    assert outcome.allowed is False
    assert outcome.remaining_micro == 2_000
    assert outcome.retry_after_seconds == (TOKENS_PER_TOKEN_MICRO - 2_000) / 2_000
    clock.advance(100 * TOKENS_PER_TOKEN_MICRO)
    later = await limiter.evaluate("ep", 2_000)
    assert later.allowed is False
    assert later.remaining_micro == 2_000


async def test_parity_with_token_bucket_evaluate() -> None:
    clock = FakeMicroClock()
    limiter = TokenBucketEmergencyLimiter(now_micro=clock)
    rate = 1_500_000
    tokens, last_refill = rate, clock()
    for _ in range(6):
        outcome = await limiter.evaluate("ep", rate)
        allowed, tokens_after, last_refill_after = token_bucket_evaluate(
            capacity_micro=rate,
            refill_rate_micro_per_sec=rate,
            tokens_micro=tokens,
            last_refill_micro=last_refill,
            now_micro=clock(),
        )
        assert outcome.allowed is allowed
        assert outcome.remaining_micro == tokens_after
        # The emergency limiter mirrors the Lua's "denied requests never
        # write": the reference state advances only on ALLOW. (The pre-fix
        # test stored every evaluation's output unconditionally, which was
        # self-consistent with the double-refill bug it hid.)
        if allowed:
            tokens, last_refill = tokens_after, last_refill_after
        clock.advance(400_000)


async def test_default_clock_refills_over_real_time() -> None:
    limiter = TokenBucketEmergencyLimiter()
    assert (await limiter.evaluate("ep", 30_000_000)).allowed is True
    await asyncio.sleep(0.05)
    outcome = await limiter.evaluate("ep", 30_000_000)
    assert outcome.allowed is True


async def test_bucket_misses_initialize_then_drain_correctly() -> None:
    limiter, clock = _outcome(1_000_000)
    assert (await limiter.evaluate("fresh", 1_000_000)).allowed is True
    assert (await limiter.evaluate("fresh", 1_000_000)).allowed is False
    clock.advance(TOKENS_PER_TOKEN_MICRO)
    assert (await limiter.evaluate("fresh", 1_000_000)).allowed is True


async def test_initial_burst_then_immediate_deny() -> None:
    limiter, clock = _outcome(TOKENS_PER_TOKEN_MICRO)
    assert (await limiter.evaluate("ep", TOKENS_PER_TOKEN_MICRO)).allowed is True
    assert (await limiter.evaluate("ep", TOKENS_PER_TOKEN_MICRO)).allowed is False


async def test_sustained_traffic_matches_refill_rate() -> None:
    """Phase 14 regression: sustained traffic never exceeds the configured rate.

    capacity = rate = 1 token/s at a 100 ms cadence over 3 s admits exactly
    the initial burst plus one token per elapsed second (t = 0.0/1.0/2.0/3.0).
    The pre-fix limiter admitted 8 (t = 0.0/0.4/0.8/...) because every denied
    call banked its partial refill while last_refill_micro stayed put.
    """
    limiter, clock = _outcome(TOKENS_PER_TOKEN_MICRO)
    allowed_steps: list[int] = []
    for step in range(31):
        if (await limiter.evaluate("ep", TOKENS_PER_TOKEN_MICRO)).allowed:
            allowed_steps.append(step)
        clock.advance(100_000)
    assert allowed_steps == [0, 10, 20, 30]


async def test_denied_requests_do_not_accelerate_replenishment() -> None:
    limiter, clock = _outcome(TOKENS_PER_TOKEN_MICRO)
    assert (await limiter.evaluate("ep", TOKENS_PER_TOKEN_MICRO)).allowed is True
    for _ in range(9):
        clock.advance(100_000)
        assert (await limiter.evaluate("ep", TOKENS_PER_TOKEN_MICRO)).allowed is False
    clock.advance(100_000)
    assert (await limiter.evaluate("ep", TOKENS_PER_TOKEN_MICRO)).allowed is True


async def test_sustained_traffic_rate_two_tokens_per_second() -> None:
    # Phase 10 design: capacity == rate == fallback_rate_per_process_micro, so
    # a 2 token/s fallback is a 2-token burst; 3 s at 100 ms cadence admits
    # exactly capacity + elapsed * rate = 2 + 6 = 8.
    limiter, clock = _outcome(TOKENS_PER_TOKEN_MICRO)
    rate = 2 * TOKENS_PER_TOKEN_MICRO
    allowed_steps: list[int] = []
    for step in range(31):
        if (await limiter.evaluate("ep", rate)).allowed:
            allowed_steps.append(step)
        clock.advance(100_000)
    assert len(allowed_steps) == 8
    assert allowed_steps[0] == 0
    assert allowed_steps[-1] == 30


async def test_sustained_traffic_rate_five_tokens_per_second() -> None:
    # 5 token/s fallback = 5-token burst; 3 s at 100 ms cadence admits
    # exactly capacity + elapsed * rate = 5 + 15 = 20.
    limiter, clock = _outcome(TOKENS_PER_TOKEN_MICRO)
    rate = 5 * TOKENS_PER_TOKEN_MICRO
    allowed_steps: list[int] = []
    for step in range(31):
        if (await limiter.evaluate("ep", rate)).allowed:
            allowed_steps.append(step)
        clock.advance(100_000)
    assert len(allowed_steps) == 20
    assert allowed_steps[0] == 0
    assert allowed_steps[-1] == 30
