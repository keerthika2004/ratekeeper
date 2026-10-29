"""The circuit breaker's state machine, driven by a fake clock."""

from __future__ import annotations

import pytest

from ratekeeper.check.breaker import CircuitBreaker, CircuitOpenError, State


class FakeClock:
   def __init__(self) -> None:
       self.now = 0.0

   def __call__(self) -> float:
       return self.now


def counts(error: BaseException) -> bool:
   return isinstance(error, ConnectionError)


async def ok() -> str:
   return "ok"


async def outage() -> str:
   raise ConnectionError("redis down")


async def bug() -> str:
   raise KeyError("bug")


def make(clock: FakeClock, transitions: list[tuple[State, State]] | None = None) -> CircuitBreaker:
   return CircuitBreaker(
       failure_rate_threshold=0.5,
       window_seconds=10,
       minimum_calls=4,
       open_seconds=5,
       half_open_calls=2,
       clock=clock,
       on_state_change=None if transitions is None else lambda old, new: transitions.append((old, new)),
   )


async def fail(breaker: CircuitBreaker, times: int) -> None:
   for _ in range(times):
       with pytest.raises(ConnectionError):
           await breaker.call(outage, counts)


async def succeed(breaker: CircuitBreaker, times: int) -> None:
   for _ in range(times):
       assert await breaker.call(ok, counts) == "ok"


async def test_opens_once_enough_recent_calls_fail() -> None:
   breaker = make(FakeClock())

   await fail(breaker, 3)
   assert breaker.state is State.CLOSED  # 3 calls: below the minimum needed to judge
   await fail(breaker, 1)
   assert breaker.state is State.OPEN

   with pytest.raises(CircuitOpenError):
       await breaker.call(ok, counts)


async def test_a_low_failure_rate_keeps_it_closed() -> None:
   breaker = make(FakeClock())
   await succeed(breaker, 3)
   await fail(breaker, 2)  # 2 of 5 = 40% < 50%

   assert breaker.state is State.CLOSED


async def test_a_brief_burst_of_timeouts_under_heavy_traffic_does_not_open_it() -> None:
   """Regression test for the benchmark finding: at 1,000 checks/s, one 100 ms
   stall times out ~100 checks at once. Judged over 10 s of traffic that's ~1%
   of calls, not an outage, so the breaker must stay closed."""
   clock = FakeClock()
   breaker = make(clock)
   for second in range(10):
       clock.now = second + 0.5
       await succeed(breaker, 1_000)
       if second == 5:
           await fail(breaker, 100)

   assert breaker.state is State.CLOSED


async def test_failures_older_than_the_window_are_forgotten() -> None:
   clock = FakeClock()
   breaker = make(clock)
   await fail(breaker, 3)

   clock.now = 11  # the first three failures have left the 10 s window
   await fail(breaker, 3)

   assert breaker.state is State.CLOSED  # only 3 calls in the window: not enough to judge


async def test_half_open_trials_close_it_again() -> None:
   clock = FakeClock()
   transitions: list[tuple[State, State]] = []
   breaker = make(clock, transitions)
   await fail(breaker, 4)

   clock.now = 5  # open period over
   assert await breaker.call(ok, counts) == "ok"
   assert breaker.state is State.HALF_OPEN
   assert await breaker.call(ok, counts) == "ok"

   assert breaker.state is State.CLOSED
   assert transitions == [
       (State.CLOSED, State.OPEN),
       (State.OPEN, State.HALF_OPEN),
       (State.HALF_OPEN, State.CLOSED),
   ]


async def test_a_failed_trial_reopens_it() -> None:
   clock = FakeClock()
   breaker = make(clock)
   await fail(breaker, 4)

   clock.now = 5
   await fail(breaker, 1)

   assert breaker.state is State.OPEN
   with pytest.raises(CircuitOpenError):
       await breaker.call(ok, counts)


async def test_errors_that_are_not_outages_pass_through_without_counting() -> None:
   breaker = make(FakeClock())
   for _ in range(10):
       with pytest.raises(KeyError):
           await breaker.call(bug, counts)

   assert breaker.state is State.CLOSED
 