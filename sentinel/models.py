"""Domain models and contracts for Sentinel (Phase 1)."""

import enum

from pydantic import BaseModel, ConfigDict, Field

_ENDPOINT_ID_PATTERN = r"^[a-z0-9._-]+$"


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
    capacity_micro: int = Field(ge=1)
    refill_rate_micro_per_sec: int = Field(ge=0)
    algorithm: AlgorithmType
    fail_mode: FailMode
    fallback_rate_per_process_micro: int = Field(ge=1)
    policy_version: int = Field(ge=1)


class Decision(BaseModel):
    allowed: bool
    reason: DecisionReason
    remaining_micro: int = Field(ge=0)
    decision_time_micro: int = Field(ge=0)
    retry_after_seconds: float | None = None
