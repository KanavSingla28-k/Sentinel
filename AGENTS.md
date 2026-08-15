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
- `sentinel/limiter.py` — `RateLimiter.evaluate(policy, key)`, `TokenBucketStrategy`, `SlidingWindowStrategy`, `build_bucket_key(tenant_id, endpoint_id, policy_version)`, `hash_tenant(tenant_id)`; Phase 8–10 wiring: breaker check → strategy → RedisError classification (fail-closed → `FAIL_CLOSED`, fail-open → emergency limiter). `RateLimiter` requires explicit `breaker` and `emergency` dependencies.
- `sentinel/errors.py` — `classify_redis_error(exc) -> DecisionReason` (timeout → `REDIS_TIMEOUT`, connection → `REDIS_CONNECTION_ERROR`, `ScriptMissingError` → `REDIS_NOSCRIPT_RETRY`); `ScriptMissingError` (NOSCRIPT re-load exhaustion; raised by `ScriptLoader`). Programming errors (KeyError, etc.) are never caught.
- `sentinel/circuit_breaker.py` — per-process CLOSED/OPEN/HALF-OPEN breaker (`CircuitBreaker`, `FAILURE_THRESHOLD`, `OPEN_TIMEOUT_SECONDS`); OPEN short-circuits before Redis; only genuine Redis successes reset `failure_count`. Injected `now` clock for tests.
- `sentinel/emergency.py` — `TokenBucketEmergencyLimiter` (per-process, endpoint-keyed token bucket; capacity = refill rate = `fallback_rate_per_process_micro`); deliberately uses the local monotonic clock — documented exception to the Redis-clock invariant, since it runs precisely when Redis is unreachable. `EmergencyOutcome(allowed, remaining_micro, retry_after_seconds)`, `EmergencyLimiter` protocol.
- `sentinel/algorithms.py` — pure Python reference functions (`token_bucket_evaluate`, `sliding_window_evaluate`) used to validate the Lua scripts
- `sentinel/auth.py` — `verify_bearer_token(token, secret, algorithms) -> sub`, `AuthenticationError`, `AuthReason`
- `sentinel/observability.py` — `SentinelObservability` (injected into `SentinelGuard`, like `breaker`/`emergency`): `record_decision(tenant_hash, endpoint_id, decision, latency_micro, breaker_state)` emits a WARNING deny log (structured `extra` fields; never raw tenant) and increments `sentinel_decisions_total` + `sentinel_evaluate_latency_microseconds`, both labeled ONLY by `endpoint_id`/`decision_reason`. Process-wide collectors registered once on the default registry; injectable `logger`/`registry` for tests.
- `sentinel/http.py` — `SentinelGuard` FastAPI integration: `guard_for(endpoint_id)` dependency; `await guard.load_scripts()` required before first request; denied reasons map to 429 (with `Retry-After`) or 503 (`_denied_status`, `_HTTP_429_REASONS`, `_HTTP_503_REASONS`); Phase 12 emits decision telemetry (latency measured around `limiter.evaluate`).
- `benchmarks/benchmark.py` — Phase 14 dependency-free benchmark harness (B1–B9 cells × concurrency {1,8}: unguarded / with-Sentinel / detached / short-circuit / dead-port fail-open + fail-closed; p50/p95/p99, API+Redis CPU, decision-reason error rates, environment block; `--smoke`/`--out`/`--reps`); baseline in `docs/benchmark-results.md`
- `tests/` — 22 files, 287 tests (23 `security`-marked, 10 `slow`-marked incl. `test_benchmark_smoke.py`; see Testing below)
- `docs/` — `sentinel-project-record.md` (canonical, V1 spec frozen; `vision.md` superseded), `implementation_plan.md` (phase roadmap), `phase-14-plan.md` + `benchmark-results.md` (executed Phase 14 plan + baseline), `phase-13-plan.md` (executed Phase 13 plan; template for future phase plans), `phase-12-plan.md`, `phase-11-plan.md`, `phase-8-10-summary.md`, `assets/*.svg`
- `docker-compose.yml` — Redis 7 with `noeviction` + bounded `maxmemory` (required config)
- `sentinel.example.json` — example config
- `.github/workflows/ci.yml` — lint job (ruff check / format / mypy), test job (pytest `-m "not slow"`, real Redis service), security job (`pytest -m security`, real Redis service), slow job (`pytest -m slow`, real Redis service)

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
pytest                                  # 287 passed (incl. integration tests against real Redis)
pytest --cov=sentinel --cov-report=term-missing   # 100% coverage
mypy sentinel                           # strict, clean
ruff check .                            # clean
ruff format --check .                   # clean
pre-commit run --all-files              # clean
```

- Dev deps: `pip install -e ".[dev]"` (pytest, pytest-asyncio, mypy strict, ruff, pre-commit, coverage, redis, fastapi, uvicorn, httpx, pyjwt, pydantic).
- Integration tests (`pytestmark = pytest.mark.integration`) need real Redis; the `redis_client` fixture in `tests/conftest.py` uses `SENTINEL_REDIS_URL` (default `redis://localhost:6379/0`) and auto-skips when Redis is unreachable.
- Security regression tests (`pytest -m security`, 23 tests: `tests/test_security.py` + security-tagged tests in `test_http.py`/`test_circuit_breaker.py`/`test_models.py`/`test_redis.py`/`test_observability.py`) run in a dedicated CI job and require real Redis for the tagged noeviction checks.
- If shell file reads ever look corrupted, verify against git objects (`git cat-file -p HEAD:<path>`) or the working tree via pytest rather than trusting the first view.

## Work history (most recent first)

1. **Fixed the Phase 14 emergency-limiter double-refill defect (branch `fix/emergency-limiter-double-refill`):**
   - Root cause: `TokenBucketEmergencyLimiter.evaluate` persisted `(tokens_after, last_refill_after)`
     on every call, but `token_bucket_evaluate` advances `last_refill_micro` only on ALLOW (the
     Lua's "denied requests never write" contract, `token_bucket.lua:8`). Each denied call banked
     the partial refill of its elapsed window while the stored `last_refill_micro` stayed put, so
     the next evaluation refilled the same window again on top of banked tokens — sustained
     fail-open allowance reached ~2.3× the configured `fallback_rate_per_process_micro`
     (measured: 8 allows in 3 s at 1 token/s, 100 ms cadence, vs the expected 4).
   - Fix (`sentinel/emergency.py`): write bucket state only on ALLOW. A denied call leaves the
     state untouched; the next evaluation recomputes the refill over the full elapsed window since
     the last write, so every elapsed interval contributes exactly once and denied traffic cannot
     accelerate replenishment. No API/behavior change outside the emergency path.
   - Regression coverage: deterministic injected-clock sustained-traffic tests in
     `tests/test_emergency.py` (initial burst + immediate deny; 1/2/5 tokens/s at 100 ms cadence —
     allows == capacity + elapsed × rate exactly; denied-calls-no-acceleration), an end-to-end
     fail-open test through `RateLimiter` in `tests/test_limiter.py`, and the parity test now
     applies no-write-on-deny to the reference state (it previously stored every evaluation
     unconditionally — self-consistent with the bug it hid).
   - Verification: dead-port benchmark journey at 1 token/s, 100 ms cadence over 5 s allows
     exactly 6 (burst + 5 refills ≈1.1 s spacing); full `benchmarks/benchmark.py` re-run — all 18
     cells within noise of the baseline (B2 c=1 p50 826 vs 827 µs; B7 ~96.4k ops/s; B8/B9 p99
     ≈ 26 ms), B8 counts drop from 8 phantom allows/batch to exactly the initial burst per rep,
     B9 fail-closed counts unchanged. Full suite 293 passed, 100% coverage, ruff/mypy clean.
   - Docs: `docs/benchmark-results.md` "Fix status" section, `docs/phase-14-plan.md` follow-up
     bullet, project record §09/§11.
2. **Completed Phase 14 benchmarking (branch `bench/perf-phase14`, merged via PR #15, commit 351c5dc):**
   - `benchmarks/benchmark.py` — dependency-free stdlib harness (no new deps): cells B1–B9 ×
     concurrency {1,8} (unguarded / with-Sentinel token-bucket + sliding-window / detached /
     auth+decide / breaker-OPEN short-circuit / dead-port fail-open + fail-closed); p50/p95/p99
     (median of 3 reps), API CPU (`process_time` deltas), Redis CPU (`INFO stats` deltas),
     decision-reason error-rate histograms, full environment block. No-refill fresh-key policies
     (invariant #6) so `over_limit == 0` everywhere; B8/B9 warm-up 0 (a warm-up would trip the
     breaker and erase the measured failure journey).
   - Baseline in `docs/benchmark-results.md`: with-Sentinel ≈ 5.2× throughput overhead at c=1
     (p50 150 → 827 µs, one loopback Redis round trip dominating); breaker short-circuit ≈ 7 µs
     p50 (~96k ops/s); failure-path p99 ≈ 27 ms ≈ the dead-port socket timeout (the limiter is
     not the failure-path cost). Windows `process_time` is 15.6 ms tick-quantized — API CPU
     disclosed as order-of-magnitude only. Numbers are single-machine loopback, reported as-is
     (vision §12); no thresholds asserted.
   - `tests/test_benchmark_smoke.py` (`slow`+`integration`) subprocesses the harness `--smoke` and
     asserts structural invariants; self-configures bounded `noeviction` on the CI Redis around
     the run (CI service is a plain container; harness `assert_noeviction()` is a real startup
     check) and restores prior config in `finally`. Rides the existing `slow` CI job — no new CI
     job. PR #15 CI fully green (lint/test/security/slow).
   - **Production defect surfaced by the benchmark and deliberately NOT fixed in this phase
     (disclosed in `docs/benchmark-results.md` + project record §09):** the fail-open emergency
     limiter double-refills on denied calls (`emergency.py` persists `tokens_after` while
     `token_bucket_evaluate` advances `last_refill_micro` only on ALLOW — the Lua's "denied
     requests never write" contract, token_bucket.lua:8, is violated), admitting up to ~2.3× the
     configured `fallback_rate_per_process_micro` under sustained failure (decisive experiment:
     Lua allows at 0.0/1.10/2.19 s vs emergency 0.0/0.44/0.87/1.32/1.76/2.19/2.63 s at 1 token/s).
     Why tests missed it: the emergency parity test drives the reference with the same
     store-everything pattern (self-consistent); Lua-parity tests are single-step; Phase 13 bursts
     are ms-scale (refill ≈ 0). Fix (mirror no-write-on-deny, update the parity test, add a
     sustained-denial regression test) ships as a separate PR.
   - Zero production-code changes (`git diff sentinel/` empty at merge); docs: `docs/phase-14-plan.md`
     (executed plan), `docs/benchmark-results.md`, project record §09 "Phase 14 benchmark
     verification" + §11 status, Phase 14 ticked in the implementation-plan checklist.
2. **Completed Phase 13 concurrency/failure-injection tests (branch `test/concurrency-phase13`, merged via PR #14, commit c173c90):**
   - `tests/test_concurrency.py` (6 tests, `test_conc_<n>_...`, all `slow`-marked) — 50-coroutine
     races on shared real-Redis keys: token-bucket exact capacity (`refill_rate=0`, invariant #6),
     sliding-window bound vs the pure reference (never exact equality), unbounded 50-coroutine
     stress asserting failure-tolerant invariants, fake-loader emergency cap (exactly 1 burst token
     admitted, 49 `EMERGENCY_LOCAL_LIMIT`, breaker OPEN), fail-closed all-denied, and real dead-port
     failure injection (breaker OPEN, emergency cap, fail-closed counterpart).
   - `tests/test_concurrency_multiprocess.py` (2 tests) — 3 spawned processes share one bucket via
     `mp.get_context("spawn")` + Barrier + `mp.Queue`; distributed atomicity invariants
     (`redis_total <= capacity`, `emergency_total <= PROCESS_COUNT`, strict equality on healthy
     Redis); spawn smoke check.
   - **Determinism design record** (in `docs/phase-13-plan.md`): the hardcoded 20ms socket budget
     cannot sustain >=20 simultaneous connections on Windows/WSL2 loopback (measured), so strict
     assertions run under an in-flight semaphore (4) and unbounded bursts assert documented
     fail-open invariants + a strict branch when no failure reasons appear; the dead-port client
     surfaces as `REDIS_CONNECTION_ERROR` on Linux / `REDIS_TIMEOUT` on Windows/WSL2 (both
     accepted for that path). Zero production-code changes; 100% coverage kept.
   - `.github/workflows/ci.yml` gained a dedicated `slow` job (`pytest -m slow`, real Redis
     service); PR #14 CI fully green (lint/test/security/slow).
   - Docs: `docs/phase-13-plan.md` (executed plan), project record §09 "Phase 13 concurrency
     verification" + §11 status, Phase 13 ticked in the implementation-plan checklist.
2. **Completed Phase 12 observability (branch `feat/observability`, merged via PR #13, commit dbbb26c):**
   - `sentinel/observability.py` — `SentinelObservability.record_decision(tenant_hash, endpoint_id,
     decision, latency_micro, breaker_state)`: WARNING structured log on deny only (fields:
     `tenant_hash` — never raw tenant id, `endpoint_id`, `decision_reason`, `latency_micro`,
     `breaker_state`); Prometheus `sentinel_decisions_total` counter + `sentinel_evaluate_latency_microseconds`
     histogram labeled ONLY by `endpoint_id`/`decision_reason`. Process-wide collectors registered
     once on the default registry; injectable `logger`/`registry` for tests.
   - Wired into `SentinelGuard` as an explicit injected dependency (like `breaker`/`emergency`);
     latency measured around `limiter.evaluate` with `perf_counter_ns`. 401/404 paths emit nothing
     (auth failures never produce a `DecisionReason`). Fail-open ALLOWED-by-emergency decisions
     count as metrics but get no log line.
   - Extracted `hash_tenant(tenant_id)` in `sentinel/limiter.py` (shared by `build_bucket_key` and
     the guard); `build_bucket_key` stays a pure function of its inputs (SEC-08 tripwire intact).
   - `prometheus-client>=0.20` added as a runtime dep (`pyproject.toml`) and to the pre-commit mypy
     hook's `additional_dependencies`.
   - `tests/test_observability.py` (13 tests, `test_obs_<n>_...`, Redis-free, FakeLoader-based):
     counter/label semantics, deny-log fields via caplog (raw tenant absent), no-log-on-allow,
     fail-open nuances, 401/404/unloaded-scripts emit nothing, bounded labelnames tripwire, and the
     live SEC-08 cardinality-bomb test (`security`-marked): dynamic sub-paths + endpoint-lookalike
     query strings under one guarded catch-all route emit exactly one `endpoint_id` label value.
   - Docs: `docs/phase-12-plan.md`; project record §07 gained a "Phase 12 observability verification"
     note and §11 status updated; Phase 12 ticked in the implementation-plan checklist.
2. **Completed Phase 11 security hardening (branch `test/security-hardening`, merged via PR #12, commit f5e1d5b):**
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
3. **Bug fix (done, merged via PR #10, commit d4da3a5):** Sliding window Lua script anchored the
   current window to the request arrival time instead of Redis `TIME()` `now`, and did not expire a
   previous window that had fully elapsed when the arrival time preceded it. Fixed by anchoring to
   `now`, removing fully-elapsed previous windows, and writing the full updated window state
   (arrival + previous) so consecutive requests see consistent state. Added regression tests
   (`test_token_bucket_...` unaffected; new `test_sliding_window_partial_rollover_counts_remaining_time`
   and friends in `tests/test_algorithms.py`, parity cases in `tests/test_lua_parity.py`).
   Root cause: the anchor was derived from Lua's arrival-time argument instead of `TIME()`; the
   distributed invariant is "Redis TIME() is the one clock."
4. **Refactor PR #9 (done, merged):** Extracted pure reference algorithms to `sentinel/algorithms.py`
   and hardened the sliding-window parity suite (`tests/test_lua_parity.py`, 25 tests) comparing
   Python reference vs real Redis Lua output; restored `sentinel/lua/sliding_window.lua` so it is
   now the single source of truth tested directly (no generated/embedded duplicates).
5. **Completed phases 0–7** of `docs/implementation_plan.md`:
   - Phase 0 skeleton (smoke test, pyproject) — Phases 1–7: models/config, Redis foundation,
     pure algorithms, Lua scripts + NOSCRIPT recovery, PolicyResolver, RateLimiter strategies,
     FastAPI guard + JWT auth (401/429/Retry-After semantics).
   - Repo started from a design-only state (project record + implementation plan + superseded vision).
6. **CI/repo hygiene:** verified the GitHub Actions workflow runs the full fast suite against real
   Redis; PR template exists at `.github/PULL_REQUEST_TEMPLATE.md`.

## Where things stand

- Branch `main`, clean working tree, `HEAD == origin/main` (351c5dc). Phases 0–14 merged
  (PRs #9–#15); stale feature branches (local and remote) pruned. The post-merge docs commit
  `351c5dc` ("bench(perf): phase-14 benchmark harness and baseline (#15)") holds the Phase 14
  work; this AGENTS.md update is the sanctioned follow-up docs commit.
- **Implemented:** phases 0–14 of the plan. `DecisionReason` (8 members) is fully exercised:
  all failure paths produce decisions and the HTTP layer maps them to 429/503. §07 security
  findings are locked in by 23 `security`-marked regression tests (dedicated CI job), including
  the Phase 12 live metrics cardinality assertion. The §09 invariants are proven under
  concurrency: exact in-process and cross-process capacity, sliding-window reference bound,
  breaker OPEN under load, emergency cap (10 `slow`-marked tests incl. the benchmark smoke,
  dedicated CI job). Phase 14 baseline recorded in `docs/benchmark-results.md` (harness in
  `benchmarks/benchmark.py`); the benchmark surfaced one fail-open defect (emergency limiter
  double-refill, ~2.3× fallback rate under sustained failure) disclosed in
  `docs/benchmark-results.md` + project record §09 — **fixed** (no-write-on-deny in
  `sentinel/emergency.py`, sustained-rate regression tests, post-fix benchmark re-run with no
  regression; see work-history entry 1).
- **Not yet implemented (next work):**
  - Phases 15–18 docs/packaging/integration/release. Phase 15 (documentation: README, Known
    Limitations) is the next phase.

## Conventions

- No comments in code unless asked; follow existing style (black-formatted, type-annotated, strict mypy).
- Pre-commit hooks: ruff (check + format) + mypy; run `pre-commit run --all-files` after changes.
- Run the full test suite after any change; keep 100% coverage on `sentinel/`.
- Integration tests must use unique keys per run (see `_unique()` pattern) and clean up after themselves; Redis state is shared.
- **Git workflow:** trunk-based. Work on short-lived branches (`feat/`, `fix/`, `test/`, `chore/`) off
  `main`; conventional-commit messages (`test(security): ...`); land via squash-merge PRs (merge +
  delete branch); never commit implementation directly to `main` — the only direct-to-main commits are
  the sanctioned post-merge docs commits (e.g., AGENTS.md/checklist updates after a phase PR merges).
- `gh` is not authenticated on this machine; git pushes work via the Windows credential manager.
  For `gh pr create`/`merge`/`checks`, set `GH_TOKEN` in-process by piping a PowerShell here-string
  (`"protocol=https<newline>host=github.com<newline>"`) into `git credential fill`, extracting the
  `password=` line, then clearing the env var.
