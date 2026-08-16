"""Redis connection foundation for Sentinel (Phase 2)."""

from typing import Any, cast

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import NoScriptError

from sentinel.errors import ScriptMissingError

SOCKET_TIMEOUT_SECONDS = 0.02
SOCKET_CONNECT_TIMEOUT_SECONDS = 0.02
MAX_CONNECTIONS = 50
NOEVICTION_POLICY = "noeviction"


class SentinelRedis:
    def __init__(
        self,
        redis_url: str,
        socket_timeout: float | None = SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout: float | None = SOCKET_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._pool = ConnectionPool.from_url(
            redis_url,
            max_connections=MAX_CONNECTIONS,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            decode_responses=True,
        )
        self._client = Redis(connection_pool=self._pool)

    @property
    def client(self) -> Redis:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def assert_noeviction(self) -> None:
        policy = await self._client.config_get("maxmemory-policy")
        configured_policy = policy.get("maxmemory-policy")
        if configured_policy != NOEVICTION_POLICY:
            raise RuntimeError(
                f"maxmemory-policy is {configured_policy!r}; "
                f"Sentinel requires {NOEVICTION_POLICY!r}"
            )
        maxmemory = await self._client.config_get("maxmemory")
        maxmemory_bytes = int(maxmemory.get("maxmemory") or "0")
        if maxmemory_bytes <= 0:
            raise RuntimeError("maxmemory is unset; Sentinel requires a bounded memory limit")


class ScriptLoader:
    def __init__(self, client: Redis) -> None:
        self._client = client
        self._sources: dict[str, str] = {}
        self._shas: dict[str, str] = {}

    async def load(self, name: str, source: str) -> str:
        sha: str = cast(Any, await self._client.script_load(source))
        self._sources[name] = source
        self._shas[name] = sha
        return sha

    def sha(self, name: str) -> str | None:
        return self._shas.get(name)

    async def execute(self, name: str, keys: list[str], args: list[str]) -> int | list[int] | None:
        source = self._sources.get(name)
        if source is None:
            raise KeyError(f"script {name!r} has not been loaded")
        try:
            result = await cast(
                Any, self._client.evalsha(self._shas[name], len(keys), *keys, *args)
            )
        except NoScriptError:
            self._shas[name] = cast(Any, await self._client.script_load(source))
            try:
                result = await cast(
                    Any, self._client.evalsha(self._shas[name], len(keys), *keys, *args)
                )
            except NoScriptError as exc:
                raise ScriptMissingError(f"script {name!r} missing again after re-load") from exc
        return cast("int | list[int] | None", result)
