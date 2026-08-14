# Sentinel — Phases 8–10 Summary (The Resiliency Triangle)

Date: 2026-08-14 — Branch `feat/failure-handling` (commit `73b0ef6`), PR open.

Phases 8, 9, and 10 are the plan's *Resiliency Triangle*: they make Sentinel
behave predictably when Redis fails, and together cover every failure path end
to end — from a raw `redis.exceptions` error all the way to an HTTP status code
on the wire. They structurally depend on each other: you cannot fail open
responsibly without the emergency limiter (Phase 10), and you shouldn't reach
for it without the circuit breaker (Phase 9) stopping the bleeding first.

## Phase 8 — Failure handling & classification

**Files:** `sentinel/errors.py` (new), `sentinel/redis.py`, `sentinel/limiter.py`

- `classify_redis_error(exc) -> DecisionReason` maps every Redis failure to a
  bounded decision reason:
  - `RedisTimeoutError` → `REDIS_TIMEOUT`
  - `ConnectionError` → `REDIS_CONNECTION_ERROR`
  - `ScriptMissingError` → `REDIS_NOSCRIPT_RETRY`
  - anything else redis-typed → `REDIS_CONNECTION_ERROR`
- `ScriptMissingError` is now its own error type (subclass of `RedisError`),
  raised by `ScriptLoader` when a script is still missing after the single
  NOSCRIPT re-load. Previously it was a bare `RedisError` re-raise.
- `RateLimiter.evaluate` catches only `RedisError` (no bare `except:`):
  - **fail-closed** policy → `FAIL_CLOSED` decision (deny, no emergency)
  - **fail-open** policy → delegates to the emergency limiter (Phase 10)
  - programming errors (`KeyError`, `RuntimeError`, …) are never caught and
    propagate unchanged
- The classifier's ordering matters: `ScriptMissingError` is checked before
  `RedisTimeoutError`/connection errors (a missing-script exhaustion is its own
  reason, not a connection failure).

## Phase 9 — Circuit breaker

**File:** `sentinel/circuit_breaker.py` (new)

- Per-process CLOSED / OPEN / HALF_OPEN state machine (ADR-007: the breaker is
  per-process, not distributed).
- Defaults: `FAILURE_THRESHOLD = 5`, `OPEN_TIMEOUT_SECONDS = 30.0`.
- CLOSED: calls reach Redis; each Redis failure increments `failure_count`;
  reaching the threshold opens the breaker and stamps `opened_at`.
- OPEN: `is_open()` short-circuits **before any Redis call**; after the
  quarantine elapses it lazily transitions to HALF_OPEN.
- HALF_OPEN: the next arrival is a probe. Success → CLOSED (count reset).
  Failure → OPEN with a fresh quarantine.
- Only *genuine Redis successes* (`record_success`) reset `failure_count` —
  emergency-limiter pass-throughs do NOT reset it. Decided in `is_open()`,
  before Redis: `RateLimiter` consults the breaker first, so an OPEN breaker
  never touches Redis.
- Injected `now` clock (`time.monotonic` default) for deterministic tests.
- Uses the local monotonic clock — the deliberate, documented exception to
  "Redis `TIME()` is the one clock", since it runs precisely when Redis is
  unreachable.

## Phase 10 — Emergency local limiter

**File:** `sentinel/emergency.py` (new)

- `TokenBucketEmergencyLimiter` — a plain in-process token bucket reusing the
  pure-Python `token_bucket_evaluate` oracle from `sentinel/algorithms.py`.
- Capacity = refill rate = `fallback_rate_per_process_micro`: a burst of one
  second's worth of the fallback rate, then sustained at the fallback rate.
- Buckets are keyed by `endpoint_id` only (tenant fairness is a V2 topic).
- `EmergencyOutcome(allowed, remaining_micro, retry_after_seconds)`; denied
  outcomes produce `remaining_micro` and a `retry_after_seconds` computed from
  the standard token-bucket formula.
- Output is deterministic in tests through an injected `now_micro` clock; the
  default is the local monotonic clock (documented exception to the Redis-clock
  invariant, because this limiter runs exactly when Redis is unreachable).
- `EmergencyLimiter` protocol keeps `RateLimiter` decoupled from this
  implementation (and from test doubles).

## Wiring in `RateLimiter` (Phase 8 → 9 → 10)

```text
evaluate(policy, key)
  ├─ breaker.is_open()? ── YES ── fail-closed → CIRCUIT_OPEN (no Redis)
  │                            └── fail-open   → emergency limiter (CIRCUIT_OPEN)
  ├─ strategy.evaluate(...) → pydantic/Redis errors propagate; RedisError caught
  │     ├─ success → breaker.record_success() → ALLOWED / RATE_LIMITED
  │     └─ RedisError → breaker.record_failure() + classify
  │           ├─ fail-closed → FAIL_CLOSED
  │           └─ fail-open   → emergency limiter (REDIS_TIMEOUT / … / EMERGENCY_LOCAL_LIMIT)
```

`RateLimiter` and `SentinelGuard` now take explicit `breaker` and `emergency`
constructor dependencies — no hidden singletons, no import-time defaults, easy
to inject fakes. The previous always-allow test stub was deleted; the tests
exercise the real emergency limiter.

## HTTP semantics (`sentinel/http.py`)

`_denied_status(reason)` maps every denied `DecisionReason` to an HTTP status:

| Reason | Status | Retry-After |
|---|---|---|
| `RATE_LIMITED` | 429 | round-up seconds |
| `EMERGENCY_LOCAL_LIMIT` | 429 | round-up seconds |
| `FAIL_CLOSED` | 503 | — |
| `CIRCUIT_OPEN` | 503 | — |
| `REDIS_TIMEOUT` | 503 | — |
| `REDIS_CONNECTION_ERROR` | 503 | — |
| `REDIS_NOSCRIPT_RETRY` | 503 | — |

`_HTTP_429_REASONS` / `_HTTP_503_REASONS` sets are exhaustive and disjoint over
all 7 denied reasons (test-pinned); `_denied_status` raises `RuntimeError` on
any unmapped reason.

## Testing

New test files: `tests/test_errors.py`, `tests/test_circuit_breaker.py`,
`tests/test_emergency.py`; extended: `tests/test_limiter.py`,
`tests/test_limiter_integration.py`, `tests/test_http.py`,
`tests/test_redis.py` (ScriptMissingError).

Covered behaviors include:

- classifier mapping for every Redis failure type, including ordering against
  `ScriptMissingError`
- fail-closed vs fail-open branching for timeout / connection / NOSCRIPT
- breaker: threshold accumulation, OPEN short-circuit with zero Redis calls,
  HALF-OPEN probe success→CLOSED, probe failure→OPEN, per-process isolation
- emergency limiter: first-use full bucket, exhaustion, refill, clamping at
  capacity, endpoint isolation, tiny-fallback boundedness, parity against the
  `token_bucket_evaluate` oracle, real-clock refill
- end-to-end HTTP: 429 with Retry-After (incl. rounding and zero-rate cases),
  503 for all store failures, fail-open pass-through with the underlying
  failure reason, emergency exhaustion → 429 with `EMERGENCY_LOCAL_LIMIT`

## Quality gates (all green)

```
pytest                     # 252 passed (incl. integration tests against real Redis)
pytest --cov=sentinel      # 100% coverage
mypy sentinel              # strict, clean (incl. pre-existing redis-py evalsha stub debt)
ruff check .               # clean
ruff format --check .      # clean
pre-commit run --all-files # clean
```

## Files touched

| File | Change |
|---|---|
| `sentinel/errors.py` | new — classifier + `ScriptMissingError` |
| `sentinel/circuit_breaker.py` | new — per-process breaker |
| `sentinel/emergency.py` | new — emergency token bucket |
| `sentinel/limiter.py` | breaker + emergency wiring, fail-mode branching |
| `sentinel/http.py` | `_denied_status`, 429/503 maps |
| `sentinel/redis.py` | raise `ScriptMissingError`; mypy debt fix |
| `tests/test_errors.py` | new |
| `tests/test_circuit_breaker.py` | new |
| `tests/test_emergency.py` | new |
| `tests/test_limiter.py` | failure-path suites, real emergency limiter |
| `tests/test_limiter_integration.py` | real-Redis fixture on real emergency limiter |
| `tests/test_http.py` | fail-open/emergency/circuit HTTP cases |
| `tests/test_redis.py` | ScriptMissingError assertion |

## Next up

Phase 11 (security regression suite), Phase 12 (observability: structured logs
on deny, Prometheus metrics bounded to `endpoint_id`/`decision_reason`),
Phase 13 (slow-marked concurrency/failure-injection tests).
