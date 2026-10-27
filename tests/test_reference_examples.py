"""Hand-written scenarios, one per behaviour worth knowing.


 

The property tests cover far more cases; these exist so a reader can see each

algorithm's personality with concrete numbers.

"""


 

from __future__ import annotations


 

from ratekeeper.core.models import Algorithm, Band, Policy

from tests.reference import ReferenceRateLimiter

from tests.support import ManualClock, make_policy


 

MS = 1_000

SECOND = 1_000_000



 

def admitted(limiter: ReferenceRateLimiter, policy: Policy, attempts: int) -> int:

    return sum(limiter.check(policy, "user", 1).allowed for _ in range(attempts))



 

def setup() -> tuple[ManualClock, ReferenceRateLimiter]:

    clock = ManualClock()  # starts exactly on a whole second

    return clock, ReferenceRateLimiter(clock)



 

def test_fixed_window_lets_twice_the_limit_through_across_a_boundary() -> None:

    clock, limiter = setup()

    five_per_second = make_policy(Algorithm.FIXED_WINDOW, Band(5, 1_000))


 

    clock.advance(999 * MS)  # last millisecond of window 0

    assert admitted(limiter, five_per_second, 5) == 5

    clock.advance(1 * MS)  # first millisecond of window 1

    assert admitted(limiter, five_per_second, 5) == 5  # 10 requests within 1 ms



 

def test_sliding_log_closes_that_gap_and_knows_the_exact_wait() -> None:

    clock, limiter = setup()

    five_per_second = make_policy(Algorithm.SLIDING_WINDOW_LOG, Band(5, 1_000))


 

    clock.advance(999 * MS)

    assert admitted(limiter, five_per_second, 5) == 5

    clock.advance(1 * MS)

    rejected = limiter.check(five_per_second, "user", 1)


 

    assert not rejected.allowed

    # The oldest entry (t = 999 ms) leaves the window at t = 1999 ms, 999 ms from now.

    assert rejected.retry_after_us == 999 * MS



 

def test_sliding_counter_weighs_the_previous_window_by_time_left() -> None:

    clock, limiter = setup()

    ten_per_second = make_policy(Algorithm.SLIDING_WINDOW_COUNTER, Band(10, 1_000))


 

    assert admitted(limiter, ten_per_second, 10) == 10  # window 0 is full

    clock.advance(1_500 * MS)  # halfway through window 1

    # Estimate = 10 * (500 ms left / 1000 ms) + current = 5 + current, so 5 more fit.

    assert admitted(limiter, ten_per_second, 10) == 5



 

def test_gcra_allows_the_burst_at_once_then_paces_evenly() -> None:

    clock, limiter = setup()

    # 10 per second = one unit every 100 ms, with up to 3 back-to-back.

    paced = make_policy(Algorithm.GCRA, Band(10, 1_000, burst=3))


 

    assert admitted(limiter, paced, 3) == 3

    fourth = limiter.check(paced, "user", 1)

    assert not fourth.allowed

    assert fourth.retry_after_us == 100 * MS


 

    clock.advance(100 * MS)

    assert admitted(limiter, paced, 2) == 1  # exactly one unit refilled



 

def test_bands_are_all_or_nothing() -> None:

    clock, limiter = setup()

    # 2 per second AND 3 per 10 seconds.

    stacked = make_policy(Algorithm.FIXED_WINDOW, Band(2, 1_000), Band(3, 10_000))


 

    assert admitted(limiter, stacked, 3) == 2  # the per-second band stops the third

    clock.advance(1 * SECOND)

    assert admitted(limiter, stacked, 1) == 1  # third unit of the 10-second band


 

    rejected = limiter.check(stacked, "user", 1)

    assert not rejected.allowed

    assert rejected.bands[0].allowed  # the per-second band had room...

    assert rejected.bands[0].remaining == 1  # ...and was NOT charged

    assert not rejected.bands[1].allowed



 

def test_cost_consumes_several_units_at_once() -> None:

    _, limiter = setup()

    ten_per_second = make_policy(Algorithm.SLIDING_WINDOW_LOG, Band(10, 1_000))


 

    assert limiter.check(ten_per_second, "user", 4).allowed

    assert limiter.check(ten_per_second, "user", 4).bands[0].remaining == 2

    assert not limiter.check(ten_per_second, "user", 3).allowed

    assert limiter.check(ten_per_second, "user", 2).allowed



 

def test_identities_are_independent() -> None:

    _, limiter = setup()

    one_per_second = make_policy(Algorithm.GCRA, Band(1, 1_000))


 

    assert limiter.check(one_per_second, "alice", 1).allowed

    assert not limiter.check(one_per_second, "alice", 1).allowed

    assert limiter.check(one_per_second, "bob", 1).allowed


 