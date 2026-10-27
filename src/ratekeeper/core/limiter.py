from __future__ import annotations

from typing import Protocol

from ratekeeper.core.models import Decision, Policy

class RateLimiter(Protocol):
    """Checks a request against a policy and, if it fits, charges its cost.

    Implemented by RedisRateLimiter (production). The tests have a reference
    model with identical behaviour, and a property test enforces that the two
    agree decision for decision.
    """

    async def check(self, policy: Policy, identity: str, cost: int) -> Decision: ...
