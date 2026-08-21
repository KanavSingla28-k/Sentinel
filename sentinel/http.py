"""FastAPI guard wiring JWT auth, policy resolution, and rate limiting (Phases 7, 19).

The guard is a per-route dependency factory: each route supplies its
configured ``endpoint_id`` explicitly (ADR-009), never deriving it from the
URL. Authentication failures produce 401 before the resolver or RateLimiter
run. Denied decisions map to HTTP status by DecisionReason (Phase 8):
rate-limit denials are 429 with Retry-After, store-failure denials are 503.

Anonymous policies (Phase 19) are wired through ``anonymous_guard_for``: no
bearer token is required, identity is a server-issued signed client cookie
plus the trusted-client IP, and denials map with the exact same 429/503
semantics as tenant-mode denials (SEC-ANON-05 — no distinguishable error
shape). The cookie is minted on the first request that carries no valid
cookie and is delivered via the FastAPI ``Response`` injection; a denied
request never receives one (flooders without cookies stay IP-bounded,
SEC-ANON-06).
"""

import math
import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response, status

from sentinel.anonymous import (
    anonymous_cookie_identity,
    anonymous_ip_identity,
    client_ip,
    hash_identity,
    mint_cookie,
    parse_cookie,
)
from sentinel.auth import AuthenticationError, verify_bearer_token
from sentinel.circuit_breaker import CircuitBreaker
from sentinel.config import SentinelConfig
from sentinel.emergency import EmergencyLimiter, TokenBucketEmergencyLimiter
from sentinel.limiter import RateLimiter, build_anonymous_key, build_bucket_key, hash_tenant
from sentinel.lua import load_scripts as load_lua_scripts
from sentinel.models import Decision, DecisionReason, IdentityMode
from sentinel.observability import SentinelObservability
from sentinel.redis import ScriptLoader, SentinelRedis
from sentinel.resolver import StaticPolicyResolver

_RATE_LIMITED_DETAIL = "rate limit exceeded"
_UNAVAILABLE_DETAIL = "rate limiter unavailable"

_HTTP_429_REASONS = frozenset(
    {
        DecisionReason.RATE_LIMITED,
        DecisionReason.EMERGENCY_LOCAL_LIMIT,
    }
)
_HTTP_503_REASONS = frozenset(
    {
        DecisionReason.FAIL_CLOSED,
        DecisionReason.CIRCUIT_OPEN,
        DecisionReason.REDIS_TIMEOUT,
        DecisionReason.REDIS_CONNECTION_ERROR,
        DecisionReason.REDIS_NOSCRIPT_RETRY,
    }
)


def _denied_status(reason: DecisionReason) -> int:
    if reason in _HTTP_429_REASONS:
        return status.HTTP_429_TOO_MANY_REQUESTS
    if reason in _HTTP_503_REASONS:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    raise RuntimeError(f"no HTTP status mapped for denied reason {reason.value!r}")


def _raise_denied(decision: Decision) -> None:
    """Raise the HTTPException for a denied decision (Phases 8 and 19)."""
    if _denied_status(decision.reason) == status.HTTP_429_TOO_MANY_REQUESTS:
        headers: dict[str, str] = {}
        if decision.retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after_seconds)))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMITED_DETAIL,
            headers=headers,
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_UNAVAILABLE_DETAIL,
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token_from(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if authorization is None:
        raise _unauthorized()
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise _unauthorized()
    return value


class SentinelGuard:
    def __init__(
        self,
        config: SentinelConfig,
        redis: SentinelRedis,
        loader: ScriptLoader,
        *,
        breaker: CircuitBreaker | None = None,
        emergency: EmergencyLimiter | None = None,
        observability: SentinelObservability | None = None,
    ) -> None:
        self._config = config
        self._redis = redis
        self._loader = loader
        self._resolver = StaticPolicyResolver(config)
        self._breaker = breaker or CircuitBreaker()
        self._limiter = RateLimiter(
            loader,
            breaker=self._breaker,
            emergency=emergency or TokenBucketEmergencyLimiter(),
        )
        self._observability = observability or SentinelObservability()
        self._scripts_loaded = False

    async def load_scripts(self) -> None:
        await load_lua_scripts(self._loader)
        self._scripts_loaded = True

    def guard_for(self, endpoint_id: str) -> Callable[[Request], Awaitable[None]]:
        self._assert_identity_mode(endpoint_id, IdentityMode.TENANT_JWT)

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
            self._assert_scripts_loaded()
            key = build_bucket_key(tenant_id, policy.endpoint_id, policy.policy_version)
            started = time.perf_counter_ns()
            decision = await self._limiter.evaluate(policy, key)
            latency_micro = (time.perf_counter_ns() - started) // 1_000
            self._observability.record_decision(
                identity_mode=IdentityMode.TENANT_JWT,
                identity_hash=hash_tenant(tenant_id),
                endpoint_id=policy.endpoint_id,
                decision=decision,
                latency_micro=latency_micro,
                breaker_state=self._breaker.state,
            )
            request.state.decision = decision
            if not decision.allowed:
                _raise_denied(decision)

        return _guard

    def anonymous_guard_for(
        self, endpoint_id: str
    ) -> Callable[[Request, Response], Awaitable[None]]:
        self._assert_identity_mode(endpoint_id, IdentityMode.ANONYMOUS)

        async def _guard(request: Request, response: Response) -> None:
            app_config = self._config.app
            policy = self._resolver.resolve_anonymous(endpoint_id)
            if policy is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="unknown endpoint"
                )
            self._assert_scripts_loaded()
            secret = app_config.anonymous_cookie_secret
            if secret is None:
                raise RuntimeError(
                    "anonymous policies require app.anonymous_cookie_secret to be configured"
                )
            cookie_value = request.cookies.get(app_config.anonymous_cookie_name)
            now = int(time.time())
            client_id = parse_cookie(
                cookie_value,
                secret=secret.get_secret_value(),
                now=now,
            )
            issued = client_id is None
            if issued:
                client_id, cookie_value = mint_cookie(
                    secret.get_secret_value(),
                    app_config.anonymous_cookie_ttl_seconds,
                    now=now,
                )
            assert client_id is not None
            assert cookie_value is not None
            ip = client_ip(request.client.host if request.client is not None else None)
            identities = [anonymous_ip_identity(ip)]
            if not issued:
                identities.insert(0, anonymous_cookie_identity(client_id))
            keys = tuple(
                build_anonymous_key(identity, policy.endpoint_id, policy.policy_version)
                for identity in identities
            )
            started = time.perf_counter_ns()
            decision = await self._limiter.evaluate_anonymous(policy, keys)
            latency_micro = (time.perf_counter_ns() - started) // 1_000
            self._observability.record_decision(
                identity_mode=IdentityMode.ANONYMOUS,
                identity_hash=hash_identity(identities[0]),
                endpoint_id=policy.endpoint_id,
                decision=decision,
                latency_micro=latency_micro,
                breaker_state=self._breaker.state,
            )
            request.state.decision = decision
            request.state.anonymous_client_id = client_id
            if issued and decision.allowed:
                response.set_cookie(
                    key=app_config.anonymous_cookie_name,
                    value=cookie_value,
                    max_age=app_config.anonymous_cookie_ttl_seconds,
                    httponly=True,
                    secure=app_config.anonymous_cookie_secure,
                    samesite="lax",
                    path="/",
                )
            if not decision.allowed:
                _raise_denied(decision)

        return _guard

    def _assert_identity_mode(self, endpoint_id: str, expected: IdentityMode) -> None:
        policy = self._resolver.resolve_anonymous(endpoint_id)
        if policy is not None and policy.identity is not expected:
            raise RuntimeError(
                f"endpoint {endpoint_id!r} is a {policy.identity.value} policy; "
                "use the matching guard factory"
            )

    def _assert_scripts_loaded(self) -> None:
        if not self._scripts_loaded:
            raise RuntimeError(
                "Sentinel scripts are not loaded; "
                "call await guard.load_scripts() before evaluating requests"
            )
