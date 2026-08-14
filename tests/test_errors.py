"""Redis failure classification tests (Phase 8)."""

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sentinel.errors import ScriptMissingError, classify_redis_error
from sentinel.models import DecisionReason


def test_script_missing_error_subclasses_redis_error() -> None:
    assert issubclass(ScriptMissingError, RedisError)


def test_classify_script_missing_error() -> None:
    error = ScriptMissingError("script missing again after re-load")
    assert classify_redis_error(error) is DecisionReason.REDIS_NOSCRIPT_RETRY


def test_classify_timeout_error() -> None:
    error = RedisTimeoutError("timeout reading from socket")
    assert classify_redis_error(error) is DecisionReason.REDIS_TIMEOUT


def test_classify_connection_error() -> None:
    error = RedisConnectionError("connection refused")
    assert classify_redis_error(error) is DecisionReason.REDIS_CONNECTION_ERROR


def test_classify_other_redis_error_falls_back_to_connection_error() -> None:
    error = ResponseError("unknown command")
    assert classify_redis_error(error) is DecisionReason.REDIS_CONNECTION_ERROR
