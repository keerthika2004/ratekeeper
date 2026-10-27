"""The guarantees each algorithm promises, checked against thousands of random

request sequences.


 

Hypothesis generates the inputs. When a property fails, it shrinks the input

to the smallest sequence that still fails and prints it, so failures are easy

to debug.


 

These test the reference model; integration/test_redis_equivalence.py then

shows the Lua scripts behave identically, so the guarantees carry over.

"""


 

from __future__ import annotations


 

from collections import defaultdict


 

from hypothesis import given, settings


 

from ratekeeper.core.models import Algorithm, Band, Policy

from ratekeeper.core.rules import gcra_interval

from tests.reference import ReferenceRateLimiter

from tests.strategies import Step, policies, scenarios

from tests.support import ManualClock


 

MANY = settings(max_examples=500, deadline=None)



 

@MANY

@given(policies(), scenarios)

def test_never_admits_more_than_the_algorithm_allows(policy: Policy, steps: list[Step]) -> None:

    clock = ManualClock()

    model = ReferenceRateLimiter(clock)

    admitted: dict[str, list[tuple[int, int]]] = defaultdict(list)  # identity -> [(time, cost)]

    for step in steps:

        clock.advance(step.advance_us)

        cost = step.cost(policy)

        if model.check(policy, step.identity, cost).allowed:

            admitted[step.identity].append((clock.now_us, cost))


 

    for history in admitted.values():

        for band in policy.bands:

            _assert_within_guarantee(policy.algorithm, band, history)



 

def _assert_within_guarantee(algorithm: Algorithm, band: Band, history: list[tuple[int, int]]) -> None:

    match algorithm:

        case Algorithm.FIXED_WINDOW | Algorithm.SLIDING_WINDOW_COUNTER:

            # At most `limit` per fixed window.

            per_window: dict[int, int] = defaultdict(int)

            for at, cost in history:

                per_window[at // band.window_us] += cost

            assert all(units <= band.limit for units in per_window.values())

        case Algorithm.SLIDING_WINDOW_LOG:

            # At most `limit` in ANY window-long stretch of time.

            for end, _ in history:

                in_window = sum(cost for at, cost in history if end - band.window_us < at <= end)

                assert in_window <= band.limit

        case Algorithm.GCRA:

            # Over any stretch of length d: at most burst + d / interval units.

            assert band.burst is not None

            interval = gcra_interval(band.window_us, band.limit)

            for i in range(len(history)):

                units = 0

                for j in range(i, len(history)):

                    units += history[j][1]

                    span = history[j][0] - history[i][0]

                    assert units * interval <= band.burst * interval + span



 

@MANY

@given(policies(), scenarios)

def test_retry_after_is_exact(policy: Policy, steps: list[Step]) -> None:

    """Retrying exactly retry_after later succeeds; one microsecond earlier still fails."""

    clock = ManualClock()

    model = ReferenceRateLimiter(clock)

    for step in steps:

        clock.advance(step.advance_us)

        cost = step.cost(policy)

        decision = model.check(policy, step.identity, cost)

        if decision.allowed:

            continue

        now, retry = clock.now_us, decision.retry_after_us

        assert retry > 0

        assert model.copy(ManualClock(now + retry)).check(policy, step.identity, cost).allowed

        assert not model.copy(ManualClock(now + retry - 1)).check(policy, step.identity, cost).allowed



 

@MANY

@given(policies(max_bands=1), scenarios)

def test_reset_after_is_exact(policy: Policy, steps: list[Step]) -> None:

    """At now + reset_after the band is completely full again; one microsecond earlier it isn't."""

    clock = ManualClock()

    model = ReferenceRateLimiter(clock)

    for step in steps:

        clock.advance(step.advance_us)

        band = model.check(policy, step.identity, step.cost(policy)).bands[0]

        now, reset = clock.now_us, band.reset_after_us


 

        probe = model.copy(ManualClock(now + reset)).check(policy, step.identity, 1)

        assert probe.allowed

        assert probe.bands[0].remaining == band.limit - 1


 

        if reset > 0:

            early = model.copy(ManualClock(now + reset - 1)).check(policy, step.identity, 1)

            assert not early.allowed or early.bands[0].remaining < band.limit - 1



 

@MANY

@given(policies(), scenarios)

def test_rejections_change_nothing(policy: Policy, steps: list[Step]) -> None:

    """A rejected request charges nothing: repeating it at the same instant gives the same answer."""

    clock = ManualClock()

    model = ReferenceRateLimiter(clock)

    for step in steps:

        clock.advance(step.advance_us)

        cost = step.cost(policy)

        decision = model.check(policy, step.identity, cost)

        if not decision.allowed:

            assert model.check(policy, step.identity, cost) == decision



 

@MANY

@given(policies(), scenarios)

def test_reported_numbers_stay_in_range(policy: Policy, steps: list[Step]) -> None:

    clock = ManualClock()

    model = ReferenceRateLimiter(clock)

    for step in steps:

        clock.advance(step.advance_us)

        for band in model.check(policy, step.identity, step.cost(policy)).bands:

            assert 0 <= band.remaining <= band.limit

            assert band.reset_after_us >= 0

            assert (band.retry_after_us == 0) == band.allowed


 