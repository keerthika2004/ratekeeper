"""The policy cache: freshness, serving stale data during outages, single-flight loads."""

from __future__ import annotations

import asyncio

import pytest

from ratekeeper.core.models import Algorithm, Band, Policy
from ratekeeper.policies.cache import PolicyCache
from tests.support import make_policy

class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

class FakeDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, Policy] = {}
        self.loads = 0
        self.down = False
        self.gate: asyncio.Event | None = None  # when set, loads wait for it

    async def load(self, name: str) -> Policy | None:
        self.loads += 1
        if self.down:
            raise ConnectionError("postgres down")
        row = self.rows.get(name)  # the query reads the row now...
        if self.gate is not None:
            await self.gate.wait()  # ...but the answer arrives later
        return row

def version(n: int) -> Policy:
    return make_policy(Algorithm.GCRA, Band(10, 1_000), name="api", version=n)

def make(db: FakeDatabase, clock: FakeClock, max_size: int = 100) -> PolicyCache:
    return PolicyCache(db.load, refresh_after=30, expire_after=3600, max_size=max_size, clock=clock)

async def settle() -> None:
    """Let background tasks run."""
    for _ in range(5):
        await asyncio.sleep(0)

async def test_serves_from_memory_after_the_first_load() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    cache = make(db, clock)

    assert await cache.get("api") == version(1)
    assert await cache.get("api") == version(1)
    assert db.loads == 1

async def test_unknown_names_are_cached_too() -> None:
    db, clock = FakeDatabase(), FakeClock()
    cache = make(db, clock)

    assert await cache.get("nope") is None
    assert await cache.get("nope") is None
    assert db.loads == 1

async def test_invalidation_forces_a_fresh_load() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    cache = make(db, clock)
    await cache.get("api")

    db.rows["api"] = version(2)
    cache.invalidate("api")

    assert await cache.get("api") == version(2)

async def test_old_entries_are_refreshed_in_the_background() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    cache = make(db, clock)
    await cache.get("api")

    db.rows["api"] = version(2)
    clock.now = 31  # past refresh_after
    assert await cache.get("api") == version(1)  # served immediately, refresh starts behind it
    await settle()
    assert await cache.get("api") == version(2)

async def test_keeps_serving_the_old_copy_while_the_database_is_down() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    cache = make(db, clock)
    await cache.get("api")

    db.down = True
    clock.now = 31
    assert await cache.get("api") == version(1)
    await settle()  # the background refresh fails...
    assert await cache.get("api") == version(1)  # ...and the old copy survives
    await settle()

async def test_entries_too_old_to_trust_need_the_database() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    cache = make(db, clock)
    await cache.get("api")

    db.down = True
    clock.now = 3601  # past expire_after
    with pytest.raises(ConnectionError):
        await cache.get("api")

async def test_concurrent_misses_share_one_load() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    db.gate = asyncio.Event()
    cache = make(db, clock)

    waiting = [asyncio.create_task(cache.get("api")) for _ in range(10)]
    await settle()
    db.gate.set()

    assert await asyncio.gather(*waiting) == [version(1)] * 10
    assert db.loads == 1

async def test_a_load_racing_an_invalidation_is_not_cached() -> None:
    db, clock = FakeDatabase(), FakeClock()
    db.rows["api"] = version(1)
    db.gate = asyncio.Event()
    cache = make(db, clock)

    slow_load = asyncio.create_task(cache.get("api"))
    await settle()  # the load has read version 1 and is waiting...
    db.rows["api"] = version(2)
    cache.invalidate("api")  # ...while the policy changes
    db.gate.set()
    await slow_load

    db.gate = None
    assert await cache.get("api") == version(2)

async def test_least_recently_used_entries_are_evicted() -> None:
    db, clock = FakeDatabase(), FakeClock()
    cache = make(db, clock, max_size=2)

    await cache.get("a")
    await cache.get("b")
    await cache.get("a")  # "a" is now more recently used than "b"
    await cache.get("c")  # evicts "b"
    loads = db.loads

    await cache.get("a")
    assert db.loads == loads
    await cache.get("b")
    assert db.loads == loads + 1
