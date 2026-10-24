"""Alembic environment (async, PostgreSQL).

The URL comes from DATABASE_URL (normalised by app.config). A PostgreSQL advisory lock makes
concurrent `alembic upgrade head` runs (e.g. two containers starting during a deploy) safe.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.database import models  # noqa: F401  (register models on metadata)
from app.database.base import Base

config = context.config
target_metadata = Base.metadata
MIGRATION_LOCK_ID = 727274001


def _database_url() -> str:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is not set")
    return Settings._normalize_db_url(raw)  # type: ignore[call-arg]


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        await connection.commit()
        try:
            await connection.run_sync(_do_run_migrations)
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
            await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
