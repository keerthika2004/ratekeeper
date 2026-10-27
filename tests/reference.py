"""The reference model: the four algorithms again, in plain Python, with state in dicts.

It is the "obviously correct" version. Property tests check its guarantees
directly (never over-admits, Retry-After is exact, ...), and
integration/test_redis_equivalence.py checks that the Lua scripts in real
Redis return exactly the same decisions for thousands of random request
sequences. Together, that shows the Lua scripts have the same guarantees.

Keep this file line-for-line comparable with the .lua files. Python's `//`
rounds down like Lua's math.floor, and Python integers never overflow.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from ratekeeper.core.keys import redis_keys
from ratekeeper.core.models import Algorithm, Band, BandResult, Decision, Policy
from ratekeeper.core.rules import gcra_interval

Clock = Callable[[], int]


@dataclass(frozen=True, slots=True)
class _BandEval:
    """One band's evaluation, before deciding whether to charge it."""

    allowed: bool
    finish: Callable[[bool], BandResult]  # charge the band if True, then report its state


class ReferenceRateLimiter:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._hashes: dict[str, list[int]] = {}  # fixed window [w, used]; sliding counter [w, cur, prev]
        self._logs: dict[str, list[int]] = {}  # sliding log: admission times, ascending
        self._sequences: dict[str, int] = {}  # sliding log: member sequence counters
        self._tats: dict[str, int] = {}  # GCRA: theoretical arrival time

    def copy(self, clock: Clock) -> ReferenceRateLimiter:
        """An independent copy of the current state, driven by another clock.
        Lets tests probe "what would happen at time T" without changing anything."""
        other = ReferenceRateLimiter(clock)
        other._hashes = copy.deepcopy(self._hashes)
        other._logs = copy.deepcopy(self._logs)
        other._sequences = dict(self._sequences)
        other._tats = dict(self._tats)
        return other

    def check(self, policy: Policy, identity: str, cost: int) -> Decision:
        now = self._clock()
        keys = redis_keys(policy, identity)
        evals: list[_BandEval] = []
        for i, band in enumerate(policy.bands):
            match policy.algorithm:
                case Algorithm.FIXED_WINDOW:
                    evals.append(self._fixed_window(keys[i], band, now, cost))
                case Algorithm.SLIDING_WINDOW_LOG:
                    evals.append(self._sliding_log(keys[2 * i], keys[2 * i + 1], band, now, cost))
                case Algorithm.SLIDING_WINDOW_COUNTER:
                    evals.append(self._sliding_counter(keys[i], band, now, cost))
                case Algorithm.GCRA:
                    evals.append(self._gcra(keys[i], band, now, cost))
        # All-or-nothing: charge every band only if every band has room.
        all_allowed = all(e.allowed for e in evals)
        return Decision(all_allowed, tuple(e.finish(all_allowed) for e in evals))

    def _fixed_window(self, key: str, band: Band, now: int, cost: int) -> _BandEval:
        limit, window = band.limit, band.window_us
        win = now // window
        stored = self._hashes.get(key)
        used_before = stored[1] if stored is not None and stored[0] == win else 0
        allowed = used_before + cost <= limit
        window_end = (win + 1) * window

        def finish(charge: bool) -> BandResult:
            used = used_before
            if charge:
                used += cost
                self._hashes[key] = [win, used]
            reset_after = window_end - now if used > 0 else 0
            retry_after = 0 if allowed else window_end - now
            return BandResult(allowed, limit, max(0, limit - used), reset_after, retry_after)

        return _BandEval(allowed, finish)

    def _sliding_log(self, log_key: str, seq_key: str, band: Band, now: int, cost: int) -> _BandEval:
        limit, window = band.limit, band.window_us
        log = self._logs.setdefault(log_key, [])
        # A unit admitted at time t is inside the window while t > now - window.
        log[:] = [t for t in log if t > now - window]
        used_before = len(log)
        allowed = used_before + cost <= limit

        def finish(charge: bool) -> BandResult:
            used = used_before
            if charge:
                self._sequences[seq_key] = self._sequences.get(seq_key, 0) + cost
                log.extend([now] * cost)
                log.sort()
                used += cost
            reset_after = log[-1] + window - now if used > 0 else 0
            retry_after = 0
            if not allowed:
                need = used + cost - limit
                retry_after = log[need - 1] + window - now
            return BandResult(allowed, limit, max(0, limit - used), reset_after, retry_after)

        return _BandEval(allowed, finish)

    def _sliding_counter(self, key: str, band: Band, now: int, cost: int) -> _BandEval:
        limit = band.limit
        now_ms = now // 1000
        sub_ms = now - now_ms * 1000
        window = band.window_ms
        win = now_ms // window
        elapsed = now_ms - win * window
        stored = self._hashes.get(key)
        # Same window: keep both counters. Next window: last window's cur becomes prev. Older: start over.
        if stored is None:
            cur_before, prev = 0, 0
        elif stored[0] == win:
            cur_before, prev = stored[1], stored[2]
        elif stored[0] == win - 1:
            cur_before, prev = 0, stored[1]
        else:
            cur_before, prev = 0, 0
        weight = window - elapsed
        allowed = prev * weight + (cur_before + cost) * window <= limit * window

        def finish(charge: bool) -> BandResult:
            cur = cur_before
            if charge:
                cur += cost
                self._hashes[key] = [win, cur, prev]
            remaining = max(0, (limit * window - prev * weight - cur * window) // window)

            if cur > 0:
                reset_ms = 2 * window - elapsed
            elif prev > 0:
                reset_ms = weight
            else:
                reset_ms = 0
            reset_after = reset_ms * 1000 - sub_ms if reset_ms > 0 else 0

            retry_after = 0
            if not allowed:
                if cur + cost <= limit:
                    wait_ms = weight - ((limit - cur - cost) * window) // prev
                else:
                    wait_ms = weight + max(0, window - ((limit - cost) * window) // cur)
                retry_after = wait_ms * 1000 - sub_ms
            return BandResult(allowed, limit, remaining, reset_after, retry_after)

        return _BandEval(allowed, finish)

    def _gcra(self, key: str, band: Band, now: int, cost: int) -> _BandEval:
        assert band.burst is not None
        burst = band.burst
        interval = gcra_interval(band.window_us, band.limit)
        tat_before = self._tats.get(key, now)
        new_tat = max(tat_before, now) + interval * cost
        allowed = new_tat - now <= interval * burst

        def finish(charge: bool) -> BandResult:
            tat = tat_before
            if charge:
                tat = new_tat
                self._tats[key] = new_tat
            ahead = max(tat, now) - now
            remaining = max(0, (interval * burst - ahead) // interval)
            retry_after = 0 if allowed else new_tat - interval * burst - now
            return BandResult(allowed, burst, remaining, ahead, retry_after)

        return _BandEval(allowed, finish)


class AsyncReferenceLimiter:
    """The reference model behind the async RateLimiter interface, for service tests."""

    def __init__(self, clock: Clock) -> None:
        self.model = ReferenceRateLimiter(clock)

    async def check(self, policy: Policy, identity: str, cost: int) -> Decision:
        return self.model.check(policy, identity, cost)
