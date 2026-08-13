"""Shared test fixtures."""

from collections.abc import AsyncGenerator

import pytest
from redis.exceptions import RedisError
from sentinel.redis import SentinelRedis

TEST_REDIS_URL = "redis://localhost:6379/0"


@pytest.fixture
async def redis_client() -> AsyncGenerator[SentinelRedis, None]:
    client = SentinelRedis(TEST_REDIS_URL)
    try:
        await client.client.ping()
    except RedisError:
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    yield client
    await client.aclose()
