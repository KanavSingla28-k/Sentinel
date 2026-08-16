# Phase 15 — Documentation: Implementation Plan

## Scope (from `docs/implementation_plan.md`, Phase 15)

- **Objective:** Compile architecture, API, and limitation docs.
- **Prerequisites:** Phases 0–14 (all shipped).
- **Deliverables (4):**
  1. README (currently two lines — must become the entry point).
  2. Architecture doc.
  3. Failure handling doc.
  4. **Known Limitations doc** (ADR-011, per-process breaker, JWKS deferred to V2, etc.).

## Current-state audit (verified against `main`, commit 1328652)

| Concern | Current state | Phase 15 gap |
|---|---|---|
| README | Two lines: title + one-liner (`README.md`) | No install, no quickstart, no config reference, no doc links |
| Architecture | The canonical spec (`docs/sentinel-project-record.md`) is review-oriented and long; `AGENTS.md` carries a terse module map | No implementer-facing walkthrough (module map, request journey, state design, clock discipline) |
| Failure handling | Spec §06 decision table + `docs/phase-8-10-summary.md` (phase summary) + `docs/phase-13-plan.md` (concurrency proofs) + `docs/benchmark-results.md` (failure-path latency) | No dedicated consolidated doc covering classification, breaker, emergency limiter, HTTP semantics, and measurements |
| Known limitations | Scattered across the spec: §06 (ADR-011, fail tradeoffs), §07 (JWT replay, JWKS), §10 (deferred to V2), plus code-level decisions (20 ms budget, no `Retry-After` on sliding window, endpoint-keyed emergency buckets) | No single authoritative list a deployer can read |
| Docs conventions | Phase plans (`docs/phase-*plan.md`) follow a stable template; `docs/` excluded from ruff; spec frozen | Phase 15 must not touch the frozen spec's decisions — status/notes only |
| Production code | Phases 0–14, 100 % coverage, all gates green | **Expected: zero production-code changes** (like Phases 11 and 14) |

**Design constraints:**

- **Invariant discipline:** zero production-code changes; docs must not contradict the frozen
  spec — every claim must trace to `sentinel/` code, the project record, or
  `docs/benchmark-results.md`.
- **The project record stays canonical.** Phase 15 docs reference it; they do not fork it.
- **Docs-only quality gates:** ruff/mypy/pytest are unaffected by `docs/` and `README.md`
  (ruff `extend-exclude = ["docs"]`); pre-commit's trailing-whitespace / end-of-file hooks do
  apply and must stay green. The full test suite is unnecessary for a docs-only change — the
  gate is `git diff` showing only markdown.
- **Trunk-based git workflow:** short-lived `docs/` branch, conventional commits, squash-merge
  PR. AGENTS.md updates normally land in a post-merge commit on `main`; see the executed
  deviations note.

---

## Design decisions

1. **Four new/changed documents, one entry point.** `README.md` becomes the library entry
   point (install, quickstart with real wiring, config tables, pointer to everything else);
   `docs/architecture.md`, `docs/failure-handling.md`, and `docs/known-limitations.md` are the
   three deep dives. Each deep dive opens by stating which frozen document it elaborates.
2. **Grounded, not invented.** Every fact in the new docs carries a code reference
   (`sentinel/<module>.py:<line>`) or a benchmark/spec citation. Quickstart code is copied
   from the real integration pattern in `tests/test_http_integration.py` so it runs as written.
3. **Known Limitations is the crucial deliverable.** A summary table (≈19 items) with
   consequence + source (ADR / file), then prose sections for the items that need real
   attention (single Redis, 20 ms budget, ADR-011, per-process fail-open, HS*-only JWTs, JWT
   replay, sliding-window estimate), then "documented decisions that look like limitations"
   and the V2 list — so deployers get one authoritative reading list.
4. **File layout.** `docs/architecture.md`, `docs/failure-handling.md`,
   `docs/known-limitations.md` beside the existing `docs/` corpus; no new directories.
5. **Status updates only in frozen docs.** `docs/implementation_plan.md` (tick Phase 15) and
   `docs/sentinel-project-record.md` §11 (status line) get scope-completion updates; the
   spec's decisions stay untouched.
6. **AGENTS.md refresh.** Work history entry + repo map additions + "Where things stand"
   (phases 0–15 complete; next work starts at Phase 16). Conventionally a post-merge `main`
   commit — see executed deviations.

---

## Task breakdown with git activities

Branch: `docs/phase-15` (short-lived; trunk-based convention).

### Task 1 — Deep dives: architecture, failure handling, known limitations

```powershell
git add docs/architecture.md docs/failure-handling.md docs/known-limitations.md
git commit -m "docs(phase15): architecture, failure-handling, and known-limitations docs"
```

### Task 2 — README rewrite

```powershell
git add README.md
git commit -m "docs(phase15): rewrite README as the library entry point"
```

### Task 3 — Plan + checklist + project-record status

- `docs/phase-15-plan.md` (executed plan, this file).
- `docs/implementation_plan.md` Part 7: tick Phase 15.
- `docs/sentinel-project-record.md` §11: status line updated (phases 0–15 complete; next:
  Phase 16).

```powershell
git add docs/phase-15-plan.md docs/implementation_plan.md docs/sentinel-project-record.md
git commit -m "docs(phase15): record plan, tick checklist, update project-record status"
```

### Task 4 — AGENTS.md refresh (see executed deviations)

```powershell
git add AGENTS.md
git commit -m "docs: refresh AGENTS.md for phase-15 completion"
```

### Task 5 — Quality gates (no commit)

```powershell
git diff sentinel/ tests/ benchmarks/ pyproject.toml   # MUST be empty (zero code changes)
git diff --stat                                        # docs + AGENTS.md only
pre-commit run --all-files                             # trailing-whitespace/EOF hooks on markdown
git status                                             # clean tree on docs/phase-15
```

### Task 6 — Merge and record (left to the maintainer; see executed deviations)

```powershell
git push -u origin docs/phase-15
# GH_TOKEN in-process via git credential fill (AGENTS.md recipe), then:
gh pr create --title "docs(phase15): architecture, failure-handling, and known-limitations docs" --body "..."
gh pr diff                   # self-review
gh pr merge --squash --delete-branch
```

---

## Risks / guardrails

- **Zero production-code changes.** `git diff sentinel/ tests/ benchmarks/ pyproject.toml`
  empty at the end; only markdown files touched.
- **No invented facts.** Every claim traceable to source, spec, or benchmark artifact; the
  quickstart wiring is copied from the real integration test.
- **Frozen spec respected.** Project record and implementation plan get status/checklist
  updates only; no decision is revisited in Phase 15 docs.
- **Pre-commit stays green.** Markdown files must have no trailing whitespace and a final
  newline (pre-commit hooks apply to them).
- **No emojis, repo style.** Docs follow the existing tone (short titles, tables, terse prose).

## Executed design — deviations from the plan above (recorded at completion)

- **AGENTS.md updated on the branch, not as a post-merge `main` commit.** The repo convention
  lands AGENTS.md changes in a sanctioned direct-to-main docs commit after a phase PR merges;
  since this branch is not being pushed/merged by this session, the AGENTS.md refresh was
  included as the final branch commit so the branch is complete and merge-ready. If preferred,
  drop that commit and apply the same content on `main` post-merge.
- **Pre-existing uncommitted AGENTS.md update folded in.** The working tree already carried the
  uncommitted post-`1328652` AGENTS.md refresh (benchmark socket-budget separation work-history
  entry). It was folded into the Phase 15 AGENTS.md commit rather than left dangling.
- **No test suite run.** The phase touches no code; the documented gate is the empty
  `git diff` over code paths plus pre-commit over the markdown. Full-suite runs remain the
  gate for any code-touching PR.
- Everything else matches the plan: four documents, grounded quickstart, status-only updates
  to the frozen docs, conventional commits, zero code changes.

## Definition of done

- [x] `README.md` rewritten as the entry point (install, quickstart, config tables, doc links).
- [x] `docs/architecture.md` — module map, request journey, state/keys, clock discipline,
      invariants, evidence map.
- [x] `docs/failure-handling.md` — decision table, classifier, breaker, emergency limiter,
      HTTP semantics, measured failure-path latency, ADR-011.
- [x] `docs/known-limitations.md` — the authoritative limitation list (ADR-011, per-process
      breaker, JWKS deferred, 20 ms budget, etc.) with sources.
- [x] `docs/phase-15-plan.md` executed plan; Phase 15 ticked in the implementation-plan
      checklist; project-record §11 status updated; AGENTS.md refreshed.
- [x] Zero production-code changes; docs-only diff; pre-commit green.
- [ ] Phase merged via squashed PR (left to the maintainer; branch is merge-ready).
