"""Plain SQL through SQLAlchemy Core.


 

Two small tables don't need an ORM, and explicit SQL keeps the Postgres

features we rely on (advisory locks, upsert, jsonb) visible. Every function

takes a connection, so the caller decides the transaction boundaries.

"""


 

from __future__ import annotations


 

import json

from dataclasses import dataclass

from datetime import datetime

from enum import StrEnum

from typing import Any


 

from sqlalchemy import text

from sqlalchemy.ext.asyncio import AsyncConnection


 

from ratekeeper.core.models import Algorithm, Band, FailureMode, Policy, PolicySpec



 

class Action(StrEnum):

    CREATED = "CREATED"

    UPDATED = "UPDATED"

    DELETED = "DELETED"



 

@dataclass(frozen=True, slots=True)

class HistoryEntry:

    version: int

    action: Action

    spec: PolicySpec

    changed_at: datetime



 

_POLICY_COLUMNS = "name, version, algorithm, failure_mode, CAST(bands AS text) AS bands"



 

async def lock_name(conn: AsyncConnection, name: str) -> None:

    """Make writers of the same policy name take turns until the transaction ends.


 

    A row lock (SELECT ... FOR UPDATE) can't do this for a policy that doesn't

    exist yet, because there's no row to lock. An advisory lock on the name

    works either way.

    """

    await conn.execute(text("SELECT 1 FROM pg_advisory_xact_lock(hashtext(:name))"), {"name": name})



 

async def find(conn: AsyncConnection, name: str) -> Policy | None:

    result = await conn.execute(text(f"SELECT {_POLICY_COLUMNS} FROM policies WHERE name = :name"), {"name": name})

    row = result.mappings().one_or_none()

    return None if row is None else _policy(row)



 

async def find_all(conn: AsyncConnection) -> list[Policy]:

    result = await conn.execute(text(f"SELECT {_POLICY_COLUMNS} FROM policies ORDER BY name"))

    return [_policy(row) for row in result.mappings()]



 

async def highest_version_ever(conn: AsyncConnection, name: str) -> int:

    """Highest version this name has ever had, including deleted incarnations; 0 if never used."""

    result = await conn.execute(

        text("SELECT COALESCE(MAX(version), 0) FROM policy_history WHERE policy_name = :name"), {"name": name}

    )

    return int(result.scalar_one())



 

async def save(conn: AsyncConnection, policy: Policy) -> None:

    await conn.execute(

        text(

            """

            INSERT INTO policies (name, version, algorithm, failure_mode, bands)

            VALUES (:name, :version, :algorithm, :failure_mode, CAST(:bands AS jsonb))

            ON CONFLICT (name) DO UPDATE SET

                version = EXCLUDED.version,

                algorithm = EXCLUDED.algorithm,

                failure_mode = EXCLUDED.failure_mode,

                bands = EXCLUDED.bands,

                updated_at = now()

            """

        ),

        _params(policy),

    )



 

async def delete(conn: AsyncConnection, name: str) -> None:

    await conn.execute(text("DELETE FROM policies WHERE name = :name"), {"name": name})



 

async def append_history(conn: AsyncConnection, policy: Policy, action: Action) -> None:

    await conn.execute(

        text(

            """

            INSERT INTO policy_history (policy_name, version, action, algorithm, failure_mode, bands)

            VALUES (:name, :version, :action, :algorithm, :failure_mode, CAST(:bands AS jsonb))

            """

        ),

        {**_params(policy), "action": action.value},

    )



 

async def history(conn: AsyncConnection, name: str, limit: int) -> list[HistoryEntry]:

    """Most recent first."""

    result = await conn.execute(

        text(

            """

            SELECT version, action, algorithm, failure_mode, CAST(bands AS text) AS bands, changed_at

            FROM policy_history WHERE policy_name = :name

            ORDER BY id DESC LIMIT :limit

            """

        ),

        {"name": name, "limit": limit},

    )

    return [

        HistoryEntry(row["version"], Action(row["action"]), _spec(row), row["changed_at"])

        for row in result.mappings()

    ]



 

def _params(policy: Policy) -> dict[str, Any]:

    return {

        "name": policy.name,

        "version": policy.version,

        "algorithm": policy.algorithm.value,

        "failure_mode": policy.failure_mode.value,

        "bands": json.dumps(

            [{"limit": b.limit, "window_ms": b.window_ms, "burst": b.burst} for b in policy.bands]

        ),

    }



 

def _policy(row: Any) -> Policy:

    return Policy(row["name"], row["version"], _spec(row))



 

def _spec(row: Any) -> PolicySpec:

    bands = tuple(Band(b["limit"], b["window_ms"], b["burst"]) for b in json.loads(row["bands"]))

    return PolicySpec(Algorithm(row["algorithm"]), FailureMode(row["failure_mode"]), bands)


 