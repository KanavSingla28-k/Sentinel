# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

Sentinel — an application-aware, tenant-aware, distributed rate limiting **library** for
FastAPI (Python ≥ 3.11). It runs inside the user's FastAPI process, backed by a single
dedicated Redis 7 instance and atomic Lua scripts. It is a library, not a service: no
sidecar, no proxy.

**Phase 19 (v1.2.0):** Adds anonymous (unauthenticated) rate limiting with signed
client cookie + trusted-client IP dual bucket. No forwarding headers read; cookie minted
on first request, delivered only on allowed responses; dual-bucket AND semantics.

Read `docs/architecture.md` (module map, request journey, clock discipline, design
invariants) and `docs/sentinel-project-record.md` (history, ADRs, security findings) before
making changes.

## Commands

```powershell
pip install -e ".[dev]"            # install with dev tooling (pytest, ruff, mypy, pre-commit)
pytest                              # run suite (Redis-dependent tests SKIP if unreachable)
pytest -m slow                      # concurrency / failure-injection / packaging suite
pytest -m security                  # security regression suite
pytest -m integration               # tests requiring a real reachable Redis
pytest --cov=sentinel --cov-report=term-missing   # coverage check (must be 100%)
ruff check .                        # lint (line-length 100, E/F/I/UP/B/SIM)
ruff format --check .               # format check (double quotes)
mypy sentinel                       # strict type checking (pydantic plugin, warn_unreachable)
pre-commit run --all-files          # pre-commit hooks (ruff, mypy, trailing-whitespace, etc.)
python -m build                     # build wheel + sdist (validated by tests/test_packaging.py)
```

## Non-negotiable design invariants

Each is enforced by tests and documented in `docs/architecture.md` §7. Do not break them:

1. **Redis `TIME()` is the only clock** in the rate-limiting Lua scripts — application clocks
   never enter the algorithm.
2. **Integer microtokens only** — no floats in state.
3. **Explicit `endpoint_id`** — always a configured id, never derived from the URL/path.
4. **JWT-only tenant identity** — the `X-Tenant-ID` header is ignored; auth failures never
   produce a `DecisionReason`. Raw tenant ids must never reach Redis keys, logs, or metrics
   (only `sha256` hashes).
5. **No client-reachable numeric input** — no `cost` parameter; script arguments are
   server-side policy values only.
6. **Lua integer exactness** — config arithmetic must stay exact below 2^52 (see constants in
   `sentinel/models.py`). Denied requests never write to Redis.
7. **Anonymous identity hygiene** — cookie/IP identities are `anon:cookie:*` / `anon:ip:*`,
   sha256-hashed into `sentinel:v2:` keyspace; raw ids/IPs never reach keys, logs, or metrics.
   Forwarding headers (`X-Forwarded-For`, `X-Real-IP`, `CF-Connecting-IP`) are never read.

## Architecture at a glance

- `sentinel/lua/*.lua` — the two atomic rate-limit scripts (`token_bucket.lua`,
  `sliding_window.lua`); each decision is one Redis script execution.
- `sentinel/redis.py` — `SentinelRedis` client + `ScriptLoader` with NOSCRIPT recovery.
- `sentinel/limiter.py` — `RateLimiter` orchestration; `build_bucket_key`, `build_anonymous_key`,
  `hash_tenant`, `evaluate_anonymous` (dual-bucket AND, terminal failure short-circuit).
- `sentinel/anonymous.py` — anonymous identity: `mint_cookie`, `parse_cookie`, `client_ip`,
  `anonymous_cookie_identity`, `anonymous_ip_identity`, `hash_identity`.
- `sentinel/http.py` — `SentinelGuard`, the **only** FastAPI-aware layer; per-route dependency
  factory taking an explicit `endpoint_id`. `guard_for` (tenant JWT) and `anonymous_guard_for`
  (cookie + IP). Denials map to 429 (with `Retry-After`) or 503.
- `sentinel/auth.py` — JWT verification (HS* allowlist only, requires `exp` + `sub`).
- `sentinel/resolver.py` — static policy lookup from config; `resolve` + `resolve_anonymous`.
- `sentinel/config.py` — strict config loading; unknown keys rejected (`extra="forbid"`).
- `sentinel/models.py` — `Policy`, `Decision`, `DecisionReason` (8 bounded reasons),
  `IdentityMode` (`TENANT_JWT` / `ANONYMOUS`).
- Resiliency triangle: `sentinel/errors.py` (classify), `sentinel/circuit_breaker.py`
  (per-process CLOSED/OPEN/HALF_OPEN, trips after 5 failures for 30 s),
  `sentinel/emergency.py` (fail-open in-process limiter).
- `sentinel/observability.py` — structured logs + Prometheus metrics (bounded labels only:
  `endpoint_id`/`decision_reason`, never tenant). `record_decision` takes `identity_mode` +
  `identity_hash`.

State is one Redis string per bucket, written with `SET` + `EXPIRE` only (no `DEL`, no
`KEYS`/`SCAN`, no eviction). Keys:
- Tenant: `sentinel:v1:{sha256(tenant)}:{endpoint_id}:{policy_version}`
- Anonymous: `sentinel:v2:{sha256(identity)}:{endpoint_id}:{policy_version}` where
  identity is `anon:cookie:{client_id}` or `anon:ip:{ip}`.

## Conventions

- Every module has a docstring; many cite phase numbers/ADRs (e.g. "Phase 7", "ADR-009") —
  preserve and reference these.
- Follow existing style: pydantic v2 `model_config = ConfigDict(frozen=True, extra="forbid")`,
  `enum.StrEnum`, type hints everywhere.
- `tests/conftest.py` provides a `redis_client` fixture that **skips** when Redis is
  unreachable (`SENTINEL_REDIS_URL` env var, default `redis://localhost:6379/0`).
- Test markers (`slow`, `security`, `integration`) are strict — new tests must declare them.
- Real Redis tests must keep working with a plain `redis:7-alpine` service container (see
  `.github/workflows/ci.yml`).

## Quality gates (all enforced in CI)

- 375 tests passing (0 skipped against real Redis), **100% coverage** on `sentinel/` — CI
  fails below it (`fail_under = 100`).
- `mypy --strict` clean on `sentinel`.
- `ruff check` + `ruff format --check` clean.
- Packaging is proven by `tests/test_packaging.py` (wheel build, twine check, fresh-venv
  install smoke).
- CI jobs: `lint`, `security`, `test` (fast + coverage), `slow`, `packaging`; `publish` to
  PyPI only on `v*` tags.
- Trunk-based workflow: short-lived `feat/`/`fix/`/`test/`/`docs/` branches, squash-merge PRs.

## Development notes

- The library refuses to start unless Redis is configured with `noeviction` and bounded
  `maxmemory` — Sentinel is single-Redis by design (see `docs/known-limitations.md`).
- `benchmarks/benchmark.py` is a dependency-free harness; results are single-machine loopback
  numbers for regression comparison only.
- `.hypothesis/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/` are local caches — never
  commit or reference them.
