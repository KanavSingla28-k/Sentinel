"""Redis failure types and classification for Sentinel (Phase 8)."""

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from sentinel.models import DecisionReason


class ScriptMissingError(RedisError):
    """Raised when a script is still missing after the NOSCRIPT re-load."""


def classify_redis_error(exc: RedisError) -> DecisionReason:
    """Map a Redis failure to the bounded failure decision reason."""
    if isinstance(exc, ScriptMissingError):
        return DecisionReason.REDIS_NOSCRIPT_RETRY
    if isinstance(exc, RedisTimeoutError):
        return DecisionReason.REDIS_TIMEOUT
    if isinstance(exc, RedisConnectionError):
        return DecisionReason.REDIS_CONNECTION_ERROR
    return DecisionReason.REDIS_CONNECTION_ERROR
