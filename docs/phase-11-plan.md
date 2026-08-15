# Phase 11 — Security Hardening: Implementation Plan

## Scope (from `docs/implementation_plan.md`, Phase 11)

- **Objective:** Verify all spec §07 hardening decisions hold via a security regression suite.
- **Prerequisites:** Phases 2, 4, 7, 9 (all shipped).
- **Deliverables (3):**
  1. Walk the §07 Security Findings table and write security regression tests.
  2. Structural test asserting no code path constructs `endpoint_id` from a raw URL/path string
     (the *live* metrics cardinality assertion stays in Phase 12).
  3. Document JWT replay as an accepted upstream boundary.
- **Testing command:** `pytest -m security`.

## Current-state audit (verified against `main`, commit 249f28d)

The `security` marker is already registered (`pyproject.toml:43`) but **no test uses it**.
Below is the §07 findings table with existing coverage and the Phase 11 gap for each row.

| # | Finding | Resolution (invariant) | Existing coverage | Phase 11 gap |
|---|---|---|---|---|
| SEC-01 | Negative-cost token minting | `cost` removed; no client-reachable numeric input (invariant #5) | `tests/test_models.py:136 test_rejects_extra_fields` (rejects `cost=5`); Lua bounds in `Policy` validation | Not security-marked; no source-level tripwire |
| SEC-02 | Eviction-as-bypass | Dedicated Redis, `noeviction`, TTL-only expiry | `tests/test_redis.py:30/38/47` startup-check tests (integration-marked) | Not security-marked; Lua scripts never audited for TTL-only writes |
| SEC-03 | Tenant identity spoofing via headers | JWT `sub` only; header ignored (invariant #4) | `tests/test_http.py:233 test_x_tenant_id_header_is_ignored` | Not security-marked; no case: header alone (no token) must still 401 |
| SEC-04 | JWT replay | Accepted threat; mitigation lives upstream | none | **Documentation only** (deliverable 3) |
| SEC-05 | Circuit-breaker instance targeting | Breaker per-process; emergency limiter caps damage | `tests/test_circuit_breaker.py:119 test_breakers_are_per_process_isolated`; `tests/test_http.py:476` (OPEN + fail-open → emergency) | Not security-marked |
| SEC-06 | Redis Cluster migration cost | Deferred to V2 — a decision, not a defect | `sentinel/config.py:16` (`^redis://` scheme) | No test warranted; note in docs |
| SEC-07 | Float drift in long-lived buckets | Integer microtokens only (invariant #2) | `tests/test_models.py:255 test_policy_micro_fields_are_integers` | Not security-marked |
| SEC-08 | Metrics cardinality bomb | `endpoint_id` explicit configured id, never raw path (ADR-009) | `tests/test_http.py:344 test_endpoint_id_comes_from_dependency_not_url` | Not security-marked; **no structural tripwire** (deliverable 2) |

**Design constraint:** this phase expects **zero production-code changes**. `sentinel/` is
untouched; every new artifact is a test or a doc. If a new test fails against current code, the
invariant was violated — fix the invariant, never weaken the test.

---

## Task breakdown with git activities

Branch: `test/security-hardening` (short-lived, per the trunk-based convention in the plan's
Part 4). PRs are small and self-reviewed; merge and delete the branch.

### Task 0 — Branch setup
```powershell
git fetch origin
git status            # expect: clean, on main, HEAD == origin/main
git checkout -b test/security-hardening
```
No commit in this task.

### Task 1 — New regression suite `tests/test_security.py` (~12–14 tests, all `@pytest.mark.security`)

All tests use the existing `FakeLoader`/`_make_app` helpers (import from `test_http.py` or a shared
conftest helper — prefer importing the helpers to avoid duplication). No real Redis required, so the
suite auto-runs anywhere. Name tests `test_sec_<n>_...` per the table above so the §07 mapping is
auditable from a `pytest --collect-only` listing.

**SEC-01 group — no client-reachable numeric input:**
- Source tripwire: `"cost" not in inspect.getsource(RateLimiter.evaluate)` and
  `"cost" not in Policy.model_fields`.
- Lua tripwire: `"cost"` appears in neither `TOKEN_BUCKET_SCRIPT` nor `SLIDING_WINDOW_SCRIPT`
  sources (`sentinel.lua.script_source`).
- Behavioral (complements `test_models.py:136`): `Policy(cost=...)` raises `ValidationError`.

**SEC-02 group — eviction-as-bypass:**
- Lua source tripwire via `script_source(name)`: both scripts must contain `PEXPIRE` (TTL-only
  writes), and must NOT contain any of `DEL`, `UNLINK`, `KEYS`, `SCAN`, `FLUSHALL`, `FLUSHDB`.
- (Startup-check behavior stays covered by the tagged integration tests in Task 2.)

**SEC-03 group — tenant spoofing:**
- `X-Tenant-ID` header with **no** `Authorization` header → 401, `WWW-Authenticate: Bearer`,
  zero `FakeLoader.calls` (header alone never authenticates).
- Two different tokens (different `sub`) + identical spoof header → bucket keys differ and key
  derivation only involves each `sub`.
- (Same-token/different-header invariance already proven at `test_http.py:233` — tagged, not duplicated.)

**SEC-08 group — structural `endpoint_id` tripwire (deliverable 2):**
- Source inspection of `SentinelGuard.guard_for` (via `inspect.getsource`): must NOT contain
  `request.url`, `.url.path`, `path_params`, `request.path`, `scope[`, `route`.
- Source inspection of `build_bucket_key`: must NOT contain `request` at all (pure function of
  tenant/endpoint/version).
- Behavioral injection: request paths and query strings containing endpoint-lookalike strings
  (`/a/resumint.tailor/`, `?endpoint_id=pdftalk.ingest`, `?endpoint=...`) must produce the
  configured key (`build_bucket_key("tenant-a", "resumint.tailor", 1)`) — URL fragments cannot
  influence the bucket.

**SEC-05 group — breaker/emergency under attack (one or two tests):**
- A second breaker instance stays CLOSED while a sibling is OPEN (extends the existing isolation
  test to the guard level via `_make_app(breaker=...)`), and the OPEN instance still resolves to
  `EMERGENCY_LOCAL_LIMIT` decisions when fail-open (already at `test_http.py:476` — tag only).

**Git:**
```powershell
git add tests/test_security.py
git commit -m "test(security): add regression suite for spec 07 findings"
```

### Task 2 — Tag existing regression tests with `security` marker

One-line `@pytest.mark.security` additions (plus `pytest` import where missing):
- `tests/test_http.py:233` (SEC-03), `tests/test_http.py:344` (SEC-08),
  `tests/test_http.py:465/476` (SEC-05 HTTP behavior)
- `tests/test_circuit_breaker.py:119` (SEC-05 isolation)
- `tests/test_models.py:136` (SEC-01), `tests/test_models.py:255` (SEC-07)
- `tests/test_redis.py:30/38/47` (SEC-02; these become `integration` **and** `security`, so
  `pytest -m security` needs the real-Redis auto-skip/CI handling already in place)

Verify the marker isn't silently ignored anywhere in the suite.

```powershell
git add tests/test_http.py tests/test_circuit_breaker.py tests/test_models.py tests/test_redis.py
git commit -m "test(security): tag existing regression tests with security marker"
```

### Task 3 — Marker hygiene + CI

- `pyproject.toml`: add `--strict-markers` to `addopts` (every marker must be registered; unknown
  markers fail the run — guards the new marker from typos). All current markers are registered.
- `.github/workflows/ci.yml`: add a dedicated `security` job mirroring the `test` job's Redis
  service block, running `pytest -m security` with `SENTINEL_REDIS_URL` set. This makes the phase's
  canonical testing command a first-class CI gate. (Security tests are fast; they also still run in
  the `not slow` job — harmless overlap.)

```powershell
git add pyproject.toml .github/workflows/ci.yml
git commit -m "chore(ci): enforce registered markers and run security suite in CI"
```

### Task 4 — Document JWT replay as accepted upstream boundary (deliverable 3)

Edit `docs/sentinel-project-record.md` §07 (spec stays frozen — this is a boundary note, not a spec
change):
- Expand the existing JWT-replay table row into a short "Accepted upstream boundaries" note:
  replay is in-scope for V1; mitigations live upstream (short-lived tokens, mTLS, single-use/nonce,
  revocation at `exp`); Sentinel's own controls already present are `exp`+`sub` required, strict
  algorithm allowlist, no token caching, no token state.
- Mark SEC-06 (Redis Cluster, deferred to V2) as a documented decision with no test.

```powershell
git add docs/sentinel-project-record.md
git commit -m "docs(security): document JWT replay as accepted upstream boundary"
```

### Task 5 — Quality gates (no commit)

Start Redis (`docker compose up -d`), then verify all gates stay green — suite grows ~252 → ~270:
```powershell
pytest -m security
pytest
pytest --cov=sentinel --cov-report=term-missing   # 100% coverage
mypy sentinel
ruff check .
ruff format --check .
pre-commit run --all-files
```

### Task 6 — Merge and record

```powershell
git push -u origin test/security-hardening
gh pr create --title "test(security): phase-11 hardening regression suite" --body "..."
# self-review via gh pr diff; merge; delete branch
gh pr merge --squash --delete-branch
```
Post-merge (on `main`, matching repo convention commits `537cfeb`/`249f28d`):
- Update `AGENTS.md` work history + "Where things stand": phases 0–11 complete; Phase 11 checklist
  item ticked in `docs/implementation_plan.md` Part 7; add a status row in project-record §11.
- Housekeeping: prune stale branches `origin/fix/sliding-window-anchor`, `origin/feat/failure-handling`.

```powershell
git checkout main; git pull
git add AGENTS.md docs/implementation_plan.md docs/sentinel-project-record.md
git commit -m "docs: record phase-11 completion and summary"
git push
```

## Risks / guardrails

- **Zero prod-code changes expected.** Any red test against current code means a spec invariant was
  broken — fix `sentinel/` and let the test stay red until fixed, never `@pytest.mark.skip`.
- **Coverage stays 100%** — the suite adds tests only, so `fail_under = 100` is unaffected; the
  tagged tests don't change paths.
- No comments in test code; test names carry intent (`test_sec_<n>_...`). No new integration tests
  (avoids the `_unique()` key/cleanup obligations — `FakeLoader` suffices for every new case).
- Structural source-inspection tests are intentional tripwires: they read `inspect.getsource` and
  fail loudly if a future refactor ever derives identity from the request object.
- The live cardinality-bomb assertion is explicitly deferred to Phase 12 per the roadmap.

## Definition of done

- [ ] `pytest -m security` green locally and in CI (dedicated job).
- [ ] Every §07 row maps to a security-marked test or an explicit documented decision (SEC-04, SEC-06).
- [ ] Structural `endpoint_id` tripwire committed (source + behavioral layers).
- [ ] JWT-replay boundary documented in the project record.
- [ ] All quality gates green; phase merged via squashed PR; AGENTS.md + checklist updated.
