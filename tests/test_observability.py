"""Observability tests: structured deny logging and bounded metrics (Phase 12)."""

import logging
from typing import cast

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest
from sentinel.http import SentinelGuard
from sentinel.limiter import hash_tenant
from sentinel.lua import TOKEN_BUCKET_SCRIPT
from sentinel.observability import SentinelObservability
from sentinel.redis import ScriptLoader, SentinelRedis

from test_http import FakeLoader, _config, _make_app, _token, _tripped_breaker

_LABEL_KEYS = frozenset({"endpoint_id", "decision_reason"})


def _samples(registry: CollectorRegistry) -> list[tuple[str, dict[str, str], str]]:
    samples: list[tuple[str, dict[str, str], str]] = []
    for line in generate_latest(registry).decode().splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        labels: dict[str, str] = {}
        if "{" in name:
            name, raw = name.split("{", 1)
            for pair in raw.rstrip("}").split(","):
                key, _, val = pair.partition("=")
                labels[key] = val.strip('"')
        samples.append((name, labels, value))
    return samples


def _decision_samples(
    registry: CollectorRegistry,
) -> list[tuple[str, dict[str, str], str]]:
    return [sample for sample in _samples(registry) if sample[0].endswith("_total") and sample[1]]


def _sentinel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "sentinel"]


async def test_obs_01_allowed_decision_counts_metrics() -> None:
    registry = CollectorRegistry()
    observability = SentinelObservability(logger=logging.getLogger("sentinel"), registry=registry)
    app, guard, _ = _make_app("resumint.tailor", observability=observability)
    await guard.load_scripts()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    samples = _decision_samples(registry)
    assert len(samples) == 1
    name, labels, value = samples[0]
    assert name.endswith("_total")
    assert labels == {"endpoint_id": "resumint.tailor", "decision_reason": "allowed"}
    assert float(value) == 1.0
    histogram = [s for s in _samples(registry) if "microseconds" in s[0]]
    assert histogram
    assert all(set(s[1]) - {"le"} == _LABEL_KEYS for s in histogram)


async def test_obs_02_denied_decision_logs_structured_deny(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, loader = _make_app(
        "resumint.tailor", observability=SentinelObservability(registry=registry)
    )
    await guard.load_scripts()
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 429
    records = _sentinel_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelname == "WARNING"
    assert record.getMessage() == "rate limit decision denied"
    assert record.identity_mode == "tenant_jwt"
    assert record.identity_hash == hash_tenant("tenant-a")
    assert record.endpoint_id == "resumint.tailor"
    assert record.decision_reason == "rate_limited"
    assert record.latency_micro >= 0
    assert record.breaker_state == "closed"
    assert "tenant-a" not in record.getMessage()


async def test_obs_03_allowed_decision_emits_no_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, _ = _make_app(
        "resumint.tailor", observability=SentinelObservability(registry=registry)
    )
    await guard.load_scripts()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    assert _sentinel_records(caplog) == []


async def test_obs_04_fail_open_allowed_by_emergency_counts_but_does_not_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, _ = _make_app(
        "resumint.tailor",
        breaker=_tripped_breaker(),
        observability=SentinelObservability(registry=registry),
    )
    await guard.load_scripts()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    samples = _decision_samples(registry)
    assert len(samples) == 1
    assert samples[0][1]["decision_reason"] == "circuit_open"
    assert _sentinel_records(caplog) == []


async def test_obs_05_emergency_denied_logs_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, _ = _make_app(
        "resumint.tailor",
        breaker=_tripped_breaker(),
        observability=SentinelObservability(registry=registry),
    )
    await guard.load_scripts()
    client = TestClient(app)
    token = _token()
    assert client.post("/a", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/a", headers={"Authorization": f"Bearer {token}"}).status_code == 429
    samples = _decision_samples(registry)
    assert len(samples) == 2
    reasons = {s[1]["decision_reason"] for s in samples}
    assert reasons == {"circuit_open", "emergency_local_limit"}
    records = _sentinel_records(caplog)
    assert len(records) == 1
    assert records[0].decision_reason == "emergency_local_limit"


async def test_obs_06_fail_closed_circuit_open_logs_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, loader = _make_app(
        "pdftalk.ingest",
        breaker=_tripped_breaker(),
        observability=SentinelObservability(registry=registry),
    )
    await guard.load_scripts()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 503
    assert loader.calls == []
    samples = _decision_samples(registry)
    assert samples[0][1]["decision_reason"] == "circuit_open"
    records = _sentinel_records(caplog)
    assert len(records) == 1
    assert records[0].decision_reason == "circuit_open"
    assert records[0].breaker_state == "open"


async def test_obs_07_labels_are_bounded_and_endpoint_only() -> None:
    registry = CollectorRegistry()
    app, guard, loader = _make_app(
        "resumint.tailor", observability=SentinelObservability(registry=registry)
    )
    await guard.load_scripts()
    client = TestClient(app)
    token = _token()
    assert client.post("/a", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    assert client.post("/a", headers={"Authorization": f"Bearer {token}"}).status_code == 429
    samples = _samples(registry)
    assert samples
    for _, labels, _ in samples:
        assert set(labels) - {"le"} <= _LABEL_KEYS
    assert {s[1]["endpoint_id"] for s in samples} == {"resumint.tailor"}
    assert {s[1]["decision_reason"] for s in samples} == {"allowed", "rate_limited"}


async def test_obs_08_auth_failure_emits_no_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, _ = _make_app(
        "resumint.tailor", observability=SentinelObservability(registry=registry)
    )
    await guard.load_scripts()
    response = TestClient(app).post("/a")
    assert response.status_code == 401
    assert _samples(registry) == []
    assert _sentinel_records(caplog) == []


async def test_obs_09_unknown_endpoint_emits_no_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, _ = _make_app(
        "not.configured", observability=SentinelObservability(registry=registry)
    )
    await guard.load_scripts()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 404
    assert _samples(registry) == []
    assert _sentinel_records(caplog) == []


async def test_obs_10_unloaded_scripts_emits_no_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, _, _ = _make_app("resumint.tailor", observability=SentinelObservability(registry=registry))
    with pytest.raises(RuntimeError, match="load_scripts"):
        TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert _samples(registry) == []
    assert _sentinel_records(caplog) == []


async def test_obs_11_latency_measured_in_microseconds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CollectorRegistry()
    app, guard, loader = _make_app(
        "resumint.tailor", observability=SentinelObservability(registry=registry)
    )
    await guard.load_scripts()
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 429
    record = _sentinel_records(caplog)[0]
    assert isinstance(record.latency_micro, int)
    assert record.latency_micro >= 0


def test_obs_12_metric_labelnames_are_exactly_endpoint_and_reason() -> None:
    observability = SentinelObservability(registry=CollectorRegistry())
    assert set(observability._decisions._labelnames) == _LABEL_KEYS
    assert set(observability._latency._labelnames) == _LABEL_KEYS


@pytest.mark.security
async def test_obs_13_live_metrics_cardinality_bomb() -> None:
    registry = CollectorRegistry()
    loader = FakeLoader()
    guard = SentinelGuard(
        _config(),
        SentinelRedis("redis://localhost:6379/0"),
        cast(ScriptLoader, loader),
        observability=SentinelObservability(registry=registry),
    )
    await guard.load_scripts()
    app = FastAPI()

    @app.post("/{path:path}")
    async def catch_all(
        request: Request, _: None = Depends(guard.guard_for("resumint.tailor"))
    ) -> dict[str, bool]:
        return {"allowed": request.state.decision.allowed}

    client = TestClient(app)
    token = _token()
    paths = [
        "/a/1",
        "/b/deep",
        "/tenants/tenant-a/documents/123",
        "/a/resumint.tailor/",
        "/x?endpoint_id=pdftalk.ingest",
        "/y?endpoint=evil",
        "/z?decision_reason=allowed",
    ]
    for path in paths:
        response = client.post(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
    samples = _decision_samples(registry)
    assert samples
    assert {s[1]["endpoint_id"] for s in samples} == {"resumint.tailor"}
    assert all(s[1]["decision_reason"] == "allowed" for s in samples)


async def test_obs_14_anonymous_denied_logs_identity_mode_and_hash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sentinel.anonymous import anonymous_ip_identity
    from sentinel.limiter import hash_identity as hash_identity_alias

    registry = CollectorRegistry()
    observability = SentinelObservability(registry=registry)
    app, guard, loader = _make_anonymous_app(observability=observability)
    await guard.load_scripts()
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    response = TestClient(app).post("/login")
    assert response.status_code == 429
    records = [r for r in caplog.records if r.getMessage().startswith("rate limit decision")]
    assert len(records) == 1
    record = records[0]
    assert record.identity_mode == "anonymous"
    assert record.identity_hash == hash_identity_alias(anonymous_ip_identity("unknown"))
    assert record.endpoint_id == "auth.login"
    assert record.decision_reason == "rate_limited"
    assert "unknown" not in record.getMessage()
    samples = _decision_samples(registry)
    assert samples
    assert all(set(s[1]) - {"le"} == _LABEL_KEYS for s in samples)


def _make_anonymous_app(
    *,
    observability: SentinelObservability | None = None,
) -> tuple[FastAPI, SentinelGuard, FakeLoader]:
    from pydantic import SecretStr
    from sentinel.config import AppConfig, SentinelConfig
    from sentinel.models import AlgorithmType, FailMode, IdentityMode, Policy

    loader = FakeLoader()
    config = SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr("test-secret-0123456789abcdef0123456789abcdef"),
            jwt_algorithm_allowlist=frozenset({"HS256"}),
            anonymous_cookie_secret=SecretStr("anon-test-secret-0123456789abcdef0123456789abcdef"),
            anonymous_cookie_secure=False,
        ),
        policies={
            "auth.login": Policy(
                endpoint_id="auth.login",
                identity=IdentityMode.ANONYMOUS,
                algorithm=AlgorithmType.TOKEN_BUCKET,
                fail_mode=FailMode.FAIL_OPEN,
                fallback_rate_per_process_micro=1_000_000,
                policy_version=1,
                capacity_micro=2_000_000,
                refill_rate_micro_per_sec=500_000,
            )
        },
    )
    guard = SentinelGuard(
        config,
        SentinelRedis(config.app.redis_url),
        cast(ScriptLoader, loader),
        observability=observability,
    )
    app = FastAPI()

    @app.post("/login")
    async def route(
        request: Request, _: None = Depends(guard.anonymous_guard_for("auth.login"))
    ) -> dict[str, bool]:
        return {"allowed": request.state.decision.allowed}

    return app, guard, loader
