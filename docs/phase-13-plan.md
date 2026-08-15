# Phase 13 — Concurrency & Failure-Injection Tests: Implementation Plan

## Scope (from `docs/implementation_plan.md`, Phase 13)

- **Objective:** Prove Sentinel works under adversarial distributed conditions.
- **Prerequisites:** Phases 8, 9, 10, 12 (all shipped).
- **Deliverables (5):**
  1. 50-coroutine concurrency test — Token Bucket asserts **exact capacity** (strict precondition:
     `refill_rate=0`); Sliding Window asserts a **bound against the reference formula**, never an
     exact equality (Review 1: `allowed == limit` cannot work for either algorithm).
  2. Multi-process concurrency test — processes share one real Redis; the bucket key is the shared
     state.
  3. Failure-injection concurrency test — concurrent evaluations against a failing loader: breaker
     trips OPEN, fail-open traffic is capped by the emergency limiter.
  4. Integration test — real Redis, heavy concurrent load, then an injected Redis failure; assert
     the breaker trips OPEN and the emergency limiter caps fail-open traffic at
     `fallback_rate_per_process`; plus the fail-closed counterpart (all denied).
  5. Wire the `slow` suite into CI as a dedicated job.
- **Testing command:** `pytest -m slow` (dedicated CI job; excluded from the fast `not slow` job).

## Current-state audit (verified against `main`, commit 7172a84)

| Concern | Current state | Phase 13 gap |
|---|---|---|
| `slow` marker | Registered (`pyproject.toml:42`) with `--strict-markers` | **Zero tests use it**; no CI job runs it |
| Integration infra | `redis_client` fixture (auto-skip on unreachable Redis, `tests/conftest.py`); `limiter` fixture + `_unique()` key pattern (`tests/test_limiter_integration.py`); all keys cleaned up after use | No shared-key concurrency tests; cleanup under concurrency (DEL) must be planned |
| Token Bucket exactness | `token_bucket_evaluate` reference + Lua parity suite; existing integration tests prove sequential exact capacity (`test_token_bucket_zero_rate_allows_exact_capacity_then_denies`) | Nothing proves exact capacity under simultaneous coroutines or across processes |
| Sliding Window bound | `sliding_window_evaluate(limit, current, previous, window_start, window_size, now) -> bool` (`sentinel/algorithms.py:35`); state format `current:previous:window_start`; window anchored to Redis `TIME()` | Nothing proves the estimate bound under a burst of simultaneous arrivals |
| Breaker | `FAILURE_THRESHOLD = 5`, `OPEN_TIMEOUT_SECONDS = 30`, per-process, injected `now` clock (`sentinel/circuit_breaker.py:16`) | Failure counting under concurrent failures never stress-tested |
| Emergency limiter | `TokenBucketEmergencyLimiter`: capacity = refill = `fallback_rate_per_process_micro` (burst = one second's worth); per-process, endpoint-keyed (`sentinel/emergency.py`) | Cap under concurrent fail-open load never asserted |
| Failure injection | Unit-level via `FakeLoader.set_exception` (`tests/test_http.py`) | No failure injected *under concurrent load*; no real-Redis failure injection |
| CI | lint / security / test jobs (`.github/workflows/ci.yml`); fast job runs `-m "not slow"` | No `slow` job |

**Design constraints:**
- **Invariant #6 (time-source testing):** no-refill tests use `refill_rate=0` so time is
  irrelevant; sliding-window concurrency tests make time irrelevant by construction (all arrivals
  land in one window — see Design decision 2). No wall-clock sleeps in assertions.
- **Expected: zero production-code changes.** Tests + docs + CI only, like Phase 11. If a
  concurrency test fails against current code, the invariant was violated — investigate and fix
  `sentinel/`, never weaken the test. (A fix would touch prod code and must keep 100% coverage.)
- **Trunk-based git workflow** (plan Part 4): short-lived `test/` branch, conventional commits,
  squash-merge PR, sanctioned post-merge docs commit on `main`.

---

## Design decisions

1. **Test files.** `tests/test_concurrency.py` — every test marked `slow` (the concurrency suite
   belongs to the `slow` CI job by design); tests touching real Redis also carry the `integration`
   marker so they inherit the `redis_client` auto-skip. Suite layout:
   - `tests/test_concurrency.py` — coroutine tests (Redis-free emergency test + real-Redis
     coroutine tests).
   - `tests/test_concurrency_multiprocess.py` — the multi-process test.
   - Naming: `test_conc_<n>_...`, mirroring `test_sec_<n>` / `test_obs_<n>`.

2. **Sliding-window concurrency bound (deliverable 1).** With `window_size_micro` huge (e.g.
   `10**12` — ~11.5 days), every arrival of the burst lands in the same window: the `previous`
   term is always 0 and time (`TIME()` microseconds) is irrelevant — no rollover can occur during
   the test. Reference bound: simulate the 50 arrivals **sequentially** with the pure
   `sliding_window_evaluate` (state transitions: +1 to `current` on allow, else unchanged) at a
   fixed `now`; the atomic serialized Lua execution cannot admit more than this sequential
   worst-case reference, so assert `real_allowed <= reference_allowed`, plus a sanity floor
   (`real_allowed >= limit - 1`) so the test still catches gross under-admission. The inequality
   (not equality) is deliberate: real `TIME()` microsecond jitter and nondeterministic coroutine
   ordering forbid an exact expectation.

3. **Failure injection mechanism (deliverables 3–4).** Two layers:
   - Unit layer (Redis-free, fast): `FakeLoader` from `tests/test_http.py` with
     `set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError(...))` — concurrent
     `RateLimiter.evaluate` calls drive the real breaker + real emergency limiter.
   - Integration layer: a **dedicated** `SentinelRedis` constructed in the test pointing at a
     closed port (`redis://localhost:6399/0`) — connection refused is immediate on localhost (no
     20 ms timeout stalls) and leaves the shared `redis_client` fixture untouched. Do NOT call
     `aclose()` on the shared fixture — it would poison later tests.

4. **Emergency-cap assertions (deliverables 3–4).** The emergency limiter's burst is exactly
   `fallback_rate_per_process_micro` of tokens — one second's worth of the fallback rate. With
   `fallback_rate_per_process_micro = TOKENS_PER_TOKEN_MICRO` (1 token), assert the number of
   fail-open decisions ALLOWED by the emergency limiter is **exactly 1** and every subsequent
   fail-open decision is `EMERGENCY_LOCAL_LIMIT`. Because `TokenBucketEmergencyLimiter.evaluate`
   is async but contains no `await`, concurrent asyncio calls serialize deterministically.
   Multi-process capping is out of scope (documented V1 property: emergency limiter is
   per-process).

5. **Multi-process test (deliverable 2).** `multiprocessing` with the `spawn` start context
   (explicit `mp.get_context("spawn")` — portable across Windows dev machines and Linux CI):
   - Worker functions at module top level (picklable); children receive only the Redis URL, key,
     and policy parameters; each child builds its own `SentinelRedis` + `ScriptLoader` +
     `RateLimiter` and uses `asyncio.run` internally.
   - `mp.Barrier` (with timeout) synchronizes the start so all processes race for the same fresh
     key; results flow back via `mp.Queue`; parent asserts `sum(allowed across processes) ==
     capacity` exactly (token bucket, `refill_rate=0`, capacity = 10 tokens). This is the core
     distributed claim: Lua atomicity across processes.
   - Cleanup: parent DELs the shared key after aggregation.
   - Keep it small: 3 processes × 20 requests each (total 60 evaluations); runtime target under
     ~30 s.

6. **CI (deliverable 5).** New `slow` job in `.github/workflows/ci.yml` mirroring the `test` job's
   Redis service block, running `pytest -m slow` with `SENTINEL_REDIS_URL` set. No coverage run in
   the slow job (coverage + `fail_under=100` stays in the fast job; the suite adds tests only, no
   new `sentinel/` lines). Slow tests also run nowhere else: fast job is `-m "not slow"`, and the
   security job is `-m security` (no overlap).

7. **Docs.** Update `docs/sentinel-project-record.md` §09 (four invariants now proven under
   concurrency: exact cross-process capacity, sliding-window bound, breaker OPEN under load,
   emergency cap) and §11 status; tick Phase 13 in `docs/implementation_plan.md` Part 7; record the
   executed plan as `docs/phase-13-plan.md`. AGENTS.md updates land in the post-merge docs commit
   (work history, repo map counts, `slow` CI job).

---

## Task breakdown with git activities

Branch: `test/concurrency-phase13` (short-lived; trunk-based convention).

### Task 0 — Branch setup
```powershell
git fetch origin
git status            # expect: clean, on main, HEAD == origin/main
git checkout -b test/concurrency-phase13
```
No commit in this task.

### Task 1 — `tests/test_concurrency.py` coroutine suite (deliverables 1 + 3, unit layer)

Tests (all `@pytest.mark.slow`; real-Redis ones also `@pytest.mark.integration`):

- `test_conc_01_token_bucket_50_coroutines_exact_capacity` — capacity 10 tokens,
  `refill_rate=0`, 50 concurrent `limiter.evaluate` on one fresh key via `asyncio.gather`.
  Assert exactly 10 `ALLOWED`, 40 `RATE_LIMITED`; unique key, DEL after.
- `test_conc_02_sliding_window_50_coroutines_bounded` — `limit=5`,
  `window_size_micro=10**12`; 50 concurrent; `real_allowed <= reference_allowed` and
  `real_allowed >= limit - 1`; reference from sequential `sliding_window_evaluate` simulation at
  fixed `now`.
- `test_conc_03_emergency_limiter_caps_concurrent_fail_open` (Redis-free) —
  `TokenBucketEmergencyLimiter` + 50 concurrent `RateLimiter.evaluate` where
  `FakeLoader.set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError(...))`; assert exactly 1
  allowed (reason `REDIS_TIMEOUT`), 49 `EMERGENCY_LOCAL_LIMIT`, and the breaker counted the
  failures (CLOSED→OPEN after `FAILURE_THRESHOLD`, state OPEN by the end).
- `test_conc_04_fail_closed_concurrent_failure_all_denied` (Redis-free) — same fake-failure
  setup with a `FAIL_CLOSED` policy; assert all 50 decisions are `FAIL_CLOSED`/`CIRCUIT_OPEN`
  (never allowed) and the breaker ends OPEN.

Use the `limiter`-style fixture pattern from `tests/test_limiter_integration.py` and unique
`uuid4().hex` keys with cleanup.

```powershell
git add tests/test_concurrency.py
git commit -m "test(concurrency): coroutine concurrency and failure-injection suite"
```

### Task 2 — `tests/test_concurrency_multiprocess.py` (deliverable 2)

- `test_conc_10_multiprocess_shared_bucket_exact_capacity` — spawn 3 processes, Barrier sync,
  20 evaluations each on one fresh token-bucket key (`capacity=10`, `refill_rate=0`); assert
  total allowed == 10, total denied == 50; DEL the key. Worker helper functions at module top
  level; `mp.get_context("spawn")`; children use `asyncio.run`.
- Add a process-count check so the test fails loudly if the platform can't spawn (rather than
  silently passing with 1 process).

```powershell
git add tests/test_concurrency_multiprocess.py
git commit -m "test(concurrency): multiprocess shared-bucket exact capacity"
```

### Task 3 — Real-Redis failure injection under concurrent load (deliverable 4)

Add to `tests/test_concurrency.py` (or a focused `tests/test_concurrency_failure.py` if it grows
unwieldy — prefer the first):

- `test_conc_20_failure_injection_trips_breaker_and_emergency_caps` — Phase A: fresh key,
  capacity 5, `refill_rate=0`, 30 concurrent against real Redis → exactly 5 allowed, breaker
  CLOSED. Phase B: swap the limiter's loader/redis for a `SentinelRedis` pointed at a closed port
  (dedicated instance, never the shared fixture), then 30 concurrent fail-open evaluations →
  assert every decision reason ∈ {`REDIS_CONNECTION_ERROR`, `CIRCUIT_OPEN`,
  `EMERGENCY_LOCAL_LIMIT`}, breaker state == OPEN, and exactly 1 allowed-by-emergency
  (`fallback_rate_per_process_micro = 1_000_000`).
- `test_conc_21_failure_injection_fail_closed_all_denied` — same Phase B with a `FAIL_CLOSED`
  policy: all 30 decisions in {`FAIL_CLOSED`, `CIRCUIT_OPEN`}, none allowed.
- Cleanup: DEL the Phase A key; no cleanup needed for the dead-port client (nothing written).

```powershell
git add tests/test_concurrency.py
git commit -m "test(concurrency): real-redis failure injection under concurrent load"
```

### Task 4 — CI `slow` job (deliverable 5)

`.github/workflows/ci.yml`: add a `slow` job cloning the `test` job's Redis service block,
running `pytest -m slow` with `SENTINEL_REDIS_URL: redis://localhost:6379/0`. No other workflow
changes (fast job already excludes `slow`).

```powershell
git add .github/workflows/ci.yml
git commit -m "ci: run the slow concurrency suite in a dedicated job"
```

### Task 5 — Docs (spec stays frozen; status/notes only)

- `docs/sentinel-project-record.md` §09: note which invariants are now proven under concurrency
  (exact cross-process capacity; sliding-window bound vs reference; breaker OPEN under load;
  emergency cap) and that the emergency limiter remains documented per-process (V1).
- §11 status row: Phase 13 done. `docs/implementation_plan.md` Part 7: tick Phase 13.
- This file (`docs/phase-13-plan.md`) is committed as the executed plan.

```powershell
git add docs/sentinel-project-record.md docs/implementation_plan.md docs/phase-13-plan.md
git commit -m "docs(concurrency): record phase-13 testing scope completion"
```

### Task 6 — Quality gates (no commit)

```powershell
docker compose up -d        # real Redis
pytest -m slow              # the new suite
pytest                      # full fast + integration suite still green
pytest --cov=sentinel --cov-report=term-missing   # must stay 100%
mypy sentinel
ruff check .
ruff format --check .
pre-commit run --all-files
pytest -m security          # untouched suite stays green
```

### Task 7 — Merge and record

```powershell
git push -u origin test/concurrency-phase13
# set GH_TOKEN in-process via git credential fill (AGENTS.md recipe), then:
gh pr create --title "test(concurrency): phase-13 concurrency and failure-injection suite" --body "..."
gh pr diff                   # self-review
gh pr merge --squash --delete-branch
```
Post-merge (on `main` — the sanctioned direct-to-main docs commit):
- Update `AGENTS.md` work history + "Where things stand": phases 0–13 complete; repo map
  (test file counts, `slow` CI job); "Not yet implemented" now starts at Phase 14.
- Housekeeping: prune stale branches (`git remote prune origin`).

```powershell
git checkout main; git pull
git add AGENTS.md docs/implementation_plan.md docs/sentinel-project-record.md
git commit -m "docs: record phase-13 completion and summary"
git push
```

---

## Risks / guardrails

- **Zero production-code changes expected.** Any red test against current code means a spec
  invariant was broken under concurrency — investigate `sentinel/` and fix the invariant; never
  `@pytest.mark.skip` or weaken the assertion. A prod fix must keep 100% coverage.
- **Flakiness budget.** No sleeps; time made irrelevant by construction (invariant #6:
  `refill_rate=0`, huge window). Cross-process sync via `mp.Barrier(timeout=...)` with an explicit
  failure, not sleeps. If a test proves flaky in CI, tighten the bound or increase the barrier
  timeout — never loosen the assertion.
- **Windows compatibility.** `spawn` context, top-level picklable workers, `asyncio.run` inside
  workers, no closures/queues of un-picklable objects. Local run on Windows must pass; CI runs
  Ubuntu.
- **Shared-Redis hygiene.** Every test uses fresh `uuid4().hex` keys and DELs them; the
  dead-port `SentinelRedis` never touches the shared fixture's client; children must not reuse
  the parent's asyncio loop.
- **Coverage stays 100%.** Tests-only phase → `fail_under = 100` unaffected.
- **Suite runtime.** Target: `pytest -m slow` < ~60 s total so the CI job stays reasonable.
- No comments in test code; test names carry intent (`test_conc_<n>_...`).

## Executed design — deviations from the plan above (recorded at completion)

All nine tests landed as planned in `tests/test_concurrency.py` and
`tests/test_concurrency_multiprocess.py`, green on Windows/WSL2 and designed to be green on Linux
CI. Three findings changed the assertion design; none touched production code:

1. **The 20 ms socket budget is a real constraint on Windows/WSL2.** Measured: ≥20 simultaneous
   connections reliably exceed `SOCKET_TIMEOUT_SECONDS=0.02` on this machine's WSL2 loopback (even
   warm-pool reads), while ≤15 fit comfortably (~24 ms total at 15). The strict exact-capacity
   assertions therefore run under an in-flight semaphore (`IN_FLIGHT_LIMIT=4`) — still 50 racing
   coroutines, but never more than 4 Redis round trips in flight, so the exact claim is
   deterministic on every host (verified: gated bursts produce exactly 10 ALLOWED / 40 RATE_LIMITED
   on every local run). The unbounded 50-coroutine stress became `test_conc_05`: it asserts the
   documented failure-tolerant invariants unconditionally (Redis never admits more than capacity —
   the atomicity claim — the per-process emergency burst is ≤1, and every decision reason belongs
   to the taxonomy) plus the strict branch when no failure reasons appear (CI Linux takes it).
2. **Dead-port failure injection is environment-dependent at the socket layer.** Linux surfaces an
   unreachable localhost port as connection-refused → `REDIS_CONNECTION_ERROR`; Windows/WSL2
   surfaces the same path as a connect timeout → `REDIS_TIMEOUT`. Both are genuine boundary
   failures from `classify_redis_error`, so `test_conc_20` accepts
   `{REDIS_CONNECTION_ERROR, REDIS_TIMEOUT}` for the fail-open admission and keeps the strict
   counts (exactly 1 allowed-by-emergency, 29 `EMERGENCY_LOCAL_LIMIT`, breaker OPEN).
3. **Multi-process exactness is conditional on health for the same budget reason** (20 in-flight
   per process exceeds it locally). Workers now report `redis_admitted` as well, and the parent
   asserts: `redis_total ≤ capacity`, `emergency_total ≤ PROCESS_COUNT`, `allowed ≤ capacity +
   PROCESS_COUNT`, denied counts consistent, and — when `emergency_total == 0` — the strict
   `total_allowed == capacity` equality (CI Linux).

Everything else matches the plan: fake-loader unit injection (03/04), fail-closed dead-port
counterpart (21), spawn context + Barrier + mp.Queue + asyncio.run workers, fresh
`uuid4().hex` keys with DEL cleanup, dedicated `slow` CI job, and the docs record. Definition of
done (below) is fully met.

## Definition of done

- [x] 50-coroutine Token Bucket test: exactly `capacity` allowed with `refill_rate=0`.
- [x] 50-coroutine Sliding Window test: real result bounded by the reference simulation.
- [x] Multi-process test: 3 spawned processes sharing one bucket admit exactly `capacity` total.
- [x] Real-Redis failure injection under load: breaker OPEN, emergency cap exactly 1 for a
      1-token fallback, fail-closed variant fully denied.
- [x] `pytest -m slow` green locally and in CI (dedicated job); fast + security suites unchanged.
- [x] All quality gates green (full suite, 100% coverage, mypy, ruff, pre-commit).
- [ ] Phase merged via squashed PR; AGENTS.md + project-record §09/§11 + checklist updated.
