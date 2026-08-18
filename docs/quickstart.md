# Quick Start

The smallest realistic path from zero to a rate-limited FastAPI endpoint:

```text
install Sentinel
      ↓
configure Sentinel
      ↓
integrate with FastAPI
      ↓
apply rate limiting
```

The full, annotated walkthrough lives in the [Installation Guide](installation.md) — this page
is the compressed version.

---

## 1 · Install Sentinel

```powershell
pip install sentinel-rate-limiter
```

The import name is `sentinel` (the distribution name is `sentinel-rate-limiter`). Requires
Python ≥ 3.11 and a dedicated Redis 7 instance with `noeviction` + bounded `maxmemory`.

## 2 · Configure Sentinel

Create `sentinel.json` (copy the working example from
[`sentinel.example.json`](https://github.com/KanavSingla28-k/Sentinel/blob/main/sentinel.example.json)):

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

Every endpoint policy must declare an explicit `endpoint_id`, an algorithm
(`token_bucket` or `sliding_window`), a fail mode (`fail_open` or `fail_closed`), and the
per-algorithm fields. See [Configuration](configuration.md) for every field and its constraints.

## 3 · Integrate with FastAPI

Wire the guard once at startup, then attach it as a dependency per endpoint:

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

## 4 · Apply rate limiting

Guard an endpoint by passing the configured `endpoint_id` to `guard_for(...)`:

```python
@app.post("/tailor")
async def tailor(
    request: Request, _: None = Depends(guard.guard_for("resumint.tailor"))
) -> dict[str, object]:
    return {"allowed": request.state.decision.allowed}
```

The `endpoint_id` is the explicit configured id — it is never derived from the URL path.

Run it:

```powershell
uvicorn app:app --port 8000
```

Clients must send `Authorization: Bearer <JWT>` with a validated `sub` claim (the tenant
identity). When a request exceeds the limit, Sentinel denies it with **429** (rate limit
exceeded, `Retry-After` where computable) — or **503** for store failures on fail-closed
endpoints. The handler runs only when `request.state.decision.allowed` is `True`.

---

That's it. For the full guide — Redis setup, JWT generation, testing 401/429/503 paths, and
watching the failure semantics in action — see the [Installation Guide](installation.md).
