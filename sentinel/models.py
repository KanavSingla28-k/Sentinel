"""Domain models and contracts for Sentinel (Phase 1)."""

import enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sentinel.algorithms import TOKENS_PER_TOKEN_MICRO

_ENDPOINT_ID_PATTERN = r"^[a-z0-9._-]+$"

# Lua 5.1 numbers are IEEE doubles: integer arithmetic is exact only below 2**53.
# The phase-4 scripts therefore bound every intermediate product to 2**52.
# Token-bucket Lua computes elapsed_seconds * rate, which is at most
# (capacity + 2 * rate) * 1e6 while the key is alive (the key expires once the
# bucket is full again, so elapsed can never outgrow the refill horizon).
LUA_MAX_EXACT_INT = 2**52
TOKEN_BUCKET_MAX_CAPACITY_MICRO = 2**30
TOKEN_BUCKET_MAX_RATE = 2**30
# (capacity + 2 * rate) * 1e6 is bounded by 3 * 2**30 * 1e6 ~= 3.2e15 < 2**52.
TOKEN_BUCKET_LUA_PRODUCT_LIMIT = 3 * 2**30 * 1_000_000


class AlgorithmType(enum.StrEnum):
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"


class FailMode(enum.StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class DecisionReason(enum.StrEnum):
    ALLOWED = "allowed"
    RATE_LIMITED = "rate_limited"
    REDIS_TIMEOUT = "redis_timeout"
    REDIS_CONNECTION_ERROR = "redis_connection_error"
    REDIS_NOSCRIPT_RETRY = "redis_noscript_retry"
    CIRCUIT_OPEN = "circuit_open"
    EMERGENCY_LOCAL_LIMIT = "emergency_local_limit"
    FAIL_CLOSED = "fail_closed"


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_id: str = Field(pattern=_ENDPOINT_ID_PATTERN)
    capacity_micro: int = Field(ge=TOKENS_PER_TOKEN_MICRO)
    refill_rate_micro_per_sec: int = Field(ge=0)
    algorithm: AlgorithmType
    fail_mode: FailMode
    fallback_rate_per_process_micro: int = Field(ge=1)
    policy_version: int = Field(ge=1)
    limit: int | None = Field(default=None, ge=1)
    window_size_micro: int = Field(default=60_000_000, ge=1_000)

    @model_validator(mode="after")
    def validate_algorithm_parameters(self) -> Self:
        if self.algorithm is AlgorithmType.SLIDING_WINDOW:
            if self.limit is None:
                raise ValueError("limit is required when algorithm is sliding_window")
            if self.limit * self.window_size_micro > LUA_MAX_EXACT_INT:
                raise ValueError(
                    "limit * window_size_micro must not exceed 2**52 (Lua integer exactness)"
                )
        else:
            if self.limit is not None:
                raise ValueError("limit is only valid when algorithm is sliding_window")
            if "window_size_micro" in self.model_fields_set:
                raise ValueError("window_size_micro is only valid when algorithm is sliding_window")
            if self.capacity_micro > TOKEN_BUCKET_MAX_CAPACITY_MICRO:
                raise ValueError("capacity_micro must not exceed 2**30 (Lua integer exactness)")
            if self.refill_rate_micro_per_sec > TOKEN_BUCKET_MAX_RATE:
                raise ValueError(
                    "refill_rate_micro_per_sec must not exceed 2**30 (Lua integer exactness)"
                )
        return self


class Decision(BaseModel):
    allowed: bool
    reason: DecisionReason
    remaining_micro: int = Field(ge=0)
    decision_time_micro: int = Field(ge=0)
    retry_after_seconds: float | None = None
