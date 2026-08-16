# Phase 14 — Performance / Benchmarking: Implementation Plan

## Scope (from `docs/implementation_plan.md`, Phase 14)

- **Objective:** Measure actual overhead.
- **Prerequisites:** Phase 13.
- **Deliverables (2):**
  1. Run throughput and latency benchmarks (p50/p95/p99) with and without Sentinel. Measure API CPU
     utilization, Redis CPU utilization, and error rates.
  2. Record failure-path latency.

## Current-state audit (verified against `main`, commit af5955c)

| Concern | Current state | Phase 14 gap |
|---|---|---|
| Benchmark tooling | None — no `pytest-benchmark`, no profiling deps in `pyproject.toml` | A harness must be built from scratch |
| Benchmark methodology | `docs/vision.md` §12: report numbers as-is with disclosed topology; no pre-committed thresholds or success criteria | Phase 14 must follow the as-is/disclose discipline |
| Failure-path machinery | Phase 8–10 shipped: `classify_redis_error`, breaker, emergency limiter; Phase 13 proved real dead-port injection (dedicated `SentinelRedis` at a closed port; Linux `REDIS_CONNECTION_ERROR` vs Windows `REDIS_TIMEOUT`) | Failure-path latency was never *measured* |
| HTTP layer | `SentinelGuard` FastAPI integration; `tests/test_http.py` uses httpx `ASGITransport` (no live server) | Same in-process pattern is the fastest faithful path for a benchmark |
| Slow suite + CI | `slow` marker, dedicated CI job (`pytest -m slow`) | A benchmark *smoke* test belongs here (runtime-sensitive); no new CI job |
| Socket budget | `SentinelRedis` hardcodes 20 ms socket timeouts; Phase 13 measured ≥20 in-flight connections exceeding it on Windows/WSL2 loopback | Benchmark concurrency must stay far below that (1 and 8) |
| Production code | Phases 0–13, 100% coverage, all gates green | **Expected: zero production-code changes** (like Phase 11) |

**Design constraints:**

- **Invariant discipline:** no production-code changes; if a benchmark measurement conflicts with a
  documented invariant, stop and report rather than silently changing scope.
- **No performance thresholds:** vision §12 requires numbers reported as-is with disclosed topology,
  not judged against pre-committed targets.
- **No new dependencies:** the harness is pure stdlib (`asyncio`, `statistics`, `time`, `platform`,
  `json`, `argparse`); httpx is already a dev dependency (used for `ASGITransport`).
- **Trunk-based git workflow:** short-lived `bench/` branch, conventional commits, squash-merge PR,
  sanctioned post-merge docs commit on `main`.

---

## Design decisions

1. **Custom harness, not a pytest plugin.** `benchmarks/benchmark.py` runs as a plain script
   (`python benchmarks/benchmark.py`), reporting a machine-readable JSON artifact via `--out` plus a
   human table to stdout. A `--smoke` flag runs a tiny version (100 ops/cell, 1 rep) suitable for CI.
   Rationale: benchmark tooling (pytest-benchmark, locust) would be a new dependency with its own
   footprint; the requirements here are small and the measurement code must be reviewable.

2. **Cell matrix.** Nine cells × concurrency {1, 8} (Phase 13's in-flight budget rules out ≥20):
   - `B1` unguarded handler (HTTP baseline).
   - `B2` with-Sentinel, token bucket; `B3` with-Sentinel, sliding window (full HTTP journey).
   - `B4` token bucket, `B5` sliding window — detached (`RateLimiter.evaluate` + JWT decode, no
     HTTP/ASGI layer) to attribute HTTP overhead.
   - `B6` auth + `decide` only — no Redis round trip; attributes the Redis leg.
   - `B7` breaker-OPEN short-circuit (pre-tripped; measures the fail-fast path).
   - `B8` dead-port fail-open; `B9` dead-port fail-closed (failure-path latency, Phase 14
     deliverable 2). A dedicated `SentinelRedis` at `localhost:6399` (never the shared fixture),
     mirroring the Phase 13 pattern; the breaker trips OPEN partway through, so the measured
     journey is a real mix of `redis_timeout` → `circuit_open` decisions.
   - HTTP cells run through httpx `ASGITransport` against the real `SentinelGuard` (guard +
     scripts loaded, real Redis, real JWT `sub`). Detached cells reuse the same
     `RateLimiter`/policy/breaker/emergency objects with a tiny ASGI-free driver.

3. **Fresh-state, no-refill design (invariant #6).** Token-bucket policies use
   `refill_rate_micro_per_sec = 0` with `capacity_micro = 2**30` (time is irrelevant; a fresh
   per-run `uuid4` bucket key guarantees every op is ALLOWED — `over_limit` is asserted == 0
   everywhere, so throughput/latency are measured on the happy path). Sliding window uses
   `limit=1000`, `window_size_micro=60_000_000` (1-minute window; 100 ops per cell stay inside).
   No sleeps; batches of 100 ops per cell per rep.

4. **Latency + CPU + error-rate instrumentation.** Per-op latency via `perf_counter_ns`; three reps
   per cell, median-of-reps reported (throughput, p50/p95/p99, max). API CPU via
   `time.process_time()` deltas around each batch; Redis CPU via `INFO stats
   used_cpu_sys+used_cpu_user` deltas. Error rates are the decision-reason histograms
   (`counts`) — never suppressed, reported as measured. `over_limit` (rate-limit denials) is
   asserted 0 for all cells so happy-path numbers are not polluted by the limiter itself.

5. **Environment disclosure.** Every artifact carries the full environment block (git commit,
   platform, Python version, CPU model + count, Redis version via `INFO server`, timestamp).
   Numbers are single-machine Docker-Compose loopback figures — vision §12 says disclose the
   topology, never imply scale.

6. **CI.** No new CI job. A `slow`+`integration`-marked smoke test
   (`tests/test_benchmark_smoke.py`) subprocesses the harness with `--smoke` and asserts the
   artifact's structural invariants (18 cells, `p50 <= p95 <= p99`, `over_limit == 0`, counts
   consistent with ops, environment present); it rides the existing `slow` CI job. Runtime target
   < ~5 s. Benchmark runs themselves are manual/on-demand — a full run costs minutes, not
   something to gate CI on.

7. **Docs.** `docs/phase-14-plan.md` (this file, executed), `docs/benchmark-results.md` (the
   baseline, as-is with disclosed topology and caveats); project-record §09 note + §11 status;
   tick Phase 14 in the implementation-plan checklist. AGENTS.md updates land in the post-merge
   docs commit.

---

## Task breakdown with git activities

Branch: `bench/perf-phase14` (short-lived; trunk-based convention).

### Task 1 — `benchmarks/benchmark.py` harness core (statistics + engine plumbing)

```powershell
git add benchmarks/benchmark.py
git commit -m "bench(perf): phase-14 benchmark harness with quantile statistics"
```

### Task 2 — with/without-Sentinel cells (B1–B6)

```powershell
git add benchmarks/benchmark.py
git commit -m "bench(perf): with/without-sentinel throughput and latency cells"
```

### Task 3 — failure-path cells (B7–B9)

```powershell
git add benchmarks/benchmark.py
git commit -m "bench(perf): failure-path latency cells"
```

### Task 4 — smoke test in the slow suite

```powershell
git add tests/test_benchmark_smoke.py
git commit -m "test(bench): smoke-run benchmark harness in the slow suite"
```

### Task 5 — Docs

- `docs/phase-14-plan.md` (executed plan, this file).
- `docs/benchmark-results.md` — full baseline from the recorded JSON artifact.
- `.gitignore`: `benchmarks/results/`.
- Project record §09 + §11; implementation-plan checklist tick.

```powershell
git add .gitignore docs/phase-14-plan.md docs/benchmark-results.md docs/sentinel-project-record.md docs/implementation_plan.md
git commit -m "docs(bench): record phase-14 plan, baseline results, and status"
```

### Task 6 — Quality gates (no commit)

```powershell
docker compose up -d        # real Redis
pytest -m slow              # smoke test + concurrency suite
pytest                      # full suite still green
pytest --cov=sentinel --cov-report=term-missing   # must stay 100%
mypy sentinel
ruff check .
ruff format --check .
pre-commit run --all-files
pytest -m security          # untouched suite stays green
git diff sentinel/          # MUST be empty (zero production-code changes)
```

### Task 7 — Merge and record

```powershell
git push -u origin bench/perf-phase14
# GH_TOKEN in-process via git credential fill (AGENTS.md recipe), then:
gh pr create --title "bench(perf): phase-14 benchmark harness and baseline" --body "..."
gh pr diff                   # self-review
gh pr merge --squash --delete-branch
```

Post-merge (on `main` — the sanctioned direct-to-main docs commit): update `AGENTS.md` work
history + "Where things stand" (phases 0–14 complete; benchmark harness + baseline recorded;
"Not yet implemented" now starts at Phase 15).

---

## Risks / guardrails

- **Zero production-code changes.** `git diff sentinel/` empty at the PR. A benchmark finding that
  looks like a production defect is *reported* (see the executed-deviations note below), never
  silently fixed inside a `bench/` PR.
- **No flaky timing asserts in CI.** The smoke test asserts structure, not absolute numbers; no
  sleeps; per-cell batches keep the smoke runtime small.
- **Shared-Redis hygiene.** Fresh `uuid4` keys per run; DEL after each cell; the dead-port client
  is a dedicated instance; `assert_noeviction()` at startup.
- **Coverage stays 100%.** Harness lives outside `sentinel/`; the smoke test subprocesses it.
- **No comments in benchmark code** (repo convention); cell names carry intent.

## Executed design — deviations from the plan above (recorded at completion)

- **B8/B9 warm-up set to 0 ops.** A warm-up pass over the dead port would trip the breaker before
  the measured batch, erasing the very failure journey the cell exists to measure. The pre-trip
  cell (B7) covers breaker-OPEN; B8/B9 therefore start CLOSED and the measured counts include the
  real `redis_timeout → circuit_open` transition.
- **Smoke test self-configures a bounded noeviction Redis.** The CI Redis service is a plain
  `redis:7-alpine` container (no `maxmemory`), while the harness — like `SentinelRedis` in
  production — refuses to start without a bounded `noeviction` policy. Following the
  `tests/test_redis.py` precedent, the smoke test sets `maxmemory-policy=noeviction` +
  `maxmemory=256 MB` on the shared fixture around the subprocess run and restores the prior
  values in a `finally` block (the repo's docker-compose Redis already matches, so local runs
  are a no-op restore).
- **One production defect surfaced and deliberately NOT fixed in this phase.** The fail-open
  emergency limiter's sustained rate measures ~2.3× the configured
  `fallback_rate_per_process_micro` under repeated failure ops (double-refill: denied calls
  persist `tokens_after` while `last_refill_micro` only advances on ALLOW — `sentinel/emergency.py`
  vs the Lua's deliberate "denied requests never write"). Decisive experiment: Lua allows at
  0.0/1.10/2.19 s, the emergency limiter at 0.0/0.44/0.87/1.32/1.76/2.19/2.63 s (1 token/s, 100 ms
  cadence). This is a Phase 8/10 defect the benchmark exists to surface; per the phase contract it
  is **disclosed in `docs/benchmark-results.md`** and the fix ships as a separate PR after Phase 14
  merges.
- **Follow-up fix shipped post-Phase-14** (PR #16): the emergency limiter now persists bucket state
  only on ALLOW (mirrors the Lua contract), with sustained-traffic regression tests at 1/2/5
  tokens/s, a full-journey fail-open regression, and a corrected parity test. Post-fix benchmark
  re-run confirms the sustained allowance matches the configured rate and all 18 cells stay within
  noise of the baseline; see `docs/benchmark-results.md` → "Fix status".
- Everything else matches the plan: custom stdlib harness, nine-cell matrix, no-refill fresh-key
  policies, median-of-reps quantiles, CPU deltas, environment block, smoke test on the `slow` job,
  zero production-code changes.

## Definition of done

- [x] Custom dependency-free harness (`benchmarks/benchmark.py`) with quantile statistics.
- [x] With/without-Sentinel throughput + p50/p95/p99 (HTTP and detached), API CPU, Redis CPU,
      error rates.
- [x] Failure-path latency recorded (B7–B9, real dead-port injection, decision-reason mix).
- [x] Baseline artifact recorded (`docs/benchmark-results.md` + JSON), environment disclosed.
- [x] Smoke test on the `slow` CI job; no new CI job; no new dependencies.
- [x] Zero production-code changes; all quality gates green.
- [ ] Phase merged via squashed PR; AGENTS.md + project-record §09/§11 + checklist updated.
