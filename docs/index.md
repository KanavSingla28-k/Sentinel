# Sentinel

**Application-aware, tenant-aware, distributed rate limiting for FastAPI** — backed by a single
dedicated Redis instance and atomic Lua scripts.

Sentinel sits one layer above edge/CDN limiters: it understands *tenants* and *endpoints*, not
just IPs. Every endpoint gets its own algorithm (token bucket or sliding window), its own
capacity and rate, and — crucially — its own **failure semantics**: an endpoint that must never
overrun (expensive OCR compute) fails closed; an endpoint that must never block paying users
fails open, but is capped by an in-process emergency limiter so fail-open never means unlimited.

Sentinel is a **library, not a service**: it runs inside your FastAPI process. There is no
sidecar, no proxy, no deployment to manage — you install it, wire one dependency, and configure
policies.

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

---

## Key capabilities

- **Tenant-aware** — buckets are keyed on the validated JWT `sub` claim; the `X-Tenant-ID`
  header is ignored (spoofing is a locked regression test). Raw tenant ids never reach Redis
  keys, logs, or metrics — only a SHA-256 hash does.
- **Per-endpoint policies** — each endpoint declares its own algorithm, capacity/rate, and
  fail mode; the `endpoint_id` is always explicit, never derived from the URL.
- **Atomic distributed decisions** — every rate-limit decision is one Redis Lua script
  execution, clocked by Redis `TIME()` so every API instance agrees on time.
- **Designed failure semantics** — fail-closed (503) or fail-open (capped by an in-process
  emergency limiter), with a per-process circuit breaker so a down Redis never makes a request
  wait beyond a 20 ms budget.
- **Observable out of the box** — two bounded Prometheus metrics and structured WARNING logs on
  every denial.
- **Proven correctness** — 374 tests including 50-coroutine races, 3-process shared-bucket
  atomicity, and real failure injection; 100% coverage on `sentinel/`.

---

## Architecture at a glance

Three components carry the correctness story:

1. **Atomic Lua scripts** (`sentinel/lua/*.lua`) — each rate-limit decision is one Redis script
   execution, so concurrent requests cannot interleave a read-modify-write. The scripts use
   Redis `TIME()` as their only clock.
2. **The resiliency triangle** (`errors.py`, `circuit_breaker.py`, `emergency.py`) — Redis
   failures are classified, a per-process breaker short-circuits a dead Redis, and fail-open
   endpoints fall back to a capped in-process emergency limiter.
3. **Hashed tenant identity** — buckets are keyed
   `sentinel:v1:{sha256(tenant)}:{endpoint_id}:{policy_version}`; raw tenant ids never reach
   Redis keys, logs, or metrics.

See [Architecture](architecture.md) for the full walkthrough: module map, request journey,
clock discipline, and the eight non-negotiable invariants.

---

## Getting started

```powershell
pip install sentinel-rate-limiter
```

Requirements: Python ≥ 3.11 and a dedicated Redis 7 instance configured with `noeviction` and
a bounded `maxmemory` (Sentinel refuses to start otherwise).

- [Installation Guide](installation.md) — step-by-step from `pip install` to a working
  FastAPI integration, including Redis setup, configuration, and testing.
- [Quick Start](quickstart.md) — the smallest realistic path: install → configure →
  integrate → rate limit.
- [Configuration](configuration.md) — every setting in the JSON config, with defaults and
  constraints.
- [Usage](usage.md) — how to initialize Sentinel and apply it to FastAPI endpoints.
- [Observability](observability.md) — what Sentinel records and how to observe it.
- [Architecture](architecture.md) — module map, request journey, clock discipline, invariants.
- [Failure Semantics](failure-semantics.md) — fail-open vs fail-closed vs the emergency
  limiter, and why different endpoints need different behavior.

The repository also ships deeper project documentation:
[Failure handling](failure-handling.md), [Known limitations](known-limitations.md),
[Benchmark results](benchmark-results.md), and the
[project record](sentinel-project-record.md).
