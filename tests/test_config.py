"""Static config loading tests (Phase 1)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sentinel.config import SentinelConfig, load_config
from sentinel.models import AlgorithmType, FailMode


def _make_app(**overrides: object) -> dict[str, object]:
    app: dict[str, object] = {
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret": "dev-only-secret-change-me-0123456789abcdef",
        "jwt_algorithm_allowlist": ["HS256"],
    }
    app.update(overrides)
    return app


def _make_policy(**overrides: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "endpoint_id": "pdftalk.ingest",
        "capacity_micro": 1_000_000,
        "refill_rate_micro_per_sec": 50_000,
        "algorithm": "sliding_window",
        "fail_mode": "fail_closed",
        "fallback_rate_per_process_micro": 5_000,
        "policy_version": 1,
    }
    policy.update(overrides)
    return policy


def make_config(
    *,
    app: dict[str, object] | None = None,
    policies: dict[str, object] | None = None,
    **overrides: object,
) -> SentinelConfig:
    data: dict[str, object] = {
        "app": _make_app() if app is None else app,
        "policies": {"pdftalk.ingest": _make_policy()} if policies is None else policies,
    }
    data.update(overrides)
    return SentinelConfig.model_validate(data)


def test_valid_config_loads() -> None:
    config = make_config()
    policy = config.policies["pdftalk.ingest"]
    assert policy.algorithm is AlgorithmType.SLIDING_WINDOW
    assert policy.fail_mode is FailMode.FAIL_CLOSED
    assert config.app.jwt_algorithm_allowlist == frozenset({"HS256"})
    assert config.app.redis_url == "redis://localhost:6379/0"
    assert config.app.jwt_secret.get_secret_value() == "dev-only-secret-change-me-0123456789abcdef"


def test_rejects_endpoint_id_mismatch_with_dict_key() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        make_config(policies={"pdftalk.ingest": _make_policy(endpoint_id="other.endpoint")})


def test_rejects_empty_algorithm_allowlist() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        make_config(app=_make_app(jwt_algorithm_allowlist=[]))


def test_rejects_rs256_in_allowlist() -> None:
    with pytest.raises(ValidationError, match="deferred to V2"):
        make_config(app=_make_app(jwt_algorithm_allowlist=["RS256"]))


def test_rejects_lowercase_algorithm() -> None:
    with pytest.raises(ValidationError):
        make_config(app=_make_app(jwt_algorithm_allowlist=["hs256"]))


def test_rejects_short_secret() -> None:
    with pytest.raises(ValidationError):
        make_config(app=_make_app(jwt_secret="too-short"))


def test_rejects_non_redis_scheme() -> None:
    with pytest.raises(ValidationError):
        make_config(app=_make_app(redis_url="http://localhost:6379"))


def test_rejects_unknown_root_key() -> None:
    with pytest.raises(ValidationError):
        make_config(unknown_setting="x")


def test_load_config_from_json_file(tmp_path: Path) -> None:
    config_path = tmp_path / "sentinel.json"
    config_path.write_text(
        json.dumps({"app": _make_app(), "policies": {"pdftalk.ingest": _make_policy()}}),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert "pdftalk.ingest" in config.policies


def test_load_config_rejects_invalid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "sentinel.json"
    config_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_config(config_path)
