"""Keeps policies in memory so the check path never waits on Postgres.

Freshness comes from three layers:

1. When a policy changes, the instance that changed it drops its own copy and
   publishes the name on a Redis channel; every other instance drops its copy
   too (see `listen_for_changes`).
2. Pub/sub is fire-and-forget, so a message can be missed (e.g. during a
   reconnect). As a backstop, an entry older than `refresh_after` is reloaded
   in the background while the old copy keeps being served.
3. If Postgres is down, that reload fails and the old copy is kept, up to
   `expire_after`. A Postgres outage doesn't take rate limiting down with it.

"No such policy" is cached too (as None), so a caller hammering an unknown
name doesn't turn into one database query per request.

Concurrent requests for the same missing policy share one database load
("single flight") instead of each running its own query.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import redis.asyncio as redis
from redis.exceptions import RedisError

from ratekeeper.core.models import Policy

log = logging.getLogger(__name__)

CHANNEL = "ratekeeper:policy-changed"

Loader = Callable[[str], Awaitable[Policy | None]]

@dataclass(slots=True)
class _Entry:
    value: Policy | None
    loaded_at: float

class PolicyCache:
    def __init__(
        self,
        loader: Loader,
        *,
        refresh_after: float,
        expire_after: float,
        max_size: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._refresh_after = refresh_after
        self._expire_after = expire_after
        self._max_size = max_size
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()  # oldest-used first, for eviction
        self._in_flight: dict[str, asyncio.Task[Policy | None]] = {}
        # Bumped by every invalidation. A load that started before an
        # invalidation may have read the old row, so its result isn't cached.
        self._epoch = 0

    async def get(self, name: str) -> Policy | None:
        entry = self._entries.get(name)
        if entry is not None:
            age = self._clock() - entry.loaded_at
            if age < self._expire_after:
                self._entries.move_to_end(name)
                if age >= self._refresh_after:
                    self._start_load(name)  # refresh in the background; serve the current copy now
                return entry.value
        # Missing or too old to trust: the caller has to wait for the database.
        # shield() keeps one caller's cancellation from cancelling the load the others share.
        return await asyncio.shield(self._start_load(name))

    def invalidate(self, name: str) -> None:
        self._entries.pop(name, None)
        self._epoch += 1

    def invalidate_all(self) -> None:
        self._entries.clear()
        self._epoch += 1

    def _start_load(self, name: str) -> asyncio.Task[Policy | None]:
        task = self._in_flight.get(name)
        if task is None:
            task = asyncio.create_task(self._load(name))
            self._in_flight[name] = task
            task.add_done_callback(lambda done: self._finished(name, done))
        return task

    async def _load(self, name: str) -> Policy | None:
        epoch = self._epoch
        value = await self._loader(name)
        if epoch == self._epoch:
            self._entries[name] = _Entry(value, self._clock())
            self._entries.move_to_end(name)
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)
        return value

    def _finished(self, name: str, task: asyncio.Task[Policy | None]) -> None:
        if self._in_flight.get(name) is task:
            del self._in_flight[name]
        if not task.cancelled() and task.exception() is not None:
            # Retrieving the exception here also stops asyncio warning that it
            # was "never retrieved" when nobody was waiting (background refresh).
            log.warning("Loading policy %r failed; keeping any cached copy", name, exc_info=task.exception())

async def publish_change(client: redis.Redis, name: str) -> None:
    """Tell every instance that `name` changed. Best effort: on failure, the
    other instances still catch up at their next background refresh."""
    try:
        await client.publish(CHANNEL, name)
    except (RedisError, OSError):
        log.warning("Could not broadcast change of policy %r", name, exc_info=True)

async def listen_for_changes(client: redis.Redis, cache: PolicyCache, retry_seconds: float = 1.0) -> None:
    """Runs for the app's lifetime: drop cached policies that other instances changed.

    `client` must be a connection WITHOUT the short check-path timeout: a
    subscription sits idle between messages, and a 100 ms read timeout would
    break it constantly.
    """
    while True:
        try:
            async with client.pubsub() as pubsub:
                await pubsub.subscribe(CHANNEL)
                # Messages sent while we weren't subscribed are lost, so start clean.
                cache.invalidate_all()
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        data = message["data"]
                        cache.invalidate(data.decode() if isinstance(data, bytes) else str(data))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Lost the policy-change subscription; reconnecting", exc_info=True)
            await asyncio.sleep(retry_seconds)
