"""Redis foundation tests against a real Redis instance (Phase 2)."""

import inspect
from time import monotonic

import pytest
from redis.exceptions import NoScriptError, RedisError
from sentinel.errors import ScriptMissingError
from sentinel.redis import MAX_CONNECTIONS, NOEVICTION_POLICY, ScriptLoader, SentinelRedis

pytestmark = pytest.mark.integration

IDENTITY_SCRIPT = "return tonumber(ARGV[1])"
MAXMEMORY_BYTES = 268_435_456


async def test_pool_enforces_timeout_budget_and_fixed_capacity(redis_client: SentinelRedis) -> None:
    kwargs = redis_client.client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 0.02
    assert kwargs["socket_connect_timeout"] == 0.02
    assert kwargs["decode_responses"] is True
    assert redis_client.client.connection_pool.max_connections == MAX_CONNECTIONS


def test_max_connections_is_not_configurable() -> None:
    params = list(inspect.signature(SentinelRedis.__init__).parameters)
    assert params == ["self", "redis_url"]


async def test_startup_check_passes_on_noeviction_with_bounded_memory(
    redis_client: SentinelRedis,
) -> None:
    await redis_client.client.config_set("maxmemory-policy", NOEVICTION_POLICY)
    await redis_client.client.config_set("maxmemory", MAXMEMORY_BYTES)
    await redis_client.assert_noeviction()


async def test_startup_check_fails_on_allkeys_lru(redis_client: SentinelRedis) -> None:
    await redis_client.client.config_set("maxmemory-policy", "allkeys-lru")
    try:
        with pytest.raises(RuntimeError, match="maxmemory-policy"):
            await redis_client.assert_noeviction()
    finally:
        await redis_client.client.config_set("maxmemory-policy", NOEVICTION_POLICY)


async def test_startup_check_fails_when_maxmemory_unset(redis_client: SentinelRedis) -> None:
    await redis_client.client.config_set("maxmemory", 0)
    try:
        with pytest.raises(RuntimeError, match="maxmemory is unset"):
            await redis_client.assert_noeviction()
    finally:
        await redis_client.client.config_set("maxmemory", MAXMEMORY_BYTES)


async def test_unreachable_redis_fails_within_budget() -> None:
    client = SentinelRedis("redis://10.255.255.1:6379/0")
    started = monotonic()
    with pytest.raises(RedisError):
        await client.client.ping()
    elapsed = monotonic() - started
    assert elapsed < 1.0


async def test_script_load_returns_sha_and_executes(redis_client: SentinelRedis) -> None:
    loader = ScriptLoader(redis_client.client)
    sha = await loader.load("identity", IDENTITY_SCRIPT)
    assert len(sha) == 40
    assert sha.isalnum()
    result = await loader.execute("identity", keys=[], args=["42"])
    assert result == 42


async def test_script_load_is_idempotent(redis_client: SentinelRedis) -> None:
    loader = ScriptLoader(redis_client.client)
    first = await loader.load("identity", IDENTITY_SCRIPT)
    second = await loader.load("identity", IDENTITY_SCRIPT)
    assert first == second


async def test_execute_recovers_from_noscript(redis_client: SentinelRedis) -> None:
    loader = ScriptLoader(redis_client.client)
    await loader.load("identity", IDENTITY_SCRIPT)
    await redis_client.client.script_flush()
    result = await loader.execute("identity", keys=[], args=["7"])
    assert result == 7


async def test_execute_raises_redis_error_when_script_missing_again(
    redis_client: SentinelRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = ScriptLoader(redis_client.client)
    await loader.load("identity", IDENTITY_SCRIPT)
    calls: list[int] = []

    async def flaky_evalsha(*args: object, **kwargs: object) -> object:
        calls.append(1)
        raise NoScriptError

    monkeypatch.setattr(loader._client, "evalsha", flaky_evalsha)
    with pytest.raises(ScriptMissingError, match="missing again after re-load"):
        await loader.execute("identity", keys=[], args=["1"])
    assert calls == [1, 1]


async def test_execute_unloaded_script_raises_key_error(redis_client: SentinelRedis) -> None:
    loader = ScriptLoader(redis_client.client)
    with pytest.raises(KeyError):
        await loader.execute("missing", keys=[], args=[])
