# Observability

Every Sentinel decision is observable out of the box — no extra wiring required. This page
documents exactly what Sentinel records, where it goes, and what it deliberately does **not**
collect.

---

## What Sentinel records

Every evaluation ends in exactly one `DecisionReason` (8 members) and is recorded in two ways:

### Prometheus metrics

Registered once on the **default `prometheus_client` registry** (process-wide), so an existing
scrape endpoint picks them up automatically:

| Metric | Type | Meaning |
|---|---|---|
| `sentinel_decisions_total` | counter | Rate-limit decisions by endpoint and reason |
| `sentinel_evaluate_latency_microseconds` | histogram | Rate-limit evaluation latency by endpoint and reason |

Labels are **bounded by design** — `endpoint_id` (an explicit configured id, ADR-009) and
`decision_reason` (the closed 8-member enum). There is **no tenant label** and no free-form
dimension: a cardinality bomb is structurally impossible (a locked security regression test).

### Structured logs

Every **denied** decision emits a WARNING log on the `sentinel` logger with these fields:

| Field | Content |
|---|---|
| `tenant_hash` | SHA-256 hash of the tenant id — **never** the raw tenant |
| `endpoint_id` | The explicit configured endpoint id |
| `decision_reason` | One of the 8 `DecisionReason` values |
| `latency_micro` | Evaluation latency in microseconds |
| `breaker_state` | `CLOSED` / `OPEN` / `HALF_OPEN` at decision time |

Allowed decisions are **not** logged (they still increment the metrics).

The 8 `DecisionReason` values: `ALLOWED`, `RATE_LIMITED`, `EMERGENCY_LOCAL_LIMIT`,
`FAIL_CLOSED`, `CIRCUIT_OPEN`, `REDIS_TIMEOUT`, `REDIS_CONNECTION_ERROR`,
`REDIS_NOSCRIPT_RETRY`.

## What Sentinel does NOT do

- **No telemetry is sent anywhere.** Sentinel ships no phone-home, no analytics, nothing to its
  package creator or to any third party. Metrics stay in your process's Prometheus registry;
  logs go to your application's `sentinel` logger. What you observe is what stays in your
  infrastructure.
- **No payload collection.** Sentinel never reads or records request bodies, URLs, or headers
  beyond the `Authorization` header it must verify.
- **No raw tenant ids.** Tenant identity appears only as `tenant_hash` (SHA-256) in metrics,
  logs, and Redis keys — a hash is not a secret, but raw ids never leak through Sentinel's
  output surfaces.
- **No auth-event noise.** 401s (missing/invalid/expired token) happen before any Redis call
  and produce **no** decision, no metric increment, and no deny log.
- **No dashboard.** Sentinel emits metrics; building a dashboard is up to you (explicitly
  out of scope for V1).

## Redis-related observability

Sentinel does not instrument Redis itself (no Redis-side metrics or tracing). Redis failures
are observable **through Sentinel's own decision surface**:

- Every store failure resolves to a classified reason — `REDIS_TIMEOUT`,
  `REDIS_CONNECTION_ERROR`, `REDIS_NOSCRIPT_RETRY`, `CIRCUIT_OPEN`, `FAIL_CLOSED` — and shows
  up in `sentinel_decisions_total` and the deny logs.
- `REDIS_TIMEOUT` is a **normal, classified outcome** of the 20 ms fail-fast budget, not a
  crash — alert on it, because a slow Redis degrades exactly like a dead one.
- The circuit breaker trips OPEN after 5 consecutive failures and short-circuits for 30 s; the
  breaker state is carried on every log line (`breaker_state`), so an outage shows up as a
  shift from `CLOSED` to `OPEN` in the logs.

## Debugging cheatsheet

| You see | Meaning / next step |
|---|---|
| 429s on a healthy endpoint | The token-bucket/sliding-window quota is genuinely exhausted — check the tenant's allowance and `Retry-After` |
| A burst of `REDIS_TIMEOUT` reasons | The 20 ms budget was hit: Redis unreachable or saturated — check Redis, not Sentinel |
| `breaker_state=OPEN` across the fleet | Per-process breakers: each instance trips independently and recovers on its own (30 s quarantine) |
| `EMERGENCY_LOCAL_LIMIT` reasons | Fail-open endpoints absorbing a store outage — allowance is per-process, so size `fallback_rate_per_process_micro` with your instance count in mind |
| No deny logs for a 401 | Expected: auth failures are not rate-limit decisions |

## Related

- [Failure Semantics](failure-semantics.md) — what each `decision_reason` means and how
  fail-open/fail-closed routes decisions.
- [Configuration](configuration.md) — where the policy fields behind the metrics come from.
