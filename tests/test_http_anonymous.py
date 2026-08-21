"""Anonymous FastAPI guard tests with a fake script loader (Phase 19)."""

import time
from typing import cast

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import TimeoutError as RedisTimeoutError
from sentinel.anonymous import (
    anonymous_cookie_identity,
    anonymous_ip_identity,
    hash_identity,
    mint_cookie,
)
from sentinel.circuit_breaker import CircuitBreaker
from sentinel.config import AppConfig, SentinelConfig
from sentinel.emergency import EmergencyLimiter
from sentinel.http import SentinelGuard
from sentinel.limiter import build_anonymous_key
from sentinel.lua import TOKEN_BUCKET_SCRIPT
from sentinel.models import (
    AlgorithmType,
    Decision,
    DecisionReason,
    FailMode,
    IdentityMode,
    Policy,
)
from sentinel.observability import SentinelObservability
from sentinel.redis import ScriptLoader, SentinelRedis

from test_http import FakeLoader

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
ANON_SECRET = "anon-test-secret-0123456789abcdef0123456789abcdef"
COOKIE_NAME = "sentinel_anon_id"
COOKIE_TTL = 2_592_000
TB_RATE = 500_000

# TestClient peers are the non-IP host "testclient", which collapses to the
# shared "unknown" bucket (fail-safe over-blocking, SEC-ANON-04).
TEST_IP = "unknown"


def _anonymous_token_bucket_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "auth.login",
        "identity": IdentityMode.ANONYMOUS,
        "algorithm": AlgorithmType.TOKEN_BUCKET,
        "fail_mode": FailMode.FAIL_OPEN,
        "fallback_rate_per_process_micro": 1_000_000,
        "policy_version": 1,
        "capacity_micro": 2_000_000,
        "refill_rate_micro_per_sec": TB_RATE,
    }
    base.update(overrides)
    return Policy(**base)


def _anonymous_config(policy: Policy | None = None) -> SentinelConfig:
    return SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr(SECRET),
            jwt_algorithm_allowlist=frozenset({"HS256", "HS384", "HS512"}),
            anonymous_cookie_secret=SecretStr(ANON_SECRET),
            anonymous_cookie_name=COOKIE_NAME,
            anonymous_cookie_ttl_seconds=COOKIE_TTL,
            anonymous_cookie_secure=False,
        ),
        policies={"auth.login": policy or _anonymous_token_bucket_policy()},
    )


def _ip_key(endpoint_id: str = "auth.login", policy_version: int = 1) -> str:
    return build_anonymous_key(anonymous_ip_identity(TEST_IP), endpoint_id, policy_version)


def _cookie_key(client_id: str, endpoint_id: str = "auth.login", policy_version: int = 1) -> str:
    return build_anonymous_key(anonymous_cookie_identity(client_id), endpoint_id, policy_version)


def _valid_cookie() -> tuple[str, str]:
    return mint_cookie(ANON_SECRET, COOKIE_TTL, now=int(time.time()))


class KeyedFakeLoader(FakeLoader):
    """FakeLoader that routes results by bucket key."""

    def __init__(self) -> None:
        super().__init__()
        self._key_results: dict[str, list[int]] = {}

    def set_key_result(self, key: str, result: list[int]) -> None:
        self._key_results[key] = result

    async def execute(self, name: str, keys: list[str], args: list[str]) -> int | list[int] | None:
        self.calls.append((name, keys, args))
        exc = self._exceptions.get(name)
        if exc is not None:
            raise exc
        if keys and keys[0] in self._key_results:
            return self._key_results[keys[0]]
        return self._results.get(
            name, [1, 0, 0, 0] if name == TOKEN_BUCKET_SCRIPT else [1, 0, 0, 0, 0]
        )


def _make_app(
    endpoint_id: str,
    *,
    policy: Policy | None = None,
    breaker: CircuitBreaker | None = None,
    emergency: EmergencyLimiter | None = None,
    observability: SentinelObservability | None = None,
) -> tuple[FastAPI, SentinelGuard, KeyedFakeLoader]:
    loader = KeyedFakeLoader()
    config = _anonymous_config(policy)
    guard = SentinelGuard(
        config,
        SentinelRedis(config.app.redis_url),
        cast(ScriptLoader, loader),
        breaker=breaker,
        emergency=emergency,
        observability=observability,
    )
    app = FastAPI()

    @app.post("/login")
    async def route(
        request: Request,
        response: Response,
        _: None = Depends(guard.anonymous_guard_for(endpoint_id)),
    ) -> dict[str, object]:
        app.state.decision_log.append(request.state.decision)
        return {
            "allowed": request.state.decision.allowed,
            "client_id": getattr(request.state, "anonymous_client_id", None),
        }

    @app.exception_handler(HTTPException)
    async def _record_decision(request: Request, exc: HTTPException) -> JSONResponse:
        decision = getattr(request.state, "decision", None)
        if decision is not None:
            app.state.decision_log.append(decision)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    app.state.decision_log: list[Decision] = []
    return app, guard, loader


@pytest.fixture
async def anon_env() -> tuple[FastAPI, SentinelGuard, KeyedFakeLoader]:
    app, guard, loader = _make_app("auth.login")
    await guard.load_scripts()
    return app, guard, loader


def test_first_request_mints_cookie_and_keys_ip_only(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    response = TestClient(app).post("/login")
    assert response.status_code == 200
    assert response.json()["allowed"] is True
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Secure" not in set_cookie
    assert len(loader.calls) == 1
    assert loader.calls[0][1] == [_ip_key()]
    client_id = response.json()["client_id"]
    assert len(client_id) == 32


def test_second_request_with_cookie_keys_cookie_and_ip(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    client = TestClient(app)
    first = client.post("/login")
    cookie = first.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client_id = first.json()["client_id"]
    second = client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert second.status_code == 200
    assert second.json()["client_id"] == client_id
    assert "set-cookie" not in second.headers
    assert len(loader.calls) == 3
    assert loader.calls[0][1] == [_ip_key()]
    assert loader.calls[1][1] == [_cookie_key(client_id)]
    assert loader.calls[2][1] == [_ip_key()]


def test_cookie_bucket_denies_even_when_ip_allows(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    client = TestClient(app)
    first = client.post("/login")
    cookie = first.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client_id = first.json()["client_id"]
    loader.set_key_result(_cookie_key(client_id), [0, 0, 0, 0])
    denied = client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert denied.status_code == 429
    assert denied.json() == {"detail": "rate limit exceeded"}
    assert [d.reason for d in app.state.decision_log] == [
        DecisionReason.ALLOWED,
        DecisionReason.RATE_LIMITED,
    ]
    assert loader.calls[1][1] == [_cookie_key(client_id)]


def test_ip_bucket_denies_even_when_cookie_allows(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    client = TestClient(app)
    first = client.post("/login")
    cookie = first.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    loader.set_key_result(_ip_key(), [0, 0, 0, 0])
    denied = client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert denied.status_code == 429
    assert denied.json() == {"detail": "rate limit exceeded"}
    assert [d.reason for d in app.state.decision_log] == [
        DecisionReason.ALLOWED,
        DecisionReason.RATE_LIMITED,
    ]


def test_cleared_cookie_falls_back_to_ip_bucket(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    client = TestClient(app)
    assert client.post("/login").status_code == 200
    client.cookies.clear()
    assert client.post("/login").status_code == 200
    keys = [call[1][0] for call in loader.calls]
    assert keys[0] == keys[1] == _ip_key()
    assert "anon:cookie" not in keys[0]


def test_tampered_cookie_is_treated_as_missing(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    _, good_cookie = _valid_cookie()
    client_id, _, mac = good_cookie.split(".")
    forged = f"{client_id}.9999999999.{mac}"
    response = TestClient(app).post("/login", headers={"Cookie": f"{COOKIE_NAME}={forged}"})
    assert response.status_code == 200
    assert len(loader.calls) == 1
    assert loader.calls[0][1] == [_ip_key()]
    assert response.headers["set-cookie"].startswith(f"{COOKIE_NAME}=")


@pytest.mark.security
def test_x_forwarded_for_header_is_ignored(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    response = TestClient(app).post(
        "/login", headers={"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "198.51.100.7"}
    )
    assert response.status_code == 200
    assert loader.calls[0][1] == [_ip_key()]
    assert "203.0.113.9" not in loader.calls[0][1][0]
    assert "198.51.100.7" not in loader.calls[0][1][0]


@pytest.mark.security
def test_x_tenant_id_header_is_ignored_on_anonymous_route(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    response = TestClient(app).post("/login", headers={"X-Tenant-ID": "attacker"})
    assert response.status_code == 200
    assert loader.calls[0][1] == [_ip_key()]


@pytest.mark.security
def test_anonymous_deny_response_is_indistinguishable_from_tenant_deny(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    response = TestClient(app).post("/login")
    assert response.status_code == 429
    assert response.json() == {"detail": "rate limit exceeded"}
    assert "WWW-Authenticate" not in response.headers


async def test_missing_cookie_secret_at_runtime_raises() -> None:
    config = SentinelConfig.model_construct(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr(SECRET),
            jwt_algorithm_allowlist=frozenset({"HS256"}),
        ),
        policies={"auth.login": _anonymous_token_bucket_policy()},
    )
    guard = SentinelGuard(
        config,
        SentinelRedis(config.app.redis_url),
        cast(ScriptLoader, KeyedFakeLoader()),
    )
    await guard.load_scripts()
    dependency = guard.anonymous_guard_for("auth.login")
    fake_request = type("R", (), {"cookies": {}, "client": None, "state": object()})()
    fake_response = type("S", (), {"set_cookie": lambda *args, **kwargs: None})()
    with pytest.raises(RuntimeError, match="anonymous_cookie_secret"):
        await dependency(fake_request, fake_response)
    app, _, loader = _make_app("not.configured")
    response = TestClient(app).post("/login")
    assert response.status_code == 404
    assert loader.calls == []


def test_evaluation_before_script_loading_raises() -> None:
    app, _, _ = _make_app("auth.login")
    with pytest.raises(RuntimeError, match="load_scripts"):
        TestClient(app).post("/login")


def test_anonymous_guard_on_tenant_policy_raises() -> None:
    from test_http import _make_app as _make_tenant_app

    _, guard, _ = _make_tenant_app("resumint.tailor")
    with pytest.raises(RuntimeError, match="use the matching guard factory"):
        guard.anonymous_guard_for("resumint.tailor")


def test_tenant_guard_on_anonymous_policy_raises() -> None:
    _, guard, _ = _make_app("auth.login")
    with pytest.raises(RuntimeError, match="use the matching guard factory"):
        guard.guard_for("auth.login")


async def test_fail_closed_timeout_returns_503() -> None:
    app, guard, loader = _make_app(
        "auth.login",
        policy=_anonymous_token_bucket_policy(fail_mode=FailMode.FAIL_CLOSED),
    )
    await guard.load_scripts()
    loader.set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError("timeout"))
    response = TestClient(app).post("/login")
    assert response.status_code == 503
    assert response.json() == {"detail": "rate limiter unavailable"}
    assert "set-cookie" not in response.headers
    assert [d.reason for d in app.state.decision_log] == [DecisionReason.FAIL_CLOSED]


def test_denied_request_does_not_receive_cookie(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
) -> None:
    app, _, loader = anon_env
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    response = TestClient(app).post("/login")
    assert response.status_code == 429
    assert "set-cookie" not in response.headers


def test_cookie_is_secure_by_default() -> None:
    config = SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr(SECRET),
            jwt_algorithm_allowlist=frozenset({"HS256"}),
            anonymous_cookie_secret=SecretStr(ANON_SECRET),
        ),
        policies={"auth.login": _anonymous_token_bucket_policy()},
    )
    assert config.app.anonymous_cookie_secure is True
    assert config.app.anonymous_cookie_name == COOKIE_NAME
    assert config.app.anonymous_cookie_ttl_seconds == COOKIE_TTL


def test_deny_log_identity_hash_is_primary_identity(
    anon_env: tuple[FastAPI, SentinelGuard, KeyedFakeLoader],
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, loader = anon_env
    client = TestClient(app)
    first = client.post("/login")
    cookie = first.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client_id = first.json()["client_id"]
    loader.set_key_result(_cookie_key(client_id), [0, 0, 0, 0])
    denied = client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert denied.status_code == 429
    records = [
        r
        for r in caplog.records
        if r.name == "sentinel" and r.getMessage().startswith("rate limit decision")
    ]
    assert len(records) == 1
    assert records[0].identity_mode == "anonymous"
    assert records[0].identity_hash == hash_identity(anonymous_cookie_identity(client_id))
    assert records[0].endpoint_id == "auth.login"
    assert client_id not in records[0].getMessage()
    assert records[0].getMessage() == "rate limit decision denied"
