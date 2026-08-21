# Sentinel — Failure Handling

_Updated for v1.2.0. The frozen decision table lives in project record §06; this
document walks the shipped machinery — classification, circuit breaker, emergency limiter, HTTP
semantics, and the measured failure-path latency — with code references._

Sentinel's failure philosophy, stated once: **a Redis failure never causes a request to wait
beyond the configured timeout, and every failure resolves to a row in the decision table.** No
bare `except:`, no silent fallbacks, no unbounded fail-open.

---

## 1 · The decision table

Every evaluation ends in exactly one `DecisionReason` (8 members, `sentinel/models.py:34`):

| Redis outcome                  | Fail-open (Resumint)                    | Fail-closed (PDFTalk)                   |
| ------------------------------ | --------------------------------------- | --------------------------------------- |
| Success                        | Lua result (`ALLOWED` / `RATE_LIMITED`) | Lua result (`ALLOWED` / `RATE_LIMITED`) |
| Timeout (20 ms)                | Emergency local limiter                 | Deny → `FAIL_CLOSED`                    |
| Connection error               | Emergency local limiter                 | Deny → `FAIL_CLOSED`                    |
| `NOSCRIPT` (re-load exhausted) | Emergency local limiter                 | Deny → `FAIL_CLOSED`                    |
| Circuit breaker OPEN           | Emergency local limiter                 | Deny → `CIRCUIT_OPEN`                   |

The `fail_mode` comes from the endpoint's `Policy` — it is a product decision per integration,
not a global setting (ADR-006): PDFTalk's expensive OCR fails closed to protect compute;
Resumint's UX-sensitive tailoring fails open but never unlimited.

---

## 2 · Classification (`sentinel/errors.py`)

`classify_redis_error(exc) -> DecisionReason` maps the failure to a bounded reason
(`sentinel/errors.py:14`):

| Exception                                         | Reason                   |
| ------------------------------------------------- | ------------------------ |
| `redis.exceptions.TimeoutError`                   | `REDIS_TIMEOUT`          |
| `redis.exceptions.ConnectionError`                | `REDIS_CONNECTION_ERROR` |
| `ScriptMissingError` (NOSCRIPT re-load exhausted) | `REDIS_NOSCRIPT_RETRY`   |
| Any other `RedisError`                            | `REDIS_CONNECTION_ERROR` |

`RateLimiter.evaluate` catches only `RedisError` (`sentinel/limiter.py:147`). Programming
errors — `KeyError`, `RuntimeError`, the unloaded-scripts guard — are never caught and
propagate to the host app: a programming bug must not be mistaken for a Redis outage.

**NOSCRIPT recovery** (`sentinel/redis.py:68`): `ScriptLoader` runs `EVALSHA`; on
`NoScriptError` it re-`SCRIPT LOAD`s the source and re-runs once. If the script is still
missing (e.g. script cache flushed mid-flight again), it raises `ScriptMissingError` — which is
itself a classified failure reason, not a crash.

---

## 3 · Circuit breaker (`sentinel/circuit_breaker.py`)

A per-process CLOSED / OPEN / HALF_OPEN state machine guarding the Redis boundary:

- **CLOSED** — calls reach Redis; each failure counts; **5 consecutive failures** (default
  `FAILURE_THRESHOLD`, `sentinel/circuit_breaker.py:16`) trip OPEN. Any genuine Redis success
  resets the count.
- **OPEN** — every evaluation short-circuits with `CIRCUIT_OPEN` _before_ any Redis call
  (`sentinel/limiter.py:143`), for the quarantine window (30 s default, `OPEN_TIMEOUT_SECONDS`).
- **HALF_OPEN** — entered lazily after the quarantine; each arriving call is a probe. Success →
  CLOSED (count reset); failure → OPEN with a fresh quarantine.

Deliberate properties:

- **Per-process, not distributed** (ADR-007): N API instances have N independent breakers.
  Instance-targeted abuse is capped by the emergency limiter regardless of which instance a
  request lands on.
- **Local monotonic clock** (`sentinel/circuit_breaker.py:29`): the documented exception to
  the Redis-clock invariant — the breaker exists precisely because Redis may be unreachable.
- **Only genuine successes reset** the failure count — a fail-open allowance via the emergency
  limiter is not a Redis success and leaves the breaker alone.

---

## 4 · Emergency limiter (`sentinel/emergency.py`)

The fail-open cap: a per-process, in-memory token bucket keyed by `endpoint_id` only.

- Capacity = refill rate = `fallback_rate_per_process_micro` — a burst of one second's worth
  of the fallback rate, then sustained at the fallback rate.
- **No-write-on-deny** (`sentinel/emergency.py:70`): bucket state is persisted only on ALLOW,
  mirroring the Lua's "denied requests never write" contract. This is the Phase 14
  double-refill fix — before it, denied calls banked partial refills and sustained fail-open
  allowance reached ~2.3× the configured rate.
- Denied → `EMERGENCY_LOCAL_LIMIT` with `remaining_micro` and `retry_after_seconds`.
- Local monotonic clock (`sentinel/emergency.py:42`) — same documented exception as the
  breaker.
- Fail-open requests allowed by the emergency limiter carry the _cause_ as the reason
  (`CIRCUIT_OPEN`, `REDIS_TIMEOUT`, ...) with `allowed=True` (`sentinel/limiter.py:173`) — the
  decision table stays lossless: metrics count them, the deny log does not.

Limits of the emergency limiter (see [known-limitations.md](known-limitations.md)): per-process allowance
multiplies with instance count, and buckets are per-endpoint — no cross-tenant fairness during
an outage.

---

## 5 · HTTP semantics (`sentinel/http.py`)

`_denied_status` (`sentinel/http.py:47`) maps denied reasons to status codes:

| HTTP status             | Reasons                                                                                          | Body / headers                                                              |
| ----------------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| 429 Too Many Requests   | `RATE_LIMITED`, `EMERGENCY_LOCAL_LIMIT`                                                          | `detail="rate limit exceeded"`; `Retry-After` when the decision carries one |
| 503 Service Unavailable | `FAIL_CLOSED`, `CIRCUIT_OPEN`, `REDIS_TIMEOUT`, `REDIS_CONNECTION_ERROR`, `REDIS_NOSCRIPT_RETRY` | `detail="rate limiter unavailable"`                                         |

`Retry-After` is emitted only when the decision provides it: the token-bucket strategy computes
`ceil((1_000_000 - tokens_after) / rate)` (min 1, `sentinel/limiter.py:74`); the sliding-window
strategy deliberately returns none — the Lua result does not expose enough timing information
for a precise value (`sentinel/limiter.py:106`).

Auth failures (missing/invalid/expired token) return 401 with `WWW-Authenticate: Bearer`
_before_ the resolver or limiter run (`sentinel/http.py:110`) — they never produce a
`DecisionReason`, never touch Redis, and emit no log or metric.

---

## 6 · Measured failure-path latency

Phase 14 baseline, disclosed as-is (single-machine Docker-Compose loopback; [benchmark-results.md](benchmark-results.md)):

| Cell  | Journey                                                                                | p50                 | p99        |
| ----- | -------------------------------------------------------------------------------------- | ------------------- | ---------- |
| B7    | Breaker-OPEN short-circuit (pre-tripped)                                               | ≈ 7 µs (~96k ops/s) | —          |
| B8/B9 | Real dead-port failure, fail-open / fail-closed (breaker starts CLOSED, trips mid-run) | ≈ 22–27 ms          | ≈ 22–29 ms |

The failure path is dominated by the **20 ms dead-port socket budget** (`sentinel/redis.py:10`)
— the limiter itself is not the failure-path cost; the breaker short-circuit is essentially
free. Under CPU saturation the failure cells stay in the same band (p99 ≈ 29–37 ms), nowhere
near the benchmark client's 5 s budget (a benchmark-only override; production keeps 20 ms).

---

## 7 · Not idempotent (ADR-011)

A Redis call can time out locally while the script still commits server-side. A client retry
after a timeout may consume additional quota beyond what the client believes it used. This is a
documented property, not a bug: idempotency keys are out of scope for V1 (see
[known-limitations.md](known-limitations.md)).

---

## 8 · Testing the failure paths

- **Unit**: `tests/test_errors.py` (classification), `tests/test_circuit_breaker.py` (state
  machine with injected clock), `tests/test_emergency.py` (sustained-rate regression at
  1/2/5 tokens/s).
- **Integration**: `tests/test_limiter.py` fail-open/fail-closed journeys through
  `RateLimiter`; `tests/test_http.py` HTTP mapping (429 vs 503, Retry-After).
- **Concurrency + real failure injection** (`slow` suite): a dedicated
  `SentinelRedis` pointed at a dead port (Linux surfaces `REDIS_CONNECTION_ERROR`, Windows/WSL2
  `REDIS_TIMEOUT` — both accepted) trips the breaker under 50-coroutine load while the
  emergency limiter caps fail-open traffic; fail-closed counterparts deny everything.
- **Benchmark**: B7–B9 in `benchmarks/benchmark.py` record the failure journey end to end.
