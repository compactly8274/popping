"""HTTP-level smoke tests for the public routes.

These tests use the in-process ASGI transport
(``httpx.AsyncClient(transport=ASGITransport(app=app))``) so the
request goes through the full FastAPI middleware/route stack
but never leaves the test process. The DB session and Redis
client are mocked at the dep level — the conftest's
``db_session`` fixture needs a real Postgres, but for the
public-route contract we only care that the route resolves,
parses, and returns the right shape.

Not a replacement for the conftest-backed tests
(``test_preferences_anonymous.py`` already covers /api/preferences
end-to-end with a real DB). These tests fill a different gap:
the route resolution + auth gating + response shape, without the
DB ceremony. Faster to run, easier to extend to new endpoints.
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Mirror the conftest's env setup. Without these, ``from
# app.main import app`` would fail at StaticFiles mount time
# (the /app/assets default doesn't exist on most dev
# machines) and at the postgres host lookup.
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "x")
os.environ.setdefault("POSTGRES_PASSWORD", "x")
os.environ.setdefault("POSTGRES_DB", "x")
os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("ASSETS_DIR", tempfile.mkdtemp(prefix="smoke-"))

sys.path.insert(0, "/tmp/popping-review/backend")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Importing app.main wires up the FastAPI app with all
# routers (health, entries, preferences, brief, sources, etc.)
# The mocked deps override the DB + Redis at the
# get_session / redis_client dep level.
from app.main import app  # noqa: E402
from app.db import get_session  # noqa: E402
from app.deps import redis_client  # noqa: E402


# --- Mock fixtures ---------------------------------------------------------

class FakeSession:
    """Minimal async-session stand-in. Implements just enough
    of the SQLAlchemy async session surface to keep the
    health route happy.

    The health route calls:
      - session.scalar(select(func.count()).select_from(...))
      - session.scalar(select(...).order_by(...).limit(1))

    The preferences route uses session.scalars() (plural) for
    the bulk GET. The mocked values are configured per-test.
    """

    def __init__(self, sources: int = 0, entries: int = 0, last_fetch: Any = None):
        self._sources = sources
        self._entries = entries
        self._last_fetch = last_fetch

    async def scalar(self, _stmt):
        # The health route makes three scalar() calls in a fixed
        # order. We can't easily distinguish them from the
        # SQLAlchemy expression, so return the values in a
        # round-robin. This is fine for the contract test
        # (right shape, right status code).
        if not hasattr(self, "_call_index"):
            self._call_index = 0
        result = [self._sources, self._entries, self._last_fetch][self._call_index]
        self._call_index += 1
        return result

    async def scalars(self, _stmt):
        # The preferences route uses .scalars() and then
        # ``for row in rows`` directly (not .all()). For the
        # test, we return an empty list — the route maps that
        # to {items: []}. A real implementation would execute
        # the statement and return a ScalarResult; we only
        # need the iteration path to work.
        return []


@pytest_asyncio.fixture
async def app_client() -> AsyncClient:
    """An httpx AsyncClient wired to the FastAPI app in-process,
    with the DB session + Redis client mocked out. Routes that
    need a real DB return their dep's default mock value;
    routes that don't (the health route's metadata) are
    unaffected.

    The conftest's ``app_client`` fixture uses the real
    ``SessionLocal``. This one doesn't, so the test doesn't
    need a Postgres running.
    """
    fake_session = FakeSession(sources=3, entries=42, last_fetch=None)
    fake_redis = MagicMock()
    fake_redis.ping = AsyncMock(return_value=True)

    async def override_session():
        yield fake_session

    def override_redis():
        return fake_redis

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[redis_client] = override_redis
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# --- /health ---------------------------------------------------------------

class TestHealth:
    """The /health route returns DB + Redis + counts."""

    async def test_returns_200(self, app_client: AsyncClient) -> None:
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200

    async def test_response_shape(self, app_client: AsyncClient) -> None:
        resp = await app_client.get("/api/health")
        body = resp.json()
        # HealthOut schema — all keys present, right types.
        assert body["status"] in ("ok", "degraded")
        assert body["db"] in ("ok", "error")
        assert body["redis"] in ("ok", "error")
        assert isinstance(body["sources"], int)
        assert isinstance(body["entries"], int)
        # last_fetch is None when no source has been fetched yet.
        assert body["last_fetch"] is None

    async def test_redis_failure_marks_degraded(self, app_client: AsyncClient) -> None:
        # Override the Redis mock to fail. The route should
        # catch the exception, set redis=error, and downgrade
        # the overall status to "degraded".
        failing_redis = MagicMock()
        failing_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))
        app.dependency_overrides[redis_client] = lambda: failing_redis
        try:
            resp = await app_client.get("/api/health")
            body = resp.json()
            assert body["redis"] == "error"
            assert body["status"] == "degraded"
            # DB still ok (the mock is unchanged).
            assert body["db"] == "ok"
        finally:
            # Restore for the next test in the same session.
            pass  # dependency_overrides cleared at fixture teardown


# --- /api/preferences auth gating ----------------------------------------

class TestPreferencesAuth:
    """The /api/preferences routes accept anonymous (soft-auth)
    callers when LOCAL_AUTH_BYPASS is set (the default in the
    test env: bypass=false, oidc_disabled=true). The contract
    under each mode is what we test here.

    Note: the actual auth logic is exercised end-to-end in
    test_preferences_anonymous.py (which needs the real DB).
    These tests are about the HTTP-level contract: response
    shape, status codes, the JSON body field names.
    """

    async def test_list_returns_200_with_items_array(self, app_client: AsyncClient) -> None:
        # The route requires a session-resolved user. With no
        # auth and no bypass, resolve_user_id returns "anonymous"
        # and the route returns an empty items list (no rows
        # belong to that user in the mocked session).
        resp = await app_client.get("/api/preferences")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)
