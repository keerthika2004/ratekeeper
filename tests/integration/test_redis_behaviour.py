"""Concurrency and key expiry against real Redis."""

from __future__ import annotations

import asyncio

import pytest
import redis.asyncio as redis

from ratekeeper.core.keys import redis_keys
from ratekeeper.core.models import Algorithm, Band
from ratekeeper.redis_limiter import RedisRateLimiter
from tests.support import ManualClock, make_policy

pytestmark = pytest.mark.integration

LIMIT = 50
REQUESTS = 500

async def admitted(limiter: RedisRateLimiter, algorithm: Algorithm, window_ms: int) -> int:
    policy = make_policy(algorithm, Band(LIMIT, window_ms), name="concurrency")
    results = await asyncio.gather(*(limiter.check(policy, "hot-user", 1) for _ in range(REQUESTS)))
    return sum(result.allowed for result in results)

@pytest.mark.parametrize("algorithm", list(Algorithm))
async def test_exactly_the_limit_is_admitted_under_contention(
    redis_client: redis.Redis, algorithm: Algorithm
) -> None:
    """500 requests in flight at once against a limit of 50: the scenario where a
    read-then-write limiter lets far more through. A frozen clock puts every
    request at the same instant, so every algorithm must admit exactly 50."""
    limiter = RedisRateLimiter(redis_client, ManualClock())

    assert await admitted(limiter, algorithm, window_ms=60_000) == LIMIT

@pytest.mark.parametrize("algorithm", [Algorithm.SLIDING_WINDOW_LOG, Algorithm.GCRA])
async def test_exactly_the_limit_is_admitted_with_real_time(
    redis_client: redis.Redis, algorithm: Algorithm
) -> None:
    """Same with Redis' real clock (the production path). Limited to the two
    algorithms whose count can't be affected by the test straddling a window
    boundary, with an hour-long window so nothing refills meanwhile."""
    limiter = RedisRateLimiter(redis_client)

    assert await admitted(limiter, algorithm, window_ms=3_600_000) == LIMIT

WINDOW_MS = 10_000

@pytest.mark.parametrize("algorithm", list(Algorithm))
async def test_every_key_expires_on_its_own(redis_client: redis.Redis, algorithm: Algorithm) -> None:
    """Otherwise idle identities would fill Redis forever."""
    policy = make_policy(algorithm, Band(5, WINDOW_MS), name="expiry")
    await RedisRateLimiter(redis_client).check(policy, "idle-user", 1)

    for key in redis_keys(policy, "idle-user"):
        ttl_ms = await redis_client.pttl(key)
        # The sliding counter keeps state longest: through the following window.
        assert 0 < ttl_ms <= 2 * WINDOW_MS, key

@pytest.mark.parametrize("algorithm", list(Algorithm))
async def test_simulated_clock_sets_no_ttl(redis_client: redis.Redis, algorithm: Algorithm) -> None:
    """Documents the trade-off in prelude.lua: TTLs follow Redis' real clock,
    which a simulated clock can't move, so none are set in that mode."""
    policy = make_policy(algorithm, Band(5, WINDOW_MS), name="expiry")
    await RedisRateLimiter(redis_client, ManualClock()).check(policy, "test-user", 1)

    for key in redis_keys(policy, "test-user"):
        assert await redis_client.pttl(key) == -1, key
