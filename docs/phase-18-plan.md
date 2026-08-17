# Phase 18 Plan — Production Readiness Review & v1.0.0 Release

**Status:** planned (not started) — written against the current repo state
**Base commit:** `32f74c6` ("Phase 16: Packaging & distribution (#17)"), `main` in sync with `origin/main`, working tree clean
**Predecessor plan:** `docs/phase-16-18-plan.md` (Parts 4–5); this document is the detailed, current-state-grounded version
**Scope discipline:** production-readiness review + release **only**. No behavior changes, no new features, no refactors. Any genuine defect surfaced during triage ships as a separate `fix/` branch (freeze rule — precedent: `606e1f0`).

---

## 0. Where the project actually stands (ground truth as of this plan)

| Item | State |
|---|---|
| Phases 0–16 | Done, merged to `main` (PR #17 squash-merged as `32f74c6`) |
| Phase 17 (in-repo `examples/`) | **Superseded by real-app integration** — see §1 below |
| Tags | None yet |
| `PYPI_TOKEN` secret | **Unverified — must exist before any `v*` tag push** (publish job fails loudly if absent) |
| Publish workflow | `.github/workflows/ci.yml`, `publish` job: `needs: [lint, test, security, slow, packaging]`, runs only on `refs/tags/v*`, `twine upload --skip-existing dist/*` |
| Version locations | `pyproject.toml:7` `version = "0.1.0"` and `sentinel/__init__.py` `__version__ = "0.1.0"` (tripwire-locked by `tests/test_packaging.py`) |
| Stale local branches | `feat/examples` (points at `32f74c6`), `docs/phase-15`, `chore/init-repo`, `feat/domain-models`, `feat/failure-handling` — hygiene target for post-release |

---

## 1. Phase 17 disposition (decision to record)

The Phase 16–18 plan defined Phase 17 as in-repo example apps (`examples/pdftalk`, `examples/resumint`, `tests/test_examples.py`). **Those were never created.** Instead, integration was proven in the **real PDFTalk application** — the stronger form of the same evidence:

- Session outcome: **PASS WITH LIMITATIONS**, all 8 scenarios passed end-to-end against the vendored `sentinel-0.1.0-py3-none-any.whl` wheel (built from the Phase 16 packaging branch — so the wheel artifact itself received a real install + integration run):
  - S1 normal rate limiting (202/202/202/429 + `Retry-After`), S2 Redis state shape, S3 fail-closed (503), S4 recovery after outage, S5 multi-process shared bucket, S6 multi-tenant isolation, S7 auth/401s, S8 script reload after Redis restart, S9 observability metrics.
- **No genuine Sentinel defects found.** Two pre-existing PDFTalk-side issues surfaced (not Sentinel bugs, documented for the PDFTalk repo): 500 on non-UUID `sub` in `get_current_user` (app validation gap), and PDFTalk's structlog config dropping Sentinel's `extra` fields (reasons still visible via Prometheus `sentinel_decisions_total`).
- Evidence lives in the **PDFTalk repo** (cross-repo pointer): `docs/sentinel/integration-test-report.md`, `docs/sentinel/test-results.json`, `docs/sentinel/evidence/`.
- **Decision:** record Phase 17 as *satisfied by real-app integration testing (PDFTalk)*; do not create `examples/` now. The plan for the release PR's bookkeeping commits must reflect this.

Cosmetic observation (disclose, do not fix): the published wheel metadata carries `Provides-Extra: dev` with `build`/`twine` dev-extra requirement markers — normal setuptools output when extras are declared; harmless (not installed without `[dev]`).

---

## 2. Pre-flight checklist (before any code/branch work)

- [ ] Create the `PYPI_TOKEN` GitHub Actions secret (PyPI account API token with project scope) in repo settings → Secrets and variables → Actions. **The publish job hard-fails without it.**
- [ ] Optionally reserve/verify the `sentinel` project name on PyPI (publish is to the real PyPI index; confirm no name conflict before the tag push).
- [ ] Re-run the full quality gates on clean `main` to re-baseline before triage:
  - `pytest` (full fast + integration suite; 302 tests as of Phase 16) — expect 0 failures, 0 skips
  - `pytest --cov=sentinel --cov-report=term-missing` — expect 100%
  - `mypy sentinel` — strict, clean
  - `ruff check .` + `ruff format --check .` — clean
  - `pre-commit run --all-files` — clean
  - `python benchmarks/benchmark.py --smoke` — pass (subprocess-driven in the slow suite; also run directly once)
- [ ] Confirm CI on `origin/main` is green for `32f74c6` (lint/test/security/slow/packaging; publish skipped as expected).

## 3. P0 triage (readiness review, no code)

Walk the deliverables below as an explicit read-through; record findings in the project record. **If nothing blocks, proceed to release. If a genuine P0 defect surfaces: stop the phase, ship a `fix/` branch first, merge, re-run gates.**

- [ ] **Known-limitations walk** — `docs/known-limitations.md` (~19 items: per-process breaker, JWKS deferred to V2, 20ms socket budget, HS*-only JWT, sliding-window estimate, per-process fail-open scaling, etc.). Each item: confirmed still true, consequence accepted, no V1-blocking surprise.
- [ ] **Failure-path inventory** — all 8 `DecisionReason` members exercised end-to-end (RATE_LIMITED, FAIL_CLOSED, CIRCUIT_OPEN, REDIS_TIMEOUT, REDIS_CONNECTION_ERROR, REDIS_NOSCRIPT_RETRY, EMERGENCY_LOCAL_LIMIT, ALLOWED) with correct HTTP 429/503 + Retry-After mapping (`docs/failure-handling.md` decision table).
- [ ] **PDFTalk findings disposition** — confirm both app-side issues are tracked in the PDFTalk repo (not here); confirm no Sentinel-side action items.
- [ ] **Security posture** — `pytest -m security` (23 tests) green; §07 findings locked; §09 invariants proven under concurrency (slow suite incl. benchmark smoke).
- [ ] **Benchmark sanity** — full `benchmarks/benchmark.py` run (18 cells); compare to `docs/benchmark-results.md` baseline within run-to-run noise. Informational only; no thresholds asserted (vision §12).
- [ ] **Record the triage** — project record §09/§11 note: Phase 18 triage summary (date, gates re-run, PDFTalk integration reference, no blocking findings).

## 4. Release mechanics

1. **Branch** `chore/release-v1.0.0` off `main` (`32f74c6`).
2. **Version bump** `0.1.0` → `1.0.0` in both `pyproject.toml:7` and `sentinel/__init__.py` — commit `chore(release): v1.0.0`. The `tests/test_packaging.py` tripwire (pyproject vs `__version__`) must stay green.
3. **Bookkeeping in the same branch** (docs-only commits, or one combined docs commit):
   - `docs/implementation_plan.md` Part 7: tick Phase 18; mark Phase 17 with the *superseded-by-PDFTalk-integration* note (§1 above); phases 0–18 complete.
   - `docs/sentinel-project-record.md` §11: status line → phases 0–18 complete, v1.0.0 released; §09 gains the Phase 18 triage note + PDFTalk integration reference.
   - `AGENTS.md`: refresh work-history top entry + "Where things stand" (this file conventionally lands in a sanctioned post-merge main commit; decide per convention before the PR).
4. **Full gates on the branch** — same suite as §2 pre-flight, all green, incl. the version tripwire.
5. **Open PR (#18)** `Phase 18: Production readiness review & v1.0.0` — squash-merge, delete branch. All CI jobs green on the merge commit before tagging.

## 5. Tag, publish, verify

1. On the green merged `main` commit: `git tag -a v1.0.0 -m "Sentinel V1 — application-aware distributed rate limiting for FastAPI"` and **push the tag separately** (`git push origin v1.0.0`) so a bad tag can be deleted without touching `main`. Annotated tag only; never moved or force-pushed (plan Part 5).
2. **Publish job** triggers on the tag push: lint → test → security → slow → packaging must all pass before `twine upload dist/*`. Watch the run; a `PYPI_TOKEN` failure shows as the hard error step.
3. **Verify the published artifact** (not just CI's upload step):
   - Fresh venv: `pip install sentinel==1.0.0` (real index), then import + a quickstart-style smoke (guarded evaluation with a dummy policy) to prove the published wheel is consumable.
   - `pip index versions sentinel` (or PyPI page) shows `1.0.0`.
4. **GitHub Release** — create from the `v1.0.0` tag: title "v1.0.0 — Sentinel V1", body = merged PR list (PRs #9–#18), pointers to `docs/architecture.md`, `docs/failure-handling.md`, `docs/known-limitations.md`, `docs/benchmark-results.md`, and the PDFTalk integration report. No `CHANGELOG.md` file (documented decision — GitHub Releases is the changelog).

## 6. Post-release

1. **Dev bump** to `1.1.0.dev0` in both version locations (so local/editable installs are distinguishable from the published `1.0.0`), commit `chore(release): post-release dev bump to 1.1.0.dev0` — direct to `main` or a tiny branch per convention.
2. **AGENTS.md refresh** if not already done in the release PR (work-history entry: Phase 18 release; "Where things stand" → phases 0–18 complete, v1.0.0 on PyPI; next = post-V1 roadmap).
3. **Hygiene sweep:** delete stale local branches (`feat/examples`, `docs/phase-15`, `chore/init-repo`, `feat/domain-models`, `feat/failure-handling`); confirm `main == origin/main` and tags pushed (`git ls-remote --tags origin`).
4. **Close-out check:** `pytest` + coverage + pre-commit still green on the final `main`.

## 7. Definition of Done

- [ ] `PYPI_TOKEN` secret created; publish job verified on the real tag push
- [ ] P0 triage completed with **no blocking findings** (or blocking defects fixed via separate `fix/` PRs first); triage recorded in project record
- [ ] Version `1.0.0` in both locations; tripwire green; all CI jobs green on the merge commit
- [ ] Annotated `v1.0.0` tag pushed from green `main`; `sentinel 1.0.0` live on PyPI, verified by fresh-venv install + import smoke
- [ ] GitHub Release `v1.0.0` with notes; implementation-plan checklist + project record §11 updated
- [ ] Post-release `1.1.0.dev0` bump; stale branches cleaned; `main == origin/main`

## 8. Git workflow (Phase 18)

- Branch `chore/release-v1.0.0` off `main`; squash-merge PR (#18); delete branch after merge.
- Conventional commits: `chore(release): v1.0.0`, `docs(...)` for bookkeeping. Never bundle a version bump with unrelated changes.
- Tag pushed separately, annotated, never force-moved. Secrets never committed/logged/echoed.
- Post-release `1.1.0.dev0` bump is the only post-tag change; hotfixes afterwards follow the standard `fix/` branch flow.
