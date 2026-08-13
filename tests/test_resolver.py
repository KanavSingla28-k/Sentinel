"""Policy resolver contract tests (Phase 5)."""

from sentinel.config import SentinelConfig
from sentinel.models import AlgorithmType, FailMode
from sentinel.resolver import StaticPolicyResolver

REDIS_URL = "redis://localhost:6379/0"
JWT_SECRET = "dev-only-secret-change-me-0123456789abcdef"


def _make_app() -> dict[str, object]:
    return {
        "redis_url": REDIS_URL,
        "jwt_secret": JWT_SECRET,
        "jwt_algorithm_allowlist": ["HS256"],
    }


def _sliding_policy() -> dict[str, object]:
    return {
        "endpoint_id": "pdftalk.ingest",
        "algorithm": AlgorithmType.SLIDING_WINDOW,
        "fail_mode": FailMode.FAIL_CLOSED,
        "fallback_rate_per_process_micro": 5_000,
        "policy_version": 1,
        "limit": 1000,
        "window_size_micro": 60_000_000,
    }


def _token_bucket_policy() -> dict[str, object]:
    return {
        "endpoint_id": "resumint.tailor",
        "algorithm": AlgorithmType.TOKEN_BUCKET,
        "fail_mode": FailMode.FAIL_OPEN,
        "fallback_rate_per_process_micro": 2_000,
        "policy_version": 1,
        "capacity_micro": 10_000_000,
        "refill_rate_micro_per_sec": 10_000,
    }


def _make_config() -> SentinelConfig:
    return SentinelConfig.model_validate(
        {
            "app": _make_app(),
            "policies": {
                "pdftalk.ingest": _sliding_policy(),
                "resumint.tailor": _token_bucket_policy(),
            },
        }
    )


def test_sliding_window_endpoint_returns_configured_policy() -> None:
    config = _make_config()
    resolver = StaticPolicyResolver(config)
    resolved = resolver.resolve("tenant-a", "pdftalk.ingest")
    assert resolved is config.policies["pdftalk.ingest"]


def test_token_bucket_endpoint_returns_configured_policy() -> None:
    config = _make_config()
    resolver = StaticPolicyResolver(config)
    resolved = resolver.resolve("tenant-a", "resumint.tailor")
    assert resolved is config.policies["resumint.tailor"]


def test_unknown_endpoint_returns_none() -> None:
    resolver = StaticPolicyResolver(_make_config())
    assert resolver.resolve("tenant-a", "unknown.endpoint") is None


def test_none_tenant_returns_none() -> None:
    resolver = StaticPolicyResolver(_make_config())
    assert resolver.resolve(None, "pdftalk.ingest") is None


def test_empty_tenant_returns_none() -> None:
    resolver = StaticPolicyResolver(_make_config())
    assert resolver.resolve("", "pdftalk.ingest") is None


def test_different_tenants_receive_same_policy_instance() -> None:
    config = _make_config()
    resolver = StaticPolicyResolver(config)
    first = resolver.resolve("tenant-a", "pdftalk.ingest")
    second = resolver.resolve("tenant-b", "pdftalk.ingest")
    assert first is second is config.policies["pdftalk.ingest"]


def test_resolution_does_not_depend_on_tenant_id() -> None:
    config = _make_config()
    resolver = StaticPolicyResolver(config)
    for tenant_id in ("tenant-a", "tenant-b", "tenant-c"):
        assert resolver.resolve(tenant_id, "pdftalk.ingest") is config.policies["pdftalk.ingest"]
