"""Real Redis and Postgres in Docker (Testcontainers), shared by every
integration test in the run and removed when the run ends.

These tests need Docker. Without it they fail (not skip), so a missing Docker
daemon can never quietly turn into a green build. To run only the unit tests:
`pytest -m "not integration"`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import redis.asyncio as redis
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7.4-alpine") as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A fresh Postgres with all migrations applied, exactly as production gets them."""
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        url = container.get_connection_url()
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        yield url


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[redis.Redis]:
    # Same kind of pool as production (see main.py): bursts wait for a connection.
    client = redis.Redis.from_pool(
        redis.BlockingConnectionPool.from_url(
            redis_url,
            max_connections=50,
        )
    )
    await client.flushdb()
    yield client
    await client.aclose()
