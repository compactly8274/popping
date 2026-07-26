"""Pytest fixtures shared across the backend test suite.

Test database
-------------
Tests run against a real Postgres + pgvector instance (never sqlite —
``Entry.embedding`` is a native ``vector`` column and several queries use
Postgres-only features like ``ROW_NUMBER() OVER (PARTITION BY ...)``).
Point the suite at a throwaway database via the same ``POSTGRES_*`` env
vars the app itself reads (see ``app.config.Settings``); CI provisions
one via a ``pgvector/pgvector:pg16`` service container (see
``.github/workflows/test.yml``). For local runs, create one database
and set the env vars before invoking pytest, e.g.:

    createdb popping_test
    psql popping_test -c 'CREATE EXTENSION vector'
    POSTGRES_DB=popping_test pytest

The env vars must be set before any ``app.*`` module is imported —
``app.config.settings`` is a module-level singleton built once at
import time — hence this file does it at the very top, ahead of the
``from app...`` imports below.
"""

from __future__ import annotations

import os
import asyncio
import tempfile

os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "popping_test")
os.environ.setdefault("POSTGRES_PASSWORD", "popping_test")
os.environ.setdefault("POSTGRES_DB", "popping_test")
# Embeddings are exercised with hand-rolled fixed-length vectors in
# tests (see fixtures below) rather than the real sentence-transformers
# model — loading it would make the suite slow and network-dependent
# for no benefit, since every scorer under test takes plain lists of
# floats. Disabling it here also keeps any code path that checks
# ``settings.embedding_enabled`` from trying to spin up the model.
# (No ``POPPING_`` prefix — ``Settings`` doesn't declare one, so
# pydantic-settings matches the bare, case-insensitive field name.)
os.environ.setdefault("EMBEDDING_ENABLED", "false")
# ``app.main`` mounts StaticFiles(directory=settings.assets_dir) at
# import time (not inside the lifespan), and that constructor raises
# immediately if the directory doesn't exist. The default
# ("/app/assets") only exists inside the production container/volume —
# point it at a throwaway temp dir so importing the app doesn't depend
# on incidental host filesystem state (a bare CI runner doesn't have
# "/app"; some dev machines do, which is why this can pass locally and
# fail in CI without this).
os.environ.setdefault("ASSETS_DIR", tempfile.mkdtemp(prefix="popping-test-assets-"))

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, engine
from app.models import Base

# Markers that opt a test out of the DB-backed fixtures. A test
# with ANY of these markers skips the schema setup and the
# truncate-with-retry teardown; it's a pure unit test as far as
# the test infrastructure is concerned.
_NO_DB_MARKERS = frozenset({"no_db", "unit"})


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema(request):
    """Create every table once for the whole test session, drop at the end.

    ``Base.metadata.create_all`` (not Alembic) — same models the
    migrations are generated from, and much faster to set up per CI
    run. Assumes the ``vector`` extension already exists in the target
    database (a one-time ``CREATE EXTENSION vector``, not something
    that needs to happen per test run).

    Skipped when *every* collected test in the session is marked
    ``@pytest.mark.no_db`` (or any of its aliases — see
    ``_NO_DB_MARKERS``). The check is at session setup time, so
    a mixed run still provisions the schema. Pure unit tests
    that don't touch the DB get the speed win.
    """
    # If any collected item in this session needs the schema, do
    # the work. If NONE of them do (a pure unit-test session),
    # skip the connection entirely — no DB host required.
    items = request.session.items if hasattr(request, "session") else []
    needs_db = any(
        not any(mark.name in _NO_DB_MARKERS for mark in item.iter_markers())
        for item in items
    )
    if not needs_db:
        # Yield nothing — but the fixture is async, so we still
        # have to yield once. Tests run without schema setup.
        yield
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(request):
    """Truncate every table after each test.

    Deliberately not a rolled-back transaction: several functions under
    test (``_rescore_recent_entries``, ``_ingest``, etc.) open their own
    session via the module-level ``SessionLocal`` — a different
    connection than whatever a nested-transaction fixture would hand
    the test, so it wouldn't see uncommitted test data anyway. Truncate
    real, committed rows after the fact instead.

    Retry on ``DeadlockDetectedError``
    ---------------------------------

    A handful of flakes (slice-2 PR #45, slice-5 PR #48, and earlier
    cycles) showed the truncate occasionally deadlocking with a
    leaked background task that's still holding ``AccessShareLock``
    on ``sources`` or ``entries`` from an in-flight ``SELECT`` that
    outran the test's ``await``. The deadlock detector picks one
    transaction as victim and aborts it; our ``TRUNCATE`` is that
    victim because it requested ``AccessExclusiveLock`` last.

    The leaked task usually finishes within a few ms — it's an
    ``asyncio.create_task(...)`` from the previous test that wasn't
    awaited. A short exponential-backoff retry (max 5 attempts,
    capped at 800 ms) lets the lock drain without failing the test.
    This is the same pattern PostgreSQL recommends for any
    application-side bulk-DDL that can race with background work
    on the same connection pool.

    Skipped entirely when the test is marked ``@pytest.mark.no_db``
    (or any of its aliases — see ``_NO_DB_MARKERS``). Pure unit
    tests that don't touch the DB run with no DB round-trip in
    the teardown, which keeps them fast and lets them run on
    hosts without a Postgres reachable.
    """
    yield

    # Opt-out: a pure unit test (marked ``no_db``/``unit``)
    # doesn't need the truncate, and trying to run it would
    # force a DB connection. Skip the teardown entirely.
    test_marks = {m.name for m in request.node.iter_markers()}
    if test_marks & _NO_DB_MARKERS:
        return

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    stmt = text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")

    async def _truncate_with_retry():
        last_exc = None
        # Exponential backoff: 50ms, 100ms, 200ms, 400ms, 800ms.
        # Total worst-case wait: 1.55 s, which is shorter than the
        # cost of one CI rebuild (~30s) — a few-flake-percent
        # retry pays for itself.
        delays = [0.05, 0.1, 0.2, 0.4, 0.8]
        for delay_s in delays:
            try:
                async with engine.begin() as conn:
                    await conn.execute(stmt)
                return  # success
            except Exception as exc:
                # asyncpg wraps the deadlock as DBAPIError with
                # ``pgcode == "40P01"`` (deadlock_detected). Match on
                # the SQLSTATE code rather than the exception type
                # so we catch both the asyncpg native and the
                # SQLAlchemy-wrapped forms (and any future driver
                # wrapper that preserves the pgcode attribute).
                pgcode = getattr(getattr(exc, "orig", None), "sqlstate", None)
                if pgcode != "40P01":
                    raise
                last_exc = exc
                await asyncio.sleep(delay_s)
        # Exhausted retries — surface the original error so the test
        # fails loudly with the actual deadlock detail (which query
        # was the victim, which other tx was the blocker) rather
        # than a generic "fixture failed" traceback.
        raise last_exc

    await _truncate_with_retry()


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """A real session from the app's own session factory — same one
    ``SessionLocal()`` hands out in production code, so rows this
    fixture commits are visible to any other session the code under
    test opens for itself."""
    async with SessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def app_client():
    """An ``httpx.AsyncClient`` wired to the FastAPI app in-process,
    without running its lifespan (no scheduler, no embedder, no
    background jobs — this is a route-level test, not a full-app
    boot). Routes that depend on ``get_session`` get the real
    ``SessionLocal`` bound to the test database, same as ``db_session``.
    """
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def make_vector():
    """Build a fixed-length float vector for embedding columns without
    touching the real embedding model. Default dim (384) matches
    ``Entry.embedding``'s declared ``Vector(384)`` width."""

    def _make(seed: float, dim: int = 384) -> list[float]:
        return [seed] * dim

    return _make
