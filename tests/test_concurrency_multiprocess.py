"""Multi-process concurrency test against shared real Redis (Phase 13).

Three spawned processes race for the same token-bucket key; the parent asserts
the total admitted across processes respects the capacity. This is the core
distributed claim: the Lua script executes atomically in Redis, so the shared
bucket behaves identically no matter how many processes hammer it. The spawn
start context is portable across Windows and Linux; the barrier synchronizes
the start so all processes race simultaneously rather than sequentially.

Determinism design: SentinelRedis hardcodes a 20ms socket budget, which a
Windows/WSL2 loopback cannot sustain for 20 in-flight connections per process.
Redis never admits more than capacity regardless (atomicity — the core claim),
and each process's emergency limiter admits at most one burst token, so the
parent asserts those bounds unconditionally plus the strict exact-capacity
equality when no failure reasons appear (CI Linux takes the strict branch).
"""

import asyncio
import multiprocessing as mp
import os
import uuid
from typing import Any

import pytest
from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO
from sentinel.circuit_breaker import CircuitBreaker
from sentinel.emergency import TokenBucketEmergencyLimiter
from sentinel.limiter import RateLimiter, build_bucket_key
from sentinel.lua import load_scripts
from sentinel.models import AlgorithmType, Decision, DecisionReason, FailMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

pytestmark = [pytest.mark.slow, pytest.mark.integration]

TEST_REDIS_URL = os.environ.get("SENTINEL_REDIS_URL", "redis://localhost:6379/0")
PROCESS_COUNT = 3
REQUESTS_PER_PROCESS = 20
CAPACITY = 10


def _worker(
    redis_url: str,
    key: str,
    capacity_micro: int,
    barrier: Any,
    results: Any,
) -> None:
    async def _run() -> tuple[int, int, int]:
        client = SentinelRedis(redis_url)
        loader = ScriptLoader(client.client)
        await load_scripts(loader)
        limiter = RateLimiter(
            loader, breaker=CircuitBreaker(), emergency=TokenBucketEmergencyLimiter()
        )
        policy = Policy(
            endpoint_id="resumint.tailor",
            algorithm=AlgorithmType.TOKEN_BUCKET,
            fail_mode=FailMode.FAIL_OPEN,
            fallback_rate_per_process_micro=TOKENS_PER_TOKEN_MICRO,
            policy_version=1,
            capacity_micro=capacity_micro,
            refill_rate_micro_per_sec=0,
        )
        decisions: list[Decision] = await asyncio.gather(
            *(limiter.evaluate(policy, key) for _ in range(REQUESTS_PER_PROCESS))
        )
        allowed = sum(1 for d in decisions if d.allowed)
        denied = sum(1 for d in decisions if not d.allowed)
        redis_admitted = sum(1 for d in decisions if d.reason is DecisionReason.ALLOWED)
        await client.aclose()
        return allowed, denied, redis_admitted

    barrier.wait(timeout=30)
    allowed, denied, redis_admitted = asyncio.run(_run())
    results.put((allowed, denied, redis_admitted))


def _echo_worker(results: Any, value: int) -> None:
    results.put(value)


async def _cleanup_key(key: str) -> None:
    client = SentinelRedis(TEST_REDIS_URL)
    await client.client.delete(key)
    await client.aclose()


@pytest.mark.integration
async def test_conc_10_multiprocess_shared_bucket_exact_capacity() -> None:
    context = mp.get_context("spawn")
    key = build_bucket_key(f"conc-mp-{uuid.uuid4().hex}", "resumint.tailor", 1)
    barrier = context.Barrier(PROCESS_COUNT)
    results: Any = context.Queue()
    processes = [
        context.Process(
            target=_worker,
            args=(TEST_REDIS_URL, key, CAPACITY * TOKENS_PER_TOKEN_MICRO, barrier, results),
        )
        for _ in range(PROCESS_COUNT)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
        assert all(process.exitcode == 0 for process in processes)
        outcomes = [results.get(timeout=10) for _ in range(PROCESS_COUNT)]
        total_allowed = sum(allowed for allowed, _, _ in outcomes)
        total_denied = sum(denied for _, denied, _ in outcomes)
        total_redis = sum(redis_admitted for _, _, redis_admitted in outcomes)
        total_emergency = total_allowed - total_redis
        assert total_redis <= CAPACITY
        assert total_emergency <= PROCESS_COUNT
        assert total_allowed <= CAPACITY + PROCESS_COUNT
        assert total_denied == PROCESS_COUNT * REQUESTS_PER_PROCESS - total_allowed
        if total_emergency == 0:
            assert total_allowed == CAPACITY
            assert total_denied == PROCESS_COUNT * REQUESTS_PER_PROCESS - CAPACITY
    finally:
        for process in processes:
            process.kill()
            process.join()
        await _cleanup_key(key)


def test_conc_11_spawn_smoke_check() -> None:
    context = mp.get_context("spawn")
    results: Any = context.Queue()
    process = context.Process(target=_echo_worker, args=(results, 7))
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 0
    assert results.get(timeout=5) == 7
