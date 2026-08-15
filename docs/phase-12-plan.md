# Phase 12 — Observability: Implementation Plan

## Scope (from `docs/implementation_plan.md`, Phase 12)

- **Objective:** Emit safe, structured logs and metrics.
- **Prerequisites:** Phases 7, 8, 9 (all shipped).
- **Deliverables (3):**
  1. Structured log line on deny carrying `tenant_hash`, `decision_reason`, latency, and breaker
     state — never raw `tenant_id`.
  2. Prometheus metrics keyed ONLY by `decision_reason` and `endpoint_id` (bounded labels; no
     tenant label at all).
  3. The live metrics cardinality-bomb test deferred from Phase 11 (SEC-08): requests fired at
     dynamic sub-paths under one `endpoint_id` must produce exactly one label value, proving no
     code path derives a label from the URL/path.
- **Testing command:** `pytest -m "not slow"` (fast suite; all new tests are Redis-free).

## Current-state audit (verified against `main`, commit 24259e9)

What exists today and what Phase 12 adds:

| Concern | Current state | Phase 12 gap |
|---|---|---|
| Decision availability | `Decision` (8 `DecisionReason` members) set on `request.state.decision` in `SentinelGuard.guard_for` (`sentinel/http.py:120`); produced by `RateLimiter`/strategies/`_fail_open` | No consumer emits logs or metrics |
| Tenant hash | Computed privately inside `build_bucket_key` (`sentinel/limiter.py:42`) | Log needs the hash separately from the key — extract a shared `hash_tenant(tenant_id)` helper |
| Breaker state | `CircuitBreaker.state` property (`sentinel/circuit_breaker.py:42`), reachable from the guard | Not observed anywhere |
| Latency | Not measured; `Decision.decision_time_micro` is a timestamp, not a duration | Measure `evaluate()` wall time in the guard (`time.perf_counter_ns`) |
| Metrics dep | `prometheus-client` NOT in `pyproject.toml` dependencies | First new runtime dependency since Phase 0 |
| Cardinality proof | SEC-08 structural tripwire shipped (Phase 11); live assertion explicitly deferred | Live HTTP-level test with dynamic sub-paths |
| 401 purity | Auth failures raise before any decision (invariant #4) | Must not emit a metric or log line for 401s |
| Test helpers | `FakeLoader` / `_make_app` in `tests/test_http.py`, `redis_client` in `conftest.py` | Reuse for the new suite (no real Redis needed) |

**Design constraint (unchanged from the repo's conventions):** no hidden singletons. The
observability object is an explicit dependency injected into `SentinelGuard`, exactly like
`breaker`/`emergency` were in Phases 8–10. It defaults to real logging + the default Prometheus
registry, and accepts injectable `logger`/`registry` for tests.

## Design decisions

1. **New module `sentinel/observability.py`** with a single class `SentinelObservability`:
   - Constructor: `(logger: logging.Logger | None = None, registry: CollectorRegistry | None =
     None)` — defaults to `logging.getLogger("sentinel")` and `prometheus_client.REGISTRY`.
   - Single method `record_decision(tenant_hash, endpoint_id, decision, latency_micro,
     breaker_state)`:
     - **Logs only when `decision.allowed is False`** at `WARNING` level, via
       `logger.warning(..., extra={...})`-style structured fields — never f-string interpolation
       (plan Part 4: "structured logging only"). Fields: `tenant_hash`, `endpoint_id`,
       `decision_reason`, `latency_micro`, `breaker_state`.
     - **Metrics on every decision** (allowed and denied): counter
       `sentinel_decisions_total{endpoint_id, decision_reason}` and histogram
       `sentinel_evaluate_latency_microseconds{endpoint_id, decision_reason}`. Both label sets are
       bounded by construction: `endpoint_id` is a configured id (ADR-009), `decision_reason` is
       the 8-member enum.
   - Nuance: a fail-open request ALLOWED by the emergency limiter (e.g. `allowed=True` with
     `reason=CIRCUIT_OPEN`) is counted by metrics but gets **no log line** — "logs on deny" means
     `allowed is False`, keeping the failure-path telemetry lossless while the deny log stays
     high-signal.
2. **Wire into `SentinelGuard`** (`sentinel/http.py`): new keyword-only `observability:
   SentinelObservability | None = None` constructor arg. In `_guard`, after the decision is
   produced (and after the `_scripts_loaded` check), measure latency around `limiter.evaluate`
   with `time.perf_counter_ns()`, compute `tenant_hash` once via the shared helper, then call
   `record_decision(...)` with `self._breaker.state`. Auth 401 and 404 paths return before this
   call — no decision, no metric, no log.
3. **Extract `hash_tenant(tenant_id) -> str`** in `sentinel/limiter.py`; `build_bucket_key`
   calls it. Keeps the hash in one place; `build_bucket_key` remains a pure function of its
   inputs, so the Phase 11 SEC-08 source tripwire (`"request" not in build_bucket_key`) stays
   green.
4. **No config changes.** Observability is always-on; log verbosity is the host app's
   `logging` level, the metrics registry is the host's default unless injected. Avoids touching
   `SentinelConfig`/`AppConfig`, `sentinel.example.json`, and the strict-config test matrix.
5. **New runtime dependency:** `prometheus-client>=0.20` added to `[project.dependencies]` in
   `pyproject.toml` (not dev-only — metrics are a shipped feature).

---

## Task breakdown with git activities

Branch: `feat/observability` (short-lived, trunk-based, per the plan's Part 4). Small commits,
squash-merged PR, branch deleted after merge.

### Task 0 — Branch setup
```powershell
git fetch origin
git status            # expect: clean, on main, HEAD == origin/main
git checkout -b feat/observability
```
No commit in this task.

### Task 1 — Dependency + `sentinel/observability.py` + `hash_tenant` helper

- `pyproject.toml`: add `"prometheus-client>=0.20"` to `[project.dependencies]`.
- `sentinel/limiter.py`: extract `hash_tenant(tenant_id)`; `build_bucket_key` delegates to it.
- `sentinel/observability.py`: `SentinelObservability` per Design decisions 1. Strict mypy,
  black-style formatting, no comments.

```powershell
git add pyproject.toml sentinel/limiter.py sentinel/observability.py
git commit -m "feat(observability): structured deny logging and bounded decision metrics"
```

### Task 2 — Wire into `SentinelGuard`

- `sentinel/http.py`: constructor arg `observability: SentinelObservability | None = None`
  (default-construct inside, matching the `breaker or CircuitBreaker()` pattern); measure latency
  around `await self._limiter.evaluate(...)`; emit via `record_decision(...)` before the
  deny/allow HTTP mapping. 401/404 paths untouched.

```powershell
git add sentinel/http.py
git commit -m "feat(http): emit decision telemetry from the guard"
```

### Task 3 — New suite `tests/test_observability.py` (Redis-free, ~10–14 tests)

Use `FakeLoader`/`_make_app` helpers (import from `tests/test_http.py`); construct
`SentinelObservability` with a fresh `CollectorRegistry()` and a `logging.Logger` whose records
are captured via `caplog`. Cover:

- **Counter/labels:** a decision increments `sentinel_decisions_total` once with exactly the
  `{endpoint_id, decision_reason}` label pair; labelnames never include `tenant`/`tenant_hash`.
- **Log on deny:** `caplog` shows one WARNING record per denied decision with fields
  `tenant_hash` (== `hash_tenant(tenant_id)`) and the **raw tenant string absent from the
  record message and args**; `endpoint_id`, `decision_reason`, `latency_micro >= 0`,
  `breaker_state` present.
- **No log on allow:** an `ALLOWED` decision emits no log record, but still increments the counter.
- **Fail-open allow nuance:** `allowed=True` with `reason=CIRCUIT_OPEN` (breaker OPEN + fail-open
  via `_make_app`) → counter incremented, no log line.
- **Denied-by-emergency:** breaker OPEN + fail-open past fallback → `EMERGENCY_LOCAL_LIMIT`
  logged + counted.
- **Fail-closed:** breaker OPEN + fail-closed → `CIRCUIT_OPEN` logged + counted.
- **401 purity:** request with no/expired token → 401, zero new metric samples, zero log records.
- **Live cardinality bomb (deliverable 3, also `@pytest.mark.security`):** one guarded route
  (`resumint.tailor`), then ~30 requests at dynamic sub-paths and query strings
  (`/x/1?a=1`, `/a/resumint.tailor/`, `?endpoint_id=pdftalk.ingest`, ...) → parse
  `generate_latest(registry)` and assert exactly ONE `endpoint_id` label value
  (`resumint.tailor`) across all samples.
- **Latency sanity:** latency fields are non-negative integers.

Name tests `test_obs_<n>_...` (auditable mapping to this plan, mirroring the Phase 11
`test_sec_<n>_...` convention). Register nothing new in `pyproject.toml` markers — reuse
`security` (already registered) for the cardinality test.

```powershell
git add tests/test_observability.py
git commit -m "test(observability): bounded metrics, deny logging, and live cardinality proof"
```

### Task 4 — Docs (spec stays frozen; status/notes only)

- `docs/sentinel-project-record.md` §07: note that the SEC-08 metrics-cardinality row now has its
  live assertion (Phase 12) in addition to the structural tripwire; §11 status row: Phase 12 done.
- `docs/implementation_plan.md` Part 7: tick Phase 12.

```powershell
git add docs/sentinel-project-record.md docs/implementation_plan.md
git commit -m "docs(observability): record phase-12 scope completion and SEC-08 live assertion"
```

### Task 5 — Quality gates (no commit)

```powershell
docker compose up -d        # Redis for the full suite
pytest -m "not slow"        # new suite is Redis-free; rest unchanged
pytest --cov=sentinel --cov-report=term-missing   # must stay 100% (new module fully exercised)
mypy sentinel
ruff check .
ruff format --check .
pre-commit run --all-files
pytest -m security          # new cardinality test joins the security suite
```

### Task 6 — Merge and record

```powershell
git push -u origin feat/observability
# set GH_TOKEN in-process via git credential fill (AGENTS.md recipe), then:
gh pr create --title "feat(observability): phase-12 structured logs and bounded metrics" --body "..."
gh pr diff                   # self-review
gh pr merge --squash --delete-branch
```
Post-merge (on `main` — the sanctioned direct-to-main docs commit):
- Update `AGENTS.md` work history + "Where things stand": phases 0–12 complete.
- Housekeeping: prune stale branches if any remain.

```powershell
git checkout main; git pull
git add AGENTS.md docs/implementation_plan.md docs/sentinel-project-record.md
git commit -m "docs: record phase-12 completion and summary"
git push
```

---

## Risks / guardrails

- **Coverage stays 100%.** The new module and every new branch in `http.py` must be exercised;
  `fail_under = 100` is enforced in CI.
- **No cardinality drift.** The labelnames assertion (exactly `{endpoint_id, decision_reason}`) is
  a structural tripwire in the suite; the live cardinality test is the behavioral half.
- **No PII in telemetry.** Only `tenant_hash` leaves the process; tests assert the raw tenant
  string never appears in log records or metric labels.
- **401 purity preserved.** Auth failures produce no `DecisionReason`, therefore no log and no
  metric — the test enforces this.
- **No config surface added.** Avoids touching `SentinelConfig`/example config and the strict
  extra-key-forbidden tests.
- **`prometheus-client` is the only new dependency.** Pinned loosely (`>=0.20`) like the others;
  it is a pure-Python, thread-safe library consistent with the async model (increments are
  O(1), no I/O).
- **No comments in code; no f-string logging.** Structured logging only, per plan Part 4.
- Real-Redis integration tests are not required for this phase (`FakeLoader` suffices) — no new
  `_unique()`/cleanup obligations.

## Definition of done

- [ ] `SentinelObservability` emits: one WARNING log per denied decision (hash, reason, latency,
      breaker state; no raw tenant) and bounded `{endpoint_id, decision_reason}` metrics for every
      decision.
- [ ] Live cardinality-bomb test proves one `endpoint_id` label value under dynamic sub-paths;
      merged with the Phase 11 SEC-08 structural tripwire.
- [ ] 401s emit neither log nor metric.
- [ ] All quality gates green (`pytest`, 100% coverage, mypy, ruff, pre-commit, `pytest -m security`).
- [ ] Phase merged via squashed PR; AGENTS.md + project-record §11 + checklist updated.
