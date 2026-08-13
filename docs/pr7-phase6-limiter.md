# PR #7 — Phase 6: RateLimiter Orchestration

## Summary

Binds the Phase 5 `PolicyResolver` to the Phase 4 Lua scripts: a `RateLimiter`
that picks the matching algorithm strategy, and a pure `build_bucket_key`
function producing the Fig 4 key format with a full 64-hex SHA-256 tenant
hash. Each strategy executes the existing Phase 4 Lua script through the
Phase 2 `ScriptLoader` and maps the raw result into the Phase 1 `Decision`
model (happy path only: ALLOWED / RATE_LIMITED; Redis exceptions propagate
unchanged). No FastAPI, no JWT, no failure handling — those are later phases.

## Files changed

- `sentinel/limiter.py` — `build_bucket_key`, `RateLimitStrategy` (Protocol),
  `TokenBucketStrategy`, `SlidingWindowStrategy`, `RateLimiter`
- `tests/test_limiter.py` — pure unit tests (key format, Decision mapping via
  a tiny fake loader)
- `tests/test_limiter_integration.py` — real-Redis integration tests

## Contract

- Key: `sentinel:v1:{sha256_hex}:{endpoint_id}:{policy_version}`, full 64-char
  digest, hashing only in `build_bucket_key`
- TB: `remaining_micro` = `tokens_after`; denied retry-after =
  `(1_000_000 - tokens_after) / rate` (float), `None` when `rate == 0` or
  allowed
- SW: `remaining_micro` = `max(0, limit - current_after) * 1_000_000`;
  retry-after always `None` (Lua result lacks window-timing info — documented
  in code; Lua unchanged)
- `decision_time_micro` = Python wall clock (`time.time_ns() // 1_000`) —
  observability only; Redis `TIME()` remains the algorithm clock (two clocks,
  intentional)
- Scripts are loaded outside `RateLimiter`; `RateLimiter` maps `AlgorithmType`
  → strategy and delegates

## Verification

- 152 tests pass (143 prior + 19 new: 10 unit, 9 integration)
- `--cov=sentinel` 100% (248 stmts / 56 branches, 0 missing; limiter.py
  49/10, 0 missing)
- mypy --strict clean (20 files)
- ruff check + format clean; pre-commit green
