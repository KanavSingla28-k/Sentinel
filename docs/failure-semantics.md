# Failure Semantics

Sentinel's failure philosophy, stated once: **a Redis failure never causes a request to wait
beyond the configured 20 ms budget, and every failure resolves to a row in the decision table.**
No bare `except:`, no silent fallbacks, no unbounded fail-open.

This page explains the three concepts — fail-open, fail-closed, and the in-process emergency
limiter — exactly as the code implements them. A deeper, code-referenced walkthrough of the
machinery lives in [Failure handling](failure-handling.md).

---

## The three modes

### Fail-closed

The endpoint's `fail_mode` is `fail_closed`. When the Redis store fails (timeout, connection
error, circuit breaker OPEN, exhausted NOSCRIPT retry), the request is **denied with 503**
(`{"detail": "rate limiter unavailable"}`). Traffic resumes automatically when the store
recovers.

### Fail-open

The endpoint's `fail_mode` is `fail_open`. When the store fails, the request is **not blocked
by the outage** — but it is never unmetered: it falls back to the in-process emergency limiter
below, so fail-open means *limited*, not *unlimited*.

### Emergency / in-process limiting

A per-process, in-memory token bucket keyed by `endpoint_id` only (`sentinel/emergency.py`):

- Capacity = refill rate = `fallback_rate_per_process_micro` — a burst of one second's worth of
  the fallback rate, then sustained at the fallback rate.
- Runs on the local monotonic clock (the documented exception to the "Redis `TIME()` is the
  only clock" invariant — it operates precisely when Redis is unreachable).
- Mirrors the Lua's **"denied requests never write"** contract: state is persisted only on
  ALLOW, so sustained fail-open traffic cannot bank partial refills (this fixed a real
  double-refill defect found by benchmarking).
- Because it is per-process, a deployment of N instances can admit up to **N × the fallback
  rate** during an outage — choose `fallback_rate_per_process_micro` with your instance count
  in mind (see [Known limitations](known-limitations.md)).

## The decision table

Every evaluation ends in exactly one `DecisionReason` (8 members). The fail mode comes from the
endpoint's `Policy` — it is a product decision per integration, not a global setting:

| Redis outcome | Fail-open | Fail-closed |
|---|---|---|
| Success | Lua result (`ALLOWED` / `RATE_LIMITED`) | Lua result (`ALLOWED` / `RATE_LIMITED`) |
| Timeout (20 ms) | Emergency local limiter | Deny → `FAIL_CLOSED` |
| Connection error | Emergency local limiter | Deny → `FAIL_CLOSED` |
| `NOSCRIPT` (re-load exhausted) | Emergency local limiter | Deny → `FAIL_CLOSED` |
| Circuit breaker OPEN | Emergency local limiter | Deny → `CIRCUIT_OPEN` |

Fail-open requests admitted by the emergency limiter carry the *cause* as the reason
(`CIRCUIT_OPEN`, `REDIS_TIMEOUT`, ...) with `allowed=True` — metrics count them, the deny log
does not, so the decision table stays lossless.

Supporting machinery:

- **Classification** (`sentinel/errors.py`) — every `RedisError` maps to a bounded reason:
  `TimeoutError` → `REDIS_TIMEOUT`, `ConnectionError` → `REDIS_CONNECTION_ERROR`,
  `ScriptMissingError` → `REDIS_NOSCRIPT_RETRY`, anything else → `REDIS_CONNECTION_ERROR`.
- **Circuit breaker** (`sentinel/circuit_breaker.py`) — per-process CLOSED/OPEN/HALF_OPEN
  state machine: 5 consecutive failures trip OPEN, a 30 s quarantine follows, and only genuine
  Redis successes reset it. While OPEN, requests short-circuit before any Redis call.
- **HTTP mapping** (`sentinel/http.py`) — `RATE_LIMITED` and `EMERGENCY_LOCAL_LIMIT` → **429**
  with `Retry-After` when the decision provides one; the five store-failure reasons → **503**.

## Why different endpoints need different failure behavior

`fail_mode` is declared per endpoint because a store outage hits different workloads
differently:

```text
Expensive OCR endpoint (unmetered jobs = real cost)
→ fail closed: 503 during an outage; compute is never unmetered

Paid/user-facing endpoint (blocked users = real cost)
→ fail open + emergency limiter: users are never blocked by an outage;
  the in-process cap keeps the overrun bounded
```

The repo's example config encodes exactly this split — PDFTalk's `pdftalk.ingest` (expensive
OCR compute) fails closed; Resumint's `resumint.tailor` (UX-sensitive) fails open with an
emergency cap. The same rule generalizes: when a denied request is worse than a small,
bounded overrun, fail open; when an overrun is worse than a denial, fail closed.

## What is NOT guaranteed during failure

- **Not idempotent** — a Redis call can time out locally while the script commits server-side;
  a client retry can consume quota the client believes it never used (ADR-011).
- **No cross-tenant fairness in fail-open** — emergency buckets are per-endpoint, not
  per-tenant.
- **Per-process, not distributed** — N instances have N independent breakers and N independent
  emergency allowances.

## Related

- [Failure handling](failure-handling.md) — the full machinery: classification, breaker state
  machine, emergency limiter internals, measured failure-path latency.
- [Observability](observability.md) — how each failure reason shows up in metrics and logs.
- [Known limitations](known-limitations.md) — the accepted boundaries around these semantics.
