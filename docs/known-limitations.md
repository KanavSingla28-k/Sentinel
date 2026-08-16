# Sentinel — Known Limitations (V1)

*Phase 15 documentation deliverable. Every accepted V1 limitation, its consequence, and its
source in the spec or the code. The canonical treatment lives in project record §06 (failure
tradeoffs), §07 (accepted upstream boundaries), and §10 (deferred to V2); this document is the
single list.*

Sentinel V1 is deliberately small. The limitations below are **accepted decisions with
documented consequences**, not unexamined gaps — each one survived the three review rounds of
the project record. Read this document before production deployment: several items are
operational requirements (items 1–2), several are correctness properties you must design
around (items 3, 7, 10), and several are upstream boundaries that belong to the host
application (items 8–9).

---

## Summary table

| # | Limitation | Consequence | Source |
|---|---|---|---|
| 1 | Single dedicated Redis instance, `noeviction` + bounded `maxmemory` required | Sentinel refuses to start otherwise; Redis is a single point of correctness | ADR-002/004/005, `sentinel/redis.py:39` |
| 2 | 20 ms socket budget, fail-fast | Timeouts are a normal, classified outcome (`REDIS_TIMEOUT`), not a crash | ADR, `sentinel/redis.py:10` |
| 3 | No idempotency keys | A retry after a timeout can consume extra quota | ADR-011, project record §06 |
| 4 | Circuit breaker is per-process | N instances = N independent breakers; not distributed | ADR-007, `sentinel/circuit_breaker.py` |
| 5 | Emergency limiter is per-process and per-endpoint | Fail-open allowance scales with instance count; no cross-tenant fairness during an outage | `sentinel/emergency.py`, project record §10 |
| 6 | Emergency limiter + breaker use local clocks | Deliberate exceptions to the Redis-clock invariant | `sentinel/emergency.py:9`, `sentinel/circuit_breaker.py:7` |
| 7 | JWT: HS* symmetric allowlist only; no JWKS/asymmetric keys | Static shared secret; key rotation is a redeploy; JWKS deferred to V2 | `sentinel/config.py:10`, project record §01 |
| 8 | JWT replay not detectable at the Sentinel layer | Mitigation lives upstream (short-lived tokens, mTLS, single-use) | Project record §07 |
| 9 | No per-request cost / weighted requests | Every request costs exactly one token; no client-reachable numeric input (SEC-01) | Project record §05, §07 |
| 10 | Sliding window is an estimate; no `Retry-After` on sliding-window denials | Bounded by the reference formula, not an exact count; clients retry on their own schedule | `sentinel/limiter.py:106` |
| 11 | Static configuration only; no dynamic policy updates | Changing a policy requires a restart; `policy_version` exists for manual bumps | Project record §02 scope discipline |
| 12 | Simple per-key state; no per-tenant hash consolidation | Redis key overhead grows with (tenant × endpoint); consolidation is a V2 follow-up if memory proves a problem | Project record §05 |
| 13 | No replicas, failover, or cluster | Redis downtime = rate limiting unavailable; failure semantics per §06 table | ADR-010, project record §10 |
| 14 | Idle buckets expire (TTL-only expiry) | A tenant's history resets after its bucket refills to full / two windows elapse | `sentinel/lua/*.lua`, ADR-005 |
| 15 | Emergency burst = one second of the fallback rate | Fail-open admits at most one second's worth of fallback rate immediately, then sustains | `sentinel/emergency.py:55` |
| 16 | Benchmark numbers are single-machine loopback, reported as-is | Not a throughput guarantee for any other topology | Vision §12, `docs/benchmark-results.md` |
| 17 | In-process Python library for FastAPI (asyncio, Python ≥ 3.11) | Not a sidecar, no WSGI binding, no other-language SDK | `pyproject.toml`, project record §10 |
| 18 | Metrics are process-wide on the default Prometheus registry | Multiple guards share collectors; labels bounded to `endpoint_id`/`decision_reason`; no tenant label | `sentinel/observability.py:20` |
| 19 | Tenant ids are hashed, not encrypted | A SHA-256 hash is not a secret; keys/logs carry `tenant_hash`, never the raw id | `sentinel/limiter.py:37` |

---

## The items that need real attention

### 1 · Single Redis instance with `noeviction` and bounded memory

Sentinel requires a dedicated Redis with `maxmemory-policy noeviction` **and** a positive
`maxmemory`; `SentinelRedis.assert_noeviction()` (`sentinel/redis.py:39`) refuses to start
otherwise. Eviction would silently reset quotas (eviction-as-bypass, project record §07) and
an unbounded memory limit would let state grow without bound. Consequences: Redis is a single
point of correctness, and operations must keep the instance alive and sized. Cluster is a V2
decision (ADR-010).

### 2 · 20 ms fail-fast socket budget

`socket_timeout` and `socket_connect_timeout` are both 20 ms (`sentinel/redis.py:10`). This is
the "never wait beyond the configured timeout" invariant: a slow Redis is treated as a failed
Redis. Under load, on slow hosts, or across a wide network, `REDIS_TIMEOUT` is a *normal,
classified* outcome that routes through the §06 decision table — design dashboards and alerting
for it. Only the benchmark harness overrides the budget (a 5 s client for the live cells; the
dead-port failure cells keep 20 ms).

### 3 · Not idempotent (ADR-011)

A Redis call can time out locally while the script commits server-side. A client retry after a
timeout can consume quota the client believes it never used. Sentinel does not deduplicate
requests; solving this means idempotency keys, explicitly out of scope for V1. Retry logic in
the host application should treat `REDIS_TIMEOUT`/`FAIL_CLOSED` as "may or may not have
counted."

### 5 · Fail-open allowance is per-process

The emergency limiter's capacity and refill rate are both `fallback_rate_per_process_micro`
(`sentinel/emergency.py:37`): each process admits its own fallback rate, so a deployment of N
instances can admit up to N × the fallback rate during an outage. The rate is explicitly
named *per process*. Additionally, emergency buckets are keyed by `endpoint_id` only — tenant
fairness is not enforced during fail-open (project record §10). Choose the fallback rate with
your instance count in mind.

### 7 · JWT: HS* allowlist only, no JWKS (deferred to V2)

`AppConfig.jwt_algorithm_allowlist` must be a non-empty subset of {HS256, HS384, HS512}
(`sentinel/config.py:10`); asymmetric keys and JWKS are rejected at config load. Consequences:
a static shared secret (min 32 chars), rotation requires coordinated redeploy, and the host
application must issue HS*-signed tokens. JWKS is deferred to V2 to avoid introducing unhandled
network failure modes into request-time auth.

### 8 · JWT replay is an upstream boundary

A replayed valid bearer token is indistinguishable from a legitimate request at the Sentinel
layer. Sentinel requires `exp` + `sub`, enforces a strict algorithm allowlist, and keeps no
token cache or state — but replay detection (short-lived tokens, mTLS, single-use/nonce) lives
at the issuing service (project record §07).

### 10 · Sliding window: estimate, no Retry-After

The sliding-window counter returns an *estimate* — `current + previous × (remaining/window)`
— bounded by the reference formula, not an exact count (invariant #7). And sliding-window
denials carry no `Retry-After` header: the Lua result does not expose enough timing information
for a precise value (`sentinel/limiter.py:106`). Token-bucket denials do carry `Retry-After`.

### 13 · Redis downtime semantics are a product decision

There is no replica or failover path in V1. When Redis is down, fail-closed endpoints are
unavailable (503) and fail-open endpoints fall back to the emergency limiter. That asymmetry is
intentional (ADR-006, project record §06) — PDFTalk accepts downtime over unmetered compute;
Resumint accepts limited overrun over blocked users.

---

## Documented decisions that look like limitations

- **Auth failures never produce a `DecisionReason`** — 401s are not rate-limit decisions
  (`sentinel/http.py:110`); log/metrics tooling must not expect one.
- **`endpoint_id` is explicit, never derived from the URL** — renaming a route does not create
  a new bucket (ADR-009); it also means the guard must be told the id at route definition.
- **No `cost` parameter** — every request costs exactly one token; per-request weighted cost is
  a V2 design, not a bolt-on (SEC-01, project record §05).
- **No dynamic policy / admin surface** — static JSON config, validated strictly at load; the
  `policy_version` field exists so policy changes can be deployed as deliberate, auditable
  bumps.
- **Non-goals held from the original scope discipline** — no Kafka, no Kubernetes, no Redis
  Cluster, no additional algorithms, no Node SDK, no dashboard (project record §10).

---

## Deferred to V2 (full list)

Project record §10 keeps the canonical list: weighted request cost, Redis Cluster support and
hash-tag keys, per-tenant hash consolidation, distributed circuit-breaker state, additional
algorithms, Node SDK, admin dashboard, dynamic runtime configuration — plus JWKS/asymmetric
JWT support. None of these are hidden gaps; each is a decision with a trigger condition
documented in the record.
