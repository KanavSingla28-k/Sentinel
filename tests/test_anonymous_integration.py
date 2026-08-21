"""End-to-end anonymous rate-limiting tests against real Redis (Phase 19)."""

import time
import uuid
from collections.abc import AsyncGenerator
from typing import cast

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from pydantic import SecretStr
from sentinel.anonymous import (
    anonymous_cookie_identity,
    anonymous_ip_identity,
    hash_identity,
    mint_cookie,
)
from sentinel.config import AppConfig, SentinelConfig
from sentinel.http import SentinelGuard
from sentinel.limiter import build_anonymous_key
from sentinel.lua import load_scripts
from sentinel.models import AlgorithmType, FailMode, IdentityMode, Policy
from sentinel.redis import ScriptLoader, SentinelRedis

pytestmark = pytest.mark.integration

SECRET = "integration-secret-0123456789abcdef0123456789abcdef"
ANON_SECRET = "anon-integration-secret-0123456789abcdef0123456789abcdef"
COOKIE_NAME = "sentinel_anon_id"
COOKIE_TTL = 2_592_000

TEST_IP = "unknown"


def _unique(prefix: str) -> str:
    return f"test-phase19-{prefix}-{uuid.uuid4().hex}"


def _token_bucket_policy(endpoint_id: str, capacity_micro: int = 1_000_000) -> Policy:
    return Policy(
        endpoint_id=endpoint_id,
        identity=IdentityMode.ANONYMOUS,
        algorithm=AlgorithmType.TOKEN_BUCKET,
        fail_mode=FailMode.FAIL_OPEN,
        fallback_rate_per_process_micro=2_000,
        policy_version=1,
        capacity_micro=capacity_micro,
        refill_rate_micro_per_sec=0,
    )


def _ip_key(endpoint_id: str) -> str:
    return build_anonymous_key(anonymous_ip_identity(TEST_IP), endpoint_id, 1)


def _cookie_key(client_id: str, endpoint_id: str) -> str:
    return build_anonymous_key(anonymous_cookie_identity(client_id), endpoint_id, 1)


async def _state(client: SentinelRedis, key: str) -> str | None:
    state = await client.client.get(key)
    return None if state is None else cast(str, state)


async def _cookie_from(response: httpx.Response) -> str:
    set_cookie = response.headers["set-cookie"]
    return set_cookie.split(";")[0].split("=", 1)[1]


@pytest.fixture
async def env(
    redis_client: SentinelRedis,
) -> AsyncGenerator[tuple[httpx.AsyncClient, SentinelRedis, str, list[str]], None]:
    loader = ScriptLoader(redis_client.client)
    await load_scripts(loader)
    endpoint_id = _unique("login")
    config = SentinelConfig(
        app=AppConfig(
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr(SECRET),
            jwt_algorithm_allowlist=frozenset({"HS256"}),
            anonymous_cookie_secret=SecretStr(ANON_SECRET),
            anonymous_cookie_name=COOKIE_NAME,
            anonymous_cookie_ttl_seconds=COOKIE_TTL,
            anonymous_cookie_secure=False,
        ),
        policies={endpoint_id: _token_bucket_policy(endpoint_id)},
    )
    guard = SentinelGuard(config, redis_client, loader)
    await guard.load_scripts()
    app = FastAPI()

    @app.post("/login")
    async def route(
        request: Request, _: None = Depends(guard.anonymous_guard_for(endpoint_id))
    ) -> dict[str, object]:
        return {"allowed": request.state.decision.allowed}

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    cleanup_keys: list[str] = []
    try:
        yield client, redis_client, endpoint_id, cleanup_keys
    finally:
        await client.aclose()
        if cleanup_keys:
            await redis_client.client.delete(*cleanup_keys)


def _create_cleanup(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
    keys: str | list[str],
) -> None:
    cleanup = env[3]
    if isinstance(keys, list):
        cleanup.extend(keys)
    else:
        cleanup.append(keys)


async def test_first_request_allows_keys_ip_and_sets_cookie(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    client, redis, endpoint_id, cleanup = env
    response = await client.post("/login")
    assert response.status_code == 200
    assert response.json() == {"allowed": True}
    assert response.headers["set-cookie"].startswith(f"{COOKIE_NAME}=")
    await _create_cleanup(env, _ip_key(endpoint_id))
    assert await _state(redis, _ip_key(endpoint_id)) is not None


async def test_second_request_with_cookie_consumes_both_buckets(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    client, redis, endpoint_id, cleanup = env
    first = await client.post("/login")
    cookie = await _cookie_from(first)
    client_id = cookie.split(".")[0]
    second = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert second.status_code == 200
    await _create_cleanup(env, [_ip_key(endpoint_id), _cookie_key(client_id, endpoint_id)])
    assert await _state(redis, _ip_key(endpoint_id)) is not None
    assert await _state(redis, _cookie_key(client_id, endpoint_id)) is not None


async def test_cookie_bucket_drain_denies_even_with_fresh_ip_history(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    client, redis, endpoint_id, cleanup = env
    first = await client.post("/login")
    cookie = await _cookie_from(first)
    second = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    third = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json() == {"detail": "rate limit exceeded"}
    assert "Retry-After" not in third.headers
    await _create_cleanup(
        env, [_ip_key(endpoint_id), _cookie_key(cookie.split(".")[0], endpoint_id)]
    )


async def test_cleared_cookie_is_bound_by_ip_bucket(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    client, redis, endpoint_id, cleanup = env
    first = await client.post("/login")
    assert first.status_code == 200
    client.cookies.clear()
    second = await client.post("/login")
    assert second.status_code == 429
    await _create_cleanup(env, _ip_key(endpoint_id))
    assert await _state(redis, _ip_key(endpoint_id)) is not None


async def test_denied_requests_never_write(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    client, redis, endpoint_id, cleanup = env
    first = await client.post("/login")
    cookie = await _cookie_from(first)
    second = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert second.status_code == 200
    ip_key = _ip_key(endpoint_id)
    cookie_key = _cookie_key(cookie.split(".")[0], endpoint_id)
    ip_state_before = await _state(redis, ip_key)
    cookie_state_before = await _state(redis, cookie_key)
    third = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert third.status_code == 429
    assert await _state(redis, ip_key) == ip_state_before
    assert await _state(redis, cookie_key) == cookie_state_before
    await _create_cleanup(env, [ip_key, cookie_key])


async def test_valid_signed_cookie_from_another_ip_is_still_accepted(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    """A legitimate user's cookie works regardless of which IP delivers it."""
    client, redis, endpoint_id, cleanup = env
    client_id, cookie = mint_cookie(ANON_SECRET, COOKIE_TTL, now=int(time.time()))
    response = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert response.status_code == 200
    await _create_cleanup(env, [_ip_key(endpoint_id), _cookie_key(client_id, endpoint_id)])
    assert await _state(redis, _cookie_key(client_id, endpoint_id)) is not None


async def test_expired_cookie_is_rejected_and_reissued(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    client, redis, endpoint_id, cleanup = env
    _, expired = mint_cookie(ANON_SECRET, COOKIE_TTL, now=int(time.time()) - COOKIE_TTL - 1)
    response = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={expired}"})
    assert response.status_code == 200
    assert response.headers["set-cookie"].startswith(f"{COOKIE_NAME}=")
    new_client_id = response.headers["set-cookie"].split("=")[1].split(";")[0].split(".")[0]
    assert await _state(redis, _cookie_key(new_client_id, endpoint_id)) is None
    await _create_cleanup(env, _ip_key(endpoint_id))
    assert await _state(redis, _ip_key(endpoint_id)) is not None


async def test_cookie_user_shared_ip_drained_by_cookie_less_user(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    """IP bucket is shared: a cookie user behind the same IP is bound by it."""
    client, redis, endpoint_id, cleanup = env
    first = await client.post("/login")
    cookie = await _cookie_from(first)
    client.cookies.clear()
    second = await client.post("/login")
    assert second.status_code == 429
    third = await client.post("/login", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert third.status_code == 429
    await _create_cleanup(
        env, [_ip_key(endpoint_id), _cookie_key(cookie.split(".")[0], endpoint_id)]
    )
    assert await _state(redis, _ip_key(endpoint_id)) is not None


async def test_tenants_never_share_anonymous_state(
    env: tuple[httpx.AsyncClient, SentinelRedis, str, list[str]],
) -> None:
    """Anonymous identity hashes live in the v2 namespace; tenant keys (v1)
    cannot collide with them even for identical raw values."""
    _, redis, endpoint_id, cleanup = env
    client_id = "f" * 32
    anonymous_key = build_anonymous_key(anonymous_cookie_identity(client_id), endpoint_id, 1)
    tenant_key = f"sentinel:v1:{hash_identity(f'anon:cookie:{client_id}')}:{endpoint_id}:1"
    assert anonymous_key != tenant_key
    assert not anonymous_key.startswith("sentinel:v1:")
