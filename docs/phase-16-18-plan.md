# Sentinel V1 — Phase 16–18 Plan: Packaging, Integration & Release

Status: **planned, not started** (phases 0–15 complete; `main` at `0ba5557`).
Scope: the final epic of `docs/implementation_plan.md` Part 3 — Phase 16 (packaging &
distribution), Phase 17 (example app integrations), Phase 18 (production readiness review &
`v1.0.0` tag). This plan covers all three phases, their quality gates, and the git discipline
for the epic. It is a planning document only — it contains **no code changes**.

The epic's single principle: **the library is frozen.** Phases 16–18 add packaging, integration
harnesses, and release bookkeeping. They must not change rate-limiting behavior, invariants,
or any production code path (except the version number at release time). If any step surfaces a
real defect, it ships as its own `fix/` branch + PR, exactly like the post-Phase-14 fixes
(`606e1f0`, `1328652`), not inside this epic.

---

## Part 1 — Current state audit (what Phase 16 starts from)

| Item | State today | Gap |
|---|---|---|
| Build backend | `setuptools>=68`, `build_meta` (`pyproject.toml:1-3`) | None — keep setuptools |
| Package discovery | `[tool.setuptools.packages.find] include = ["sentinel*"]` | Correct; `tests/`, `benchmarks/`, `examples/` are excluded automatically |
| Package data | `sentinel = ["py.typed", "lua/*.lua"]` (`pyproject.toml:35-36`) | Declared, but **never verified in an actual wheel** — needs a test |
| Version | `0.1.0` duplicated in `pyproject.toml:7` and `sentinel/__init__.py:3` | Two sources of truth — needs a tripwire test or single-sourcing |
| Metadata | name, description, readme, requires-python `>=3.11`, file-based license | No classifiers, keywords, `project.urls` |
| Distribution tooling | none | No `build`, no `twine`, no wheel/sdist smoke test |
| CI | lint / test / security / slow jobs (`ci.yml`) | No packaging job, no publish job |
| Consumer wiring proof | `tests/test_http_integration.py` (used by README quickstart) | No runnable example apps; PDFTalk/Resumint integration untested end-to-end |
| Docs | README (entry point), architecture, failure-handling, known-limitations | README lacks an "install from PyPI" section; project record §11 says "Next: Phase 16" |
| Release | — | No CHANGELOG convention, no tag, no GitHub Release, no publish pipeline |

---

## Part 2 — Phase 16: Packaging & distribution

### Objective

Ship a correct, installable, inspectable `sentinel` wheel + sdist: verified contents (Lua
sources, `py.typed`, nothing leaked), verified metadata, CI jobs for build-check and (later)
publish, and a wheel smoke test that survives on this Windows machine and on CI.

### Prerequisites

Phases 0–15 merged on `main`. No production-code changes required.

### Read / Learn First

- **Python Packaging User Guide** (building & publishing, wheel contents) — *Why:* the wheel
  must carry `sentinel/lua/*.lua` and `sentinel/py.typed`; both are easy to drop silently.
- **`twine check`** semantics — *Why:* metadata mistakes (long-description rendering, license)
  are caught at CI time, not PyPI time.
- **PEP 639** (license metadata) — *Why:* decide consciously whether to keep
  `license = { file = "LICENSE" }` (current) or modernize to `license = "MIT"` +
  `license-files`. Recommendation: **keep the file-based form** — it is already correct and
  changing it adds review surface with no user value in V1.
- **`python -m build` vs `pip wheel`** — *Why:* `python -m build` produces both wheel and
  sdist with isolated build envs; the sdist must also contain `lua/*.lua` (it will, because
  setuptools includes package data in sdists by default — verify, don't assume).

### Implementation steps

1. **Version single-source-of-truth (tripwire, no code change to runtime logic):**
   Keep the static `version = "0.1.0"` in `pyproject.toml` and `__version__` in
   `sentinel/__init__.py`, and add a small test asserting
   `sentinel.__version__ == importlib.metadata.version("sentinel")` when installed
   (fall back gracefully in editable installs — compare against the `pyproject.toml` parse
   otherwise). Rationale: the repo's culture is tripwire tests over machinery; a dynamic
   version (`[tool.setuptools.dynamic]`) makes the build import `sentinel` and adds a failure
   mode for zero benefit.

2. **Metadata completion (`pyproject.toml` `[project]` block only):**
   - `classifiers`: `Programming Language :: Python :: 3.11`, `Programming Language :: Python
     :: 3.12`, `Programming Language :: Python :: 3.13`, `License :: OSI Approved :: MIT
     License`, `Operating System :: OS Independent`, `Intended Audience :: Developers`,
     `Topic :: Software Development :: Libraries :: Python Modules`.
   - `keywords = ["rate-limiting", "fastapi", "redis", "lua", "token-bucket",
     "sliding-window"]`.
   - `[project.urls]`: `Homepage` / `Source` = the GitHub repo, `Documentation` =
     `docs/sentinel-project-record.md` (or the repo docs dir).
   - `authors`: leave unset unless a maintainer identity is chosen — do not invent one.

3. **Distribution dev extras:** add `build>=1.2` and `twine>=5` to the `dev` extra in
   `pyproject.toml` (CI + local only; never runtime deps).

4. **Wheel/sdist content tests — `tests/test_packaging.py`** (`slow`-marked; subprocess
   `python -m build` into a `tmp_path`; Python's `zipfile`/`tarfile` to inspect, no `pip`
   needed):
   - Wheel contains `sentinel/__init__.py`, every `sentinel/*.py` module, `sentinel/py.typed`,
     and **both** `sentinel/lua/token_bucket.lua` + `sentinel/lua/sliding_window.lua`.
   - Wheel contains **no** `tests/`, `benchmarks/`, or `examples/` members (leak tripwire).
   - sdist contains `README.md`, `LICENSE`, `pyproject.toml`, and the same Lua files.
   - Wheel metadata (via `email.message` on `*.dist-info/METADATA`): `Name: sentinel`,
     `Version` matches `__version__`, `Requires-Python >=3.11`, runtime deps exactly the five
     from `pyproject.toml`.
   - `twine check dist/*` passes (subprocess) — catches long-description/README rendering
     breakage before it ever hits PyPI.

5. **CI packaging job (new job in `.github/workflows/ci.yml`):** on every PR/`main` push —
   `pip install -e ".[dev]"` (fast) then `python -m build`, `twine check dist/*`, then the
   decisive smoke: **install the built wheel into a fresh `venv`** (`python -m venv`,
   `pip install dist/*.whl`), `python -c "import sentinel, sentinel.lua, sentinel.http"`, and
   assert the Lua resources resolve via `importlib.resources.files("sentinel")` (a wheel that
   lost `lua/` would fail here and in step 4's pytest — two layers on purpose: pytest gives
   fast local feedback, the venv install proves the artifact actually installs).

6. **CI publish job (new job in `.github/workflows/ci.yml`):** triggered on tags `v*`, depends
   on test + slow + security + lint + packaging being green on the same SHA. Uploads
   `dist/*` to **PyPI** via `twine upload --skip-existing` using
   `TWINE_USERNAME: __token__` / `TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}`. Decision to
   record in the plan: publish straight to PyPI (TestPyPI adds a second secret and a dry-run
   the `twine check` + venv smoke already cover). The job is inert until the secret exists —
   it must be **created before Phase 18** (add to the Phase 18 pre-flight checklist).
   A `v*` tag before the secret exists must fail loudly, not silently skip.

7. **README update (docs-only):** new short "Installing" paragraph —
   `pip install sentinel-rate-limiter` (+ the existing `docker compose`/Redis-`noeviction` requirement),
   plus a line in the Development section pointing at the packaging job and the new test.

### Testing & quality gates (Phase 16)

```
pip install -e ".[dev]"
pytest tests/test_packaging.py           # new wheel/sdist content tests
pytest                                  # full suite, 100% coverage kept
mypy sentinel                           # strict, clean
ruff check . && ruff format --check .   # clean
pre-commit run --all-files              # clean
python benchmarks/benchmark.py --smoke  # untouched behavior proof
```

No production-code change is expected in this phase; if any is required, it ships as a
separate `fix/` PR per the epic rule.

### Definition of Done (Phase 16)

- `python -m build` produces a wheel whose contents are asserted by `tests/test_packaging.py`
  (Lua + `py.typed` present, nothing leaked, metadata exact) and a sdist with the same sources.
- `twine check` clean; a fresh-venv install of the wheel imports and resolves the Lua
  resources.
- CI has a `packaging` job (green on the phase PR) and a `publish` job wired to `v*` tags with
  a documented secret pre-requisite.
- Version tripwire test exists; full suite 294 + new tests green; coverage 100%; ruff/mypy
  clean.

### Git workflow (Phase 16)

- Branch: `chore/packaging` off `main`.
- Commits (conventional, small): `chore(packaging): add build/twine dev extras`,
  `test(packaging): assert wheel and sdist contents`, `ci(packaging): build, twine check, and
  venv-install smoke`, `ci(publish): upload wheels on v* tags`, `docs(readme): installing from
  PyPI`.
- One PR per concern is allowed (2–3 small PRs beats one big one), each squash-merged with
  branch deletion; never commit to `main` directly.
- Merge order: strictly after Phase 15 is merged (it is) and before Phase 17 branches.

---

## Part 3 — Phase 17: Example app integrations

### Objective

Prove Sentinel against its two real consumer patterns with **runnable example apps** in-repo:
PDFTalk (expensive OCR; `sliding_window`, `fail_closed` — 503 on Redis failure) and Resumint
(UX-sensitive; `token_bucket`, `fail_open` — emergency limiter, 429 + `Retry-After`). These are
the integration harness the spec's "kill Redis mid-traffic across 3 instances" test is
preparing for, plus the deployment documentation for both consumers.

### Prerequisites

Phase 16 merged. Real Redis available locally (existing `docker-compose.yml`) and in CI.

### Read / Learn First

- **`tests/test_http_integration.py`** (the real wiring pattern the README quickstart already
  mirrors) — *Why:* examples must use the exact production wiring: `SentinelRedis` →
  `ScriptLoader` → `SentinelGuard` → `guard_for(endpoint_id)` + lifespan `load_scripts()`.
- **Phase 13 dead-port failure injection** (`tests/test_concurrency.py`) — *Why:* the example
  tests reuse its dead-port client pattern (20ms fail-fast budget, `REDIS_TIMEOUT` on
  Windows/WSL2, `REDIS_CONNECTION_ERROR` on Linux — both accepted) to exercise the real
  failure journeys through the full HTTP stack.
- **`docs/failure-handling.md`** decision table — *Why:* each example's expected status codes
  (429 with/without `Retry-After`, 503) must match the frozen table exactly.

### Implementation steps

1. **`examples/pdftalk/`** — fail-closed consumer:
   - `app.py`: FastAPI app with `pdftalk.ingest` endpoint, `sliding_window`, `fail_closed`,
     config loaded from `examples/pdftalk/sentinel.json`; lifespan `await guard.load_scripts()`.
   - `sentinel.json`: app block (redis url, dev JWT secret, `HS256` allowlist) + policy
     (`pdftalk.ingest`, `sliding_window`, `fail_closed`, `policy_version: 1`).
   - `README.md` (short): uvicorn run command, curl examples, and the documented behavior —
     "Redis down ⇒ 503, never an overrun" (OCR framing).
2. **`examples/resumint/`** — fail-open consumer:
   - `app.py`: `resumint.tailor` endpoint, `token_bucket`, `fail_open`,
     `fallback_rate_per_process_micro`; optionally a `/metrics` route using
     `prometheus_client.generate_latest` (the observability story, zero new code).
   - `sentinel.json`: policy (`resumint.tailor`, `token_bucket`, `fail_open`, capacity/refill
     in microtokens, `fallback_rate_per_process_micro`).
   - `README.md`: run command, curl examples, and "Redis down ⇒ emergency limiter caps at
     `fallback_rate_per_process_micro`, 429 with `Retry-After` when capped".
3. **Dev JWT minting helper** — a small, clearly `dev-only` function (shared module under
   `examples/`, e.g. `examples/_jwt.py`) that signs HS256 tokens with the example secret and
   `exp`/`sub` claims; used by the example READMEs and by the example tests. No auth code is
   copied into examples — they consume `SentinelGuard` exactly like production consumers.
4. **Test import plumbing:** examples are not part of the installed package
   (`packages.find` already scopes `sentinel*`), so pytest needs `pythonpath = ["."]` in
   `[tool.pytest.ini_options]` (pytest ≥ 7) so `tests/test_examples.py` can
   `from examples.pdftalk.app import ...`. Coverage stays scoped to `sentinel` — examples are
   not covered by the 100% gate.
5. **`tests/test_examples.py`** (`slow` + `integration`-marked; each app built by a factory so
   breaker/emergency state is fresh per test):
   - PDFTalk happy path: valid JWT ⇒ `DecisionReason.RATE_LIMITED`-free 200-style allow; burst
     past the sliding-window limit ⇒ 429.
   - Resumint happy path: allow; burst past capacity ⇒ 429 with `Retry-After` header.
   - Both: missing/invalid JWT ⇒ 401 + `WWW-Authenticate: Bearer`, **before** any Redis call
     (and no `DecisionReason` emitted — Phase 12 rule).
   - PDFTalk dead-port (Redis pointed at a closed port): 503, `FAIL_CLOSED` family reasons.
   - Resumint dead-port: allowed by the emergency limiter up to its cap, then 429 +
     `Retry-After`; emergency state persists only on ALLOW (Phase-14-fix regression carried
     into the HTTP journey).
   - Endpoint-id hygiene: `endpoint_id` comes from config, never the URL path (SEC-08
     behavioral tripwire at the example level).
6. **`examples/README.md`** (one page): what each example demonstrates, the config table
   columns it uses, run instructions (`docker compose up -d`, `uvicorn ...`, `pip install -e
   ".[dev]"`), and a pointer to `docs/failure-handling.md` for the decision table.
7. **CI:** the example tests ride the existing `slow` job (they are `slow`-marked) — no new
   job; the job already has real Redis. Verify the job stays within its runtime budget.

### Testing & quality gates (Phase 17)

```
docker compose up -d
pytest -m "slow and integration" tests/test_examples.py   # new example journey tests
pytest                                  # full suite, 100% coverage on sentinel/ kept
mypy sentinel                           # strict, clean (examples are not type-gated, but keep them mypy-clean anyway)
ruff check . && ruff format --check .   # clean — ruff must cover examples/ too
pre-commit run --all-files
```

### Definition of Done (Phase 17)

- Two runnable example apps (`examples/pdftalk`, `examples/resumint`) using the production
  wiring, each with a config file, a README, and curl/uvicorn instructions.
- `tests/test_examples.py` proves, over the full HTTP stack against real Redis: happy-path
  allow/deny, 401-before-Redis auth, fail-closed 503 (PDFTalk), fail-open emergency cap +
  429/`Retry-After` (Resumint), and no path-derived `endpoint_id`.
- No production-code changes; if any emerge, they ship as separate `fix/` PRs.

### Git workflow (Phase 17)

- Branch: `feat/examples` off `main` (after Phase 16 merges).
- Commits: `feat(examples): pdf-talk fail-closed app`, `feat(examples): resumint fail-open
  app`, `test(examples): full-stack journeys for both consumers`, `chore(pytest): enable root
  pythonpath for examples`, `docs(examples): run and behavior notes`.
- 2–3 small PRs, squash-merge, delete branches, `main` untouched directly.

---

## Part 4 — Phase 18: Production readiness review & v1.0.0

### Objective

The final P0 triage, the release itself (`v1.0.0` tag + PyPI publish), and the post-release
bookkeeping. The project record's own charge (§11): "Build V1 against this spec, then kill
Redis mid-traffic and run concurrent requests across 3 instances — the real adversarial test
is load." Phase 13/14 already ran the load and failure versions; Phase 18's triage verifies
the record still matches the code and nothing regressed since.

### Prerequisites

Phase 16 (publish job + `PYPI_TOKEN` secret created and verified) and Phase 17 merged. All CI
jobs green on `main`.

### Read / Learn First

- **`docs/known-limitations.md`** — *Why:* the triage walks every row (ADR-011 idempotency,
  per-process breaker/emergency scaling, 20ms socket budget, HS*-only JWT, JWKS V2, sliding-
  window estimate/no Retry-After, single dedicated Redis) and re-confirms each is either
  documented or has a locked regression test.
- **`docs/benchmark-results.md`** — *Why:* re-run the harness for the record; V1 ships with a
  fresh baseline, not a stale one.
- **`docs/sentinel-project-record.md` §11** and `docs/implementation_plan.md` Part 7 — *Why:*
  both get their final status update in this phase.

### Implementation steps

1. **Final P0 triage (test + review only, no code):**
   - Walk every `known-limitations.md` row against the code; confirm each limitation is
     either behaviorally locked by a test or explicitly a documented boundary. Record the
     walk result in the release notes.
   - Re-run the full suite + `pytest -m security` + `pytest -m slow` + `--cov` (100%) + mypy +
     ruff + pre-commit on `main`; re-run `benchmarks/benchmark.py` (full, not just `--smoke`)
     and confirm the key cells are within noise of `docs/benchmark-results.md`.
   - Confirm all 8 `DecisionReason` members are still produced by some path (the
     `decision-table` tests already lock this — re-verify).
   - Open-P0 sweep: any defect found during triage ships as a `fix/` branch + PR *before* the
     release branch is cut; a release never carries unmerged fixes.
2. **Version bump to `1.0.0`** — `pyproject.toml` `version` and `sentinel/__init__.py`
   `__version__` (the Phase 16 tripwire test asserts they match). This is the epic's single
   sanctioned production-file change, and it lands on the release branch, not `main` directly.
3. **Release branch `chore/release-v1.0.0`:**
   - The version bump commit (`chore(release): v1.0.0`).
   - Final docs updates: project record §11 status lines (phases 0–18 complete, release
     metadata), `implementation_plan.md` Part 7 checklist ticks for phases 16–18, README
     version-stamp if it mentions versions anywhere.
   - AGENTS.md refresh (work history + where-things-stand) lands as the sanctioned
     post-release commit on `main` *after* the merge, per convention.
   - No other changes. The release diff is version + docs, nothing else.
4. **Tag & publish:**
   - Merge the release branch (squash) → CI green on the merge commit → **annotated tag**
     `git tag -a v1.0.0 -m "Sentinel V1 — application-aware distributed rate limiting for
     FastAPI"` at that commit, `git push origin v1.0.0` (separate push so a bad tag can be
     deleted without touching history).
   - The Phase 16 publish job fires on the tag; verify the PyPI upload.
   - Create the **GitHub Release** from the tag with notes assembled from the PR list
     (#9–#16 + this epic's PRs) and the triage outcome; no CHANGELOG.md file is added —
     GitHub Releases is the changelog (decision to record).
5. **Final smoke:** fresh venv, `pip install sentinel-rate-limiter` (from PyPI, not the local wheel), import
   + Lua-resource check + one real-Redis integration decision via the quickstart wiring.
6. **Post-release dev bump:** on `main` after the tag, bump to `1.1.0.dev0` (both version
   locations) so local/editable installs are distinguishable from the published `1.0.0`
   (conventional post-release bump; the tripwire test keeps the two in sync).
7. **Hygiene sweep:** confirm `origin/main` == local `main` (push any stragglers, including
   the post-release commits from step 3/6), delete merged branches (`chore/packaging`,
   `feat/examples`, `chore/release-v1.0.0`, plus any stale pre-existing branches), and confirm
   the tag exists on the remote.

### Definition of Done (Phase 18)

- P0 triage complete and recorded; zero open P0s; benchmark re-run within noise.
- `v1.0.0` annotated tag on `main`, pushed; `sentinel 1.0.0` live on PyPI (verified by a
   fresh-venv install from PyPI); GitHub Release with notes.
- `implementation_plan.md` Part 7 shows all 19 phases ticked; project record §11 and AGENTS.md
  reflect the release; working tree clean; no stale branches; repo is release-ready for V2.

### Git workflow (Phase 18)

- Branch: `chore/release-v1.0.0` off `main` (after Phase 17 merges); merge via squash PR.
- The tag is created **only after** the release merge is green on `main`; the tag commit is
  the merge commit itself, never a detached/local-only commit.
- No force-pushes, ever; a broken tag is deleted and re-created, not amended.
- Post-release commits (`1.1.0.dev0` bump, AGENTS.md refresh) are the sanctioned
  direct-to-`main` docs/status commits — everything else this epic lands via PR.

---

## Part 5 — Git best practices for this epic (and beyond)

1. **Trunk-based, short-lived branches.** `main` is always green and shippable. Every change
   in Phases 16–18 lives on a short-lived branch off `main`, lands via a squash-merged PR, and
   the branch is deleted immediately after merge. No feature branch lives longer than a
   working week.
2. **Branch naming.** The three phases use the repo's established prefixes:
   - Phase 16 → `chore/packaging`
   - Phase 17 → `feat/examples`
   - Phase 18 → `chore/release-v1.0.0`
   Split large phases into 2–3 PRs (still on the same branch or sequential short branches) —
   small PRs review faster and merge cleaner.
3. **Conventional commits, one concern each.** Examples: `chore(packaging): add build and
   twine dev extras`, `test(packaging): assert wheel and sdist contents`,
   `ci(publish): upload wheels on v* tags`, `feat(examples): resumint fail-open app`,
   `docs(examples): run and behavior notes`, `chore(release): v1.0.0`. Never bundle a version
   bump with a code change; never mix docs and code in one commit.
4. **PR discipline.** Self-review every PR via the GitHub diff UI before opening; the PR
   template already exists (`.github/PULL_REQUEST_TEMPLATE.md`). Keep the diff reviewable
   (metadata + tests + CI in Phase 16; examples + tests in Phase 17; version + docs only in
   Phase 18). All CI jobs — lint, test, security, slow, and the new packaging job — must be
   green on the PR; the release tag additionally requires them green on `main`.
5. **Never commit implementation directly to `main`.** The only sanctioned direct-to-`main`
   commits are the post-merge status/docs commits (AGENTS.md refresh, checklist ticks,
   post-release dev bump). If a hotfix is needed after Phase 18, it follows the same `fix/`
   branch + PR path as `606e1f0` and `1328652`.
6. **Tagging rules.** Tags are annotated, created at the green merge commit on `main`, pushed
   separately from the branch (`git push origin v1.0.0`), and never moved or force-pushed. A
   broken tag is deleted and re-created. The publish workflow triggers on `v*` — the tag push
   is the deploy action, so it happens deliberately, from `main`, only when CI is green.
7. **Secrets hygiene.** The PyPI token lives only in GitHub Actions secrets
   (`PYPI_TOKEN`); it is never committed, never logged, and never echoed in CI output
   (`twine` masks the token by default — keep `--non-interactive` on and never pass the token
   on a command line that could be captured). `gh` is not authenticated on this machine: for
   `gh pr create`/`merge`/`checks`, set `GH_TOKEN` in-process via the credential-manager
   here-string trick documented in AGENTS.md, and clear it afterwards.
8. **Sequential merge order.** 16 → 17 → 18, each branch cut from an updated `main`. Nothing
   branches from a feature branch; rebasing is not used — branches are short enough that
   merging `main` in (or simply cutting fresh) is always cheaper than a rebase.
9. **Freeze discipline.** The epic adds no behavior. If any step finds a defect, stop, cut a
   `fix/` branch + PR, merge it, and only then continue — never fold a fix into a packaging
   or release PR.
10. **Post-release bookkeeping.** After the tag: push any stragglers, delete merged and stale
    branches (old `feat/`/`chore/` leftovers), verify `origin/main` == local `main`, and
    update the three status surfaces — `implementation_plan.md` Part 7 checklist,
    `docs/sentinel-project-record.md` §11, AGENTS.md — so the next session starts from
    reality, not stale memory.

---

## Part 6 — Epic checklist (mirrors `implementation_plan.md` Part 7 style)

**Phase 16 — Packaging & distribution (`chore/packaging`)**
- [ ] Version tripwire test (pyproject ↔ `__version__`)
- [ ] Metadata: classifiers, keywords, `project.urls`
- [ ] `build` + `twine` dev extras
- [ ] `tests/test_packaging.py`: wheel/sdist contents, leaks, METADATA, `twine check`
- [ ] CI `packaging` job (build → twine check → fresh-venv install smoke)
- [ ] CI `publish` job on `v*` tags (secret pre-requisite documented)
- [ ] README "Installing" paragraph
- [ ] Full gates green; coverage 100%; no production-code changes

**Phase 17 — Example app integrations (`feat/examples`)**
- [ ] `examples/pdftalk/` (sliding_window, fail_closed) — app + config + README
- [ ] `examples/resumint/` (token_bucket, fail_open) — app + config + README (+ optional /metrics)
- [ ] Dev-only JWT minting helper
- [ ] pytest `pythonpath = ["."]`
- [ ] `tests/test_examples.py`: happy paths, 401-before-Redis, fail-closed 503, fail-open
      emergency cap + 429/`Retry-After`, no path-derived endpoint_id
- [ ] `examples/README.md` overview; example tests ride the `slow` CI job

**Phase 18 — Production readiness review & v1.0.0 (`chore/release-v1.0.0`)**
- [ ] P0 triage: known-limitations walk, full suite + security + slow + coverage, benchmark
      re-run within noise, 8 DecisionReason paths re-verified
- [ ] `PYPI_TOKEN` secret exists and publish job verified (dry-run path)
- [ ] Version bump `1.0.0` (both locations, tripwire green)
- [ ] Project record §11, implementation plan Part 7 ticks, README/status docs
- [ ] Annotated tag `v1.0.0` pushed from green `main`; PyPI upload verified
- [ ] GitHub Release with notes (PR list + triage outcome)
- [ ] Fresh-venv `pip install sentinel-rate-limiter` from PyPI smoke test
- [ ] Post-release `1.1.0.dev0` bump + AGENTS.md refresh on `main`
- [ ] Hygiene: no stale branches, `origin/main` == local, clean tree

---

## Part 7 — Decisions to record when executing

- Keep setuptools + file-based license (no PEP 639 churn in V1).
- Static version + tripwire test (no `setuptools.dynamic`).
- Publish to PyPI directly on `v*` tags (no TestPyPI double-hop).
- GitHub Releases are the changelog (no `CHANGELOG.md` file).
- Example apps live in `examples/`, excluded from the wheel and from the coverage gate.
- Post-release dev version `1.1.0.dev0` on `main`.

*SENTINEL — PHASE 16–18 IMPLEMENTATION PLAN · EPIC: PACKAGING, INTEGRATION & RELEASE*
