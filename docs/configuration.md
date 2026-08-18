# Configuration

Sentinel loads a single strict JSON configuration file. `SentinelConfig` is frozen and
rejects unknown keys; every policy must be keyed by (and declare) its explicit `endpoint_id`,
and per-algorithm field mixing is rejected at load. A working example ships in the repo at
[`sentinel.example.json`](https://github.com/KanavSingla28-k/Sentinel/blob/main/sentinel.example.json).

```json
{
  "app": {
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret": "dev-only-secret-change-me-0123456789abcdef",
    "jwt_algorithm_allowlist": ["HS256"]
  },
  "policies": {
    "pdftalk.ingest": {
      "endpoint_id": "pdftalk.ingest",
      "algorithm": "sliding_window",
      "fail_mode": "fail_closed",
      "fallback_rate_per_process_micro": 5000,
      "policy_version": 1,
      "limit": 1000,
      "window_size_micro": 60000000
    },
    "resumint.tailor": {
      "endpoint_id": "resumint.tailor",
      "algorithm": "token_bucket",
      "fail_mode": "fail_open",
      "fallback_rate_per_process_micro": 2000,
      "policy_version": 1,
      "capacity_micro": 10000000,
      "refill_rate_micro_per_sec": 10000
    }
  }
}
```

---

## `app` section

Deployment-level settings, validated by `AppConfig`:

| Field | What it controls | Default | Constraints |
|---|---|---|---|
| `redis_url` | URL of the dedicated Redis instance the guard connects to | — (required) | Must start with `redis://` |
| `jwt_secret` | Shared HMAC secret used to verify incoming bearer tokens | — (required) | Minimum 32 characters |
| `jwt_algorithm_allowlist` | JWT algorithms accepted for verification | — (required) | Non-empty subset of `HS256`, `HS384`, `HS512`. Asymmetric keys and JWKS are rejected at load (deferred to V2) |

`jwt_secret` is treated as a secret value throughout; the algorithm allowlist exists because
the token's `alg` header alone is not trustworthy.

## `policies` section

A map of `endpoint_id → Policy`. Each policy is keyed by (and must declare) an explicit
`endpoint_id` — the id is **never** derived from the URL path (renaming a route does not create
a new bucket). Common fields:

| Field | What it controls | Default | Constraints |
|---|---|---|---|
| `endpoint_id` | The explicit configured id used by `guard_for(...)` and in the Redis key | — (required) | Pattern `^[a-z0-9._-]+$` |
| `algorithm` | Which rate-limit algorithm to use | — (required) | `token_bucket` or `sliding_window` |
| `fail_mode` | Behavior when the Redis store fails | — (required) | `fail_closed` (503 on store failure) or `fail_open` (capped in-process emergency limiter). See [Failure Semantics](failure-semantics.md) |
| `fallback_rate_per_process_micro` | Fail-open allowance per process, in µtokens/s | — (required) | ≥ 1. The emergency limiter's capacity and refill rate are both this value: a burst of one second's worth, then sustained. N instances can admit up to N × this rate |
| `policy_version` | Version stamp that is part of the Redis key | — (required) | ≥ 1. Bump deliberately when a policy changes — it names a new bucket |

### Token-bucket fields (`algorithm: "token_bucket"`)

| Field | What it controls | Default | Constraints |
|---|---|---|---|
| `capacity_micro` | Burst size — the maximum tokens the bucket can hold | — (required) | ≥ 1,000,000 (one token = 1,000,000 µtokens); ≤ 2^30 (Lua integer exactness) |
| `refill_rate_micro_per_sec` | Sustained refill rate in µtokens per second | — (required) | ≥ 0; ≤ 2^30 (Lua integer exactness). `0` = a fixed-capacity bucket with no refill |

### Sliding-window fields (`algorithm: "sliding_window"`)

| Field | What it controls | Default | Constraints |
|---|---|---|---|
| `limit` | Maximum requests admitted per window | — (required) | ≥ 1; `limit × window_size_micro` ≤ 2^52 (Lua integer exactness) |
| `window_size_micro` | The window duration in microseconds | `60000000` (60 s) | ≥ 1,000 (1 ms) |

### Per-algorithm rejection

The two algorithm families are mutually exclusive — mixing them is rejected at load:

- `sliding_window` rejects `capacity_micro` and `refill_rate_micro_per_sec`.
- `token_bucket` rejects `limit` and `window_size_micro` (explicitly setting
  `window_size_micro` on a token-bucket policy is an error even when it matches the default).

---

## Microtokens

All rate values are **microtokens** (1 token = 1,000,000 µtokens). Integer math only — no
floats anywhere in the state. Examples:

| Value | Plain meaning |
|---|---|
| `capacity_micro: 10000000` | 10 tokens of burst capacity |
| `refill_rate_micro_per_sec: 10000` | 0.01 tokens per second |
| `fallback_rate_per_process_micro: 2000` | emergency allowance of 0.002 tokens/s per process |

## Validation rules (summary)

- Unknown keys anywhere are **rejected** (`extra="forbid"` on every model).
- Policy dict keys must match the policy's `endpoint_id`, or loading fails.
- Configuration arithmetic must stay within Lua's integer-exactness envelope (2^53; products
  bounded to 2^52) — configurations that would corrupt state are rejected, not silently accepted.
- The repository ships no dynamic configuration: policies are static, validated strictly at
  load, and `policy_version` exists so policy changes ship as deliberate, auditable bumps.

## Related

- [Quick Start](quickstart.md) — minimal working configuration.
- [Usage](usage.md) — how the config is loaded and used by the guard.
- [Installation](installation.md) — step-by-step setup including the Redis `noeviction`
  requirement.
