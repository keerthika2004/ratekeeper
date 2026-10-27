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
        window_size=10,
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
    for _ in range(3):
        assert await breaker.call(ok, counts) == "ok"
    await fail(breaker, 2)  # 2 of 5 = 40% < 50%

    assert breaker.state is State.CLOSED

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
