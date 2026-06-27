"""Alembic migration environment (async).

Reads DATABASE_URL from app.config so we don't duplicate the connection
string. The pgvector extension is created here (idempotent) because the
embedding column type depends on it being installed.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def _include_object(object, name, type_, reflected, compare_to):
    # Don't try to drop the pgvector extension object.
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        # Ensure pgvector is available — safe to call repeatedly.
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.commit()
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline mode not supported; use `alembic upgrade head` against the running postgres")
else:
    asyncio.run(run_migrations_online())