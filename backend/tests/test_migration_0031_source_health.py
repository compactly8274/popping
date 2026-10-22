"""Migration 0031 (feed health): the one new head on top of 0030, and a
real upgrade / downgrade / upgrade against a scratch database.

Nothing else in the suite runs Alembic: conftest builds the schema with
``Base.metadata.create_all``. So the head check reads the migration
scripts, and the cycle runs the ``alembic`` command line in a
subprocess, the way the container does at startup (``alembic upgrade
head && uvicorn``), against a database made for the test and dropped
after it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import engine

BACKEND = Path(__file__).resolve().parents[1]

# The three columns 0031 adds, with their information_schema type.
_COLUMNS = {
    "last_attempt_at": "timestamp with time zone",
    "last_item_count": "integer",
    "last_new_count": "integer",
}


def _scripts() -> ScriptDirectory:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    # alembic.ini predates the option; set it so reading the scripts
    # doesn't warn (``prepend_sys_path = .`` splits the same way).
    cfg.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(cfg)


@pytest.mark.no_db
def test_0031_is_the_only_head_and_revises_0030():
    scripts = _scripts()

    assert scripts.get_heads() == ["0031"]
    assert scripts.get_revision("0031").down_revision == "0030"
    # Nothing else branches off 0030.
    assert [r.revision for r in scripts.walk_revisions() if r.down_revision == "0030"] == ["0031"]


def _alembic(database: str, *args: str) -> str:
    """Run ``alembic <args>`` against ``database``, as the container
    does: from the backend directory, with the app's settings."""
    env = {
        **os.environ,
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password,
        "POSTGRES_DB": database,
        "PYTHONPATH": str(BACKEND),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-3000:]}"
    return result.stdout


async def _admin(sql: str) -> None:
    """CREATE / DROP DATABASE, which can't run inside a transaction."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(sql))


async def _columns(scratch) -> dict[str, tuple[str, str, object]]:
    async with scratch.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'sources' "
                    "AND column_name = ANY(:names)"
                ),
                {"names": list(_COLUMNS)},
            )
        ).all()
    return {name: (data_type, nullable, default) for name, data_type, nullable, default in rows}


async def _version(scratch) -> str:
    async with scratch.connect() as conn:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()


async def test_upgrade_downgrade_upgrade_on_a_scratch_database():
    name = f"popping_mig0031_{uuid.uuid4().hex[:10]}"
    await _admin(f'CREATE DATABASE "{name}"')
    scratch = create_async_engine(
        make_url(settings.database_url).set(database=name), poolclass=NullPool
    )
    try:
        async with scratch.connect() as conn:
            await conn.execute(text("CREATE EXTENSION vector"))
            await conn.commit()

        _alembic(name, "upgrade", "head")
        assert await _version(scratch) == "0031"
        # Nullable, no default, the right types: an additive change that
        # older code can ignore.
        assert await _columns(scratch) == {
            column: (data_type, "YES", None) for column, data_type in _COLUMNS.items()
        }
        async with scratch.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO sources (name, type, category, url, last_attempt_at, "
                    "last_item_count, last_new_count) "
                    "VALUES ('mig_feed', 'rss', 'news', 'https://feed.test/rss', now(), 12, 3)"
                )
            )
            await conn.commit()

        _alembic(name, "downgrade", "0030")
        assert await _version(scratch) == "0030"
        assert await _columns(scratch) == {}
        # The downgrade drops the three columns and nothing else.
        async with scratch.connect() as conn:
            kept = (await conn.execute(text("SELECT name FROM sources"))).scalars().all()
        assert kept == ["mig_feed"]

        _alembic(name, "upgrade", "head")
        assert await _version(scratch) == "0031"
        assert set(await _columns(scratch)) == set(_COLUMNS)
        # No backfill: an existing row starts with NULLs.
        async with scratch.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT last_attempt_at, last_item_count, last_new_count "
                        "FROM sources WHERE name = 'mig_feed'"
                    )
                )
            ).one()
        assert tuple(row) == (None, None, None)
    finally:
        await scratch.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
