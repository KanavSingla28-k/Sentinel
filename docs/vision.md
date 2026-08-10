# Sentinel — Build Specification

**Application-aware, tenant-aware, distributed rate limiting library**
**Status:** V1 design spec
**Owner:** Kanav Singla

---

## 1. Problem and Product Thesis

PDFTalk and Resumint both expose compute-expensive endpoints (OCR ingestion, LLM-backed tailoring) with no protection against abusive or accidental overuse. Each service would otherwise need to re-solve the same problem independently: track a caller's usage, enforce a limit, and do so correctly when multiple API instances are running concurrently.

Sentinel exists to solve this **once**, at the application layer, and be reused across services.

**Why not just use Cloudflare / API Gateway / NGINX / Envoy?**
Edge and infrastructure-layer rate limiting is coarse — it operates on IP, path, or raw API key, with no visibility into *who* is calling (tenant identity), *what tier* they're on, or *how expensive* the specific operation is. Sentinel does not replace edge protection; it complements it. Edge tools stop raw traffic floods before they reach your infrastructure. Sentinel enforces policy that depends on authenticated tenant identity, subscription tier, and per-endpoint cost — decisions that require application context edge tools don't have.

**Thesis:** the same infrastructure gap exists across every service Kanav builds. Solve it once as a library, prove correctness under concurrency, and integrate it for real into PDFTalk and Resumint rather than demonstrating it in isolation.

---

## 2. Explicit Scope and Non-Goals

**In scope (V1):**
- Python library + FastAPI middleware, embedded in-process
- Redis-backed shared state, atomic via Lua scripts
- Two algorithms: token bucket, sliding window counter
- JWT-derived tenant/tier identity with a policy cache (not raw JWT trust)
- Configurable fail-open / fail-closed behavior per endpoint
- Prometheus metrics with bounded-cardinality labels
- Integration into PDFTalk and Resumint

**Explicit non-goals (deferred, not implied as done):**
- Node.js support
- Fixed window or leaky bucket algorithms (no concrete use case for them yet — see §5)
- Exact sliding-log algorithm (memory cost not justified at current scale)
- Open-source packaging / public distribution
- Admin UI or dynamic runtime policy editing
- Redis Cluster / multi-region replication
- Claiming to replace edge-layer rate limiting

---

## 3. Architecture

Sentinel is an **in-process library**, not a standalone network service. It's called synchronously inside FastAPI middleware, before the request handler runs.

```
Client
  │
  ▼
FastAPI instance (1 of N)
  │  Sentinel middleware:
  │   1. Resolve tenant_id + tier (JWT + policy cache)
  │   2. Build Redis key for (tenant, endpoint, algorithm)
  │   3. EVALSHA atomic Lua script → allow/deny + metadata
  │   4. Emit Prometheus metrics
  │   5. Allow → continue to handler / Deny → 429 + Retry-After
  ▼
Redis (single instance, V1)
```

**Why in-process, not a sidecar/service:** tenant identity and tier resolution already live inside the API process (JWT is decoded there). A separate network hop would add latency and a second point of failure without adding correctness. This is a deliberate trade-off, not an oversight — it does mean Sentinel is Python-only in V1 (see §15).

---

## 4. Core APIs / Configuration

```python
from sentinel import RateLimiter, Policy

limiter = RateLimiter(redis_url="redis://...", default_fail_mode="closed")

# Per-endpoint policy, registered at startup
limiter.register(
    endpoint_id="pdftalk.ingest",
    policy=Policy(
        algorithm="sliding_window_counter",
        limit=20,
        window_seconds=60,
        fail_mode="closed",   # expensive endpoint: reject on Redis failure
    ),
)

limiter.register(
    endpoint_id="resumint.tailor",
    policy=Policy(
        algorithm="token_bucket",
        rate=5,
        burst=10,
        fail_mode="open",     # UX-sensitive: don't block paying users on a Redis blip
    ),
)
```

```python
@app.post("/ingest")
@limiter.enforce("pdftalk.ingest")
async def ingest(request: Request):
    ...
```

`enforce()` resolves tenant identity, calls Redis, and returns `Decision(allowed: bool, remaining: int, retry_after: float | None)`. Denials return HTTP 429 with `Retry-After`.

---

## 5. Rate-Limiting Algorithms and When Each Is Used

Two algorithms, chosen deliberately rather than maximized for count:

| Algorithm | Behavior | Used for |
|---|---|---|
| **Token bucket** | Bucket refills at a fixed rate; allows short bursts up to bucket size | User-facing, bursty traffic (e.g. Resumint tailoring) — a real user issuing 3 quick requests shouldn't be punished |
| **Sliding window counter** | Weighted average of current + previous fixed window, approximating a true sliding window in O(1) memory | Expensive, abuse-sensitive endpoints (e.g. PDFTalk OCR ingestion) — smoother enforcement, no boundary-burst exploit that fixed windows have |

**Explicitly excluded from V1:**
- *Fixed window*: allows a 2x burst at window boundaries — a known correctness flaw, not worth implementing just to have a third algorithm.
- *Exact sliding log*: stores a timestamp per request — accurate but unbounded memory per key at scale. Not justified without a concrete need for exactness the counter approximation can't provide.

Each algorithm choice is a per-endpoint config decision, not a fixed default — this is where the "application-aware" part of the thesis has to actually show up in the code, not just the pitch.

---

## 6. Distributed Concurrency / Correctness Design

**The problem:** naive `GET count → compare → INCR` has a race window. Two concurrent requests on two different API instances can both read the same count before either writes, letting both through when only one should pass.

**The fix:** the entire check-and-update happens in a single Redis Lua script, executed via `EVALSHA`. Redis executes Lua scripts atomically (single-threaded), so no other command can interleave mid-script — this removes the race window entirely, without needing distributed locks.

Token bucket, conceptually:
```lua
-- KEYS[1] = bucket key, ARGV = {capacity, refill_rate, now, cost}
local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity
local last_ts = tonumber(bucket[2]) or now
local elapsed = now - last_ts
tokens = math.min(capacity, tokens + elapsed * refill_rate)
if tokens >= cost then
  tokens = tokens - cost
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', KEYS[1], ttl)
  return {1, tokens}  -- allowed
else
  return {0, tokens}  -- denied
end
```

Sliding window counter follows the same pattern: read previous + current window counts, compute the weighted estimate, compare against limit, increment atomically — all inside one script.

**Why Lua over `MULTI`/`EXEC` transactions:** transactions can't branch on a value read mid-transaction (no conditional logic). Lua can read, compute, and conditionally write in one atomic unit — required here since the decision depends on the current value.

This claim — "distributed-safe" — is only real once proven; see §11 for the concurrency test that verifies it rather than asserting it.

---

## 7. Multi-Tenant / JWT Policy Model

**JWT claims are treated as an identity hint, not a source of truth for entitlement.** A tier claim embedded in a JWT can be stale the moment a user upgrades or downgrades — trusting it directly would let a downgraded user keep elevated limits until their token expires, or (worse) create an incentive to hold onto an old token.

Design:
- JWT provides `tenant_id` only, used to look up the tenant's current policy.
- Actual tier is resolved via a **policy cache**: an in-memory (per-instance) cache backed by Redis, keyed by `tenant_id`, TTL = 30s. On expiry, the next request refreshes it from the source of truth (billing/tenant service, or config for V1 if no such service exists yet).
- Quota identity is `(tenant_id, endpoint_id)` — never a raw claim the client controls. A client cannot influence its own quota tier by crafting a JWT claim, since the claim's tier value is never read directly for limit calculation.
- Tier change propagation window is bounded and explicit: **up to 30 seconds**, not instant. This is stated as a documented trade-off, not hidden.

---

## 8. Redis Data Model and Scalability

**Key structure:**
```
rl:{tenant_hash}:{endpoint_id}:{algorithm}
```
- `tenant_hash`: short hash (8 chars) of tenant_id, keeping keys bounded regardless of raw ID length.
- `endpoint_id`: from a fixed, config-defined set (bounded cardinality by design).
- TTL: `2 × window_seconds` for sliding window counters, `bucket_capacity / refill_rate × 2` for token buckets — long enough to survive a burst gap, short enough to auto-expire idle tenants instead of accumulating unbounded keys.

**Memory:** each key is a small hash (~100 bytes). Even at 1M active tenants × a handful of endpoints, this is low tens of MB — not a concern at current scale.

**Hot keys (documented limitation, not solved in V1):** a single extremely high-volume tenant hitting one key repeatedly can approach Redis's single-key throughput ceiling. V1 does not shard hot keys — this is called out explicitly as a known V2 candidate (e.g. suffixing the key with a time-bucket or hash-mod shard and summing across shards) rather than pretending it isn't a limitation.

---

## 9. Failure Modes and Fail-Open / Fail-Closed Semantics

| Failure | Behavior |
|---|---|
| Redis down | Configurable per endpoint: **fail-closed** (reject) for expensive/abuse-sensitive endpoints like PDFTalk ingestion; **fail-open** (allow) for UX-sensitive endpoints like Resumint tailoring, where blocking a paying user on a transient Redis blip is worse than a brief unenforced window |
| Redis slow | Client-side timeout on the Lua call (20ms). A timeout is treated as a failure and follows the configured fail-mode — it does not block the request pipeline waiting on Redis |
| Redis overloaded | Same as "slow" — timeout-based, not distinguished |
| Redis failover | Brief unavailability window during failover is treated as a failure per the timeout rule above; no special-casing |
| Repeated failures | A simple circuit breaker: after N consecutive errors within a short window, skip the Redis call entirely and apply the fail-mode default directly, avoiding a latency cascade from repeatedly waiting out timeouts |

This is a deliberate trade-off, not full resilience: Redis remains a single point of correctness in V1. It's mitigated (via fail-mode config and timeouts), not eliminated — see §15.

---

## 10. Prometheus Observability

**Bounded-cardinality labels only:**
- `endpoint` — fixed, config-defined set
- `algorithm` — `token_bucket` | `sliding_window_counter`
- `decision` — `allow` | `deny`
- `tier` — bounded set (`free`, `paid`, `enterprise`)

**Never labeled:** `tenant_id`, `user_id`, `IP` — these are unbounded and would blow up Prometheus's cardinality.

Metrics:
```
sentinel_requests_total{endpoint, algorithm, decision, tier}
sentinel_decision_latency_seconds{endpoint}          # histogram
sentinel_redis_errors_total{endpoint}
```

Per-tenant debugging (e.g. "why was tenant X denied?") goes through **structured logs on deny events**, not metrics — logs can carry high-cardinality fields safely; metrics cannot.

---

## 11. Testing Strategy

1. **Unit tests** — algorithm correctness in isolation: token math, window-boundary behavior, TTL expiry.
2. **Concurrency correctness test** (the one that actually proves §6's claim):
   - Run 3 API instances as separate processes/containers, one shared Redis.
   - A load generator fires N concurrent requests for the same tenant, exceeding the configured limit, spread across all 3 instances simultaneously.
   - Assert: allowed count == configured limit **exactly**, not limit + ε. Repeat across many trials to catch race conditions statistically, not just once.
3. **Failure injection tests** — kill Redis mid-test; verify the configured fail-mode triggers correctly and requests don't hang past the timeout.
4. **Policy tests** — verify a tier downgrade takes effect within the policy cache TTL, and a stale JWT claim never grants more than the tenant's actual current tier.

---

## 12. Benchmark Methodology and Success Criteria

**Setup, documented honestly (not implied as production infra unless it is):**
```
Load Generator (separate process)
        │
   ┌────┼────┐
   ▼    ▼    ▼
 API#1 API#2 API#3   (separate containers, Docker Compose)
   └────┼────┘
        ▼
      Redis
```

**Captured, not assumed:**
- p50 / p95 / p99 decision latency
- Sustained throughput (req/s)
- Redis CPU/network during the test
- Over-limit leakage count

**Success criteria, stated as things to measure and report — not pre-committed numbers:**
- `0` over-limit decisions across a stated number of concurrent requests across 3 instances (this is the correctness claim, and it's the one that matters most).
- Actual measured p99 latency, reported as-is, with the test topology disclosed alongside it. If the topology is 3 Docker containers on one machine rather than real distributed hardware, the resume claim says so.

---

## 13. Integration Plan: PDFTalk and Resumint

**PDFTalk — `pdftalk.ingest` endpoint:**
- Algorithm: sliding window counter (smooth enforcement, no boundary burst)
- Fail-closed: this is the most compute-expensive endpoint (OCR + async ingestion); an unprotected Redis outage shouldn't become an open door for abuse
- Baseline documented before integration: currently zero protection on this endpoint

**Resumint — `resumint.tailor` endpoint:**
- Algorithm: token bucket (allows a real user's short burst of edits without penalty)
- Fail-open: a Redis blip shouldn't block a paying user from a UX-critical action

**The deliberate difference between these two integrations — different algorithm, different fail-mode, justified by the actual cost and UX profile of each endpoint — is the concrete evidence of application-level understanding, not the existence of the library itself.**

Both services should be described as **"integrated into two deployed services"** — not "production traffic" — unless real user traffic is actually flowing through them.

---

## 14. V1 → V2 Roadmap

**V1 (built exceptionally well, nothing extra):**
- Python + FastAPI middleware library
- Redis + atomic Lua scripts
- Token bucket + sliding window counter only
- JWT → tenant_id → policy cache (tier resolution, 30s TTL)
- Configurable fail-open/closed per endpoint, with circuit breaker
- Prometheus metrics, bounded cardinality
- Integrated into PDFTalk and Resumint
- Concurrency, failure-injection, and benchmark test suite, results documented as-is

**V2 (explicitly deferred, only pursued if a real need appears):**
- Hot-key sharding for high-volume tenants
- Node.js port
- Additional algorithms, only if a concrete endpoint needs one
- Redis Cluster / multi-region
- Public package distribution
- Admin/dynamic policy hot-reload

---

## 15. Risks and Design Trade-offs

| Decision | Trade-off accepted |
|---|---|
| Redis as the single shared state store | Single point of correctness; mitigated via timeouts + fail-mode config, not eliminated. Industry-standard trade-off, not unique to Sentinel. |
| In-process library vs. sidecar service | Lower latency, simpler ops — at the cost of Python-only in V1. Node support explicitly deferred rather than half-built. |
| Sliding window *counter* vs. exact sliding log | Bounded approximation error, O(1) memory — vs. perfect accuracy at unbounded memory cost. Approximation error is documented, not hidden. |
| Policy cache TTL (30s) | Tier changes take up to 30s to propagate — bounded staleness accepted in exchange for avoiding a DB/service call on every request. |
| No hot-key sharding in V1 | Known throughput ceiling for a single very-high-volume tenant; explicitly scoped as a V2 problem, not silently ignored. |
| Benchmark realism | V1 benchmarks likely run on a single machine via Docker Compose, not real distributed hardware — topology is disclosed alongside any reported numbers rather than implied to be larger-scale. |

---

**Bottom line:** Sentinel's value isn't the algorithm — it's proving distributed correctness under real concurrency, making two deliberately different integration decisions (PDFTalk vs. Resumint) for defensible reasons, and reporting every number with the setup that produced it.
