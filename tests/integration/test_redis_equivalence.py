"""The most important test in the project.


 

For random policies and random request sequences, the Lua scripts in real

Redis must return exactly the same decisions as the reference model, field

for field. test_reference_properties.py proves the model's guarantees; this

test transfers them to the code that actually runs in production.

"""


 

from __future__ import annotations


 

import asyncio


 

import pytest

import redis.asyncio as redis

from hypothesis import given, settings


 

from ratekeeper.core.models import Policy

from ratekeeper.redis_limiter import RedisRateLimiter

from tests.reference import ReferenceRateLimiter

from tests.strategies import Step, policies, scenarios

from tests.support import ManualClock


 

pytestmark = pytest.mark.integration



 

# Hypothesis runs plain (sync) test functions, so each example drives its own

# short-lived event loop with asyncio.run().

@settings(max_examples=300, deadline=None)

@given(policy=policies(), steps=scenarios)

def test_lua_scripts_match_the_reference_model(redis_url: str, policy: Policy, steps: list[Step]) -> None:

    asyncio.run(_compare(redis_url, policy, steps))



 

async def _compare(redis_url: str, policy: Policy, steps: list[Step]) -> None:

    client = redis.from_url(redis_url)

    try:

        await client.flushdb()

        clock = ManualClock()

        limiter = RedisRateLimiter(client, clock)

        model = ReferenceRateLimiter(clock)

        for i, step in enumerate(steps):

            clock.advance(step.advance_us)

            cost = step.cost(policy)


 

            expected = model.check(policy, step.identity, cost)

            actual = await limiter.check(policy, step.identity, cost)


 

            assert actual == expected, f"step {i}: redis={actual} model={expected}"

    finally:

        await client.aclose()


 