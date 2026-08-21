# Sentinel — Installation Guide

A step-by-step guide from zero to a working rate-limited FastAPI endpoint. The README is the
overview; this document is the hands-on walkthrough.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Install the library](#2-install-the-library)
3. [Set up Redis](#3-set-up-redis)
4. [Create the configuration file](#4-create-the-configuration-file)
5. [Wire Sentinel into FastAPI](#5-wire-sentinel-into-fastapi)
6. [Generate a JWT and test it](#6-generate-a-jwt-and-test-it)
7. [Watch it fail (fail-open vs fail-closed)](#7-watch-it-fail)
8. [Observability](#8-observability)
9. [Troubleshooting](#9-troubleshooting)
10. [Where to go next](#10-where-to-go-next)

---

## 1. Prerequisites

| Requirement | Version | Notes                                                                                                  |
| ----------- | ------- | ------------------------------------------------------------------------------------------------------ |
| Python      | ≥ 3.11  |                                                                                                        |
| Redis       | 7.x     | a **dedicated instance** with `noeviction` + bounded `maxmemory` — Sentinel refuses to start otherwise |
| FastAPI app | any     | Sentinel is a library that lives inside your app; no service to deploy                                 |

The library itself pulls in `redis`, `fastapi`, `pydantic`, `PyJWT`, and `prometheus-client`.

## 2. Install the library

```powershell
pip install sentinel-rate-limiter
```

The import name is `sentinel` (the distribution name differs from the import name — this is
intentional).

For development on Sentinel itself (running the test suite, benchmarks):

```powershell
pip install -e ".[dev]"
```

## 3. Set up Redis

Sentinel needs a Redis instance it can trust: `noeviction` means keys are never silently
dropped, and a bounded `maxmemory` forces you to size the instance deliberately. If you cloned
the repo, it ships a `docker-compose.yml` with exactly the required config:

```powershell
docker compose up -d
```

The equivalent raw config (if you run Redis yourself, or if you only pip-installed the library
and don't have the repo):

```text
maxmemory 256mb
maxmemory-policy noeviction
```

Verify against a running instance:

```powershell
docker compose exec redis redis-cli CONFIG GET maxmemory-policy
# -> maxmemory-policy
# -> noeviction
```

Sentinel checks this at startup (`assert_noeviction()`) and raises if the policy is not
`noeviction` — a misconfigured Redis cannot silently weaken your quotas.

## 4. Create the configuration file

Sentinel loads a strict JSON config (`SentinelConfig` is frozen; unknown keys are **rejected**,
and policy dict keys must match the policy's `endpoint_id`). The repo has a working example at
[`sentinel.example.json`](https://github.com/KanavSingla28-k/Sentinel/blob/main/sentinel.example.json) — copy it and edit (pip-only consumers can
grab the same file from the GitHub repo, or write it by hand following the annotated sections
below):

```powershell
Copy-Item sentinel.example.json sentinel.json
```

```json
{
  "app": {
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret": "dev-only-secret-change-me-0123456789abcdef",
    "jwt_algorithm_allowlist": ["HS256"],
    "anonymous_cookie_secret": "anon-dev-only-secret-change-me-0123456789abcdef",
    "anonymous_cookie_name": "sentinel_anon_id",
    "anonymous_cookie_ttl_seconds": 2592000,
    "anonymous_cookie_secure": false
  },
  "policies": {
    "pdftalk.ingest": {
      "endpoint_id": "pdftalk.ingest",
      "algorithm": "sliding_window",
      "fail_mode": "fail_closed",
      "fallback_rate_per_process_micro": 5000,
      "policy_version": 1,
      "limit": 1000,
      "window_size_micro": 60000000
    },
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

### `app` section

| Field                          | Meaning                                                                      |
| ------------------------------ | ---------------------------------------------------------------------------- |
| `redis_url`                    | `redis://` URL of the dedicated instance                                     |
| `jwt_secret`                   | Shared HMAC secret, **min 32 chars**                                         |
| `jwt_algorithm_allowlist`      | Non-empty subset of `HS256`/`HS384`/`HS512` (JWKS/asymmetric deferred to V2) |
| `anonymous_cookie_secret`      | Separate HMAC secret, min 32 chars; required for anonymous policies          |
| `anonymous_cookie_name`        | Cookie name; default `sentinel_anon_id`                                      |
| `anonymous_cookie_ttl_seconds` | Cookie lifetime; 3,600 to 7,776,000 seconds, default 30 days                 |
| `anonymous_cookie_secure`      | Defaults to `true`; set `false` only for local HTTP development              |

### `policies` section

Each policy is keyed by (and must declare) an explicit `endpoint_id` — the id is **never**
derived from the URL path. Common fields:

| Field                             | Meaning                                                                            |
| --------------------------------- | ---------------------------------------------------------------------------------- |
| `endpoint_id`                     | Explicit configured id, pattern `^[a-z0-9._-]+$`                                   |
| `algorithm`                       | `token_bucket` or `sliding_window`                                                 |
| `fail_mode`                       | `fail_closed` (503 when the store fails) or `fail_open` (capped emergency limiter) |
| `fallback_rate_per_process_micro` | Fail-open allowance per process (µtokens/s); burst = 1 s of this rate              |
| `policy_version`                  | Bump deliberately when a policy changes (it is part of the Redis key)              |

Token-bucket fields: `capacity_micro` (burst size) and `refill_rate_micro_per_sec` (sustained
rate). Sliding-window fields: `limit` and `window_size_micro` (default 60 s). Mixing the two
families is rejected at load, and configuration whose arithmetic would exceed Lua's integer
exactness (2^53) is rejected too.

For an unauthenticated endpoint, add `identity: "anonymous"` to its policy and use
`guard.anonymous_guard_for(endpoint_id)`. v1.2.0 evaluates a signed device-cookie bucket and a
trusted `request.client.host` IP bucket; both must allow. The cookie secret is separate from the
JWT secret, and raw cookie ids and IPs are hashed before keys, logs, or metrics are produced.

> All values are **microtokens** (1 token = 1,000,000 µtokens). A capacity of `10000000` is 10
> tokens; a refill rate of `10000` is 0.01 tokens/s — tune these to your scale. Integer math
> only; no floats anywhere in the state.

## 5. Wire Sentinel into FastAPI

This is the entire integration — it mirrors the real wiring in
`tests/test_http_integration.py`. Create `app.py`:

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

What each piece does:

| Line                                          | What happens                                                                                      |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `load_config(Path("sentinel.json"))`          | Parses + validates the config; bad config is an immediate, loud error                             |
| `SentinelRedis(url)`                          | Creates the client pool with a **20 ms fail-fast budget** and runs the `noeviction` startup check |
| `ScriptLoader(redis.client)`                  | Loads and executes the Lua scripts, with NOSCRIPT auto-recovery                                   |
| `SentinelGuard(config, redis, loader)`        | The FastAPI integration; explicit dependencies, no hidden singletons                              |
| `await guard.load_scripts()` in `lifespan`    | **Required** before the first request — forget it and the app raises on startup                   |
| `Depends(guard.guard_for("resumint.tailor"))` | The guard dependency; the `endpoint_id` is the explicit configured id                             |

The handler receives the decision via `request.state.decision` — `request.state.decision.allowed`
is `True` when the request is admitted. Your business logic goes inside the guarded handler.

### What clients must send

`Authorization: Bearer <JWT>` where the JWT is signed with an allowlisted HS\* algorithm and
carries both `exp` and `sub`. The `sub` claim is the tenant identity — everything is keyed on
it. The `X-Tenant-ID` header is ignored (deliberately; spoofing is a locked regression test).

Denied requests map to:

| Situation                                           | Status | Body                                     | Headers                       |
| --------------------------------------------------- | ------ | ---------------------------------------- | ----------------------------- |
| Rate limit exceeded (token bucket or emergency cap) | 429    | `{"detail": "rate limit exceeded"}`      | `Retry-After` when computable |
| Store failure on a fail-closed endpoint             | 503    | `{"detail": "rate limiter unavailable"}` | —                             |
| Missing/invalid token                               | 401    | `{"detail": "authentication required"}`  | `WWW-Authenticate: Bearer`    |
| Unknown `endpoint_id`                               | 404    | `{"detail": "unknown endpoint"}`         | —                             |

## 6. Generate a JWT and test it

`PyJWT` ships with Sentinel, so token generation is available in your environment:

```python
import time
import jwt

token = jwt.encode(
    {"sub": "tenant-alice", "exp": int(time.time()) + 3600},
    "dev-only-secret-change-me-0123456789abcdef",
    algorithm="HS256",
)
print(token)
```

Start the app:

```powershell
uvicorn app:app --port 8000
```

And exercise it (`curl.exe` shows the status line and headers, which is what you want to see
for the 429/401 cases — PowerShell's `Invoke-WebRequest` throws on non-2xx responses):

```powershell
$token = "eyJ..."   # paste the token printed above

# 1. First call — allowed: 200 with {"allowed": true}
curl.exe -i -X POST http://localhost:8000/tailor -H "Authorization: Bearer $token"

# 2. Drain the bucket — denied: 429 with a Retry-After header
curl.exe -i -X POST http://localhost:8000/tailor -H "Authorization: Bearer $token"

# 3. A second tenant gets its own independent bucket
#    (generate a token with "sub": "tenant-bob" and repeat step 1)

# 4. A bad token — 401 before any Redis call, WWW-Authenticate: Bearer header
curl.exe -i -X POST http://localhost:8000/tailor -H "Authorization: Bearer not-a-token"
```

## 7. Watch it fail

The failure semantics are the heart of the design. With the example config above, stop Redis
and watch each endpoint behave (`docker compose stop redis` — start it again with
`docker compose start redis`):

- **`resumint.tailor` (fail_open)** — requests keep flowing, but the
  in-process emergency limiter caps them at `fallback_rate_per_process_micro` (2000 µtokens/s,
  burst 1 s). Users are never blocked by a store outage; they are limited, not hung.
- **`pdftalk.ingest` (fail_closed)** — stop Redis and requests get **503** in ~20–30 ms. Expensive
  jobs are never unmetered; traffic resumes automatically when the store recovers.

The circuit breaker also trips OPEN after 5 consecutive failures and short-circuits for 30 s —
recovering requests are not penalized (only genuine Redis successes reset it), and a down Redis
never makes a request wait beyond the 20 ms socket budget. Full decision table and state
machine: [failure-handling.md](failure-handling.md).

## 8. Observability

Every decision is observable out of the box — no extra wiring required:

- **Prometheus metrics** — `sentinel_decisions_total` (counter) and
  `sentinel_evaluate_latency_microseconds` (histogram), labeled only by
  `endpoint_id`/`decision_reason` (bounded label sets — no tenant label, no cardinality bomb).
  The collectors register once on the default registry, so your existing
  `prometheus_client` scrape endpoint picks them up.
- **Logs** — every denied decision emits a WARNING structured log (`logger name: sentinel`)
  with `identity_mode`, `identity_hash`, `endpoint_id`, `decision_reason`, `latency_micro`, and
  `breaker_state`. Tenant/JWT logs hash the validated JWT `sub`; anonymous logs hash the primary
  cookie identity when valid, otherwise the IP identity. Raw identities are never logged.

The 8 `DecisionReason` values: `RATE_LIMITED`, `EMERGENCY_LOCAL_LIMIT`, `FAIL_CLOSED`,
`CIRCUIT_OPEN`, `REDIS_TIMEOUT`, `REDIS_CONNECTION_ERROR`, `REDIS_NOSCRIPT_RETRY`, `ALLOWED`.

## 9. Troubleshooting

| Symptom                                                                      | Cause / fix                                                                                                                 |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `RuntimeError: maxmemory-policy is '...'; Sentinel requires 'noeviction'`    | Redis not configured per §3 — set `maxmemory-policy noeviction`                                                             |
| `RuntimeError: maxmemory is unset; Sentinel requires a bounded memory limit` | Redis has no `maxmemory` — set a bounded limit (e.g. `maxmemory 256mb`)                                                     |
| `RuntimeError: scripts not loaded` on the first request                      | `await guard.load_scripts()` missing from the FastAPI `lifespan` (or the lifespan isn't wired)                              |
| `ValidationError` on `load_config`                                           | Strict config: unknown keys, policy-dict keys that don't match `endpoint_id`, or per-algorithm field mixes are all rejected |
| Requests take ≈20–30 ms and return 503                                       | The 20 ms fail-fast budget hit: Redis unreachable or saturated. This is the designed failure path, not a bug — check Redis  |
| 401 on every request                                                         | Wrong `jwt_secret` (min 32 chars, must match the signing secret), or tokens missing `exp`/`sub`                             |
| `sub` not a string / not valid                                               | `sub` must be a valid JWT subject — invalid values are rejected with 401                                                    |

## 10. Where to go next

- [Home](index.md) — the documentation homepage (based on the README)
- [Architecture](architecture.md) — module map, request journey, clock discipline, invariants
- [Failure handling](failure-handling.md) — the resiliency triangle in depth
- [Known limitations](known-limitations.md) — read before production (per-process breaker, HS\* only, no idempotency keys, ...)
- [Benchmark results](benchmark-results.md) — measured overhead and failure-path latency
- [Project record](sentinel-project-record.md) — the frozen V1 spec and review history
- [`sentinel.example.json`](https://github.com/KanavSingla28-k/Sentinel/blob/main/sentinel.example.json) — the working example config
