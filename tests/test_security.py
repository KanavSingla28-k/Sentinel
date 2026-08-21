"""Security regression suite for spec 07 hardening decisions (Phases 11, 19).

Every test is marked ``security`` and named ``test_sec_<n>_...`` to mirror the
Security Findings table in docs/sentinel-project-record.md section 07.
Phase 19 findings are named ``test_sec_anon_<n>_...`` and map to the anonymous
rate-limiting plan's SEC-ANON list.
"""

import inspect

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sentinel.http import SentinelGuard
from sentinel.limiter import RateLimiter, build_anonymous_key, build_bucket_key
from sentinel.lua import SCRIPT_NAMES, script_source
from sentinel.models import AlgorithmType, DecisionReason, FailMode, IdentityMode, Policy
from sentinel.redis import SentinelRedis

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


@pytest.mark.security
def test_sec_anon_01_guard_source_never_reads_forwarding_headers() -> None:
    source = inspect.getsource(SentinelGuard.anonymous_guard_for)
    for fragment in ("forwarded", "real-ip", "cf-connecting", "x-forwarded"):
        assert fragment.lower() not in source.lower(), f"anonymous guard reads {fragment!r}"


@pytest.mark.security
def test_sec_anon_01_guard_never_keys_from_request_headers() -> None:
    source = inspect.getsource(SentinelGuard.anonymous_guard_for)
    assert "request.headers" not in source
    assert "request.cookies" in source
    assert "request.client" in source


@pytest.mark.security
def test_sec_anon_02_anonymous_identity_flows_through_parse_cookie() -> None:
    from sentinel.anonymous import parse_cookie

    # The guard must verify the server signature before the client id can
    # reach any key; parse_cookie is the only cookie-reading primitive.
    source = inspect.getsource(SentinelGuard.anonymous_guard_for)
    assert "parse_cookie" in source
    assert parse_cookie.__name__ == "parse_cookie"


@pytest.mark.security
def test_sec_anon_02_config_requires_secret_for_anonymous_policy() -> None:
    with pytest.raises(ValidationError, match="anonymous_cookie_secret"):
        _anonymous_policy_without_secret()


@pytest.mark.security
def test_sec_anon_03_anonymous_key_source_hashes_identity() -> None:
    source = inspect.getsource(build_anonymous_key)
    assert "hash_identity" in source
    assert "request" not in source


@pytest.mark.security
def test_sec_anon_06_cookie_minting_is_bounded_by_ip_key() -> None:
    source = inspect.getsource(SentinelGuard.anonymous_guard_for)
    # A cookie-less request must still evaluate the IP bucket: the minted
    # cookie never creates an unbounded evaluation.
    assert "anonymous_ip_identity" in source
    assert "mint_cookie" in source


@pytest.mark.security
@pytest.mark.integration
async def test_sec_anon_03_live_no_raw_identity_in_keys_or_state(
    redis_client: SentinelRedis,
) -> None:
    import uuid

    from sentinel.anonymous import anonymous_cookie_identity, anonymous_ip_identity
    from sentinel.circuit_breaker import CircuitBreaker
    from sentinel.emergency import TokenBucketEmergencyLimiter
    from sentinel.lua import load_scripts
    from sentinel.redis import ScriptLoader

    from test_http_anonymous import _anonymous_token_bucket_policy

    raw_ip = "203.0.113.9"
    raw_cookie = "deadbeef" * 4
    endpoint_id = f"auth.sec-{uuid.uuid4().hex[:8]}"
    identity_cookie = anonymous_cookie_identity(raw_cookie)
    identity_ip = anonymous_ip_identity(raw_ip)
    assert build_anonymous_key(identity_ip, endpoint_id, 1).startswith("sentinel:v2:")
    loader = ScriptLoader(redis_client.client)
    await load_scripts(loader)
    limiter = RateLimiter(
        loader,
        breaker=CircuitBreaker(),
        emergency=TokenBucketEmergencyLimiter(),
    )
    policy = _anonymous_token_bucket_policy(endpoint_id=endpoint_id)
    key_cookie = build_anonymous_key(identity_cookie, endpoint_id, 1)
    key_ip = build_anonymous_key(identity_ip, endpoint_id, 1)
    decision = await limiter.evaluate_anonymous(policy, (key_cookie, key_ip))
    assert decision.allowed is True
    states = (
        await redis_client.client.get(key_cookie),
        await redis_client.client.get(key_ip),
    )
    for key, state in ((key_cookie, states[0]), (key_ip, states[1])):
        assert state is not None
        for raw in (raw_ip, raw_cookie, "anon:cookie", "anon:ip"):
            assert raw not in key, f"raw identity leaked into key {key!r}"
            assert raw not in state, f"raw identity leaked into state {state!r}"


def _anonymous_policy_without_secret() -> None:
    from pydantic import SecretStr
    from sentinel.config import AppConfig, SentinelConfig

    SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr("test-secret-0123456789abcdef0123456789abcdef"),
            jwt_algorithm_allowlist=frozenset({"HS256"}),
        ),
        policies={
            "auth.login": Policy(
                endpoint_id="auth.login",
                identity=IdentityMode.ANONYMOUS,
                algorithm=AlgorithmType.TOKEN_BUCKET,
                fail_mode=FailMode.FAIL_OPEN,
                fallback_rate_per_process_micro=2_000,
                policy_version=1,
                capacity_micro=1_000_000,
                refill_rate_micro_per_sec=0,
            )
        },
    )
