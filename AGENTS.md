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
- `sentinel/redis.py` — `SentinelRedis` (production fail-fast budget: 20ms socket + connect timeouts by default, `assert_noeviction()` startup check; optional `socket_timeout`/`socket_connect_timeout` constructor args — defaults unchanged, only the benchmark harness overrides them), `ScriptLoader` (load / execute with NOSCRIPT → re-EVAL once → raise `ScriptMissingError`)
- `sentinel/lua.py` — `TOKEN_BUCKET_SCRIPT`, `SLIDING_WINDOW_SCRIPT`, `SCRIPT_NAMES`, `script_source(name)`; sources in `sentinel/lua/*.lua`
- `sentinel/limiter.py` — `RateLimiter.evaluate(policy, key)`, `TokenBucketStrategy`, `SlidingWindowStrategy`, `build_bucket_key(tenant_id, endpoint_id, policy_version)`, `hash_tenant(tenant_id)`; Phase 8–10 wiring: breaker check → strategy → RedisError classification (fail-closed → `FAIL_CLOSED`, fail-open → emergency limiter). `RateLimiter` requires explicit `breaker` and `emergency` dependencies.
- `sentinel/errors.py` — `classify_redis_error(exc) -> DecisionReason` (timeout → `REDIS_TIMEOUT`, connection → `REDIS_CONNECTION_ERROR`, `ScriptMissingError` → `REDIS_NOSCRIPT_RETRY`); `ScriptMissingError` (NOSCRIPT re-load exhaustion; raised by `ScriptLoader`). Programming errors (KeyError, etc.) are never caught.
- `sentinel/circuit_breaker.py` — per-process CLOSED/OPEN/HALF-OPEN breaker (`CircuitBreaker`, `FAILURE_THRESHOLD`, `OPEN_TIMEOUT_SECONDS`); OPEN short-circuits before Redis; only genuine Redis successes reset `failure_count`. Injected `now` clock for tests.
- `sentinel/emergency.py` — `TokenBucketEmergencyLimiter` (per-process, endpoint-keyed token bucket; capacity = refill rate = `fallback_rate_per_process_micro`); deliberately uses the local monotonic clock — documented exception to the Redis-clock invariant, since it runs precisely when Redis is unreachable. Persists bucket state only on ALLOW (mirrors the Lua's "denied requests never write" contract — the Phase 14 double-refill fix). `EmergencyOutcome(allowed, remaining_micro, retry_after_seconds)`, `EmergencyLimiter` protocol.
- `sentinel/algorithms.py` — pure Python reference functions (`token_bucket_evaluate`, `sliding_window_evaluate`) used to validate the Lua scripts
- `sentinel/auth.py` — `verify_bearer_token(token, secret, algorithms) -> sub`, `AuthenticationError`, `AuthReason`
- `sentinel/observability.py` — `SentinelObservability` (injected into `SentinelGuard`, like `breaker`/`emergency`): `record_decision(tenant_hash, endpoint_id, decision, latency_micro, breaker_state)` emits a WARNING deny log (structured `extra` fields; never raw tenant) and increments `sentinel_decisions_total` + `sentinel_evaluate_latency_microseconds`, both labeled ONLY by `endpoint_id`/`decision_reason`. Process-wide collectors registered once on the default registry; injectable `logger`/`registry` for tests.
- `sentinel/http.py` — `SentinelGuard` FastAPI integration: `guard_for(endpoint_id)` dependency; `await guard.load_scripts()` required before first request; denied reasons map to 429 (with `Retry-After`) or 503 (`_denied_status`, `_HTTP_429_REASONS`, `_HTTP_503_REASONS`); Phase 12 emits decision telemetry (latency measured around `limiter.evaluate`).
- `benchmarks/benchmark.py` — Phase 14 dependency-free benchmark harness (B1–B9 cells × concurrency {1,8}: unguarded / with-Sentinel / detached / short-circuit / dead-port fail-open + fail-closed; p50/p95/p99, API+Redis CPU, decision-reason error rates, environment block; `--smoke`/`--out`/`--reps`); baseline in `docs/benchmark-results.md`. The live client uses a benchmark-specific 5s socket budget (`BENCHMARK_SOCKET_TIMEOUT_SECONDS`); B7–B9 dead-port clients keep the production 20ms fail-fast budget so the failure-path measurements are unchanged.
- `tests/` — 22 files, 302 tests (23 `security`-marked, 17 `slow`-marked incl. `test_benchmark_smoke.py`; see Testing below)
- `README.md` — library entry point (install, quickstart, config tables, doc links)
- `docs/` — `sentinel-project-record.md` (canonical, V1 spec frozen; `vision.md` superseded), `implementation_plan.md` (phase roadmap), `phase-15-plan.md` (executed Phase 15 plan) + `architecture.md` + `failure-handling.md` + `known-limitations.md` (Phase 15 deliverables), `phase-14-plan.md` + `benchmark-results.md` (executed Phase 14 plan + baseline), `phase-13-plan.md` (executed Phase 13 plan; template for future phase plans), `phase-12-plan.md`, `phase-11-plan.md`, `phase-8-10-summary.md`, `assets/*.svg`
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
pytest                                  # 302 passed (incl. integration tests against real Redis)
pytest --cov=sentinel --cov-report=term-missing   # 100% coverage
python benchmarks/benchmark.py --smoke  # pass (subprocess-driven in the slow suite)
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

1. **Post-release fixes + v1.0.1 (PRs #19–#20, branch `chore/post-release-hygiene`):**
   - **PyPI name conflict discovered:** `v1.0.0` was never published — the tag-time publish run
     failed (missing `PYPI_TOKEN`), and by the time the secret existed the name `sentinel` was
     already taken on PyPI by an unrelated package ("Create sentinel objects, akin to None,
     NotImplemented, Ellipsis"). Renamed the distribution to `sentinel-rate-limiter` (PR #19,
     `fix/pypi-package-name`; import name `sentinel` unchanged) and re-released as `v1.0.1`
     (PR #20) — the first and only live PyPI release is `sentinel-rate-limiter 1.0.1`
     (verified via PyPI JSON: correct metadata, wheel + sdist, fresh-venv installable).
   - Hygiene sweep: dev bump `1.1.0.dev0` (both locations), README/AGENTS/project-record
     refreshed (302 tests, 17 `slow`-marked, 23 `security`-marked), GitHub Releases created
     for `v1.0.0` + `v1.0.1` (v1.0.0 notes disclose the never-published status), stale
     branches deleted (`chore/release-v1.0.0`, `chore/release-v1.0.1`, `fix/pypi-package-name`,
     `feat/examples`), `main == origin/main`.
   - Wheel contents verified clean against the Phase 16 decision: only `sentinel/*.py` +
     `py.typed` + `lua/*.lua` + dist-info — no `tests/`/`benchmarks/`/`examples/`/docs/leaks
     (the `FORBIDDEN_WHEEL_DIRS` regression test covers the wheel; the sdist legitimately
     carries `tests/`, standard setuptools behavior).
1. **Released V1 (Phase 18, branch `chore/release-v1.0.0`, PR #18):** production-readiness
   review + `v1.0.0` tag. Pre-flight gates re-run green on real Redis (302 passed — note:
   PDFTalk's auth-protected `pdftalk-redis` container occupies host 6379, so local integration
   runs need `SENTINEL_REDIS_URL=redis://localhost:6380/0` against a dedicated
   `sentinel-test-redis` container; 100% coverage, mypy/ruff/pre-commit clean, benchmark smoke
   pass, CI green on main @ `32f74c6`). P0 triage: known-limitations walk with no blocking
   findings; PDFTalk integration (see below) dispositioned as app-side issues only. Bumped
   version `0.1.0 → 1.0.0` (both locations, tripwire green), ticked checklist + project record
   §11, tagged `v1.0.0`. Phase 18 plan: `docs/phase-18-plan.md` (includes the Phase 17
   disposition record). The PyPI publish step of this release ultimately shipped as
   `sentinel-rate-limiter 1.0.1` (see entry above).
2. **Phase 17 satisfied by real-app integration (PDFTalk, supersedes in-repo `examples/`):**
   integration testing in the real PDFTalk FastAPI app against the vendored
   `sentinel-0.1.0-py3-none-any.whl` wheel (built from the Phase 16 packaging branch). All 8
   scenarios passed (PASS WITH LIMITATIONS): normal 429s + Retry-After, sliding-window state
   shape, fail-closed 503, recovery, multi-process shared bucket, multi-tenant isolation, auth
   401s, Lua script reload after Redis restart, observability metrics. **No genuine Sentinel
   defects**; two pre-existing PDFTalk-side issues recorded there (500 on non-UUID `sub`,
   structlog dropping `extra` fields). Evidence in the PDFTalk repo:
   `docs/sentinel/integration-test-report.md`, `test-results.json`, `evidence/`. Decision: no
   `examples/` directory in this repo.
2. **Completed Phase 16 packaging & distribution (branch `chore/packaging`, merged via PR #17,
   commit 32f74c6):** setuptools metadata (static version, LICENSE file, classifiers),
   `tests/test_packaging.py` (fresh-venv wheel install smoke, pyproject-vs-`__version__` tripwire,
   wheel contents), `packaging` CI job, `publish` CI job (on `v*` tags, `needs` all five jobs,
   hard-fails without the `PYPI_TOKEN` secret). 302 tests, 100% coverage.
1. **Completed Phase 15 documentation (branch `docs/phase-15`, docs-only, 4 commits; merge
   pending — no PR opened):**
   - Deliverables: `README.md` rewritten as the library entry point (install, quickstart copied
     from the real wiring pattern in `tests/test_http_integration.py`, config tables, doc
     links); `docs/architecture.md` (module map, request journey with `file:line` references,
     state & key design, clock discipline — Redis `TIME()` vs wall-clock observability
     timestamps vs the monotonic breaker/emergency clocks — invariants, evidence map);
     `docs/failure-handling.md` (decision table, `classify_redis_error` mapping, breaker state
     machine, emergency limiter + no-write-on-deny, HTTP 429/503 + Retry-After semantics,
     measured failure-path latency B7–B9, ADR-011); `docs/known-limitations.md` — the crucial
     deliverable: ~19-item limitation table with consequence + source (ADR-011, per-process
     breaker, JWKS deferred to V2, 20ms socket budget, HS*-only JWT, sliding-window
     estimate/no Retry-After, per-process fail-open scaling, etc.).
   - Zero production-code changes (`git diff sentinel/ tests/ benchmarks/ pyproject.toml`
     empty — docs-only phase). Status-only updates to frozen docs: Phase 15 ticked in the
     implementation-plan checklist; project record §11 status line now reads phases 0–15
     complete, next = Phase 16.
   - Deviation from convention: the AGENTS.md refresh is the final commit on `docs/phase-15`
     (conventionally it lands in a sanctioned post-merge `main` commit; the branch is not
     pushed/merged by this session), and the pre-existing uncommitted post-`1328652` AGENTS.md
     update was folded into that commit. Quality gate for a docs-only phase: pre-commit over
     the markdown + empty code diff (no test-suite run needed).
2. **Decoupled the benchmark socket budget from the production 20ms fail-fast (commit 1328652, fast-forward merged to `main`):**
   - Symptom: `tests/test_benchmark_smoke.py::test_bench_smoke_runs_and_reports_sane_statistics`
     failed intermittently with `redis.exceptions.TimeoutError: Timeout reading from
     localhost:6379` on this Windows machine (Docker Desktop). Root cause: `SentinelRedis`
     hardcoded the 20ms production fail-fast budget, the benchmark harness inherited it, and
     under CPU saturation (14 burners on 16 cores) the loopback Redis tail exceeded 20ms —
     reproduced standalone: 8/15 `--smoke` failures pre-fix under load.
   - Fix: `SentinelRedis.__init__` gained optional `socket_timeout`/`socket_connect_timeout`
     constructor args (defaults = `SOCKET_TIMEOUT_SECONDS`/`SOCKET_CONNECT_TIMEOUT_SECONDS` =
     `0.02` — production behavior unchanged, verified by runtime pool-kwargs checks); the
     benchmark live client (B1–B6) uses a benchmark-only 5s budget
     (`BENCHMARK_SOCKET_TIMEOUT_SECONDS`), while `_dead_limiter()` (B7–B9) keeps the 20ms
     fail-fast budget so the failure-path measurements stay the same. Only the benchmark
     harness overrides the timeouts.
   - Verification: 294 passed, 100% coverage, ruff/mypy/pre-commit clean; post-fix perf
     within run-to-run noise of the baseline (B2 c=1 p50 811→830 µs; B8/B9 p99 ≈ 22–29 ms);
     CPU-saturation stress 0/22 failures (12 pytest-wrapped + 10 standalone `--smoke` runs;
     pre-fix: 1–2/12 and 8/15 respectively); under load B8/B9 p50 ≈ 22–27 ms / p99 ≈ 29–37 ms
     — same band as pre-fix, nowhere near the 5s budget, proving no leak into the failure path.
   - Tests: `tests/test_redis.py` — signature tripwire updated to
     `["self", "redis_url", "socket_timeout", "socket_connect_timeout"]`, new
     `test_custom_socket_timeouts_are_forwarded_to_the_pool` (pool kwargs reflect the custom
     values; defaults still enforce the 20ms budget). Changed files only:
     `sentinel/redis.py`, `benchmarks/benchmark.py`, `tests/test_redis.py`.
2. **Fixed the Phase 14 emergency-limiter double-refill defect (branch `fix/emergency-limiter-double-refill`, commit 606e1f0, merged to `main`):**
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

- Branch `chore/post-release-hygiene`, clean working tree, based on `main` at HEAD `83c0705`
  ("chore(release): v1.0.1 (#20)"). Phases 0–18 complete; `v1.0.0` + `v1.0.1` tagged; the only
  live PyPI release is `sentinel-rate-limiter 1.0.1` (import name `sentinel` unchanged; the
  `v1.0.0` publish never landed — see work-history entry 1). Version bumped to `1.1.0.dev0`
  (both locations) post-release. Local `main == origin/main`; stale branches cleaned
  (`chore/release-v1.0.0`, `chore/release-v1.0.1`, `fix/pypi-package-name`, `feat/examples`).
- **Implemented:** all phases 0–18 of the plan. `DecisionReason` (8 members) is fully exercised:
  all failure paths produce decisions and the HTTP layer maps them to 429/503. §07 security
  findings are locked in by 23 `security`-marked regression tests (dedicated CI job), including
  the Phase 12 live metrics cardinality assertion. The §09 invariants are proven under
  concurrency: exact in-process and cross-process capacity, sliding-window reference bound,
  breaker OPEN under load, emergency cap (17 `slow`-marked tests incl. the benchmark smoke,
  dedicated CI job). Phase 14 baseline recorded in `docs/benchmark-results.md` (harness in
  `benchmarks/benchmark.py`); the benchmark surfaced one fail-open defect (emergency limiter
  double-refill, ~2.3× fallback rate under sustained failure) disclosed in
  `docs/benchmark-results.md` + project record §09 — **fixed** (no-write-on-deny in
`sentinel/emergency.py`, sustained-rate regression tests, post-fix benchmark re-run with no
   regression; see work-history entry 4). A second post-benchmark defect was also fixed: the
   benchmark harness inherited the production 20ms fail-fast socket budget, so the smoke test
   timed out under CPU saturation — **fixed** by optional `socket_timeout`/`socket_connect_timeout`
   on `SentinelRedis` (production defaults unchanged) with the benchmark live client on a 5s
   budget and B7–B9 still on 20ms (see work-history entry 3). Phase 15 (documentation) shipped
   the README entry point plus the architecture / failure-handling / known-limitations deep
   dives (`docs/architecture.md`, `docs/failure-handling.md`, `docs/known-limitations.md`) with
   zero code changes (see work-history entry 2). Phase 16 shipped packaging + publish CI
   (work-history entry 2); Phase 17 was satisfied by real-app integration in PDFTalk rather than
   in-repo examples (work-history entry 2); Phase 18 tagged `v1.0.0` (work-history entry 1,
   plan + disposition in `docs/phase-18-plan.md`), and the post-release sweep shipped
   `sentinel-rate-limiter 1.0.1` + hygiene fixes (work-history entry 1).
- **Not yet implemented (next work):**
  - Post-V1: the deferred V2 boundaries from `docs/known-limitations.md` (JWKS rotation,
    Redis Cluster, per-process fail-open scaling) — no phase plan exists yet; the project is
    in maintenance mode until the next roadmap decision.

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
