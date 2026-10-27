"""Domain errors. api/errors.py maps each one to an HTTP status."""

from __future__ import annotations

class PolicyNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"policy '{name}' not found")

class VersionMismatchError(Exception):
    """If-Match named a version that isn't current: someone else changed the policy first."""

    def __init__(self, name: str, expected: int, actual: int | None) -> None:
        current = "<none>" if actual is None else str(actual)
        super().__init__(f"policy '{name}' is at version {current}, but If-Match expected {expected}")

class CostExceedsCapacityError(ValueError):
    """A cost larger than a band can ever hold would be rejected forever, so it's a client error instead."""

    def __init__(self, policy: str, cost: int, max_cost: int) -> None:
        super().__init__(
            f"cost {cost} exceeds the largest cost policy '{policy}' can ever admit ({max_cost})"
        )
