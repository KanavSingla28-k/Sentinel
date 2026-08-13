"""End-to-end FastAPI guard tests against real Redis (Phase 7)."""

import time
import uuid
from collections.abc import AsyncGenerator
from typing import cast

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, Request
from pydantic import SecretStr
from sentinel.config import AppConfig, SentinelConfig
from sentinel.http import SentinelGuard
from sentinel.limiter import build_bucket_key
from sentinel.lua import load_scripts
from sentinel.models import AlgorithmType, FailMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

pytestmark = pytest.mark.integration

SECRET = "integration-secret-0123456789abcdef0123456789abcdef"
ALLOWLIST = frozenset({"HS256", "HS384", "HS512"})


def _token(sub: str, *, secret: str = SECRET) -> str:
    return jwt.encode({"sub": sub, "exp": int(time.time()) + 3_600}, secret, algorithm="HS256")


def _unique(prefix: str) -> str:
    return f"test-phase7-{prefix}-{uuid.uuid4().hex}"


async def _now_micro(client: SentinelRedis) -> int:
    seconds, microseconds = await client.client.time()
    return seconds * 1_000_000 + microseconds


async def _state(client: SentinelRedis, key: str) -> str | None:
    state = await client.client.get(key)
    return None if state is None else cast(str, state)


@pytest.fixture
async def env(
    redis_client: SentinelRedis,
) -> AsyncGenerator[tuple[httpx.AsyncClient, SentinelRedis], None]:
    loader = ScriptLoader(redis_client.client)
    await load_scripts(loader)
    config = SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr(SECRET),
            jwt_algorithm_allowlist=ALLOWLIST,
        ),
        policies={
            "resumint.tailor": Policy(
                endpoint_id="resumint.tailor",
                algorithm=AlgorithmType.TOKEN_BUCKET,
                fail_mode=FailMode.FAIL_OPEN,
                fallback_rate_per_process_micro=2_000,
                policy_version=1,
                capacity_micro=1_000_000,
                refill_rate_micro_per_sec=0,
            ),
            "resumint.tailor.fast": Policy(
                endpoint_id="resumint.tailor.fast",
                algorithm=AlgorithmType.TOKEN_BUCKET,
                fail_mode=FailMode.FAIL_OPEN,
                fallback_rate_per_process_micro=2_000,
                policy_version=1,
                capacity_micro=1_000_000,
                refill_rate_micro_per_sec=1_000_000,
            ),
            "resumint.tailor.slow": Policy(
                endpoint_id="resumint.tailor.slow",
                algorithm=AlgorithmType.TOKEN_BUCKET,
                fail_mode=FailMode.FAIL_OPEN,
                fallback_rate_per_process_micro=2_000,
                policy_version=1,
                capacity_micro=1_000_000,
                refill_rate_micro_per_sec=500_000,
            ),
        },
    )
    guard = SentinelGuard(config, redis_client, loader)
    await guard.load_scripts()
    app = FastAPI()

    def _add_route(path: str, endpoint_id: str) -> None:
        @app.post(path)
        async def route(
            request: Request, _: None = Depends(guard.guard_for(endpoint_id))
        ) -> dict[str, object]:
            return {"allowed": request.state.decision.allowed}

    _add_route("/tailor", "resumint.tailor")
    _add_route("/tailor-fast", "resumint.tailor.fast")
    _add_route("/tailor-slow", "resumint.tailor.slow")
    _add_route("/unknown", "not.configured")

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client, redis_client
    finally:
        await client.aclose()


async def test_valid_token_allows(env: tuple[httpx.AsyncClient, SentinelRedis]) -> None:
    client, _ = env
    token = _token(_unique("valid"))
    response = await client.post("/tailor", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"allowed": True}


async def test_drain_token_bucket_returns_429(env: tuple[httpx.AsyncClient, SentinelRedis]) -> None:
    client, _ = env
    token = _token(_unique("drain"))
    first = await client.post("/tailor", headers={"Authorization": f"Bearer {token}"})
    second = await client.post("/tailor", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"detail": "rate limit exceeded"}
    assert "Retry-After" not in second.headers


async def test_token_bucket_retry_after(env: tuple[httpx.AsyncClient, SentinelRedis]) -> None:
    client, redis = env
    now = await _now_micro(redis)
    cases = [
        ("/tailor-fast", "resumint.tailor.fast", "500000", f"{now + 1_000_000}", "1"),
        ("/tailor-slow", "resumint.tailor.slow", "400000", f"{now + 1_000_000}", "2"),
        ("/tailor-slow", "resumint.tailor.slow", "0", str(now), "2"),
    ]
    for path, endpoint_id, tokens, last_refill, expected in cases:
        sub = _unique("retry")
        key = build_bucket_key(sub, endpoint_id, 1)
        seed = f"{tokens}:{last_refill}"
        await redis.client.set(key, seed)
        response = await client.post(path, headers={"Authorization": f"Bearer {_token(sub)}"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == expected
        assert await _state(redis, key) == seed


async def test_invalid_token_returns_401_without_evaluation(
    env: tuple[httpx.AsyncClient, SentinelRedis],
) -> None:
    client, redis = env
    sub = _unique("invalid")
    token = jwt.encode(
        {"sub": sub, "exp": int(time.time()) + 3_600},
        "wrong-secret-0123456789abcdef0123456789",
        algorithm="HS256",
    )
    response = await client.post("/tailor", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    key = build_bucket_key(sub, "resumint.tailor", 1)
    assert await _state(redis, key) is None


async def test_tenants_have_independent_buckets(
    env: tuple[httpx.AsyncClient, SentinelRedis],
) -> None:
    client, _ = env
    token_a = _token(_unique("iso-a"))
    token_b = _token(_unique("iso-b"))
    first = await client.post("/tailor", headers={"Authorization": f"Bearer {token_a}"})
    second = await client.post("/tailor", headers={"Authorization": f"Bearer {token_a}"})
    third = await client.post("/tailor", headers={"Authorization": f"Bearer {token_b}"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert third.status_code == 200


async def test_unknown_endpoint_returns_404(env: tuple[httpx.AsyncClient, SentinelRedis]) -> None:
    client, _ = env
    token = _token(_unique("unknown"))
    response = await client.post("/unknown", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


async def test_x_tenant_id_spoofing_does_not_affect_bucket(
    env: tuple[httpx.AsyncClient, SentinelRedis],
) -> None:
    client, _ = env
    target = _token(_unique("spoof-target"))
    attacker = _token(_unique("spoof-attacker"))
    spoof_headers = {"Authorization": f"Bearer {target}", "X-Tenant-ID": attacker}
    first = await client.post("/tailor", headers=spoof_headers)
    second = await client.post("/tailor", headers=spoof_headers)
    assert first.status_code == 200
    assert second.status_code == 429
    swapped = {"Authorization": f"Bearer {attacker}", "X-Tenant-ID": target}
    third = await client.post("/tailor", headers=swapped)
    assert third.status_code == 200
