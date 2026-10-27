"""Alembic entry point: connects to Postgres and runs pending migrations.

Migrations run as their own step before the app starts (the `migrate`
service in docker-compose), not from inside each app instance. As a safety
net, an advisory lock makes concurrent `alembic upgrade` runs take turns, so
two of them can never apply the same migration at once.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from ratekeeper.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Arbitrary constant: the lock id all migration runs agree on.
MIGRATION_LOCK_ID = 7_265_412


def database_url() -> str:
    # Tests pass the URL programmatically; everyone else uses the app's setting.
    url = config.attributes.get("database_url")
    return str(url) if url else Settings().database_url


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        connection.execute(text(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})"))
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
