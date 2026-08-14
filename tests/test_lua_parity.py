"""Oracle parity tests for the Lua scripts against a real Redis (Phase 4).

Strategy: seed bucket state relative to real Redis TIME, run the Lua script,
and assert the outcome against sentinel.algorithms evaluated at the same
elapsed time. Cases sit far (>= 100x the clock skew between two local reads)
from refill or rollover boundaries, so a microsecond drift between our TIME
reads and the script's own TIME can never flip a result.
"""

import uuid
from typing import cast

import pytest
from sentinel.algorithms import (
    TOKENS_PER_TOKEN_MICRO,
    sliding_window_evaluate,
    token_bucket_evaluate,
)
from sentinel.lua import SLIDING_WINDOW_SCRIPT, TOKEN_BUCKET_SCRIPT, load_scripts
from sentinel.redis import ScriptLoader, SentinelRedis

pytestmark = pytest.mark.integration

CLOCK_DRIFT_MARGIN = 50_000
TOKENS_DRIFT_TOLERANCE = 50_000
TTL_TOLERANCE = 1


def _unique_key(prefix: str) -> str:
    return f"test:lua:{prefix}:{uuid.uuid4().hex}"


async def _now_micro(client: SentinelRedis) -> int:
    seconds, microseconds = await client.client.time()
    return seconds * 1_000_000 + microseconds


async def _get_state(client: SentinelRedis, key: str) -> str:
    state = await client.client.get(key)
    assert state is not None
    return cast(str, state)


@pytest.fixture
async def loader(redis_client: SentinelRedis) -> ScriptLoader:
    loader = ScriptLoader(redis_client.client)
    await load_scripts(loader)
    return loader


async def test_load_scripts_registers_both_algorithms(loader: ScriptLoader) -> None:
    for name in (TOKEN_BUCKET_SCRIPT, SLIDING_WINDOW_SCRIPT):
        sha = loader.sha(name)
        assert sha is not None
        assert len(sha) == 40


async def test_token_bucket_rate_zero_allow(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-allow")
    before = await _now_micro(redis_client)
    await redis_client.client.set(key, f"{TOKENS_PER_TOKEN_MICRO}:100")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    allowed, tokens_after, last_after, ttl = result
    oracle_allowed, oracle_tokens, _ = token_bucket_evaluate(
        capacity, 0, TOKENS_PER_TOKEN_MICRO, 100, before
    )
    assert allowed == oracle_allowed
    assert tokens_after == oracle_tokens
    assert 0 <= last_after - before <= CLOCK_DRIFT_MARGIN
    assert ttl == -1
    assert await redis_client.client.ttl(key) == -1


async def test_token_bucket_rate_zero_deny_is_a_no_op(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-deny")
    seed = f"{500_000}:100"
    await redis_client.client.set(key, seed)
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    allowed, tokens_after, last_after, ttl = result
    assert allowed == 0
    assert tokens_after == 500_000
    assert last_after == 100
    assert ttl == -1
    assert await _get_state(redis_client, key) == seed


async def test_token_bucket_rate_zero_floor_division_matters(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-floor")
    await redis_client.client.set(key, f"{999_999}:5")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    allowed, tokens_after, last_after, _ = result
    assert allowed == 0
    assert tokens_after == 999_999
    assert last_after == 5


async def test_token_bucket_rate_zero_empty_bucket(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-empty")
    await redis_client.client.set(key, "0:0")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    assert result[0] == 0
    assert result[1] == 0
    assert result[2] == 0


async def test_token_bucket_rate_zero_near_capacity(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-cap")
    await redis_client.client.set(key, f"{1_999_999}:5")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    assert result[0] == 1
    assert result[1] == 999_999
    assert (await _get_state(redis_client, key)).startswith("999999:")


async def test_token_bucket_clamps_negative_clock_skew(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-skew")
    before = await _now_micro(redis_client)
    seed = f"0:{before + 1_000_000}"
    await redis_client.client.set(key, seed)
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    allowed, tokens_after, last_after, _ = result
    oracle_allowed, oracle_tokens, oracle_last = token_bucket_evaluate(
        capacity, 0, 0, before + 1_000_000, before
    )
    assert allowed == oracle_allowed
    assert tokens_after == oracle_tokens
    assert last_after == oracle_last
    assert await _get_state(redis_client, key) == seed


async def test_token_bucket_fresh_key_first_request_always_allows(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-fresh")
    before = await _now_micro(redis_client)
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), "0"])
    assert isinstance(result, list)
    assert result[0] == 1
    assert result[1] == capacity - TOKENS_PER_TOKEN_MICRO
    assert 0 <= result[2] - before <= CLOCK_DRIFT_MARGIN
    assert result[3] == -1


async def test_token_bucket_partial_elapsed_refill_zero(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    rate = TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-refill0")
    before = await _now_micro(redis_client)
    seed_last = before - 500_000
    seed = f"0:{seed_last}"
    await redis_client.client.set(key, seed)
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), str(rate)])
    assert isinstance(result, list)
    allowed, tokens_after, last_after, ttl = result
    oracle_allowed, oracle_tokens, _ = token_bucket_evaluate(capacity, rate, 0, seed_last, before)
    assert allowed == oracle_allowed == 0
    assert abs(tokens_after - oracle_tokens) <= TOKENS_DRIFT_TOLERANCE
    assert last_after == seed_last
    assert ttl == -1
    assert await _get_state(redis_client, key) == seed


async def test_token_bucket_partial_elapsed_refill_one(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    rate = TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-refill1")
    before = await _now_micro(redis_client)
    seed_last = before - 1_500_000
    await redis_client.client.set(key, f"0:{seed_last}")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), str(rate)])
    assert isinstance(result, list)
    allowed, tokens_after, last_after, ttl = result
    oracle_allowed, oracle_tokens, _ = token_bucket_evaluate(capacity, rate, 0, seed_last, before)
    assert allowed == oracle_allowed == 1
    assert abs(tokens_after - oracle_tokens) <= TOKENS_DRIFT_TOLERANCE
    assert 0 <= last_after - before <= CLOCK_DRIFT_MARGIN
    expected_ttl = (capacity - oracle_tokens + rate - 1) // rate + 1
    assert abs(ttl - expected_ttl) <= TTL_TOLERANCE
    remaining = await redis_client.client.ttl(key)
    assert expected_ttl - remaining <= 2 * TTL_TOLERANCE


async def test_token_bucket_refill_two_does_not_cap(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = 2 * TOKENS_PER_TOKEN_MICRO
    rate = TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-refill2")
    before = await _now_micro(redis_client)
    seed_last = before - 2_500_000
    await redis_client.client.set(key, f"0:{seed_last}")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), str(rate)])
    assert isinstance(result, list)
    allowed, tokens_after, _, _ = result
    oracle_allowed, oracle_tokens, _ = token_bucket_evaluate(capacity, rate, 0, seed_last, before)
    assert allowed == oracle_allowed
    assert tokens_after == oracle_tokens == TOKENS_PER_TOKEN_MICRO


async def test_token_bucket_refill_caps_at_capacity(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    capacity = TOKENS_PER_TOKEN_MICRO
    rate = TOKENS_PER_TOKEN_MICRO
    key = _unique_key("tb-cap")
    before = await _now_micro(redis_client)
    seed_last = before - 1_500_000
    await redis_client.client.set(key, f"0:{seed_last}")
    result = await loader.execute(TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(capacity), str(rate)])
    assert isinstance(result, list)
    allowed, tokens_after, _, _ = result
    oracle_allowed, oracle_tokens, _ = token_bucket_evaluate(capacity, rate, 0, seed_last, before)
    assert allowed == oracle_allowed
    assert tokens_after == oracle_tokens == 0


async def test_sliding_window_inside_window_matches_oracle(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-inside")
    before = await _now_micro(redis_client)
    window_start = before - 400_000
    seed = f"5:2:{window_start}"
    await redis_client.client.set(key, seed)
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, ttl = result
    oracle_allowed = sliding_window_evaluate(limit, 5, 2, window_start, window_size, before)
    assert allowed == oracle_allowed == 0
    assert current_after == 5
    assert previous_after == 2
    assert start_after == window_start
    assert ttl == -1
    assert await _get_state(redis_client, key) == seed


async def test_sliding_window_inside_window_allows_and_persists(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-allow")
    before = await _now_micro(redis_client)
    window_start = before - 400_000
    await redis_client.client.set(key, f"3:2:{window_start}")
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, ttl = result
    oracle_allowed = sliding_window_evaluate(limit, 3, 2, window_start, window_size, before)
    assert allowed == oracle_allowed == 1
    assert current_after == 4
    assert previous_after == 2
    assert start_after == window_start
    assert ttl == 2
    assert await _get_state(redis_client, key) == f"4:2:{window_start}"
    remaining = await redis_client.client.ttl(key)
    assert 2 - remaining <= TTL_TOLERANCE


async def test_sliding_window_full_current_at_window_end_rejects(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-full")
    before = await _now_micro(redis_client)
    window_start = before - 900_000
    seed = f"5:0:{window_start}"
    await redis_client.client.set(key, seed)
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, ttl = result
    oracle_allowed = sliding_window_evaluate(limit, 5, 0, window_start, window_size, before)
    assert allowed == oracle_allowed == 0
    assert current_after == 5
    assert previous_after == 0
    assert start_after == window_start
    assert ttl == -1
    assert await _get_state(redis_client, key) == seed


async def test_sliding_window_near_window_end_allows(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-near-end")
    before = await _now_micro(redis_client)
    window_start = before - 900_000
    await redis_client.client.set(key, f"4:0:{window_start}")
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, _, _ = result
    oracle_allowed = sliding_window_evaluate(limit, 4, 0, window_start, window_size, before)
    assert allowed == oracle_allowed == 1
    assert current_after == 5
    assert previous_after == 0


async def test_sliding_window_rollover_shifts_counts(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-rollover")
    before = await _now_micro(redis_client)
    window_start = before - 1_500_000
    seed = f"5:5:{window_start}"
    await redis_client.client.set(key, seed)
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, ttl = result
    # The stored (pre-rollover) state is passed straight to the oracle: the
    # corrected rollover branch handles the shift and the partial remaining
    # time itself, matching the Lua's anchor-advanced semantics.
    shifted_start = window_start + window_size
    oracle_allowed = sliding_window_evaluate(limit, 5, 5, window_start, window_size, before)
    assert allowed == oracle_allowed == 1
    assert current_after == 1
    assert previous_after == 5
    assert start_after == shifted_start
    assert ttl == 2
    assert await _get_state(redis_client, key) == f"1:5:{shifted_start}"


async def test_sliding_window_rollover_allows_with_shifted_counts(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-rollover-allow")
    before = await _now_micro(redis_client)
    window_start = before - 1_500_000
    await redis_client.client.set(key, f"4:9:{window_start}")
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, _ = result
    shifted_start = window_start + window_size
    oracle_allowed = sliding_window_evaluate(limit, 4, 9, window_start, window_size, before)
    assert allowed == oracle_allowed == 1
    assert current_after == 1
    assert previous_after == 4
    assert start_after == shifted_start
    assert await _get_state(redis_client, key) == f"1:4:{shifted_start}"


async def test_sliding_window_beyond_two_windows_resets_counts(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-twowindows")
    before = await _now_micro(redis_client)
    window_start = before - 2_500_000
    await redis_client.client.set(key, f"5:9:{window_start}")
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, _ = result
    # Both counts expired and the anchor resets to now: a fresh window. The
    # stored-state oracle call resolves >= 2 windows itself.
    oracle_allowed = sliding_window_evaluate(limit, 5, 9, window_start, window_size, before)
    assert allowed == oracle_allowed == 1
    assert current_after == 1
    assert previous_after == 0
    assert 0 <= start_after - before <= CLOCK_DRIFT_MARGIN
    assert await _get_state(redis_client, key) == f"1:0:{start_after}"


async def test_sliding_window_multi_rollover_enforces_limit(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    """Many sequential requests across several rollovers stay limited.

    Deterministic and sleep-free: every burst is seeded 1.5 windows after the
    stored anchor (a rollover boundary), so the anchor must advance by exactly
    one window and the limit must keep being enforced burst after burst.
    Regression for the P0 review: a frozen anchor rolls over on every request,
    which would let the first request of each seed burst through and then
    reset counts forever.
    """
    limit = 3
    window_size = 1_000_000
    key = _unique_key("sw-multi-rollover")

    for _ in range(4):
        before = await _now_micro(redis_client)
        # A full current window plus a full previous window, arriving 1.5
        # windows after the anchor: exactly one rollover has elapsed.
        seed_start = before - 3 * window_size // 2
        await redis_client.client.set(key, f"3:3:{seed_start}")

        first = await loader.execute(
            SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
        )
        assert isinstance(first, list)
        shifted_start = seed_start + window_size
        oracle_allowed = sliding_window_evaluate(limit, 3, 3, seed_start, window_size, before)
        assert first[0] == oracle_allowed == 1
        assert first[1] == 1
        assert first[2] == 3
        assert first[3] == shifted_start

        second = await loader.execute(
            SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
        )
        assert isinstance(second, list)
        oracle_allowed = sliding_window_evaluate(limit, 1, 3, shifted_start, window_size, before)
        assert second[0] == oracle_allowed == 1
        assert second[1] == 2
        assert second[3] == shifted_start

        third = await loader.execute(
            SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
        )
        assert isinstance(third, list)
        oracle_allowed = sliding_window_evaluate(limit, 2, 3, shifted_start, window_size, before)
        assert third[0] == oracle_allowed == 0
        assert third[1] == 2
        assert third[3] == shifted_start

        # The denied request left the shifted state untouched.
        assert await _get_state(redis_client, key) == f"2:3:{shifted_start}"


async def test_sliding_window_beyond_two_windows_not_unlimited(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    """An idle-then-active tenant is never unlimited after two windows.

    The P0 review scenario: a seed two-plus windows in the past must reset to
    a fresh window anchored at now. A stale anchor (bug) kept resets firing on
    every request, so every request was allowed and the endpoint was
    effectively unlimited. With the fix, exactly `limit` requests are allowed
    after the reset, then the limiter denies persistently.
    """
    limit = 3
    window_size = 1_000_000
    key = _unique_key("sw-not-unlimited")
    before = await _now_micro(redis_client)
    seed_start = before - 3 * window_size
    await redis_client.client.set(key, f"3:3:{seed_start}")

    allows = 0
    last_start_after = 0
    for _ in range(8):
        result = await loader.execute(
            SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
        )
        assert isinstance(result, list)
        allowed, current_after, previous_after, start_after, ttl = result
        # The anchor reset to now and must never freeze at the stale seed.
        assert 0 <= start_after - before <= CLOCK_DRIFT_MARGIN
        last_start_after = start_after
        if allowed:
            allows += 1
        else:
            # Denied requests do not modify the rate-limit state.
            assert current_after == 3
            assert previous_after == 0
            assert ttl == -1
    assert allows == limit
    assert await _get_state(redis_client, key) == f"3:0:{last_start_after}"


async def test_sliding_window_fresh_window_allows(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-fresh")
    before = await _now_micro(redis_client)
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, ttl = result
    assert allowed == 1
    assert current_after == 1
    assert previous_after == 0
    assert 0 <= start_after - before <= CLOCK_DRIFT_MARGIN
    assert ttl == 2
    state = await _get_state(redis_client, key)
    assert state == f"1:0:{start_after}"


async def test_sliding_window_corrupt_state_is_treated_as_fresh(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-corrupt")
    before = await _now_micro(redis_client)
    await redis_client.client.set(key, "garbage")
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    allowed, current_after, previous_after, start_after, _ = result
    assert allowed == 1
    assert current_after == 1
    assert previous_after == 0
    assert 0 <= start_after - before <= CLOCK_DRIFT_MARGIN


async def test_sliding_window_deny_leaves_ttl_untouched(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    limit = 5
    window_size = 1_000_000
    key = _unique_key("sw-ttl")
    before = await _now_micro(redis_client)
    window_start = before - 400_000
    seed = f"5:0:{window_start}"
    await redis_client.client.set(key, seed)
    await redis_client.client.expire(key, 3600)
    result = await loader.execute(
        SLIDING_WINDOW_SCRIPT, keys=[key], args=[str(limit), str(window_size)]
    )
    assert isinstance(result, list)
    assert result[0] == 0
    assert result[4] == -1
    remaining = await redis_client.client.ttl(key)
    assert remaining >= 3599


async def test_execute_recovers_from_noscript_flush(
    redis_client: SentinelRedis, loader: ScriptLoader
) -> None:
    key = _unique_key("noscript")
    await redis_client.client.script_flush()
    first = await loader.execute(
        TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(2 * TOKENS_PER_TOKEN_MICRO), "0"]
    )
    assert isinstance(first, list)
    assert first[0] == 1
    await redis_client.client.script_flush()
    second = await loader.execute(
        TOKEN_BUCKET_SCRIPT, keys=[key], args=[str(2 * TOKENS_PER_TOKEN_MICRO), "0"]
    )
    assert isinstance(second, list)
    assert second[0] == 1
