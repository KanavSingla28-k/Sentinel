# Sentinel — Agent Context

Project context and work history for AI agents working in this repo.
Read `docs/sentinel-project-record.md` for the frozen V1 spec; this file is the operational summary.

## What this project is

**Sentinel** — an application-layer, tenant-aware, distributed rate limiter as an in-process
Python library for FastAPI, backed by Redis with atomic Lua scripts. Consumed (conceptually) by
two services with deliberately different policy semantics:

| Service | Endpoint id | Algorithm | Fail mode |
|---|---|---|---|
| PDFTalk | `pdftalk.ingest` | sliding_window | fail_closed (expensive OCR; 503 on Redis failure) |
| Resumint | `resumint.tailor` | token_bucket | fail_open (UX-sensitive; emergency local limiter) |

## Repo map

- `sentinel/models.py` — `Policy`, `Decision`, `DecisionReason` (8 members), `FailMode`, `AlgorithmType`, Lua exactness constants
- `sentinel/config.py` — `SentinelConfig`, `AppConfig`, `load_config(path)` (strict pydantic; extra keys forbidden)
- `sentinel/resolver.py` — `StaticPolicyResolver.resolve(tenant_id, endpoint_id) -> Policy | None` (endpoint-only, tenant-agnostic)
- `sentinel/redis.py` — `SentinelRedis` (hardcoded 20ms socket timeouts, fixed pool, `assert_noeviction()` startup check), `ScriptLoader` (load / execute with NOSCRIPT → re-EVAL once → raise `ScriptMissingError`)
- `sentinel/lua.py` — `TOKEN_BUCKET_SCRIPT`, `SLIDING_WINDOW_SCRIPT`, `SCRIPT_NAMES`, `script_source(name)`; sources in `sentinel/lua/*.lua`
- `sentinel/limiter.py` — `RateLimiter.evaluate(policy, key)`, `TokenBucketStrategy`, `SlidingWindowStrategy`, `build_bucket_key(tenant_id, endpoint_id, policy_version)`; Phase 8–10 wiring: breaker check → strategy → RedisError classification (fail-closed → `FAIL_CLOSED`, fail-open → emergency limiter). `RateLimiter` requires explicit `breaker` and `emergency` dependencies.
- `sentinel/errors.py` — `classify_redis_error(exc) -> DecisionReason` (timeout → `REDIS_TIMEOUT`, connection → `REDIS_CONNECTION_ERROR`, `ScriptMissingError` → `REDIS_NOSCRIPT_RETRY`); `ScriptMissingError` (NOSCRIPT re-load exhaustion; raised by `ScriptLoader`). Programming errors (KeyError, etc.) are never caught.
- `sentinel/circuit_breaker.py` — per-process CLOSED/OPEN/HALF-OPEN breaker (`CircuitBreaker`, `FAILURE_THRESHOLD`, `OPEN_TIMEOUT_SECONDS`); OPEN short-circuits before Redis; only genuine Redis successes reset `failure_count`. Injected `now` clock for tests.
- `sentinel/emergency.py` — `TokenBucketEmergencyLimiter` (per-process, endpoint-keyed token bucket; capacity = refill rate = `fallback_rate_per_process_micro`); deliberately uses the local monotonic clock — documented exception to the Redis-clock invariant, since it runs precisely when Redis is unreachable. `EmergencyOutcome(allowed, remaining_micro, retry_after_seconds)`, `EmergencyLimiter` protocol.
- `sentinel/algorithms.py` — pure Python reference functions (`token_bucket_evaluate`, `sliding_window_evaluate`) used to validate the Lua scripts
- `sentinel/auth.py` — `verify_bearer_token(token, secret, algorithms) -> sub`, `AuthenticationError`, `AuthReason`
- `sentinel/http.py` — `SentinelGuard` FastAPI integration: `guard_for(endpoint_id)` dependency; `await guard.load_scripts()` required before first request; denied reasons map to 429 (with `Retry-After`) or 503 (`_denied_status`, `_HTTP_429_REASONS`, `_HTTP_503_REASONS`)
- `tests/` — 17 files, 252 tests (see Testing below)
- `docs/` — `sentinel-project-record.md` (canonical, V1 spec frozen; `vision.md` superseded), `implementation_plan.md` (phase roadmap), `assets/*.svg`
- `docker-compose.yml` — Redis 7 with `noeviction` + bounded `maxmemory` (required config)
- `sentinel.example.json` — example config
- `.github/workflows/ci.yml` — lint job (ruff check / format / mypy) + test job (pytest `-m "not slow"`, real Redis service)

## Non-negotiable invariants (do not "fix")

1. **Redis `TIME()` is the only clock** used by the rate-limiting scripts. Application clocks must never enter the Lua scripts.
2. **Integer microtokens only** (e.g. `capacity_micro`, `refill_rate_micro_per_sec`) — no floats in state.
3. **Key format** `sentinel:v1:{sha256(tenant_id)}:{endpoint_id}:{policy_version}` — `endpoint_id` always an explicit configured id, never derived from the URL/path.
4. **Tenant identity comes only from a validated JWT `sub` claim.** The `X-Tenant-ID` header is ignored (spoofing regression tests exist). Auth failures return 401 with `WWW-Authenticate: Bearer` before any Redis call and never produce a `DecisionReason`.
5. **No client-reachable numeric input** — `cost` was deliberately removed in review; the only ARGV values are server-side policy parameters.
6. **Time-source testing pattern:** no-refill tests use `refill_rate=0` (time becomes irrelevant); refill tests use short real durations. Never dual Lua scripts for testing.
7. Sliding window formula: `estimated = current_count + previous_count × (remaining / window_size)`, evaluated against `redis.call("TIME")` `now` and anchored to it.
8. Lua product bounds enforced in `Policy` validation (`LUA_MAX_EXACT_INT`, `TOKEN_BUCKET_LUA_PRODUCT_LIMIT`) — reject configs whose arithmetic would exceed Lua integer exactness.

## Verified quality gates (all green at last run)

```
pytest                                  # 264 passed (incl. integration tests against real Redis)
pytest --cov=sentinel --cov-report=term-missing   # 100% coverage
mypy sentinel                           # strict, clean
ruff check .                            # clean
ruff format --check .                   # clean
pre-commit run --all-files              # clean
```

- Dev deps: `pip install -e ".[dev]"` (pytest, pytest-asyncio, mypy strict, ruff, pre-commit, coverage, redis, fastapi, uvicorn, httpx, pyjwt, pydantic).
- Integration tests (`pytestmark = pytest.mark.integration`) need real Redis; the `redis_client` fixture in `tests/conftest.py` uses `SENTINEL_REDIS_URL` (default `redis://localhost:6379/0`) and auto-skips when Redis is unreachable.
- Security regression tests (`pytest -m security`, 22 tests: `tests/test_security.py` + security-tagged tests in `test_http.py`/`test_circuit_breaker.py`/`test_models.py`/`test_redis.py`) run in a dedicated CI job and require real Redis for the tagged noeviction checks.
- If shell file reads ever look corrupted, verify against git objects (`git cat-file -p HEAD:<path>`) or the working tree via pytest rather than trusting the first view.

## Work history (most recent first)

1. **Completed Phase 11 security hardening (branch `test/security-hardening`, merged via PR #12, commit f5e1d5b):**
   - Test + docs only; zero production-code changes. `tests/test_security.py` (12 tests, `test_sec_<n>_...` naming mirroring the §07 findings table, all `security`-marked) covers SEC-01 (no client-controlled `cost`: Policy/model/source/Lua tripwires), SEC-02 (Lua TTL-only: `EXPIRE`/`PEXPIRE` present, no `DEL`/`UNLINK`/`KEYS`/`SCAN`/`FLUSHALL`/`FLUSHDB` calls), SEC-03 (tenant spoofing: header-without-token 401 + identical spoof header never overrides `sub`), SEC-05 (breaker isolation across guards), SEC-08 (structural `inspect.getsource` tripwires on `SentinelGuard.guard_for`/`build_bucket_key` + behavioral URL/query-injection test). SEC-04 (JWT replay) and SEC-06 (Redis Cluster) are documented decisions, not tests.
   - Tagged 10 existing tests with `@pytest.mark.security` (integration markers preserved on the three `test_redis.py` noeviction checks); added a missing `import pytest` to `test_circuit_breaker.py`.
   - `pyproject.toml` now uses `--strict-markers`; `.github/workflows/ci.yml` gained a dedicated `security` job running `pytest -m security` against the shared Redis service.
   - Project record §07 documents JWT replay as an accepted V1 upstream boundary (exp/sub required, strict allowlist, no token cache/state) and keeps Redis Cluster deferred to V2 (ADR-010).
2. **Completed phases 8–10 (branch `feat/failure-handling`, commit 73b0ef6, merged via PR #11):**
   - Phase 8 failure handling — `sentinel/errors.py` `classify_redis_error(exc)` maps
     `RedisTimeoutError` → `REDIS_TIMEOUT`, connection errors → `REDIS_CONNECTION_ERROR`,
     `ScriptMissingError` → `REDIS_NOSCRIPT_RETRY`; `ScriptMissingError` is now its own type
     raised by `ScriptLoader` (was a bare `RedisError`). `RateLimiter` catches only `RedisError`:
     fail-closed → `FAIL_CLOSED` decision; fail-open → emergency limiter. Programming errors
     (KeyError etc.) still propagate.
   - Phase 9 circuit breaker — `sentinel/circuit_breaker.py` per-process CLOSED/OPEN/HALF-OPEN
     state machine (`FAILURE_THRESHOLD`, `OPEN_TIMEOUT_SECONDS`, injected `now` clock for tests);
     OPEN short-circuits before any Redis call (`CIRCUIT_OPEN`); only genuine Redis successes
     reset `failure_count`.
   - Phase 10 emergency limiter — `sentinel/emergency.py` per-process, endpoint-keyed token
     bucket (capacity = refill rate = `fallback_rate_per_process_micro`; burst of one second of
     fallback rate, then sustained); deliberately uses the local clock (documented exception to
     invariant #1, it runs exactly when Redis is unreachable); denied → `EMERGENCY_LOCAL_LIMIT`
     with `remaining_micro` + `retry_after_seconds`.
   - `sentinel/http.py` — `_denied_status`/`_HTTP_429_REASONS`/`_HTTP_503_REASONS`: 429 for
     `RATE_LIMITED`/`EMERGENCY_LOCAL_LIMIT` (with Retry-After), 503 for `FAIL_CLOSED`/
     `CIRCUIT_OPEN`/`REDIS_TIMEOUT`/`REDIS_CONNECTION_ERROR`/`REDIS_NOSCRIPT_RETRY`.
   - `RateLimiter` and `SentinelGuard` now take explicit `breaker`/`emergency` dependencies (no
     hidden singletons); the always-allow test stub was deleted — tests exercise the real
     emergency limiter.
   - Also fixed pre-existing mypy debt in `sentinel/redis.py` (redis-py `evalsha` stub union
     `Awaitable[str] | str`) so `mypy sentinel` is green again.
2. **Bug fix (done, merged via PR #10, commit d4da3a5):** Sliding window Lua script anchored the
   current window to the request arrival time instead of Redis `TIME()` `now`, and did not expire a
   previous window that had fully elapsed when the arrival time preceded it. Fixed by anchoring to
   `now`, removing fully-elapsed previous windows, and writing the full updated window state
   (arrival + previous) so consecutive requests see consistent state. Added regression tests
   (`test_token_bucket_...` unaffected; new `test_sliding_window_partial_rollover_counts_remaining_time`
   and friends in `tests/test_algorithms.py`, parity cases in `tests/test_lua_parity.py`).
   Root cause: the anchor was derived from Lua's arrival-time argument instead of `TIME()`; the
   distributed invariant is "Redis TIME() is the one clock."
2. **Refactor PR #9 (done, merged):** Extracted pure reference algorithms to `sentinel/algorithms.py`
   and hardened the sliding-window parity suite (`tests/test_lua_parity.py`, 25 tests) comparing
   Python reference vs real Redis Lua output; restored `sentinel/lua/sliding_window.lua` so it is
   now the single source of truth tested directly (no generated/embedded duplicates).
3. **Completed phases 0–7** of `docs/implementation_plan.md`:
   - Phase 0 skeleton (smoke test, pyproject) — Phases 1–7: models/config, Redis foundation,
     pure algorithms, Lua scripts + NOSCRIPT recovery, PolicyResolver, RateLimiter strategies,
     FastAPI guard + JWT auth (401/429/Retry-After semantics).
   - Repo started from a design-only state (project record + implementation plan + superseded vision).
4. **CI/repo hygiene:** verified the GitHub Actions workflow runs the full fast suite against real
   Redis; PR template exists at `.github/PULL_REQUEST_TEMPLATE.md`.

## Where things stand

- Branch `main`, clean working tree, `HEAD == origin/main` (f5e1d5b). Phases 0–11 merged; stale feature branches (`feat/failure-handling`, `fix/sliding-window-anchor`, all `origin/chore|feat/*`) pruned.
- **Implemented:** phases 0–11 of the plan. `DecisionReason` (8 members) is fully exercised:
  all failure paths produce decisions and the HTTP layer maps them to 429/503. §07 security
  findings are locked in by 22 `security`-marked regression tests (dedicated CI job).
- **Not yet implemented (next work, per plan):**
  - Phase 12 observability — structured logs on deny; Prometheus metrics bounded to
    `endpoint_id`/`decision_reason` (includes the live metrics cardinality bomb test deferred from Phase 11).
  - Phase 13 concurrency/failure-injection tests (the plan's `slow` marker suite — no
    `slow`-marked tests exist yet), Phases 14–18 benchmarks/docs/packaging/integration/release.

## Conventions

- No comments in code unless asked; follow existing style (black-formatted, type-annotated, strict mypy).
- Pre-commit hooks: ruff (check + format) + mypy; run `pre-commit run --all-files` after changes.
- Run the full test suite after any change; keep 100% coverage on `sentinel/`.
- Integration tests must use unique keys per run (see `_unique()` pattern) and clean up after themselves; Redis state is shared.
