"""A small asyncio circuit breaker.

States:
 CLOSED     normal. Outcomes are counted per second; if at least
            `failure_rate_threshold` of the calls in the last `window_seconds`
            failed (and there were at least `minimum_calls`), switch to OPEN.
 OPEN       every call fails immediately with CircuitOpenError, without
            touching Redis. After `open_seconds`, switch to HALF_OPEN.
 HALF_OPEN  let a few trial calls through. All succeed: CLOSED. Any fails: OPEN.

Why a window of *time*, not of the last N calls: at 1,000 checks per second,
"the last 20 calls" is only 20 ms of traffic. One 100 ms hiccup (a GC pause, a
busy laptop) then looks like a total outage and opens the breaker for seconds.
Benchmarks showed exactly that. Over 10 seconds, the same hiccup is ~1% of
calls, while a real outage is close to 100%.

Why write one instead of using a library? The common Python breakers
(pybreaker, aiobreaker) are thread-based or unmaintained, and this is about
100 lines that are fully unit-tested and easy to explain. Everything runs on
one event loop, so state changes need no locks: they happen in plain
synchronous code between awaits.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
   CLOSED = "closed"
   OPEN = "open"
   HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
   """The breaker is open: the call was not attempted."""


@dataclass(slots=True)
class _Bucket:
   """Outcomes of the calls made during one second."""

   second: int
   calls: int = 0
   failures: int = 0


class CircuitBreaker:
   def __init__(
       self,
       *,
       failure_rate_threshold: float = 0.5,
       window_seconds: int = 10,
       minimum_calls: int = 20,
       open_seconds: float = 5.0,
       half_open_calls: int = 3,
       clock: Callable[[], float] = time.monotonic,
       on_state_change: Callable[[State, State], None] | None = None,
   ) -> None:
       self._threshold = failure_rate_threshold
       self._window_seconds = window_seconds
       self._minimum_calls = minimum_calls
       self._open_seconds = open_seconds
       self._half_open_calls = half_open_calls
       self._clock = clock
       self._on_state_change = on_state_change

       self._state = State.CLOSED
       # Bumped on every state change. A call that started under an older
       # generation reports an outcome about a state that no longer exists, so
       # it's ignored.
       self._generation = 0
       # One bucket per second, oldest first: memory stays bounded by the
       # window length, however many calls per second there are.
       self._buckets: deque[_Bucket] = deque()
       self._opened_at = 0.0
       self._trials_started = 0
       self._trials_succeeded = 0

   @property
   def state(self) -> State:
       return self._state

   async def call[T](self, operation: Callable[[], Awaitable[T]], is_failure: Callable[[BaseException], bool]) -> T:
       """Run `operation` unless the breaker is open. Only exceptions for which
       `is_failure` is true count against the breaker; others propagate untouched."""
       generation = self._before_call()
       try:
           result = await operation()
       except BaseException as error:
           if is_failure(error):
               self._on_failure(generation)
           else:
               self._on_neutral(generation)
           raise
       self._on_success(generation)
       return result

   def _before_call(self) -> int:
       if self._state is State.OPEN:
           if self._clock() - self._opened_at < self._open_seconds:
               raise CircuitOpenError()
           self._transition(State.HALF_OPEN)
       if self._state is State.HALF_OPEN:
           if self._trials_started >= self._half_open_calls:
               raise CircuitOpenError()
           self._trials_started += 1
       return self._generation

   def _on_success(self, generation: int) -> None:
       if generation != self._generation:
           return
       if self._state is State.HALF_OPEN:
           self._trials_succeeded += 1
           if self._trials_succeeded >= self._half_open_calls:
               self._transition(State.CLOSED)
       else:
           self._record(failed=False)

   def _on_failure(self, generation: int) -> None:
       if generation != self._generation:
           return
       if self._state is State.HALF_OPEN:
           self._transition(State.OPEN)
           return
       self._record(failed=True)
       calls = sum(bucket.calls for bucket in self._buckets)
       failures = sum(bucket.failures for bucket in self._buckets)
       if calls >= self._minimum_calls and failures / calls >= self._threshold:
           self._transition(State.OPEN)

   def _on_neutral(self, generation: int) -> None:
       # Not a Redis problem (e.g. a bug): free the trial slot without judging Redis.
       if generation == self._generation and self._state is State.HALF_OPEN:
           self._trials_started -= 1

   def _record(self, *, failed: bool) -> None:
       second = int(self._clock())
       if not self._buckets or self._buckets[-1].second != second:
           self._buckets.append(_Bucket(second))
       bucket = self._buckets[-1]
       bucket.calls += 1
       bucket.failures += failed
       # Forget seconds that have left the window.
       while self._buckets[0].second <= second - self._window_seconds:
           self._buckets.popleft()

   def _transition(self, new_state: State) -> None:
       old_state, self._state = self._state, new_state
       self._generation += 1
       self._buckets.clear()
       self._trials_started = 0
       self._trials_succeeded = 0
       if new_state is State.OPEN:
           self._opened_at = self._clock()
       if self._on_state_change is not None:
           self._on_state_change(old_state, new_state)
 