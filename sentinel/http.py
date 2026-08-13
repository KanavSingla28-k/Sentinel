"""FastAPI guard wiring JWT auth, policy resolution, and rate limiting (Phase 7).

The guard is a per-route dependency factory: each route supplies its
configured ``endpoint_id`` explicitly (ADR-009), never deriving it from the
URL. Authentication failures produce 401 before the resolver or RateLimiter
run; Redis and rate-limiter exceptions propagate unchanged (Phase 8 owns
failure handling).
"""

import math
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from sentinel.auth import AuthenticationError, verify_bearer_token
from sentinel.config import SentinelConfig
from sentinel.limiter import RateLimiter, build_bucket_key
from sentinel.lua import load_scripts as load_lua_scripts
from sentinel.redis import ScriptLoader, SentinelRedis
from sentinel.resolver import StaticPolicyResolver

_RATE_LIMITED_DETAIL = "rate limit exceeded"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token_from(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is None:
        raise _unauthorized()
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise _unauthorized()
    return value


class SentinelGuard:
    def __init__(self, config: SentinelConfig, redis: SentinelRedis, loader: ScriptLoader) -> None:
        self._config = config
        self._redis = redis
        self._loader = loader
        self._resolver = StaticPolicyResolver(config)
        self._limiter = RateLimiter(loader)
        self._scripts_loaded = False

    async def load_scripts(self) -> None:
        await load_lua_scripts(self._loader)
        self._scripts_loaded = True

    def guard_for(self, endpoint_id: str) -> Callable[[Request], Awaitable[None]]:
        async def _guard(request: Request) -> None:
            token = _bearer_token_from(request)
            try:
                tenant_id = verify_bearer_token(
                    token,
                    secret=self._config.app.jwt_secret.get_secret_value(),
                    algorithms=self._config.app.jwt_algorithm_allowlist,
                )
            except AuthenticationError:
                raise _unauthorized() from None
            policy = self._resolver.resolve(tenant_id, endpoint_id)
            if policy is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="unknown endpoint"
                )
            if not self._scripts_loaded:
                raise RuntimeError(
                    "Sentinel scripts are not loaded; "
                    "call await guard.load_scripts() before evaluating requests"
                )
            key = build_bucket_key(tenant_id, policy.endpoint_id, policy.policy_version)
            decision = await self._limiter.evaluate(policy, key)
            request.state.decision = decision
            if not decision.allowed:
                headers: dict[str, str] = {}
                if decision.retry_after_seconds is not None:
                    headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after_seconds)))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=_RATE_LIMITED_DETAIL,
                    headers=headers,
                )

        return _guard
