# Sentinel Phase 14 — Baseline Benchmark Results

Reported as-is per vision §12: single-machine Docker-Compose loopback topology, disclosed, not
implied scale. No thresholds are asserted anywhere in this document — these numbers exist to be
regression-compared against later runs.

## Environment (recorded by the harness)

| Key | Value |
|---|---|
| git commit | `51b8af4614f7466cd541d749538dc25b5e79efbd` (branch `bench/perf-phase14`) |
| Platform | Windows-11-10.0.26200-SP0 |
| Python | 3.13.7 |
| CPU | Intel64 Family 6 Model 186 Stepping 2, GenuineIntel (16 logical cores) |
| Redis | 7.4.9 (Docker Compose, `localhost:6379/0`, `noeviction`, bounded `maxmemory`) |
| Timestamp | 2026-08-15T22:18:08+0530 |
| Method | `benchmarks/benchmark.py`, 3 reps/cell, median-of-reps, 100 ops/batch |

## Cell legend

- **B1** unguarded handler (HTTP baseline)
- **B2** with-Sentinel, token bucket · **B3** with-Sentinel, sliding window (full HTTP journey)
- **B4** token bucket, detached · **B5** sliding window, detached (no HTTP/ASGI layer)
- **B6** auth + `decide` only (no Redis round trip)
- **B7** breaker-OPEN short-circuit (pre-tripped)
- **B8** dead-port fail-open · **B9** dead-port fail-closed (failure-path latency)

Policy parameters (invariant #6: time made irrelevant): token bucket `capacity_micro = 2**30`,
`refill_rate_micro_per_sec = 0`, fresh `uuid4` bucket key per run → every op ALLOWED
(`over_limit == 0` asserted for every cell); sliding window `limit = 1000`,
`window_size_micro = 60_000_000`. Fallback rate `1_000_000` µtokens/s. Fresh JWT `sub` per batch.
All cells hit the real `SentinelGuard`/`RateLimiter`/breaker/emergency against real Redis
(`ASGITransport` in-process; the dead-port cells use a dedicated client at `localhost:6399`).

## Results

### Throughput and latency

| Cell | c=1 ops/s | c=1 p50/p95/p99 (µs) | c=8 ops/s | c=8 p50/p95/p99 (µs) |
|---|---|---|---|---|
| B1 unguarded | 5946.5 | 150 / 260 / 402 | 5790.6 | 153 / 267 / 446 |
| B2 token bucket | 1141.7 | 827 / 1115 / 1327 | 2158.0 | 3666 / 4235 / 4699 |
| B3 sliding window | 1150.7 | 832 / 1107 / 1378 | 2143.8 | 3702 / 4307 / 5013 |
| B4 token bucket, detached | 2188.4 | 440 / 590 / 777 | 8322.0 | 868 / 1387 / 1915 |
| B5 sliding window, detached | 2126.9 | 453 / 598 / 771 | 8018.0 | 901 / 1474 / 1850 |
| B6 auth + decide only | 2278.4 | 422 / 570 / 755 | 8965.5 | 805 / 1321 / 1600 |
| B7 breaker OPEN (short-circuit) | 96203.8 | 7 / 11 / 57 | 88791.0 | 8 / 13 / 28 |
| B8 dead-port fail-open | 3178.9 | 7 / 12 / **27243** | 8431.7 | 7 / 21 / **28236** |
| B9 dead-port fail-closed | 3238.3 | 5 / 7 / **26520** | 8139.7 | 5 / 10 / **25791** |

### CPU utilization (percent, median of reps)

| Cell | c=1 API / Redis | c=8 API / Redis |
|---|---|---|
| B1 | 98.1 / 0.4 | 97.0 / 0.3 |
| B2 | 64.0 / 8.3 | 98.1 / 14.0 |
| B3 | 60.2 / 9.0 | 99.2 / 14.2 |
| B4 | 43.8 / 14.0 | 99.6 / 42.4 |
| B5 | 39.2 / 14.4 | 92.7 / 42.7 |
| B6 | 42.4 / 14.4 | 98.0 / 43.5 |
| B7 | 300.6* / 0.0 | 0.0* / 0.0 |
| B8 | 9.9 / 0.0 | 26.3 / 0.0 |
| B9 | 10.1 / 0.0 | 25.4 / 0.0 |

\* `time.process_time()` on Windows is quantized to ~15.6 ms scheduler ticks; in short
measurement windows the per-cell API CPU is unreliable (B7 c=1 shows a 300.6% tick artifact and
B7 c=8 shows 0.0%). Treat all API-CPU cells as order-of-magnitude only.

### Error rates (decision-reason counts, c=1 / c=8)

| Cell | counts |
|---|---|
| B7 | `circuit_open` 3 / 3, `emergency_local_limit` 1497 / 1497 |
| B8 | `circuit_open` 5 / 5, `emergency_local_limit` 1492 / 1492, `redis_timeout` 3 / 3 |
| B9 | `circuit_open` 1485 / 1464, `fail_closed` 15 / 36 |

`over_limit` (rate-limit denials) = 0 for all 18 cells.

## Interpretation

- **With-Sentinel overhead at c=1: ~5.2× throughput (5946 → 1142 ops/s), ~+680 µs p50 (150 →
  827 µs).** One loopback Redis round trip + JWT + decide dominates the journey (B2 − B6 ≈
  400 µs p50; B6 − B1 ≈ 270 µs p50). Redis is local; a remote Redis adds its own RTT to every
  row except B6/B7.
- **The breaker short-circuit is nearly free:** B7 p50 = 7 µs, ~96k ops/s — the fail-open
  emergency decision (not the breaker) is what the failure path pays for.
- **Failure-path latency ≈ the socket timeout, not the limiter:** B8/B9 p99 ≈ 27 ms on the
  dead port (Windows connect timeout ~31 ms; Phase 13's documented
  `REDIS_TIMEOUT`-on-Windows / `REDIS_CONNECTION_ERROR`-on-Linux boundary applies). Fail-closed
  (B9) pays the same latency and then returns 503; fail-open (B8) absorbs it into the emergency
  decision.
- **c=8 p50 inflation in B2/B3 (3.7 ms) is an in-process serialization artifact** of 8 concurrent
  in-flight ops over one asyncio loop + loopback Redis (same artifact shape at B4–B6, scaled);
  throughput still improves ~1.9–3.8× over c=1. Real multi-worker deployment spreads this.
- **Redis CPU tracks live-cell load only** (0.3–14% at c=1, up to 43.5% at c=8); failure cells
  never touch Redis (0.0%).

## Disclosed finding — emergency limiter sustained-rate deviation (~2.3×)

The B8 counts (`circuit_open` 5, not the ~2 a 1-token burst + refill would predict) triggered an
investigation that confirmed a Phase 8/10 production defect in the **fail-open** emergency path:

- **Symptom:** under sustained Redis failure, `TokenBucketEmergencyLimiter` admits up to ~2.3× the
  configured `fallback_rate_per_process_micro` (measured: 7 allows in 3 s vs the Lua's 3 at
  1 token/s, 100 ms call cadence).
- **Root cause:** `sentinel/emergency.py:61` persists `(tokens_after, last_refill_after)` on every
  call, but `token_bucket_evaluate` advances `last_refill_micro` only on ALLOW
  (`sentinel/algorithms.py:31`). Each denied call therefore adds the full elapsed refill on top of
  tokens that already contain it — accumulated tokens grow as Σ elapsedᵢ, not true elapsed. The
  Lua script (the canonical, parity-tested implementation) is correct because "denied requests
  never write" (`sentinel/lua/token_bucket.lua:8`); the main limiter and fail-closed paths are
  unaffected; `over_limit == 0` here is unaffected (fresh keys, no refill).
- **Why the suites never caught it:** `test_emergency.py::test_parity_with_token_bucket_evaluate`
  drives the reference with the same store-everything pattern (self-consistent with the bug);
  Lua-parity tests are single-step; Phase 13 concurrency bursts are ms-scale where refill ≈ 0.
- **Handling:** Phase 14 is benchmark-only (zero production-code changes, `git diff sentinel/`
  empty). The fix — mirror the Lua's no-write-on-deny in the emergency limiter, update the
  self-consistent parity test, add a sustained-denial regression test — ships in a separate PR
  after this phase merges. Until then, fail-open deployments should treat
  `fallback_rate_per_process_micro` as approximate under sustained outages.
