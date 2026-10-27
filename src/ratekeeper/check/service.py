"""The hot path: find the policy (from memory), ask Redis, and fall back to the
policy's failure mode if Redis can't answer.

Redis is protected twice:
- a timeout bounds how long any single call can take;
- a circuit breaker notices when calls keep failing and stops making them for
  a while. Without it, every request during an outage would still wait out
  the full timeout before falling back.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ratekeeper.check.breaker import CircuitBreaker, CircuitOpenError
from ratekeeper.core.limiter import RateLimiter
from ratekeeper.core.models import Decision, FailureMode, Policy
from ratekeeper.errors import CostExceedsCapacityError, PolicyNotFoundError
from ratekeeper.metrics import Metrics

log = logging.getLogger(__name__)


class PolicyLookup(Protocol):
    async def get(self, name: str) -> Policy | None: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    """`decision` is None when Redis couldn't be asked and the failure mode decided."""

    policy: Policy
    identity: str
    allowed: bool
    decision: Decision | None

    @property
    def degraded(self) -> bool:
        return self.decision is None


def is_redis_outage(error: BaseException) -> bool:
    """Redis is unreachable or too slow. Deliberately NOT every Redis error: a
    script error (ResponseError) is a bug in our code, and hiding it behind a
    failure mode would make it look like an outage instead of failing loudly."""
    return isinstance(error, RedisConnectionError | RedisTimeoutError | TimeoutError | OSError)


class CheckService:
    def __init__(
        self,
        policies: PolicyLookup,
        limiter: RateLimiter,
        breaker: CircuitBreaker,
        metrics: Metrics,
        timeout_seconds: float,
    ) -> None:
        self._policies = policies
        self._limiter = limiter
        self._breaker = breaker
        self._metrics = metrics
        self._timeout = timeout_seconds

    async def check(self, policy_name: str, identity: str, cost: int) -> CheckResult:
        policy = await self._policies.get(policy_name)
        if policy is None:
            raise PolicyNotFoundError(policy_name)
        if cost > policy.max_cost:
            raise CostExceedsCapacityError(policy_name, cost, policy.max_cost)

        try:
            decision = await self._breaker.call(partial(self._ask_redis, policy, identity, cost), is_redis_outage)
        except Exception as error:
            if not (isinstance(error, CircuitOpenError) or is_redis_outage(error)):
                raise
            # Debug level: during an outage this would otherwise log thousands of
            # identical lines per second. Breaker state changes are logged instead.
            log.debug("Redis unavailable for policy %r; using failure mode %s", policy_name, policy.failure_mode)
            allowed = policy.failure_mode is FailureMode.OPEN
            self._metrics.count_check(policy_name, "degraded_allowed" if allowed else "degraded_rejected")
            return CheckResult(policy, identity, allowed, None)

        self._metrics.count_check(policy_name, "allowed" if decision.allowed else "rejected")
        return CheckResult(policy, identity, decision.allowed, decision)

    async def _ask_redis(self, policy: Policy, identity: str, cost: int) -> Decision:
        with self._metrics.limiter_latency.time():
            # Caps the whole call, including waiting for a free connection from
            # the pool; the socket timeout alone wouldn't cover that.
            async with asyncio.timeout(self._timeout):
                return await self._limiter.check(policy, identity, cost)
