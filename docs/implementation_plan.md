# Sentinel V1 — Implementation Plan & Roadmap Analysis

## Part 1 — Sentinel V1 Architecture Understanding
Sentinel V1 is a distributed, application-layer rate limiter for FastAPI backed by a single dedicated Redis instance. Its core value proposition is correctness under concurrency and failure.

**Core Architecture Flow:**
1. **Request Intake:** A request hits a FastAPI endpoint.
2. **Identity Extraction:** Middleware extracts the tenant identity solely from a validated JWT (never a raw header).
3. **Policy Resolution:** The `tenant_id` and statically configured `endpoint_id` map to a rate-limiting `Policy` (which algorithm, capacity, rate, and failure mode).
4. **Rate Limiting (Happy Path):** The `RateLimiter` invokes a Lua script in Redis. The Lua script executes either a Token Bucket or Sliding Window algorithm atomically and returns a `Decision` (allowed, remaining tokens, retry-after).
5. **Failure & Resiliency (The Triangle):**
   - If Redis times out (20ms budget) or is unreachable, the exception is classified into a bounded `decision_reason`.
   - A per-process **Circuit Breaker** tracks failures and can trip OPEN to protect the system.
   - If fail-open is configured, an in-memory **Emergency Limiter** enforces a local, per-process rate limit to prevent total unbounded abuse.
6. **Observability:** Every decision produces a structured log containing a hashed tenant ID and bounded metrics to prevent cardinality explosions.

---

## Part 2 — Dependency & Execution Order
The Notion workspace defines 19 phases (0 to 18). The execution order is highly sequential up to Phase 7, then transitions into a triad of resiliency phases.

- **Foundational:** Phase 0 (Repo, CI, Tools) and Phase 1 (Domain Models) must be built first.
- **Sequential Core:** Phase 2 (Redis) → Phase 3 (Python Algorithms) → Phase 4 (Lua Scripts). Lua must follow Python models to guarantee behavioral parity. Phase 5 (PolicyResolver) and Phase 6 (RateLimiter) bridge the domain models with the Redis clients.
- **The Resiliency Triangle:** Phase 8 (Failure Classification), Phase 9 (Circuit Breaker), and Phase 10 (Emergency Limiter). These structurally depend on each other. You cannot accurately fail-open without the Emergency Limiter (Phase 10), and you shouldn't fallback without the Circuit Breaker (Phase 9) stopping the bleeding.
- **Cross-Cutting Verification:** Phase 11 (Security) and Phase 12 (Observability) sit right before Phase 13 (Concurrency Testing) because concurrency tests need to assert that the logging and security invariants hold under pressure.

**Corrected Order Verdict:** The existing order is logically sound. There is no need to shuffle the phases. However, the mental model for Phases 8-10 should be treated as a single "Resiliency Epic" rather than isolated steps.

---

## Part 3 — Consolidated Phase-by-Phase Implementation Plan

### Phase 0 — Project init & engineering standards
**Objective:** Establish the foundation, tooling, and CI pipeline for the project.
**Prerequisites:** None.
**Read / Learn First:**
- Read the **GitHub Workflow** page.
- Read **Testing & Security Strategy**.
**Implementation:**
1. Initialize git, `sentinel/` package skeleton, `.gitignore`, and `LICENSE`.
2. Configure `pyproject.toml` (redis, fastapi, pydantic, pytest, ruff, mypy, PyJWT).
3. Setup `ruff format`, `ruff check`, and `mypy --strict`.
4. Setup `pre-commit` hooks.
5. Create `docker-compose.yml` with a dedicated Redis instance (`maxmemory-policy noeviction`).
6. Scaffold GitHub issue/PR templates and CI workflow.
**Testing:** Ensure CI runs fast tests + linting + type checks.
**GitHub Workflow:** Trunk-based, `chore/init-repo` branch, self-review PR.
**Definition of Done:** CI is green, Redis boots locally via Docker.
**Dependencies Created:** Unblocks Phase 1 and 2.

### Phase 1 — Domain model & contracts
**Objective:** Define the boundaries of the system using Pydantic models and strict enums.
**Prerequisites:** Phase 0.
**Read / Learn First:**
- Learn **Pydantic Validation** → *Why:* Boundaries are where malformed inputs enter; Pydantic rejects them at construction. → *Then implement* the `Policy` model.
- Read **ADR-008 (Integer microtokens)**.
**Implementation:**
1. Define `Policy` model (capacity, refill_rate, algorithm, fail_mode, endpoint_id, fallback_rate_per_process, policy_version).
2. Define `AlgorithmType` enum (`TOKEN_BUCKET`, `SLIDING_WINDOW`).
3. Define `DecisionReason` enum (8 explicit values) and `Decision` model.
4. Define static config-loading scaffold mapping `endpoint_id` to `Policy`.
5. Define an **app-level** configuration model (global per deployment, explicitly NOT duplicated inside individual `Policy` entries) for JWT validation. This must enforce a static secret/key and a strict cryptographic algorithm allowlist. JWKS support is explicitly deferred to V2 to avoid introducing unhandled network failure modes.
**Testing:** Unit tests verifying Pydantic rejects invalid policies (e.g., negative capacity) and enforces the app-level JWT algorithm allowlist.
**GitHub Workflow:** `feat/domain-models`.
**Definition of Done:** All domain models type-check and have unit tests.
**Dependencies Created:** Used by all subsequent phases.

### Phase 2 — Redis foundation
**Objective:** Connect to Redis safely, enforcing architecture limits.
**Prerequisites:** Phase 0, Phase 1.
**Read / Learn First:**
- Read **ADR-002, ADR-004, ADR-005**.
- Learn **Connection Pooling** → *Why:* Creating a connection per request exhausts ports/memory; pools reuse them. → *Then implement* the Redis client wrapper.
**Implementation:**
1. Build `asyncio` Redis wrapper with an explicit connection pool.
2. Implement a startup check that runs `CONFIG GET maxmemory-policy` and crashes if not `noeviction`.
3. Scaffold `SCRIPT LOAD` logic (cache SHAs).
4. Apply strict 20ms `socket_timeout` / `socket_connect_timeout`.
**Testing:** Unit tests for startup check failing on `allkeys-lru`.
**GitHub Workflow:** `feat/redis-foundation`.
**Definition of Done:** Startup check successfully enforces `noeviction`.

### Phase 3 — Rate-limit algorithms (pure Python)
**Objective:** Establish pure-math correctness for Token Bucket and Sliding Window before touching Lua.
**Prerequisites:** Phase 1.
**Read / Learn First:**
- Learn **Property-based Testing (Hypothesis)** → *Why:* Edge cases in time-series math are hard to guess; property tests find them. → *Then implement* the traffic verification tests.
**Implementation:**
1. Implement pure function: Token Bucket algorithm using integer microtokens.
2. Implement pure function: Sliding Window counter algorithm.
3. Add microtoken conversion helpers.
**Testing:** Property-based tests verifying the window formula never exceeds the limit.
**GitHub Workflow:** `feat/python-algorithms`.
**Definition of Done:** 100% test coverage on pure functions.

### Phase 4 — Lua atomic execution
**Objective:** Translate the pure algorithms into atomic Redis operations.
**Prerequisites:** Phase 2, Phase 3.
**Read / Learn First:**
- Read **ADR-003 (Lua Atomicity)**.
- Learn **Redis Race Conditions** → *Why:* Sequential INCR/GET is not thread-safe; Lua is single-threaded and atomic. → *Then implement* the Lua scripts.
**Implementation:**
1. Design `KEYS[1]` and `ARGV` schema for Lua scripts. Use `TIME()` inside Lua for the clock.
2. Write Token Bucket Lua script.
3. Write Sliding Window Lua script.
4. Implement `NOSCRIPT` recovery in Python (re-LOAD once, then timeout).
**Testing:** Run Lua scripts against real Redis in tests, asserting exact match with Phase 3 outputs.
**Definition of Done:** Lua scripts load, execute, and handle `NOSCRIPT` gracefully.

### Phase 5 — PolicyResolver
**Objective:** Map an incoming request to its rate-limit policy.
**Prerequisites:** Phase 1.
**Implementation:**
1. Create `PolicyResolver` interface.
2. Build static-config implementation (`tenant_id` + `endpoint_id` → `Policy`).
3. Handle missing/malformed tenant claims gracefully.
**Testing:** Unit tests mapping valid/invalid combinations.

### Phase 6 — RateLimiter orchestration
**Objective:** Bind the PolicyResolver to the Redis Lua scripts via a Strategy Pattern.
**Prerequisites:** Phase 4, Phase 5.
**Implementation:**
1. Build `RateLimiter.evaluate(policy, key)` using Strategy Pattern to pick the right algorithm client.
2. Construct keys using `sentinel:v1:{tenant_hash}:{endpoint_id}:{policy_version}` (SHA-256 for tenant hashing).
3. Inject Redis script clients into `RateLimiter`.
**Testing:** Integration tests against real Redis (no FastAPI yet).

### Phase 7 — FastAPI integration
**Objective:** Expose the rate limiter to HTTP traffic securely.
**Prerequisites:** Phase 5, Phase 6.
**Read / Learn First:**
- Read **ADR-009 (Explicit endpoint_id)**.
- Learn **JWT Validation** → *Why:* Trusting a raw `X-Tenant-ID` header allows tenant spoofing. → *Then implement* strict JWT extraction.
**Implementation:**
1. Implement explicit JWT signature and expiry verification using the host app's configured static key and strict algorithm allowlist. (Sentinel owns this verification).
2. Ensure JWT verification failures immediately raise a 401 Unauthorized before the `PolicyResolver` is ever reached, preserving the purity of the `DecisionReason` enum (which is strictly for rate-limiting decisions, not auth).
3. Extract `tenant_id` purely from these validated JWT claims.
4. Build FastAPI middleware/decorator mapping endpoints to `endpoint_id`.
5. Format 429 responses with `Retry-After` headers.
**Testing:** Unit tests proving forged headers are ignored, invalid signatures/claims trigger a 401, and auth failures never produce a `DecisionReason`.

### Phase 8 — Failure handling
**Objective:** Ensure network errors don't crash the API and follow strict fail-open/closed paths.
**Prerequisites:** Phase 6, Phase 7.
**Read / Learn First:**
- Read **ADR-006 (Fail-open vs closed)** and **ADR-011 (Idempotency limits)**.
**Implementation:**
1. Build exception-to-`decision_reason` classifier. No bare `except:`.
2. Implement branching logic: if fail-closed → 503; if fail-open → fallback to Phase 10 (stub for now).
**Testing:** Failure injection tests (kill Redis connection mid-test).

### Phase 9 — Circuit breaker
**Objective:** Stop hammering a dead Redis instance.
**Prerequisites:** Phase 8.
**Read / Learn First:**
- Read **ADR-007 (Per-process breaker)**.
- Learn **Circuit Breaker State Machines** → *Why:* You must allow probe requests (HALF-OPEN) to recover, and reset failure counts on success. → *Then implement* the state machine.
**Implementation:**
1. Build in-memory CLOSED/OPEN/HALF-OPEN state machine.
2. Reset `failure_count` to 0 on any success.
3. Wire into failure handler (Phase 8).
**Testing:** Unit tests proving state transitions and per-process isolation (2 instances don't share state).

### Phase 10 — Emergency local limiter
**Objective:** Prevent unbounded abuse when failing open.
**Prerequisites:** Phase 9, Phase 3.
**Implementation:**
1. Build an in-memory token bucket.
2. Wire it into the fail-open branch of Phase 8.
3. Document that its rate limit is *per-instance*.
**Testing:** Unit test proving it enforces limits without Redis.
**Definition of Done:** Explicitly document that the Emergency Limiter uses local wall-clock time as a deliberate, necessary exception to the "Redis TIME() is the one clock" rule, since it operates precisely when Redis is unreachable.

### Phase 11 — Security hardening
**Objective:** Verify all spec §07 hardening decisions hold.
**Prerequisites:** Phase 2, Phase 4, Phase 7, Phase 9.
**Implementation:**
1. Walk the Security Findings table and write security regression tests (e.g., Tenant spoofing test).
2. Write a structural test asserting no code path can construct an `endpoint_id` from a raw URL/path string (deferring the live metrics cardinality assertion to Phase 12).
3. Document JWT replay as an accepted upstream boundary.
**Testing:** `pytest -m security`.

### Phase 12 — Observability
**Objective:** Emit safe, structured logs and metrics.
**Prerequisites:** Phase 7, Phase 8, Phase 9.
**Read / Learn First:**
- Learn **Metrics Cardinality** → *Why:* Using raw paths or unhashed tenant IDs in Prometheus tags crashes the metrics server. → *Then implement* bounded fields.
**Implementation:**
1. Write structured log line using `tenant_hash`, `decision_reason`, latency, and breaker state.
2. Emit Prometheus metrics keyed ONLY by `decision_reason` and `endpoint_id`.
**Testing:** Assert that logs contain `tenant_hash` but metrics do not. Run the live metrics cardinality bomb test here, firing requests at dynamic sub-paths under one `endpoint_id` to prove only one label value is emitted.

### Phase 13 — Testing & concurrency testing
**Objective:** Prove Sentinel works under adversarial distributed conditions.
**Prerequisites:** Phase 8, 9, 10, 12.
**Read / Learn First:**
- Learn **Asyncio Concurrency Testing** → *Why:* Sequential tests never trigger race conditions; you must force simultaneous event loops. → *Then implement* the test suite.
**Implementation:**
1. Write a 50-coroutine concurrency test. For Token Bucket, assert exact-capacity (with the strict precondition: `refill_rate=0`). For Sliding Window, assert a bound against the reference formula, not an exact equality.
2. Write multi-process concurrency test.
3. Write failure-injection concurrency test.
4. Write an integration test that injects a Redis failure under heavy concurrent load, asserts the breaker trips OPEN, and confirms the Emergency Limiter caps fail-open traffic at the `fallback_rate_per_process`.
5. Wire slow suite into CI as a separate job.

### Phase 14 — Performance / benchmarking
**Objective:** Measure actual overhead.
**Prerequisites:** Phase 13.
**Implementation:**
1. Run throughput and latency benchmarks (p50/p95/p99) with and without Sentinel. Measure API CPU utilization, Redis CPU utilization, and error rates.
2. Record failure-path latency.

### Phase 15 — Documentation
**Objective:** Compile architecture, API, and limitation docs.
**Implementation:** Write README, Architecture doc, Failure handling doc, and crucially, the **Known Limitations** doc (ADR-011, per-process breaker, JWKS deferred to V2, etc.).

### Phase 16, 17, 18 — Packaging, Integration & Release
**Objective:** Publish the library and verify against real apps.
**Implementation:**
1. Build wheel and package.
2. Integrate into PDFTalk (fail-closed) and Resumint (fail-open).
3. Final P0 bug triage. Tag `v1.0.0`.

---

## Part 4 — Cross-Phase Engineering Workflow
- **Branching Strategy:** Trunk-based. `main` is stable. Branches are short-lived (`feat/`, `fix/`, `test/`).
- **PRs:** Mandate self-review via GitHub diff UI. Small PRs. Merge and delete branch.
- **Commit Convention:** Use Conventional Commits (`feat(redis): ...`, `fix(lua): ...`).
- **Coding Standards:**
  - `mypy --strict` everywhere.
  - Pydantic models for all boundary configurations.
  - No bare `except:` clauses. All errors map to `DecisionReason`.
  - Async all the way down. No blocking I/O in FastAPI routes.
  - Structured logging only, no `logging.info(f"...")`.

---

## Part 5 — Critical Decisions & ADRs
Must be read and understood before executing their respective phases:
- **ADR-001 (Phase 3):** Implement both Token Bucket and Sliding Window.
- **ADR-002, 004, 005 (Phase 2):** Dedicated Redis instance, `noeviction` policy, TTL-only expiry.
- **ADR-003 (Phase 4):** Lua scripts for atomicity.
- **ADR-006 (Phase 8):** Fail-open vs closed configured per integration.
- **ADR-007 (Phase 9):** Circuit Breaker is per-process, not distributed.
- **ADR-008 (Phase 1, 3):** Use integer microtokens, never floats.
- **ADR-009 (Phase 7):** Configured `endpoint_id`, never raw URL paths.
- **ADR-010 (Phase 2, 14):** Redis cluster is deferred to V2.
- **ADR-011 (Phase 8):** No idempotency keys in V1; double charging on timeout is accepted.

---

## Part 6 — Roadmap Gaps / Issues

*All previously identified roadmap gaps (renaming `fallback_rate` to `fallback_rate_per_process` and adding the concurrent failure-injection integration test) have been approved and integrated directly into the Phase 1 and Phase 13 implementation steps above.*

---

## Part 7 — Final V1 Execution Checklist
- [x] Phase 0: Project init & CI
- [x] Phase 1: Domain models
- [x] Phase 2: Redis foundation
- [x] Phase 3: Python reference models
- [x] Phase 4: Lua atomic execution
- [x] Phase 5: PolicyResolver
- [x] Phase 6: RateLimiter orchestration
- [x] Phase 7: FastAPI integration
- [x] Phase 8: Failure handling & classification
- [x] Phase 9: Circuit breaker
- [x] Phase 10: Emergency local limiter
- [x] Phase 11: Security hardening pass
- [x] Phase 12: Observability & metrics
- [x] Phase 13: Full concurrency/failure test suite
- [x] Phase 14: Benchmarking
- [x] Phase 15: Documentation
- [x] Phase 16: Packaging & distribution
- [x] Phase 17: Example app integrations (satisfied by real-app integration testing in PDFTalk — see `docs/phase-18-plan.md` §1; in-repo `examples/` not created by decision)
- [x] Phase 18: Production readiness review & v1.0.0 Tag
