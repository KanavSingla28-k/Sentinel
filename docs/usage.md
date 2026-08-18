# Usage

This page shows how an application actually uses Sentinel, from first import to a guarded
endpoint. It assumes you have installed the package and set up Redis
(see [Installation](installation.md) and [Quick Start](quickstart.md)).

---

## What Sentinel provides

Sentinel ships as an in-process FastAPI dependency factory:

- **`SentinelGuard`** (`sentinel/http.py`) — the FastAPI integration. You create one guard for
  your whole application and attach it to endpoints via `guard.guard_for("endpoint_id")`.
- **Strict configuration** (`sentinel.config.load_config`) — one JSON file, validated loudly at
  startup; bad config is an immediate error, not a runtime surprise.
- **A Redis foundation** (`SentinelRedis`, `ScriptLoader`) — the client pool with a 20 ms
  fail-fast budget, the `noeviction` startup check, and Lua script management with NOSCRIPT
  auto-recovery.

There is no sidecar and no service to deploy: Sentinel lives inside your FastAPI process.

## 1 · Initialize Sentinel

Create the guard once at module scope and load the Lua scripts in the app lifespan:

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
```

What each piece does:

| Piece | Role |
|---|---|
| `load_config(Path("sentinel.json"))` | Parses + validates the config; unknown keys, mismatched policy keys, or per-algorithm field mixes are immediate, loud errors |
| `SentinelRedis(url)` | Client pool with a **20 ms fail-fast budget**; runs the `noeviction` + bounded-`maxmemory` startup check and raises if Redis is misconfigured |
| `ScriptLoader(redis.client)` | Loads and executes the Lua scripts, with NOSCRIPT auto-recovery |
| `SentinelGuard(config, redis, loader)` | The FastAPI integration; explicit dependencies, no hidden singletons |
| `await guard.load_scripts()` in `lifespan` | **Required** before the first request — forget it and the app raises on startup |

## 2 · Configure rate limiting

Rate limits are static, per-endpoint policies in the JSON config — see
[Configuration](configuration.md) for every field. A minimal example:

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

## 3 · Apply Sentinel to FastAPI endpoints

Attach the guard dependency to the routes you want protected. The `endpoint_id` is the explicit
configured id — it is **never** derived from the URL path:

```python
@app.post("/tailor")
async def tailor(
    request: Request, _: None = Depends(guard.guard_for("resumint.tailor"))
) -> dict[str, object]:
    return {"allowed": request.state.decision.allowed}
```

When the request is admitted, the decision is attached to `request.state.decision` — your
business logic goes inside the guarded handler and can read
`request.state.decision.allowed` (`True` here). Different endpoints get different policies by
passing their own `endpoint_id`; a guarded endpoint without a matching policy returns 404.

### What clients must send

`Authorization: Bearer <JWT>` where the JWT is signed with an allowlisted HS* algorithm and
carries both `exp` and `sub`. The `sub` claim is the tenant identity — everything is keyed on
it. The `X-Tenant-ID` header is ignored (deliberately; spoofing is a locked regression test).

## 4 · What happens when the limit is exceeded

Denied requests never reach your handler; the guard raises an `HTTPException` before it:

| Situation | Status | Body | Headers |
|---|---|---|---|
| Rate limit exceeded (token bucket or emergency cap) | 429 | `{"detail": "rate limit exceeded"}` | `Retry-After` when computable |
| Store failure on a fail-closed endpoint | 503 | `{"detail": "rate limiter unavailable"}` | — |
| Missing/invalid token | 401 | `{"detail": "authentication required"}` | `WWW-Authenticate: Bearer` |
| Unknown `endpoint_id` | 404 | `{"detail": "unknown endpoint"}` | — |

Notes:

- Auth failures (401) happen **before** any Redis call and never count as rate-limit decisions.
- `Retry-After` is emitted when the decision provides it: token-bucket denials do, sliding-window
  denials deliberately do not (the window is an estimate).
- Every denied decision emits a WARNING structured log and increments the decision metrics —
  see [Observability](observability.md).
- When Redis fails, behavior depends on the endpoint's `fail_mode` — see
  [Failure Semantics](failure-semantics.md).

## 5 · Shutting down

Close the Redis client pool when the application exits:

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    await guard.load_scripts()
    yield
    await redis.aclose()
```

## Related

- [Quick Start](quickstart.md) — the minimal end-to-end path.
- [Configuration](configuration.md) — every config field, default, and constraint.
- [Failure Semantics](failure-semantics.md) — fail-open vs fail-closed and the emergency
  limiter.
