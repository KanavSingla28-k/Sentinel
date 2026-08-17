# Sentinel

**Application-aware, tenant-aware, distributed rate limiting for FastAPI**, backed by a single
dedicated Redis instance and atomic Lua scripts.

Sentinel sits one layer above edge/CDN limiters: it understands *tenants* and *endpoints*, not
just IPs. Each endpoint gets its own algorithm (token bucket or sliding window), its own
capacity and rate, and — crucially — its own **failure semantics**: an endpoint that must never
overrun (expensive OCR compute) fails closed; an endpoint that must never block paying users
fails open, but is capped by an in-process emergency limiter so fail-open never means unlimited.

The design survives adversarial review: tenant identity comes only from a validated JWT `sub`
claim, the Lua scripts use Redis `TIME()` as their only clock, all arithmetic is integer
microtokens, and every failure resolves to one of 8 bounded `DecisionReason` values. See
`docs/sentinel-project-record.md` for the frozen V1 spec and the review history behind it.

## Highlights

- **Atomic correctness under concurrency** — token bucket and sliding window run as Lua scripts
  (`sentinel/lua/*.lua`), executed atomically by Redis; verified against pure-Python references
  (25 parity tests) and under 50-coroutine / multi-process races.
- **The resiliency triangle** — failure classification (`sentinel/errors.py`), a per-process
  circuit breaker (`sentinel/circuit_breaker.py`), and a fail-open emergency limiter
  (`sentinel/emergency.py`). A Redis failure never makes a request wait beyond a 20 ms budget,
  and always resolves to a decision-table row.
- **Tenant isolation** — buckets keyed
  `sentinel:v1:{sha256(tenant)}:{endpoint_id}:{policy_version}`; raw tenant ids never reach
  Redis keys, logs, or metrics.
- **Bounded observability** — `sentinel_decisions_total` and
  `sentinel_evaluate_latency_microseconds`, labeled only by `endpoint_id`/`decision_reason`;
  WARNING structured deny logs carry `tenant_hash`, never the tenant.
- **Security posture locked by tests** — 23 `security`-marked regression tests cover spoofing,
  Lua TTL-only expiry, cardinality bombs, and more.

## Installing

```powershell
pip install sentinel-rate-limiter
```

Sentinel is a library, not a service: install it into your FastAPI application's environment,
then wire `SentinelGuard` into your app (quick start below). You still need a dedicated Redis 7
instance configured with `noeviction` and a bounded `maxmemory` — Sentinel refuses to start
otherwise. For development, install the library plus its tooling instead:

```powershell
pip install -e ".[dev]"
```

## Quick start

Requirements: Python ≥ 3.11, a Redis 7 instance configured with `noeviction` and a bounded
`maxmemory` (Sentinel refuses to start otherwise).

```powershell
pip install -e ".[dev]"        # library + dev tooling
docker compose up -d           # dedicated Redis with the required config
```

Write a config file (see `sentinel.example.json`):

```json
{
  "app": {
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret": "dev-only-secret-change-me-0123456789abcdef",
    "jwt_algorithm_allowlist": ["HS256"]
  },
  "policies": {
    "resumint.tailor": {
      "endpoint_id": "resumint.tailor",
      "algorithm": "token_bucket",
      "fail_mode": "fail_open",
      "fallback_rate_per_process_micro": 2000,
      "policy_version": 1,
      "capacity_micro": 10000000,
      "refill_rate_micro_per_sec": 10000
    }
  }
}
```

Wire the guard into FastAPI (this mirrors `tests/test_http_integration.py`):

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from sentinel.config import load_config
from sentinel.http import SentinelGuard
from sentinel.redis import ScriptLoader, SentinelRedis

config = load_config(Path("sentinel.json"))
redis = SentinelRedis(config.app.redis_url)
loader = ScriptLoader(redis.client)

guard = SentinelGuard(config, redis, loader)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await guard.load_scripts()  # required before the first request
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/tailor")
async def tailor(
    request: Request, _: None = Depends(guard.guard_for("resumint.tailor"))
) -> dict[str, object]:
    return {"allowed": request.state.decision.allowed}
```

That's the whole integration: clients send `Authorization: Bearer <JWT>` (signed with an
allowlisted HS* algorithm, carrying `exp` and `sub`), and Sentinel decides. Denied requests map
to 429 (rate limits, with `Retry-After` where computable) or 503 (store failures); auth
failures map to 401 before any Redis call.

## Configuration reference

`SentinelConfig` is strict: unknown keys are rejected, policy dict keys must match
`Policy.endpoint_id`, and per-algorithm parameters are validated (e.g. sliding-window policies
must not carry bucket fields; Lua integer-exactness bounds are enforced at load).

| `AppConfig` field | Meaning |
|---|---|
| `redis_url` | `redis://` URL of the dedicated instance |
| `jwt_secret` | Shared HMAC secret, min 32 chars |
| `jwt_algorithm_allowlist` | Non-empty subset of `HS256`/`HS384`/`HS512` (JWKS deferred to V2) |

| `Policy` field | Meaning |
|---|---|
| `endpoint_id` | Explicit configured id (`^[a-z0-9._-]+$`), never derived from the URL |
| `algorithm` | `token_bucket` (needs `capacity_micro`, `refill_rate_micro_per_sec`) or `sliding_window` (needs `limit`; `window_size_micro` default 60 s) |
| `fail_mode` | `fail_closed` (503 on store failure) or `fail_open` (emergency limiter, capped) |
| `fallback_rate_per_process_micro` | Fail-open allowance per process (burst = 1 s of this rate) |
| `policy_version` | Bumped deliberately when a policy changes |

## How it works

One request, six stages — auth → policy resolution → rate limiting → failure/resiliency →
observability. The deep walkthrough lives in `docs/architecture.md`; the highlights:

- **Token bucket** (`sentinel/lua/token_bucket.lua`) — integer microtokens, refill from Redis
  `TIME()`, TTL until the bucket would be full again. Denied requests never write.
- **Sliding window** (`sentinel/lua/sliding_window.lua`) — `current + previous ×
  (remaining/window)`, anchored to Redis `TIME()`, 2-window TTL (expiry is lossless).
- **Breaker + emergency limiter** — OPEN short-circuits before Redis; fail-open falls back to
  an in-process token bucket at `fallback_rate_per_process_micro` that persists state only on
  ALLOW. Details in `docs/failure-handling.md`.

## Failure semantics at a glance

| Redis outcome | Fail-open endpoint | Fail-closed endpoint |
|---|---|---|
| Success | Lua result | Lua result |
| Timeout / connection error / NOSCRIPT exhausted | Emergency limiter (429 when capped) | Deny, 503 |
| Breaker OPEN | Emergency limiter (429 when capped) | Deny, 503 |

Full table, classifier mapping, breaker state machine, and measured failure-path latency
(B8/B9 p99 ≈ 22–29 ms, dominated by the 20 ms socket budget): `docs/failure-handling.md`.

## Observability

Every decision increments `sentinel_decisions_total` and observes
`sentinel_evaluate_latency_microseconds`, both labeled `{endpoint_id, decision_reason}` — two
bounded label sets, no tenant label. Every denied decision additionally emits a WARNING log
(`logger name: sentinel`) with `tenant_hash`, `endpoint_id`, `decision_reason`, `latency_micro`,
`breaker_state`.

## Known limitations

V1 is deliberately small. Read `docs/known-limitations.md` before production: single dedicated
Redis instance (`noeviction` required), 20 ms fail-fast socket budget, no idempotency keys
(ADR-011 — retries after a timeout may double-charge), per-process breaker and emergency
limiter (fail-open allowance scales with instance count), HS* JWT only (JWKS deferred to V2),
JWT replay handled upstream, sliding-window denials carry no `Retry-After`.

## Testing & performance

- 294 tests, 100 % coverage on `sentinel/`; `security`-marked (23) and `slow`-marked (10, incl.
  concurrency, multi-process, and failure-injection) suites run as dedicated CI jobs against
  real Redis.
- Phase 14 benchmark harness (`benchmarks/benchmark.py`, stdlib-only) with the baseline in
  `docs/benchmark-results.md`: with-Sentinel ≈ 5.2× throughput overhead at concurrency 1 (one
  loopback Redis round trip dominating), breaker short-circuit ≈ 7 µs p50, failure path ≈ the
  dead-port socket timeout. Single-machine loopback figures, reported as-is — not a
  throughput guarantee.

```powershell
docker compose up -d
pytest                          # full suite (integration tests need real Redis)
pytest -m security              # security regression suite
pytest -m slow                  # concurrency + failure-injection suite
pytest --cov=sentinel --cov-report=term-missing   # must stay 100%
mypy sentinel                   # strict
ruff check . && ruff format --check .
python benchmarks/benchmark.py --smoke
```

## Development

- Trunk-based workflow; short-lived `feat/`/`fix/`/`test/`/`docs/` branches, squash-merge PRs.
- Strict `mypy --strict`, ruff check + format, pre-commit hooks; no comments in code unless
  asked.
- Packaging is proven by tests, not assumptions: `tests/test_packaging.py` builds the wheel and
  sdist, asserts their contents (Lua sources, `py.typed`, no
  `tests/`/`benchmarks/`/`examples/` leaks) and metadata, runs `twine check`, and
  smoke-installs the wheel into a fresh venv. The `packaging` CI job repeats the build and
  fresh-venv install on every PR and push; the `publish` job uploads to PyPI only on `v*`
  tags.
- Non-negotiable invariants (Redis `TIME()` is the only algorithm clock, integer microtokens,
  explicit `endpoint_id`, JWT-only identity, no client-reachable numeric input) — see
  `AGENTS.md` and `docs/architecture.md` §7.

## License

MIT — see `LICENSE`.
