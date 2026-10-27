"""Create, update and delete policies.

Writes use PUT semantics: the client sends the complete desired policy. If it
equals what's stored, nothing changes (no new version, no history row), so
retrying a PUT is always safe. That's why this API needs no Idempotency-Key.

An optional If-Match version gives optimistic locking: "apply my change only
if the policy is still at the version I last read". This stops two admins
from silently overwriting each other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from ratekeeper.core.models import Algorithm, Band, FailureMode, Policy
from ratekeeper.core.rules import normalize, validate_name
from ratekeeper.errors import PolicyNotFoundError, VersionMismatchError
from ratekeeper.policies import repository
from ratekeeper.policies.repository import Action, HistoryEntry

OnChange = Callable[[str], Awaitable[None]]

@dataclass(frozen=True, slots=True)
class PutResult:
    policy: Policy
    created: bool
    changed: bool

class PolicyService:
    def __init__(self, engine: AsyncEngine, on_change: OnChange) -> None:
        self._engine = engine
        self._on_change = on_change

    async def put(
        self,
        name: str,
        algorithm: Algorithm,
        failure_mode: FailureMode | None,
        bands: Sequence[Band],
        if_match: int | None,
    ) -> PutResult:
        validate_name(name)
        spec = normalize(algorithm, failure_mode, bands)

        async with self._engine.begin() as conn:  # one transaction; commits when the block ends
            await repository.lock_name(conn, name)
            current = await repository.find(conn, name)
            _check_version(name, if_match, current)

            if current is not None and current.spec == spec:
                return PutResult(current, created=False, changed=False)

            previous = current.version if current is not None else 0
            version = max(await repository.highest_version_ever(conn, name), previous) + 1
            policy = Policy(name, version, spec)
            await repository.save(conn, policy)
            await repository.append_history(
                conn, policy, Action.CREATED if current is None else Action.UPDATED
            )

        # Only now, after the commit. Announcing earlier could let another
        # request reload the old row and cache it again.
        await self._on_change(name)
        return PutResult(policy, created=current is None, changed=True)

    async def delete(self, name: str, if_match: int | None) -> None:
        async with self._engine.begin() as conn:
            await repository.lock_name(conn, name)
            current = await repository.find(conn, name)
            if current is None:
                raise PolicyNotFoundError(name)
            _check_version(name, if_match, current)
            await repository.delete(conn, name)
            await repository.append_history(conn, current, Action.DELETED)
        await self._on_change(name)

    async def get(self, name: str) -> Policy:
        async with self._engine.connect() as conn:
            policy = await repository.find(conn, name)
        if policy is None:
            raise PolicyNotFoundError(name)
        return policy

    async def list_all(self) -> list[Policy]:
        async with self._engine.connect() as conn:
            return await repository.find_all(conn)

    async def history(self, name: str, limit: int) -> list[HistoryEntry]:
        async with self._engine.connect() as conn:
            return await repository.history(conn, name, limit)

async def load_policy(engine: AsyncEngine, name: str) -> Policy | None:
    """The policy cache's loader."""
    async with engine.connect() as conn:
        return await repository.find(conn, name)

def _check_version(name: str, if_match: int | None, current: Policy | None) -> None:
    if if_match is None:
        return
    if current is None or current.version != if_match:
        raise VersionMismatchError(name, if_match, current.version if current else None)
