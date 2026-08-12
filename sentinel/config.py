"""Static configuration loading for Sentinel (Phase 1)."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from sentinel.models import Policy

_ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm_allowlist: frozenset[str]

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
        return self


def load_config(path: Path) -> SentinelConfig:
    """Load and validate the static JSON configuration file."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return SentinelConfig.model_validate(data)
