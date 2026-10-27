"""The check path's behaviour around failures, with no Redis involved."""

from __future__ import annotations

import pytest
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ratekeeper.check.breaker import CircuitBreaker
from ratekeeper.check.service import CheckService
from ratekeeper.core.models import Algorithm, Band, Decision, FailureMode, Policy, PolicySpec
from ratekeeper.errors import CostExceedsCapacityError, PolicyNotFoundError
from ratekeeper.metrics import Metrics
from tests.reference import AsyncReferenceLimiter
from tests.support import ManualClock


def _policy(name: str, failure_mode: FailureMode) -> Policy:
    return Policy(name, 1, PolicySpec(Algorithm.FIXED_WINDOW, failure_mode, (Band(3, 60_000),)))


READS = _policy("reads", FailureMode.OPEN)
LOGINS = _policy("logins", FailureMode.CLOSED)


class Lookup:
    async def get(self, name: str) -> Policy | None:
        return {"reads": READS, "logins": LOGINS}.get(name)


class FailingLimiter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def check(self, policy: Policy, identity: str, cost: int) -> Decision:
        self.calls += 1
        raise self.error


def service(limiter: object, metrics: Metrics | None = None) -> CheckService:
    breaker = CircuitBreaker(window_size=20, minimum_calls=10, failure_rate_threshold=0.5, open_seconds=60)
    return CheckService(Lookup(), limiter, breaker, metrics or Metrics(), timeout_seconds=1)  # type: ignore[arg-type]


async def test_normal_decisions_pass_through() -> None:
    checks = service(AsyncReferenceLimiter(ManualClock()))

    assert (await checks.check("reads", "u", 3)).allowed
    rejected = await checks.check("reads", "u", 1)
    assert not rejected.allowed
    assert not rejected.degraded


async def test_when_redis_fails_the_failure_mode_decides() -> None:
    checks = service(FailingLimiter(RedisTimeoutError("Redis did not answer in time")))

    open_result = await checks.check("reads", "u", 1)
    closed_result = await checks.check("logins", "u", 1)

    assert open_result.degraded and open_result.allowed
    assert closed_result.degraded and not closed_result.allowed


async def test_breaker_stops_calling_redis_once_it_keeps_failing() -> None:
    limiter = FailingLimiter(ConnectionRefusedError("redis down"))
    checks = service(limiter)

    for _ in range(30):
        assert (await checks.check("reads", "u", 1)).degraded
    # 10 calls is the minimum the breaker needs to judge; after that it opens
    # and the remaining 20 checks fail fast without touching Redis.
    assert limiter.calls == 10


async def test_script_errors_are_bugs_not_outages() -> None:
    checks = service(FailingLimiter(ResponseError("ERR Error running script")))

    with pytest.raises(ResponseError):
        await checks.check("reads", "u", 1)


async def test_unknown_policy_is_an_error() -> None:
    with pytest.raises(PolicyNotFoundError):
        await service(FailingLimiter(RuntimeError())).check("nope", "u", 1)


async def test_cost_above_capacity_is_rejected_before_asking_redis() -> None:
    limiter = FailingLimiter(RuntimeError("should not be called"))

    with pytest.raises(CostExceedsCapacityError):
        await service(limiter).check("reads", "u", 4)
    assert limiter.calls == 0


async def test_outcomes_are_counted_per_policy() -> None:
    metrics = Metrics()
    checks = service(AsyncReferenceLimiter(ManualClock()), metrics)
    for _ in range(5):
        await checks.check("reads", "u", 1)

    def count(outcome: str) -> float | None:
        return metrics.registry.get_sample_value("ratekeeper_checks_total", {"policy": "reads", "outcome": outcome})

    assert count("allowed") == 3
    assert count("rejected") == 2
