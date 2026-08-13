# PR #8 — Phase 7: FastAPI Integration

## Summary

Connects the Phase 5/6 components to HTTP traffic. `sentinel/auth.py` verifies
bearer JWTs (Sentinel owns verification: strict HS* allowlist, required `sub`
and `exp`). `sentinel/http.py` provides `SentinelGuard`, a per-route FastAPI
dependency factory that wires auth → resolver → key builder → RateLimiter and
maps the Decision to 200/429 responses. Authentication failures stop at 401
before the resolver ever runs; Redis/limiter exceptions propagate unchanged
(Phase 8 owns failure handling).

## Files changed

- `sentinel/auth.py` — `AuthenticationError` (+ `AuthReason`), `verify_bearer_token`
- `sentinel/http.py` — `SentinelGuard` (owns `StaticPolicyResolver` + `RateLimiter`),
  `load_scripts()`, `guard_for(endpoint_id)` dependency factory
- `tests/test_auth.py` — pure JWT verification tests (no Redis, no FastAPI)
- `tests/test_http.py` — hermetic TestClient tests with a fake script loader
- `tests/test_http_integration.py` — end-to-end tests against real Redis
- `pyproject.toml` — added `httpx>=0.27` to the `dev` extra (TestClient requirement)

## Contract

- Tenant id = validated JWT `sub`; `X-Tenant-ID` ignored entirely; no header
  is ever hashed
- `jwt.decode(..., algorithms=sorted(allowlist), options={"require": ["exp", "sub"]})`
- Auth failures → 401 + `WWW-Authenticate: Bearer`, never a `DecisionReason`
- Unknown endpoint → 404 (resolver returns None; key builder/limiter not invoked)
- ALLOWED → handler continues, Decision at `request.state.decision`
- RATE_LIMITED → 429 `{"detail": "rate limit exceeded"}`; Token Bucket sets
  `Retry-After: ceil(retry_after_seconds)` (min 1); Sliding Window omits it
- endpoint_id is supplied per route via `Depends(guard.guard_for("..."))` —
  never derived from the URL (ADR-009)
- Scripts must be loaded via `await guard.load_scripts()` before evaluation,
  else `RuntimeError` (no lazy loading)
- Redis/limiter exceptions propagate unchanged; no fail-open/fail-closed,
  breaker, emergency limiter, metrics, or observability

## Notes

- PyJWT 2.13 maps `alg: none` → `InvalidAlgorithmError` (→ UNSUPPORTED_ALGORITHM)
  and non-string `sub` → `InvalidSubjectError` (→ MALFORMED); `InvalidKeyError`
  subclasses `PyJWTError` directly (not `InvalidTokenError`), so the final catch
  is `jwt.PyJWTError`. Empty-string `sub` is rejected post-decode (MISSING_TENANT_CLAIM).
- Integration tests use `httpx.AsyncClient` + `ASGITransport` (same event loop
  as the async Redis client) instead of the sync `TestClient`, which conflicts
  with pytest-asyncio loops during real Redis I/O.

## Verification

- 193 tests pass (152 prior + 41 new: 14 auth, 20 http unit, 7 integration)
- `--cov=sentinel` 100% (336 stmts / 74 branches, 0 missing; auth.py 36/6,
  http.py 52/12)
- mypy --strict clean (25 files)
- ruff check + format clean; pre-commit green
