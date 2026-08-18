# Sentinel

**Application-aware, tenant-aware, distributed rate limiting for FastAPI** — backed by a single
dedicated Redis instance and atomic Lua scripts.

Sentinel sits one layer above edge/CDN limiters: it understands *tenants* and *endpoints*, not
just IPs. Every endpoint gets its own algorithm (token bucket or sliding window), its own
capacity and rate, and — crucially — its own **failure semantics**: an endpoint that must never
overrun (expensive OCR compute) fails closed; an endpoint that must never block paying users
fails open, but is capped by an in-process emergency limiter so fail-open never means unlimited.

> **Getting started?** Jump straight to the step-by-step guide:
> [**M-INSTALLATION.md**](M-INSTALLATION.md) — from `pip install` to a working
> FastAPI integration with configuration and example usage.

---

## Table of contents

- [What problem does it solve?](#what-problem-does-it-solve)
- [Project structure](#project-structure)
- [Architecture at a glance](#architecture-at-a-glance)
- [The request journey](#the-request-journey)
- [Design invariants](#design-invariants)
- [Test results](#test-results)
- [Benchmarking results](#benchmarking-results)
- [Observability](#observability)
- [Known limitations](#known-limitations)
- [Installation](#installation)
- [Development](#development)
- [License](#license)

---

## What problem does it solve?

A typical rate limiter counts requests per IP. That is the wrong unit for a SaaS API: a shared
office IP can exhaust one tenant's quota while another tenant is idle, and a single tenant can
fire from hundreds of IPs and bypass IP-based limits entirely.

Sentinel rates per **tenant** (a validated JWT `sub` claim) and per **endpoint** (an explicit
configured id). Each endpoint declares how it must behave:

| Service | Endpoint | Algorithm | Fail mode | Why |
|---|---|---|---|---|
| PDFTalk | `pdftalk.ingest` | sliding window | fail closed | OCR compute is expensive; a Redis failure must stop traffic (503), not unmetered jobs |
| Resumint | `resumint.tailor` | token bucket | fail open | UX-sensitive; a Redis failure must not block users, but the emergency limiter caps the overrun |

This is a *library, not a service*: it runs inside your FastAPI process. There is no sidecar, no
proxy, no deployment to manage — you install it, wire one dependency, and configure policies.

---

## Project structure

```
sentinel/                  the library (14 modules, ~500 lines total)
├── lua/
│   ├── token_bucket.lua     atomic token bucket (Redis TIME()-clocked)
│   └── sliding_window.lua   atomic sliding window (Redis TIME()-clocked)
├── models.py               Policy, Decision, DecisionReason (8 members)
├── config.py               strict config loading (unknown keys rejected)
├── resolver.py             static policy lookup
├── redis.py                SentinelRedis + ScriptLoader (NOSCRIPT recovery)
├── lua.py                  script registry
├── algorithms.py           pure-Python reference implementations
├── limiter.py              RateLimiter orchestration
├── errors.py               RedisError classification
├── circuit_breaker.py      per-process breaker (CLOSED/OPEN/HALF_OPEN)
├── emergency.py            fail-open emergency limiter
├── auth.py                 JWT verification (HS* allowlist)
├── http.py                 SentinelGuard — the FastAPI integration
└── observability.py        structured logs + Prometheus metrics

tests/                     302 tests — see [Test results](#test-results)
benchmarks/                dependency-free benchmark harness
docs/                      architecture, failure-handling, known-limitations,
                           benchmark results, project record
```

The module boundaries mirror the six-stage request pipeline:
**auth → policy resolution → rate limiting → failure/resiliency → observability**, with
`sentinel/http.py` as the only FastAPI-aware layer.

---

## Architecture at a glance

Three components carry the correctness story:

1. **Atomic Lua scripts** (`sentinel/lua/*.lua`) — each rate-limit decision is one Redis script
   execution. Redis runs scripts single-threaded, so concurrent requests — 50 racing coroutines
   or 3 separate processes — cannot interleave a read-modify-write. The scripts use Redis
   `TIME()` as their only clock, so every API instance agrees on time (application clocks never
   enter the algorithm).

2. **The resiliency triangle** (`sentinel/errors.py`, `sentinel/circuit_breaker.py`,
   `sentinel/emergency.py`) — when Redis fails:
   - the failure is *classified* (`REDIS_TIMEOUT`, `REDIS_CONNECTION_ERROR`, …),
   - a per-process circuit breaker trips OPEN after 5 consecutive failures and short-circuits
     for 30 s (no request ever waits on a dead Redis),
   - fail-open endpoints fall back to an in-process emergency limiter capped at
     `fallback_rate_per_process_micro`; fail-closed endpoints deny with 503.
   A Redis failure never makes a request wait beyond a 20 ms budget, and every failure resolves
   to one of 8 bounded `DecisionReason` values.

3. **Hashed tenant identity** — buckets are keyed
   `sentinel:v1:{sha256(tenant)}:{endpoint_id}:{policy_version}`. Raw tenant ids never reach
   Redis keys, logs, or metrics. Tenant identity comes **only** from a validated JWT `sub`
   claim — the `X-Tenant-ID` header is ignored (spoofing is a locked regression test).

State is a single Redis string per bucket, written with `SET` + `EXPIRE` only (no `DEL`, no
`KEYS`/`SCAN`, no eviction):

| Algorithm | State | Expiry |
|---|---|---|
| Token bucket | `tokens_micro:last_refill_micro` | until the bucket would be full again |
| Sliding window | `current:previous:window_start_micro` | two windows (rollover horizon, lossless) |

**Denied requests never write** — a denied evaluation returns without touching the key, so
failed attempts cannot accelerate your quota.

The full walkthrough (module map, request journey, clock discipline, concurrency proof) lives in
[`docs/architecture.md`](docs/architecture.md).

---

## The request journey

One request to a guarded endpoint, in eight steps (`docs/architecture.md` §2 for the details):

1. **Bearer token extraction** — non-empty `Authorization: Bearer <token>`; anything else is a
   401 before any Redis call.
2. **JWT verification** — PyJWT with an allowlisted HS* algorithm; requires `exp` + `sub`.
   Auth failures are 401 and never count as rate-limit decisions.
3. **Policy resolution** — `(tenant, endpoint_id) → Policy`; unknown endpoint → 404.
4. **Script readiness** — Lua scripts loaded at startup (`await guard.load_scripts()`).
5. **Bucket key** — `sentinel:v1:{sha256(tenant)}:{endpoint_id}:{policy_version}`.
6. **Evaluation** — breaker check → Lua script → on `RedisError`: classify + branch by fail
   mode. Only genuine Redis successes reset the breaker.
7. **Decision → HTTP** — allowed: handler runs; denied: 429 (rate limit, with `Retry-After`
   where computable) or 503 (store failures).
8. **Observability** — every decision increments metrics; every denial emits a WARNING log.

---

## Design invariants

These are non-negotiable design contracts (each is enforced by tests and documented in
`docs/architecture.md` §7):

1. **Redis `TIME()` is the only clock** in the rate-limiting scripts.
2. **Integer microtokens only** — no floats in state.
3. **Explicit `endpoint_id`** — always a configured id, never derived from the URL/path.
4. **JWT-only tenant identity** — spoofable headers are ignored; auth failures never produce a
   `DecisionReason`.
5. **No client-reachable numeric input** — no `cost` parameter; the only script arguments are
   server-side policy values.
6. **Lua integer exactness** — configuration arithmetic is bounded so it stays exact below 2^53.

---

## Test results

| | |
|---|---|
| **Total** | **302 tests** (all passing, 0 skipped against a real Redis) |
| **Coverage** | **100%** on `sentinel/` (enforced: CI fails below it) |
| **Type checking** | `mypy --strict` clean |
| **Linting / formatting** | ruff check + format clean, pre-commit hooks clean |

The suite is organized into dedicated CI jobs so every category runs in isolation:

| Suite | Size | What it proves |
|---|---|---|
| Default (`pytest`) | 302 tests | everything below, plus unit + HTTP integration |
| `pytest -m security` | 23 tests | spoofing (SEC-03), URL/endpoint injection (SEC-08), Lua TTL-only expiry, noeviction checks, metrics cardinality bombs |
| `pytest -m slow` | 17 tests | 50-coroutine races on shared buckets, 3-process shared-bucket atomicity, dead-port failure injection, packaging (wheel build + fresh-venv install smoke) |

Headline correctness proofs, all green:

- **Exact token-bucket capacity** under 50 racing coroutines *and* across 3 spawned processes —
  Redis never admits more than the configured capacity, with the strict equality branch in CI.
- **Sliding window bounded by the reference** — real Redis output matches the pure-Python
  reference across 25 parity tests and property tests.
- **Security findings locked in** — every §07 finding of the project record has a regression
  test (or a documented, accepted boundary).
- **The wheel is proven, not assumed** — `tests/test_packaging.py` builds the wheel + sdist,
  asserts contents (Lua sources, `py.typed`, no `tests/`/`benchmarks/`/`examples/` leaks) and
  metadata, runs `twine check`, and smoke-installs the wheel into a fresh venv.

---

## Benchmarking results

Phase 14 measured the full HTTP journey with a dependency-free harness
(`benchmarks/benchmark.py`) on a single machine (Windows 11, 16 cores, loopback Redis 7).
**These are single-machine loopback numbers, reported as-is — not a throughput guarantee for any
other topology** (vision §12). They exist to be regression-compared against later runs. The
record below is the median of three full harness runs (2026-08-18); the run-to-run spread and
per-cell detail live in [`docs/benchmark-results.md`](docs/benchmark-results.md).

Three numbers tell the story (`c=1` = one concurrent client, p50 = median latency):

| What was measured | Result | Plain-English meaning |
|---|---|---|
| Unguarded endpoint (baseline) | ~5,700 req/s, 155 µs p50 | the HTTP stack alone |
| **With Sentinel** (token bucket) | ~950 req/s, 1,036 µs p50 | one loopback Redis round trip + JWT + decision; ≈6× overhead vs unguarded |
| Breaker OPEN short-circuit | ~41k req/s, 4–21 µs p50 | when the breaker has tripped, the cost is nearly free |
| Dead-port fail-open / fail-closed | p99 ≈ 27–33 ms | dominated by the 20 ms socket timeout — the limiter is not the failure-path cost |

Key takeaways:

- **~880 µs of the with-Sentinel latency is the Redis round trip + JWT + decide** — exactly what
  an in-process limiter should cost. A remote Redis adds its own round-trip time.
- **The breaker short-circuit is nearly free (4–21 µs p50)** — fail-open protection does not tax
  healthy traffic.
- **Failure latency ≈ the socket timeout, not the limiter**: a dead Redis resolves in ~29 ms
  p99, then fail-closed returns 503 or fail-open absorbs it into the emergency decision.
- **8 concurrent clients improve throughput for the limiter and failure cells** (p50 rises due
  to in-process serialization on one asyncio loop — a real multi-worker deployment spreads
  this).

One production defect was **found by benchmarking and fixed**: the fail-open emergency limiter
was double-refilling on denied calls, admitting ~2.3× the configured fallback rate under
sustained failure. Fixed by mirroring the Lua's "denied requests never write" contract; the
post-fix runs show no throughput regression and the allowance now matches the configured rate
exactly (the fresh run's B8 counts confirm exactly one initial burst per rep). Full numbers:
[`docs/benchmark-results.md`](docs/benchmark-results.md).

---

## Observability

- **Metrics** (Prometheus, process-wide, labeled only by `endpoint_id`/`decision_reason` —
  bounded label sets, no tenant label):
  - `sentinel_decisions_total` (counter)
  - `sentinel_evaluate_latency_microseconds` (histogram)
- **Logs** — every denied decision emits a WARNING structured log (`logger name: sentinel`)
  with `tenant_hash` (never the raw tenant), `endpoint_id`, `decision_reason`, `latency_micro`,
  `breaker_state`.

---

## Known limitations

V1 is deliberately small; each item is an accepted decision with a documented consequence
(full list with sources: [`docs/known-limitations.md`](docs/known-limitations.md)). Read before
production:

- **Single dedicated Redis instance** with `noeviction` + bounded `maxmemory` — Sentinel refuses
  to start otherwise; Redis is a single point of correctness.
- **20 ms fail-fast socket budget** — a slow Redis is treated as a failed Redis
  (`REDIS_TIMEOUT` is a normal, classified outcome; alert on it).
- **No idempotency keys** — a retry after a timeout can consume quota twice (ADR-011).
- **Breaker + emergency limiter are per-process** — N instances = N independent breakers, and
  fail-open allowance scales with instance count; choose `fallback_rate_per_process_micro` with
  your instance count in mind.
- **HS* JWT only** — no JWKS/asymmetric keys yet (deferred to V2); key rotation is a redeploy.
- **JWT replay detection lives upstream** (short-lived tokens, mTLS, single-use).
- **Sliding window is an estimate** (`current + previous × remaining/window`) and its denials
  carry no `Retry-After`; token-bucket denials do.

---

## Installation

**Step-by-step installation, configuration, FastAPI integration, and example usage:**
[**M-INSTALLATION.md**](M-INSTALLATION.md).

Quick reference:

```powershell
pip install sentinel-rate-limiter     # the library (import name: sentinel)
```

Requirements: Python ≥ 3.11 and a dedicated Redis 7 instance configured with `noeviction` and
a bounded `maxmemory` (Sentinel refuses to start otherwise).

---

## Development

- Install with dev tooling: `pip install -e ".[dev]"`.
- Trunk-based workflow; short-lived `feat/`/`fix/`/`test/`/`docs/` branches, squash-merge PRs.
- Quality gates (all enforced in CI): 302 tests, 100% coverage, `mypy --strict`, ruff
  check + format, pre-commit hooks.
- Packaging is proven by tests, not assumptions — see `tests/test_packaging.py` and the
  `packaging`/`publish` CI jobs (`publish` uploads to PyPI only on `v*` tags).

---

## License

MIT — see [`LICENSE`](LICENSE).
