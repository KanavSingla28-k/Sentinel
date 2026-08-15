"""FastAPI guard unit tests with a fake script loader (Phases 7 and 8)."""

import time
from typing import cast

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sentinel.circuit_breaker import FAILURE_THRESHOLD, CircuitBreaker
from sentinel.config import AppConfig, SentinelConfig
from sentinel.emergency import EmergencyLimiter
from sentinel.errors import ScriptMissingError
from sentinel.http import (
    _HTTP_429_REASONS,
    _HTTP_503_REASONS,
    SentinelGuard,
    _denied_status,
)
from sentinel.limiter import build_bucket_key
from sentinel.lua import SLIDING_WINDOW_SCRIPT, TOKEN_BUCKET_SCRIPT
from sentinel.models import AlgorithmType, Decision, DecisionReason, FailMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
ALLOWLIST = frozenset({"HS256", "HS384", "HS512"})
TB_RATE = 500_000


def _token(sub: str = "tenant-a", *, secret: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode({"sub": sub, "exp": int(time.time()) + 3_600}, secret, algorithm=algorithm)


def _token_bucket_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "resumint.tailor",
        "algorithm": AlgorithmType.TOKEN_BUCKET,
        "fail_mode": FailMode.FAIL_OPEN,
        "fallback_rate_per_process_micro": 1_000_000,
        "policy_version": 1,
        "capacity_micro": 2_000_000,
        "refill_rate_micro_per_sec": TB_RATE,
    }
    base.update(overrides)
    return Policy(**base)


def _sliding_window_policy(**overrides: object) -> Policy:
    base: dict[str, object] = {
        "endpoint_id": "pdftalk.ingest",
        "algorithm": AlgorithmType.SLIDING_WINDOW,
        "fail_mode": FailMode.FAIL_CLOSED,
        "fallback_rate_per_process_micro": 5_000,
        "policy_version": 1,
        "limit": 5,
        "window_size_micro": 1_000_000,
    }
    base.update(overrides)
    return Policy(**base)


def _config(allowlist: frozenset[str] = ALLOWLIST, tb_rate: int = TB_RATE) -> SentinelConfig:
    return SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr(SECRET),
            jwt_algorithm_allowlist=allowlist,
        ),
        policies={
            "resumint.tailor": _token_bucket_policy(refill_rate_micro_per_sec=tb_rate),
            "pdftalk.ingest": _sliding_window_policy(),
        },
    )


class FakeLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], list[str]]] = []
        self._results: dict[str, list[int]] = {}
        self._exceptions: dict[str, Exception] = {}

    def set_result(self, name: str, result: list[int]) -> None:
        self._results[name] = result

    def set_exception(self, name: str, exc: Exception) -> None:
        self._exceptions[name] = exc

    async def load(self, name: str, source: str) -> str:
        return f"sha-{name}"

    async def execute(self, name: str, keys: list[str], args: list[str]) -> int | list[int] | None:
        self.calls.append((name, keys, args))
        exc = self._exceptions.get(name)
        if exc is not None:
            raise exc
        return self._results.get(
            name, [1, 0, 0, 0] if name == TOKEN_BUCKET_SCRIPT else [1, 0, 0, 0, 0]
        )


def _make_app(
    endpoint_id: str,
    allowlist: frozenset[str] = ALLOWLIST,
    tb_rate: int = TB_RATE,
    *,
    breaker: CircuitBreaker | None = None,
    emergency: EmergencyLimiter | None = None,
) -> tuple[FastAPI, SentinelGuard, FakeLoader]:
    loader = FakeLoader()
    config = _config(allowlist, tb_rate)
    guard = SentinelGuard(
        config,
        SentinelRedis(config.app.redis_url),
        cast(ScriptLoader, loader),
        breaker=breaker,
        emergency=emergency,
    )
    app = FastAPI()

    @app.post("/a")
    @app.post("/b/deep")
    async def route(
        request: Request, _: None = Depends(guard.guard_for(endpoint_id))
    ) -> dict[str, object]:
        app.state.decision_log.append(request.state.decision)
        return {"allowed": request.state.decision.allowed, "sub": "x"}

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
async def tailor_env() -> tuple[FastAPI, SentinelGuard, FakeLoader]:
    app, guard, loader = _make_app("resumint.tailor")
    await guard.load_scripts()
    return app, guard, loader


@pytest.fixture
async def ingest_env() -> tuple[FastAPI, SentinelGuard, FakeLoader]:
    app, guard, loader = _make_app("pdftalk.ingest")
    await guard.load_scripts()
    return app, guard, loader


def test_missing_authorization_returns_401(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    response = TestClient(app).post("/a")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert loader.calls == []


def test_malformed_bearer_header_returns_401(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    client = TestClient(app)
    for header in ("Basic abc", "Bearer", "Digest x=1", "bearer"):
        response = client.post("/a", headers={"Authorization": header})
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert loader.calls == []


def test_bad_signature_returns_401(tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader]) -> None:
    app, _, loader = tailor_env
    token = _token(secret="wrong-secret-0123456789abcdef0123456789abcdef")
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert loader.calls == []


def test_expired_token_returns_401(tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader]) -> None:
    app, _, loader = tailor_env
    token = jwt.encode({"sub": "tenant-a", "exp": int(time.time()) - 60}, SECRET, algorithm="HS256")
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert loader.calls == []


async def test_unsupported_algorithm_returns_401(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, guard, loader = _make_app("resumint.tailor", allowlist=frozenset({"HS256"}))
    await guard.load_scripts()
    token = _token(algorithm="HS512")
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert loader.calls == []


def test_missing_and_empty_sub_return_401(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    client = TestClient(app)
    for payload in (
        {"exp": int(time.time()) + 3_600},
        {"sub": "", "exp": int(time.time()) + 3_600},
    ):
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        response = client.post("/a", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
    assert loader.calls == []


def test_malformed_token_returns_401(tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader]) -> None:
    app, _, loader = tailor_env
    response = TestClient(app).post("/a", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert loader.calls == []


@pytest.mark.security
def test_x_tenant_id_header_is_ignored(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    token = _token(sub="spoof-target")
    client = TestClient(app)
    first = client.post(
        "/a", headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": "attacker"}
    )
    second = client.post("/a", headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": "other"})
    assert first.status_code == 200
    assert second.status_code == 200
    keys = [call[1][0] for call in loader.calls]
    assert keys[0] == keys[1] == build_bucket_key("spoof-target", "resumint.tailor", 1)
    assert "attacker" not in keys[0]
    assert "other" not in keys[0]


def test_unknown_endpoint_returns_404() -> None:
    app, _, loader = _make_app("not.configured")
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
    assert loader.calls == []


def test_allowed_continues_to_handler(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, _ = tailor_env
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"allowed": True, "sub": "x"}


def test_rate_limited_returns_429_exact_body(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 429
    assert response.json() == {"detail": "rate limit exceeded"}


def test_token_bucket_retry_after_rounding(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    client = TestClient(app)
    token = _token()
    cases = [([0, 950_000, 0, 0], "1"), ([0, 400_000, 0, 0], "2"), ([0, 0, 0, 0], "2")]
    for result, expected in cases:
        loader.set_result(TOKEN_BUCKET_SCRIPT, result)
        response = client.post("/a", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == expected


async def test_token_bucket_zero_rate_has_no_retry_after() -> None:
    app, guard, loader = _make_app("resumint.tailor", tb_rate=0)
    await guard.load_scripts()
    loader.set_result(TOKEN_BUCKET_SCRIPT, [0, 0, 0, 0])
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 429
    assert "Retry-After" not in response.headers


def test_sliding_window_429_has_no_retry_after(
    ingest_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = ingest_env
    loader.set_result(SLIDING_WINDOW_SCRIPT, [0, 5, 5, 0, 0])
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 429
    assert response.json() == {"detail": "rate limit exceeded"}
    assert "Retry-After" not in response.headers


def test_sliding_window_allowed_continues(
    ingest_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, _ = ingest_env
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["allowed"] is True


def test_decision_attached_to_request_state(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    loader.set_result(TOKEN_BUCKET_SCRIPT, [1, 500_000, 0, 0])
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["allowed"] is True


def test_evaluation_before_script_loading_raises() -> None:
    app, _, _ = _make_app("resumint.tailor")
    token = _token()
    with pytest.raises(RuntimeError, match="load_scripts"):
        TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.security
def test_endpoint_id_comes_from_dependency_not_url(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    client = TestClient(app)
    token = _token()
    assert client.post("/a", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/b/deep", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    keys = [call[1][0] for call in loader.calls]
    assert keys[0] == keys[1] == build_bucket_key("tenant-a", "resumint.tailor", 1)


def test_fail_closed_timeout_returns_503(
    ingest_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = ingest_env
    loader.set_exception(SLIDING_WINDOW_SCRIPT, RedisTimeoutError("timeout"))
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "rate limiter unavailable"}
    assert "Retry-After" not in response.headers
    assert "WWW-Authenticate" not in response.headers


def test_fail_closed_connection_error_returns_503(
    ingest_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = ingest_env
    loader.set_exception(SLIDING_WINDOW_SCRIPT, RedisConnectionError("connection refused"))
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "rate limiter unavailable"}


def test_fail_closed_noscript_exhaustion_returns_503(
    ingest_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = ingest_env
    loader.set_exception(SLIDING_WINDOW_SCRIPT, ScriptMissingError("missing again"))
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "rate limiter unavailable"}


def test_fail_closed_503_records_decision_reason(
    ingest_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = ingest_env
    loader.set_exception(SLIDING_WINDOW_SCRIPT, RedisTimeoutError("timeout"))
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert [d.reason for d in app.state.decision_log] == [DecisionReason.FAIL_CLOSED]


def test_fail_open_timeout_allowed_through_emergency(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    loader.set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError("timeout"))
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"allowed": True, "sub": "x"}
    assert [d.reason for d in app.state.decision_log] == [DecisionReason.REDIS_TIMEOUT]


def test_fail_open_emergency_exhaustion_returns_429(
    tailor_env: tuple[FastAPI, SentinelGuard, FakeLoader],
) -> None:
    app, _, loader = tailor_env
    loader.set_exception(TOKEN_BUCKET_SCRIPT, RedisTimeoutError("timeout"))
    client = TestClient(app)
    token = _token()
    first = client.post("/a", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200
    second = client.post("/a", headers={"Authorization": f"Bearer {token}"})
    assert second.status_code == 429
    assert second.json() == {"detail": "rate limit exceeded"}
    assert second.headers["Retry-After"] == "1"
    assert [d.reason for d in app.state.decision_log] == [
        DecisionReason.REDIS_TIMEOUT,
        DecisionReason.EMERGENCY_LOCAL_LIMIT,
    ]


def test_denied_status_mapping_is_exhaustive_and_disjoint() -> None:
    assert {
        DecisionReason.RATE_LIMITED,
        DecisionReason.EMERGENCY_LOCAL_LIMIT,
    } == _HTTP_429_REASONS
    assert {
        DecisionReason.FAIL_CLOSED,
        DecisionReason.CIRCUIT_OPEN,
        DecisionReason.REDIS_TIMEOUT,
        DecisionReason.REDIS_CONNECTION_ERROR,
        DecisionReason.REDIS_NOSCRIPT_RETRY,
    } == _HTTP_503_REASONS
    assert _HTTP_429_REASONS.isdisjoint(_HTTP_503_REASONS)
    assert set(DecisionReason) - {DecisionReason.ALLOWED} == _HTTP_429_REASONS | _HTTP_503_REASONS
    for reason in _HTTP_429_REASONS:
        assert _denied_status(reason) == 429
    for reason in _HTTP_503_REASONS:
        assert _denied_status(reason) == 503


def test_denied_status_rejects_unmapped_reason() -> None:
    with pytest.raises(RuntimeError, match="no HTTP status"):
        _denied_status(DecisionReason.ALLOWED)


def _tripped_breaker() -> CircuitBreaker:
    breaker = CircuitBreaker()
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure()
    return breaker


@pytest.mark.security
async def test_circuit_open_fail_closed_returns_503_without_redis() -> None:
    app, guard, loader = _make_app("pdftalk.ingest", breaker=_tripped_breaker())
    await guard.load_scripts()
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "rate limiter unavailable"}
    assert [d.reason for d in app.state.decision_log] == [DecisionReason.CIRCUIT_OPEN]
    assert loader.calls == []


@pytest.mark.security
async def test_circuit_open_fail_open_routes_to_emergency() -> None:
    app, guard, loader = _make_app("resumint.tailor", breaker=_tripped_breaker())
    await guard.load_scripts()
    token = _token()
    response = TestClient(app).post("/a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"allowed": True, "sub": "x"}
    assert [d.reason for d in app.state.decision_log] == [DecisionReason.CIRCUIT_OPEN]
    assert loader.calls == []
