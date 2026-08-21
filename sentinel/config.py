"""Static configuration loading for Sentinel (Phases 1 and 19)."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from sentinel.models import IdentityMode, Policy

_ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

_ANONYMOUS_COOKIE_NAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"
DEFAULT_ANONYMOUS_COOKIE_NAME = "sentinel_anon_id"
DEFAULT_ANONYMOUS_COOKIE_TTL_SECONDS = 2_592_000  # 30 days
MIN_ANONYMOUS_COOKIE_TTL_SECONDS = 3_600
MAX_ANONYMOUS_COOKIE_TTL_SECONDS = 7_776_000  # 90 days


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    redis_url: str = Field(pattern=r"^redis://")
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm_allowlist: frozenset[str]
    anonymous_cookie_name: str = Field(
        default=DEFAULT_ANONYMOUS_COOKIE_NAME,
        pattern=_ANONYMOUS_COOKIE_NAME_PATTERN,
    )
    anonymous_cookie_ttl_seconds: int = Field(
        default=DEFAULT_ANONYMOUS_COOKIE_TTL_SECONDS,
        ge=MIN_ANONYMOUS_COOKIE_TTL_SECONDS,
        le=MAX_ANONYMOUS_COOKIE_TTL_SECONDS,
    )
    anonymous_cookie_secure: bool = True
    anonymous_cookie_secret: SecretStr | None = None

    @field_validator("jwt_algorithm_allowlist")
    @classmethod
    def _validate_algorithm_allowlist(cls, value: frozenset[str]) -> frozenset[str]:
        if not value:
            raise ValueError("jwt_algorithm_allowlist must not be empty")
        unsupported = value - _ALLOWED_JWT_ALGORITHMS
        if unsupported:
            raise ValueError(
                f"unsupported JWT algorithm(s): {sorted(unsupported)}; "
                "asymmetric keys and JWKS are deferred to V2"
            )
        return value


class SentinelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    app: AppConfig
    policies: dict[str, Policy]

    @model_validator(mode="after")
    def _validate_policy_keys(self) -> "SentinelConfig":
        for endpoint_id, policy in self.policies.items():
            if endpoint_id != policy.endpoint_id:
                raise ValueError(
                    f"policy dict key {endpoint_id!r} does not match "
                    f"Policy.endpoint_id {policy.endpoint_id!r}"
                )
        if self._has_anonymous_policy() and self.app.anonymous_cookie_secret is None:
            raise ValueError(
                "anonymous policies require app.anonymous_cookie_secret "
                "(the HMAC key for anonymous client cookies)"
            )
        return self

    def _has_anonymous_policy(self) -> bool:
        return any(policy.identity is IdentityMode.ANONYMOUS for policy in self.policies.values())


def load_config(path: Path) -> SentinelConfig:
    """Load and validate the static JSON configuration file."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return SentinelConfig.model_validate(data)
