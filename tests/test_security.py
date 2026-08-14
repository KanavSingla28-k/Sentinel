"""Security regression suite for spec 07 hardening decisions (Phase 11).

Every test is marked ``security`` and named ``test_sec_<n>_...`` to mirror the
Security Findings table in docs/sentinel-project-record.md section 07.
"""

import inspect

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sentinel.http import SentinelGuard
from sentinel.limiter import RateLimiter, build_bucket_key
from sentinel.lua import SCRIPT_NAMES, script_source
from sentinel.models import DecisionReason, Policy

from test_http import _make_app, _token, _token_bucket_policy, _tripped_breaker


def _redis_command_called(source: str, command: str) -> bool:
    return f'redis.call("{command}"' in source or f"redis.call('{command}'" in source


@pytest.mark.security
def test_sec_01_policy_rejects_cost_field() -> None:
    assert "cost" not in Policy.model_fields
    with pytest.raises(ValidationError):
        _token_bucket_policy(cost=5)


@pytest.mark.security
def test_sec_01_rate_limiter_source_has_no_cost() -> None:
    assert "cost" not in inspect.getsource(RateLimiter.evaluate)


@pytest.mark.security
@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_sec_01_lua_sources_have_no_cost(name: str) -> None:
    assert "cost" not in script_source(name)


@pytest.mark.security
@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_sec_02_lua_scripts_are_ttl_only(name: str) -> None:
    source = script_source(name)
    assert _redis_command_called(source, "EXPIRE") or _redis_command_called(source, "PEXPIRE")
    for command in ("DEL", "UNLINK", "KEYS", "SCAN", "FLUSHALL", "FLUSHDB"):
        assert not _redis_command_called(source, command), f"{name} calls {command}"


@pytest.mark.security
def test_sec_03_spoof_header_without_token_returns_401() -> None:
    app, _, loader = _make_app("resumint.tailor")
    response = TestClient(app).post("/a", headers={"X-Tenant-ID": "attacker"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert loader.calls == []


@pytest.mark.security
async def test_sec_03_identical_spoof_header_never_overrides_sub() -> None:
    app, guard, loader = _make_app("resumint.tailor")
    await guard.load_scripts()
    client = TestClient(app)
    for sub in ("tenant-a", "tenant-b"):
        headers = {"Authorization": f"Bearer {_token(sub)}", "X-Tenant-ID": "attacker"}
        response = client.post("/a", headers=headers)
        assert response.status_code == 200
    keys = [call[1][0] for call in loader.calls]
    assert keys == [
        build_bucket_key("tenant-a", "resumint.tailor", 1),
        build_bucket_key("tenant-b", "resumint.tailor", 1),
    ]
    assert keys[0] != keys[1]
    assert "attacker" not in keys[0]
    assert "attacker" not in keys[1]


@pytest.mark.security
async def test_sec_05_breaker_isolation_across_guards() -> None:
    app_open, guard_open, loader_open = _make_app("pdftalk.ingest", breaker=_tripped_breaker())
    app_fresh, guard_fresh, loader_fresh = _make_app("pdftalk.ingest")
    await guard_open.load_scripts()
    await guard_fresh.load_scripts()
    token = _token()
    response_open = TestClient(app_open).post("/a", headers={"Authorization": f"Bearer {token}"})
    response_fresh = TestClient(app_fresh).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response_open.status_code == 503
    assert [d.reason for d in app_open.state.decision_log] == [DecisionReason.CIRCUIT_OPEN]
    assert loader_open.calls == []
    assert response_fresh.status_code == 200
    assert len(loader_fresh.calls) == 1


@pytest.mark.security
def test_sec_08_guard_source_never_derives_endpoint_from_url() -> None:
    source = inspect.getsource(SentinelGuard.guard_for)
    for fragment in ("request.url", ".url.path", "path_params", "request.path", "scope[", "route"):
        assert fragment not in source, f"guard_for derives identity from {fragment!r}"


@pytest.mark.security
def test_sec_08_bucket_key_source_never_references_request() -> None:
    assert "request" not in inspect.getsource(build_bucket_key)


@pytest.mark.security
async def test_sec_08_url_fragments_cannot_change_endpoint_id() -> None:
    app, guard, loader = _make_app("resumint.tailor")
    await guard.load_scripts()
    client = TestClient(app)
    token = _token()
    urls = (
        "/a",
        "/b/deep",
        "/a?endpoint_id=pdftalk.ingest",
        "/b/deep?endpoint=notconfigured&endpoint_id=pdftalk.ingest",
    )
    for url in urls:
        response = client.post(url, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
    expected = build_bucket_key("tenant-a", "resumint.tailor", 1)
    keys = [call[1][0] for call in loader.calls]
    assert keys == [expected] * len(urls)
    assert "pdftalk.ingest" not in expected
    assert "notconfigured" not in expected
