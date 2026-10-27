"""Production RateLimiter: one Lua script call per check.

The whole read-decide-write runs atomically inside Redis, which is what makes
the limit hold across any number of app instances without locks.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files

import redis.asyncio as redis

from ratekeeper.core.keys import redis_keys
from ratekeeper.core.models import Algorithm, BandResult, Decision, Policy

TimeSource = Callable[[], int | None]
"""Returns "now" in microseconds, or None to let the script read Redis' own clock."""

_VALUES_PER_BAND = 5


def redis_server_time() -> int | None:
    """Production clock: always defer to Redis' clock. Tests pass a manual clock instead."""
    return None


def load_script_sources() -> dict[Algorithm, str]:
    """One script per algorithm: prelude.lua followed by <algorithm>.lua."""
    lua = files("ratekeeper") / "lua"
    prelude = (lua / "prelude.lua").read_text(encoding="utf-8")
    return {
        algorithm: prelude + "\n" + (lua / f"{algorithm.script_name}.lua").read_text(encoding="utf-8")
        for algorithm in Algorithm
    }


class RedisRateLimiter:
    def __init__(self, client: redis.Redis, now_us: TimeSource = redis_server_time) -> None:
        self._now_us = now_us
        # redis-py runs registered scripts with EVALSHA (sends only the script's
        # SHA-1 hash) and falls back to EVAL (the full source) when Redis hasn't
        # seen the script yet, e.g. right after a Redis restart.
        self._scripts = {
            algorithm: client.register_script(source) for algorithm, source in load_script_sources().items()
        }

    async def check(self, policy: Policy, identity: str, cost: int) -> Decision:
        script = self._scripts[policy.algorithm]
        raw: list[int] = await script(keys=redis_keys(policy, identity), args=self._args(policy, cost))
        return _parse(raw, len(policy.bands))

    def _args(self, policy: Policy, cost: int) -> list[str]:
        now = self._now_us()
        args = ["" if now is None else str(now), str(cost), str(len(policy.bands))]
        for band in policy.bands:
            args += [str(band.limit), str(band.window_us), str(band.burst or 0)]
        return args


def _parse(raw: list[int], band_count: int) -> Decision:
    if len(raw) != 1 + band_count * _VALUES_PER_BAND:
        raise RuntimeError(f"unexpected script result: {raw!r}")
    bands = []
    for i in range(band_count):
        start = 1 + i * _VALUES_PER_BAND
        allowed, limit, remaining, reset_after, retry_after = raw[start : start + _VALUES_PER_BAND]
        bands.append(BandResult(allowed == 1, limit, remaining, reset_after, retry_after))
    return Decision(raw[0] == 1, tuple(bands))
