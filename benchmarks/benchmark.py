"""Phase 14 benchmark harness: throughput, p50/p95/p99 latency, CPU, error rates.

Executed plan: docs/phase-14-plan.md. Cells B4-B6 measure the limiter and the
Redis boundary, B1-B3 the full HTTP path with and without Sentinel, and B7-B9
the failure paths. Results are JSON with the environment disclosed; no
performance thresholds are asserted anywhere.
"""

import argparse
import asyncio
import contextlib
import json
import os
import platform
import statistics
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import Depends, FastAPI, Request
from pydantic import SecretStr
from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO
from sentinel.circuit_breaker import BreakerState, CircuitBreaker
from sentinel.config import AppConfig, SentinelConfig
from sentinel.emergency import TokenBucketEmergencyLimiter
from sentinel.http import SentinelGuard
from sentinel.limiter import RateLimiter, build_bucket_key
from sentinel.lua import TOKEN_BUCKET_SCRIPT, load_scripts, script_source
from sentinel.models import AlgorithmType, FailMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

ENDPOINT_ID = "bench.endpoint"
POLICY_VERSION = 1
JWT_SECRET = "benchmark-secret-0123456789abcdef0123456789abcdef"
JWT_ALGORITHMS = frozenset({"HS256"})
FALLBACK_RATE_PER_PROCESS_MICRO = 1_000_000
TOKEN_CAPACITY_MICRO = 2**30
SLIDING_LIMIT = 1_000
SLIDING_WINDOW_MICRO = 60_000_000
BATCH_OPS = 100
WARMUP_OPS = 100
REPS = 3
CONCURRENCIES = (1, 8)
DEAD_REDIS_URL = "redis://localhost:6399/0"
OP_COUNTS = {"http": 2_000, "limiter": 5_000, "redis": 10_000, "failure": 500}
SMOKE_OP_COUNTS = {"http": 50, "limiter": 50, "redis": 50, "failure": 20}


@dataclass(frozen=True)
class CellSpec:
    id: str
    name: str
    path: str
    algorithm: str
    redis: str
    concurrency: int
    op_count: int
    warmup_ops: int
    policy: Policy | None


@dataclass
class Measurement:
    latencies_us: list[int]
    counts: dict[str, int]
    allowed_by_key: dict[str, int]
    keys: set[str]
    wall_seconds: float
    api_cpu_seconds: float
    redis_cpu_seconds: float


Worker = Callable[[int], Awaitable[tuple[str, str | None, bool]]]


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def environment_info(redis_version: str) -> dict[str, Any]:
    return {
        "git_commit": _git_commit(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count() or 0,
        "redis_version": redis_version,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _redis_cpu_seconds(info: dict[str, Any]) -> float:
    user = float(info.get("used_cpu_user", "0") or "0")
    sys_cpu = float(info.get("used_cpu_sys", "0") or "0")
    return user + sys_cpu


def _quantiles(latencies_us: list[int]) -> dict[str, float]:
    ordered = sorted(latencies_us)
    percentiles = statistics.quantiles(ordered, n=100)
    return {
        "p50_us": percentiles[49],
        "p95_us": percentiles[94],
        "p99_us": percentiles[98],
    }


async def _measure(
    workers: list[Worker],
    op_count: int,
    redis_client: SentinelRedis | None,
) -> Measurement:
    latencies: list[int] = []
    counts: dict[str, int] = {}
    allowed_by_key: dict[str, int] = {}
    keys: set[str] = set()
    per_worker, remainder = divmod(op_count, len(workers))
    workloads = [per_worker + (1 if index < remainder else 0) for index in range(len(workers))]

    async def run_worker(worker: Worker, count: int) -> None:
        for seq in range(count):
            started = time.perf_counter_ns()
            bucket, key, allowed = await worker(seq)
            latencies.append((time.perf_counter_ns() - started) // 1_000)
            counts[bucket] = counts.get(bucket, 0) + 1
            if key is not None:
                keys.add(key)
                if allowed:
                    allowed_by_key[key] = allowed_by_key.get(key, 0) + 1

    cpu_before = time.process_time()
    wall_before = time.perf_counter()
    redis_cpu_before = 0.0
    if redis_client is not None:
        redis_cpu_before = _redis_cpu_seconds(await redis_client.client.info("cpu"))
    await asyncio.gather(
        *[run_worker(worker, count) for worker, count in zip(workers, workloads, strict=True)]
    )
    wall_after = time.perf_counter()
    api_cpu_seconds = time.process_time() - cpu_before
    redis_cpu_seconds = 0.0
    if redis_client is not None:
        redis_cpu_seconds = (
            _redis_cpu_seconds(await redis_client.client.info("cpu")) - redis_cpu_before
        )
    return Measurement(
        latencies_us=latencies,
        counts=counts,
        allowed_by_key=allowed_by_key,
        keys=keys,
        wall_seconds=wall_after - wall_before,
        api_cpu_seconds=api_cpu_seconds,
        redis_cpu_seconds=redis_cpu_seconds,
    )


def _capacity_tokens(policy: Policy) -> int:
    if policy.algorithm is AlgorithmType.TOKEN_BUCKET:
        assert policy.capacity_micro is not None
        return policy.capacity_micro // TOKENS_PER_TOKEN_MICRO
    assert policy.limit is not None
    return policy.limit


def _over_limit(measurement: Measurement, policy: Policy | None) -> int:
    if policy is None:
        return 0
    capacity = _capacity_tokens(policy)
    return sum(max(0, allowed - capacity) for allowed in measurement.allowed_by_key.values())


def _token_bucket_policy(fail_mode: FailMode) -> Policy:
    return Policy(
        endpoint_id=ENDPOINT_ID,
        capacity_micro=TOKEN_CAPACITY_MICRO,
        refill_rate_micro_per_sec=0,
        algorithm=AlgorithmType.TOKEN_BUCKET,
        fail_mode=fail_mode,
        fallback_rate_per_process_micro=FALLBACK_RATE_PER_PROCESS_MICRO,
        policy_version=POLICY_VERSION,
    )


def _sliding_window_policy() -> Policy:
    return Policy(
        endpoint_id=ENDPOINT_ID,
        algorithm=AlgorithmType.SLIDING_WINDOW,
        fail_mode=FailMode.FAIL_CLOSED,
        fallback_rate_per_process_micro=FALLBACK_RATE_PER_PROCESS_MICRO,
        policy_version=POLICY_VERSION,
        limit=SLIDING_LIMIT,
        window_size_micro=SLIDING_WINDOW_MICRO,
    )


def _limiter_worker(limiter: RateLimiter, policy: Policy, run_id: str, worker_id: int) -> Worker:
    async def op(seq: int) -> tuple[str, str | None, bool]:
        tenant = f"bench-{run_id}-w{worker_id}-b{seq // BATCH_OPS}"
        key = build_bucket_key(tenant, ENDPOINT_ID, POLICY_VERSION)
        decision = await limiter.evaluate(policy, key)
        return decision.reason.value, key, decision.allowed

    return op


def _redis_worker(loader: ScriptLoader, run_id: str, worker_id: int) -> Worker:
    async def op(seq: int) -> tuple[str, str | None, bool]:
        tenant = f"bench-{run_id}-w{worker_id}-b{seq // BATCH_OPS}"
        key = build_bucket_key(tenant, ENDPOINT_ID, POLICY_VERSION)
        result = await loader.execute(TOKEN_BUCKET_SCRIPT, [key], [str(TOKEN_CAPACITY_MICRO), "0"])
        assert isinstance(result, list)
        return "evalsha", key, bool(result[0])

    return op


def _token(sub: str) -> str:
    return jwt.encode({"sub": sub, "exp": int(time.time()) + 3_600}, JWT_SECRET, algorithm="HS256")


def _http_worker(
    client: httpx.AsyncClient, run_id: str, worker_id: int, with_guard: bool
) -> Worker:
    tokens: dict[str, str] = {}

    async def op(seq: int) -> tuple[str, str | None, bool]:
        tenant = f"bench-{run_id}-w{worker_id}-b{seq // BATCH_OPS}"
        token = tokens.get(tenant)
        if token is None:
            token = _token(tenant)
            tokens[tenant] = token
        headers = {"Authorization": f"Bearer {token}"} if with_guard else {}
        response = await client.post("/bench", headers=headers)
        if not with_guard:
            return str(response.status_code), None, False
        key = build_bucket_key(tenant, ENDPOINT_ID, POLICY_VERSION)
        return str(response.status_code), key, response.status_code == 200

    return op


def _make_app(
    redis_url: str,
    policy: Policy | None,
    live_redis: SentinelRedis,
    with_guard: bool,
) -> tuple[FastAPI, SentinelGuard | None]:
    if not with_guard:
        app = FastAPI()

        @app.post("/bench")
        async def route() -> dict[str, bool]:
            return {"allowed": True}

        return app, None
    assert policy is not None
    config = SentinelConfig(
        app=AppConfig(
            redis_url=redis_url,
            jwt_secret=SecretStr(JWT_SECRET),
            jwt_algorithm_allowlist=JWT_ALGORITHMS,
        ),
        policies={ENDPOINT_ID: policy},
    )
    loader = ScriptLoader(live_redis.client)
    guard = SentinelGuard(config, live_redis, loader)
    app = FastAPI()

    @app.post("/bench")
    async def route(
        request: Request, _: None = Depends(guard.guard_for(ENDPOINT_ID))
    ) -> dict[str, bool]:
        return {"allowed": request.state.decision.allowed}

    return app, guard


async def _cleanup(redis_client: SentinelRedis, keys: set[str]) -> None:
    ordered = sorted(keys)
    for index in range(0, len(ordered), 500):
        chunk = ordered[index : index + 500]
        with contextlib.suppress(Exception):
            await redis_client.client.delete(*chunk)


async def _run_measured(
    spec: CellSpec,
    redis_client: SentinelRedis | None,
    measured_workers: list[Worker],
    warmup_workers: list[Worker],
) -> dict[str, Any]:
    keys: set[str] = set()
    if spec.warmup_ops > 0:
        warmup = await _measure(warmup_workers, spec.warmup_ops, redis_client)
        keys |= warmup.keys
    measured = await _measure(measured_workers, spec.op_count, redis_client)
    keys |= measured.keys
    if redis_client is not None:
        await _cleanup(redis_client, keys)
    return _rep_result(spec, measured)


def _rep_result(spec: CellSpec, measurement: Measurement) -> dict[str, Any]:
    wall = measurement.wall_seconds
    latencies = measurement.latencies_us
    return {
        "ops": spec.op_count,
        "wall_seconds": wall,
        "throughput_ops_per_sec": spec.op_count / wall if wall > 0 else 0.0,
        "latency_us": _quantiles(latencies) if len(latencies) >= 2 else {},
        "mean_us": statistics.fmean(latencies) if latencies else 0.0,
        "min_us": min(latencies) if latencies else 0,
        "max_us": max(latencies) if latencies else 0,
        "api_cpu_percent": measurement.api_cpu_seconds / wall * 100 if wall > 0 else 0.0,
        "redis_cpu_percent": measurement.redis_cpu_seconds / wall * 100 if wall > 0 else 0.0,
        "counts": measurement.counts,
        "over_limit": _over_limit(measurement, spec.policy),
    }


def _dead_limiter() -> tuple[RateLimiter, CircuitBreaker]:
    loader = ScriptLoader(SentinelRedis(DEAD_REDIS_URL).client)
    loader._sources[TOKEN_BUCKET_SCRIPT] = script_source(TOKEN_BUCKET_SCRIPT)
    loader._shas[TOKEN_BUCKET_SCRIPT] = "sha-dead"
    breaker = CircuitBreaker()
    limiter = RateLimiter(
        loader,
        breaker=breaker,
        emergency=TokenBucketEmergencyLimiter(),
    )
    return limiter, breaker


async def _run_rep(
    spec: CellSpec, live_redis: SentinelRedis, redis_url: str, rep_id: str
) -> dict[str, Any]:
    if spec.path in ("http_unguarded", "http_guarded"):
        with_guard = spec.path == "http_guarded"
        app, guard = _make_app(redis_url, spec.policy, live_redis, with_guard)
        if guard is not None:
            await guard.load_scripts()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            measured = [
                _http_worker(client, rep_id, index, with_guard) for index in range(spec.concurrency)
            ]
            warmup = [
                _http_worker(client, f"{rep_id}-warmup", index, with_guard)
                for index in range(spec.concurrency)
            ]
            return await _run_measured(spec, live_redis, measured, warmup)
    if spec.path == "limiter":
        loader = ScriptLoader(live_redis.client)
        await load_scripts(loader)
        limiter = RateLimiter(
            loader,
            breaker=CircuitBreaker(),
            emergency=TokenBucketEmergencyLimiter(),
        )
        assert spec.policy is not None
        measured = [
            _limiter_worker(limiter, spec.policy, rep_id, index)
            for index in range(spec.concurrency)
        ]
        warmup = [
            _limiter_worker(limiter, spec.policy, f"{rep_id}-warmup", index)
            for index in range(spec.concurrency)
        ]
        return await _run_measured(spec, live_redis, measured, warmup)
    if spec.path == "redis_floor":
        loader = ScriptLoader(live_redis.client)
        await load_scripts(loader)
        measured = [_redis_worker(loader, rep_id, index) for index in range(spec.concurrency)]
        warmup = [
            _redis_worker(loader, f"{rep_id}-warmup", index) for index in range(spec.concurrency)
        ]
        return await _run_measured(spec, live_redis, measured, warmup)
    if spec.path == "failure":
        assert spec.policy is not None
        limiter, breaker = _dead_limiter()
        if spec.id == "B7":
            trip_key = build_bucket_key(f"bench-{rep_id}-trip", ENDPOINT_ID, POLICY_VERSION)
            while breaker.state is not BreakerState.OPEN:
                await limiter.evaluate(spec.policy, trip_key)
        measured = [
            _limiter_worker(limiter, spec.policy, rep_id, index)
            for index in range(spec.concurrency)
        ]
        warmup = [
            _limiter_worker(limiter, spec.policy, f"{rep_id}-warmup", index)
            for index in range(spec.concurrency)
        ]
        return await _run_measured(spec, None, measured, warmup)
    raise ValueError(f"no runner for cell path {spec.path!r}")


def _policy_brief(policy: Policy | None) -> dict[str, Any]:
    if policy is None:
        return {}
    if policy.algorithm is AlgorithmType.TOKEN_BUCKET:
        return {
            "algorithm": policy.algorithm.value,
            "capacity_micro": policy.capacity_micro,
            "refill_rate_micro_per_sec": policy.refill_rate_micro_per_sec,
            "fail_mode": policy.fail_mode.value,
            "fallback_rate_per_process_micro": policy.fallback_rate_per_process_micro,
        }
    return {
        "algorithm": policy.algorithm.value,
        "limit": policy.limit,
        "window_size_micro": policy.window_size_micro,
        "fail_mode": policy.fail_mode.value,
        "fallback_rate_per_process_micro": policy.fallback_rate_per_process_micro,
    }


def _build_cells(op_counts: dict[str, int]) -> list[CellSpec]:
    fail_open = _token_bucket_policy(FailMode.FAIL_OPEN)
    fail_closed = _token_bucket_policy(FailMode.FAIL_CLOSED)
    sliding = _sliding_window_policy()
    cells: list[CellSpec] = []
    for concurrency in CONCURRENCIES:
        cells.append(
            CellSpec(
                "B1",
                "unguarded HTTP (no Sentinel)",
                "http_unguarded",
                "none",
                "live",
                concurrency,
                op_counts["http"],
                WARMUP_OPS,
                None,
            )
        )
        cells.append(
            CellSpec(
                "B2",
                "guarded HTTP token bucket",
                "http_guarded",
                "token_bucket",
                "live",
                concurrency,
                op_counts["http"],
                WARMUP_OPS,
                fail_open,
            )
        )
        cells.append(
            CellSpec(
                "B3",
                "guarded HTTP sliding window",
                "http_guarded",
                "sliding_window",
                "live",
                concurrency,
                op_counts["http"],
                WARMUP_OPS,
                sliding,
            )
        )
        cells.append(
            CellSpec(
                "B4",
                "RateLimiter token bucket",
                "limiter",
                "token_bucket",
                "live",
                concurrency,
                op_counts["limiter"],
                WARMUP_OPS,
                fail_open,
            )
        )
        cells.append(
            CellSpec(
                "B5",
                "RateLimiter sliding window",
                "limiter",
                "sliding_window",
                "live",
                concurrency,
                op_counts["limiter"],
                WARMUP_OPS,
                sliding,
            )
        )
        cells.append(
            CellSpec(
                "B6",
                "EVALSHA token bucket",
                "redis_floor",
                "token_bucket",
                "live",
                concurrency,
                op_counts["redis"],
                WARMUP_OPS,
                fail_open,
            )
        )
        cells.append(
            CellSpec(
                "B7",
                "breaker OPEN short-circuit",
                "failure",
                "token_bucket",
                "dead",
                concurrency,
                op_counts["failure"],
                WARMUP_OPS,
                fail_open,
            )
        )
        cells.append(
            CellSpec(
                "B8",
                "fail-open dead Redis",
                "failure",
                "token_bucket",
                "dead",
                concurrency,
                op_counts["failure"],
                0,
                fail_open,
            )
        )
        cells.append(
            CellSpec(
                "B9",
                "fail-closed dead Redis",
                "failure",
                "token_bucket",
                "dead",
                concurrency,
                op_counts["failure"],
                0,
                fail_closed,
            )
        )
    return cells


def _aggregate(reps: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "throughput_ops_per_sec",
        "mean_us",
        "min_us",
        "max_us",
        "api_cpu_percent",
        "redis_cpu_percent",
    ):
        result[key] = statistics.median([rep[key] for rep in reps])
    result["latency_us"] = {
        percentile: statistics.median([rep["latency_us"][percentile] for rep in reps])
        for percentile in ("p50_us", "p95_us", "p99_us")
    }
    count_keys: set[str] = set()
    for rep in reps:
        count_keys |= set(rep["counts"])
    result["counts"] = {
        key: sum(rep["counts"].get(key, 0) for rep in reps) for key in sorted(count_keys)
    }
    result["over_limit"] = sum(rep["over_limit"] for rep in reps)
    result["ops_total"] = sum(rep["ops"] for rep in reps)
    result["wall_seconds_total"] = sum(rep["wall_seconds"] for rep in reps)
    return result


async def _run_all(args: argparse.Namespace) -> dict[str, Any]:
    live = SentinelRedis(args.redis_url)
    try:
        await live.assert_noeviction()
        server_info = await live.client.info("server")
        redis_version = str(server_info.get("redis_version", "unknown"))
        run_id = uuid.uuid4().hex[:8]
        op_counts = SMOKE_OP_COUNTS if args.smoke else OP_COUNTS
        reps = 1 if args.smoke else args.reps
        cell_results: list[dict[str, Any]] = []
        for spec in _build_cells(op_counts):
            rep_results: list[dict[str, Any]] = []
            for rep in range(reps):
                rep_id = f"{run_id}-{spec.id}-c{spec.concurrency}-r{rep}"
                rep_results.append(await _run_rep(spec, live, args.redis_url, rep_id))
            cell_results.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "path": spec.path,
                    "algorithm": spec.algorithm,
                    "redis": spec.redis,
                    "concurrency": spec.concurrency,
                    "ops": spec.op_count,
                    "warmup_ops": spec.warmup_ops,
                    "policy": _policy_brief(spec.policy),
                    "reps": rep_results,
                    "aggregate": _aggregate(rep_results),
                }
            )
        return {
            "environment": environment_info(redis_version),
            "smoke": args.smoke,
            "reps": reps,
            "concurrencies": list(CONCURRENCIES),
            "cells": cell_results,
        }
    finally:
        await live.aclose()


def _print_summary(report: dict[str, Any]) -> None:
    for cell in report["cells"]:
        aggregate = cell["aggregate"]
        latency = aggregate["latency_us"]
        print(
            f"{cell['id']} c={cell['concurrency']}: "
            f"{aggregate['throughput_ops_per_sec']:.1f} ops/s  "
            f"p50={latency['p50_us']:.0f}us p95={latency['p95_us']:.0f}us "
            f"p99={latency['p99_us']:.0f}us  api_cpu={aggregate['api_cpu_percent']:.1f}%  "
            f"redis_cpu={aggregate['redis_cpu_percent']:.1f}%  over_limit={aggregate['over_limit']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Phase 14 benchmark harness")
    parser.add_argument("--smoke", action="store_true", help="tiny op counts for CI sanity runs")
    parser.add_argument("--reps", type=int, default=REPS, help="repetitions per cell")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON output path (default: benchmarks/results/<timestamp>-<sha>.json)",
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("SENTINEL_REDIS_URL", "redis://localhost:6379/0"),
    )
    args = parser.parse_args()
    report = asyncio.run(_run_all(args))
    if args.out is None:
        sha = report["environment"]["git_commit"]
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        out = Path("benchmarks") / "results" / f"{timestamp}-{sha}.json"
    else:
        out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print_summary(report)
    print(f"report written to {out}")


if __name__ == "__main__":
    main()
