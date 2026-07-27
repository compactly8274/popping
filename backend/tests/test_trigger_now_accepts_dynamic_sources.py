"""Slice-16 wire tests: trigger_now + ingest_endpoint accept dynamic sources.

The /api/ingest/{name} endpoint and trigger_now() function only
checked ``list_sources()`` (the in-memory plugin class registry),
which excluded dynamic ``Source`` rows the user has added via
Add custom / Track anyway. The fix: if the name isn't in
the registry, fall through to a DB lookup and dispatch via
``_plugin_for(row)`` + ``_ingest`` — the same code path the
scheduled tick uses.

These tests don't touch Postgres — they mock the DB session,
the registry, and ``_ingest`` at the module boundary. The
behavioral contract:

  1. Registry hit (name in list_sources()) -> _ingest called
     with the class. Fast path, no DB session opened.
  2. DB row hit (name not in registry, but Source row with
     that name exists) -> _ingest called with the dynamic
     plugin instance returned by ``_plugin_for(row)``.
  3. Neither registry nor DB -> returns ``{"error": "unknown source", ...}``.
  4. DB row exists but _plugin_for returns None (unsupported
     ``type``) -> returns ``{"error": "unsupported source type: '...', ...}``.

The same fallback is used in the ``ingest_endpoint`` route:
it accepts the name as long as the registry OR the DB has it,
so a dynamic source POST /api/ingest/{name} no longer 404s.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))


# ---------------------------------------------------------------------------
# Imports need the conftest env vars set BEFORE app modules load.
# ---------------------------------------------------------------------------
import os
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "popping_test")
os.environ.setdefault("POSTGRES_PASSWORD", "popping_test")
os.environ.setdefault("POSTGRES_DB", "popping_test")
os.environ.setdefault("EMBEDDING_ENABLED", "false")

from app import scheduler  # noqa: E402
from app.routes import ingest as ingest_route  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Registry hit goes to _ingest with the class, no DB session
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_trigger_now_registry_hit_uses_class_directly():
    """When the source name is a registered plugin class, trigger_now
    dispatches to ``_ingest`` with the class object. The DB session
    is never opened.
    """
    fake_class = MagicMock(name="BbcNewsPlugin")
    fake_class.name = "bbc_news"

    with patch.object(scheduler, "list_sources", return_value={"bbc_news": fake_class}), \
         patch.object(scheduler, "_ingest", new=AsyncMock(return_value={"source": "bbc_news", "fetched": 5, "inserted": 5, "duplicates": 0, "error": None})) as mock_ingest, \
         patch("app.db.SessionLocal") as mock_session:
        result = await scheduler.trigger_now("bbc_news")

    assert result["source"] == "bbc_news"
    assert result["fetched"] == 5
    mock_ingest.assert_awaited_once_with(fake_class)
    # No DB session opened on the fast path.
    mock_session.assert_not_called()


# ---------------------------------------------------------------------------
# 2. DB row hit dispatches via _plugin_for
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_trigger_now_db_hit_uses_plugin_for_row():
    """When the source name is NOT a registered plugin class but IS
    a Source row in the DB, trigger_now queries the DB and
    dispatches via ``_plugin_for(row)``.
    """
    fake_plugin_instance = MagicMock(name="DynamicRssPluginInstance")
    fake_row = SimpleNamespace(
        id=42,
        name="my_custom_feed",
        type="rss",
        url="https://example.com/feed.xml",
        category="news",
    )

    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=fake_row)
    fake_session_ctx = MagicMock()
    fake_session_ctx.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(scheduler, "list_sources", return_value={}), \
         patch.object(scheduler, "_ingest", new=AsyncMock(return_value={"source": "my_custom_feed", "fetched": 3, "inserted": 3, "duplicates": 0, "error": None})) as mock_ingest, \
         patch.object(scheduler, "_plugin_for", return_value=fake_plugin_instance) as mock_pf, \
         patch("app.db.SessionLocal", return_value=fake_session_ctx):
        result = await scheduler.trigger_now("my_custom_feed")

    assert result["source"] == "my_custom_feed"
    mock_pf.assert_called_once_with(fake_row)
    mock_ingest.assert_awaited_once_with(fake_plugin_instance)
    fake_session.scalar.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Neither registry nor DB -> "unknown source"
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_trigger_now_returns_unknown_source_when_not_in_db():
    """When the source name is in neither the registry nor the DB,
    trigger_now returns the same shape it always did: an
    ``error``-bearing 200 with the legacy "unknown source" string.
    """
    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session_ctx = MagicMock()
    fake_session_ctx.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(scheduler, "list_sources", return_value={}), \
         patch("app.db.SessionLocal", return_value=fake_session_ctx):
        result = await scheduler.trigger_now("nope")

    assert result == {
        "source": "nope",
        "error": "unknown source",
        "fetched": 0,
        "inserted": 0,
        "duplicates": 0,
    }


# ---------------------------------------------------------------------------
# 4. DB row hit but _plugin_for returns None -> unsupported type
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_trigger_now_db_hit_with_unsupported_type_returns_error():
    """If a Source row exists with a ``type`` that ``_plugin_for``
    doesn't handle (e.g. a future migration leaves a row in a
    state the dispatcher doesn't know about), trigger_now returns
    a descriptive error rather than crashing or returning
    silently-empty results.
    """
    fake_row = SimpleNamespace(
        id=99,
        name="legacy_type_row",
        type="rss_legacy",  # _plugin_for returns None for this
    )

    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=fake_row)
    fake_session_ctx = MagicMock()
    fake_session_ctx.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(scheduler, "list_sources", return_value={}), \
         patch.object(scheduler, "_plugin_for", return_value=None), \
         patch("app.db.SessionLocal", return_value=fake_session_ctx):
        result = await scheduler.trigger_now("legacy_type_row")

    assert result["error"] == "unsupported source type: 'rss_legacy'"
    assert result["source"] == "legacy_type_row"
    assert result["fetched"] == 0


# ---------------------------------------------------------------------------
# 5. ingest_endpoint accepts dynamic sources (no 404 for DB rows)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_ingest_endpoint_accepts_dynamic_source():
    """The HTTP endpoint should 404 ONLY when the name is in
    neither the registry nor the DB. A dynamic ``Source`` row
    should make it through to ``trigger_now``, which then
    dispatches via ``_plugin_for``.
    """
    from fastapi import HTTPException

    fake_row = SimpleNamespace(
        id=42,
        name="chilliwackprogress",
        type="generic_scrape",
        url="https://theprogress.com",
    )
    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=fake_row)
    fake_session_ctx = MagicMock()
    fake_session_ctx.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_ctx.__aexit__ = AsyncMock(return_value=None)

    expected_summary = {"source": "chilliwackprogress", "fetched": 7, "inserted": 7, "duplicates": 0, "error": None}

    with patch.object(ingest_route, "list_sources", return_value={}), \
         patch.object(ingest_route, "trigger_now", new=AsyncMock(return_value=expected_summary)), \
         patch("app.db.SessionLocal", return_value=fake_session_ctx):
        result = await ingest_route.ingest_endpoint("chilliwackprogress")

    assert result.source == "chilliwackprogress"
    assert result.fetched == 7


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_ingest_endpoint_404s_only_when_not_in_db_or_registry():
    """The 404 case is preserved: a name in neither the registry
    nor the DB still returns ``detail='unknown source'``.
    """
    from fastapi import HTTPException

    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session_ctx = MagicMock()
    fake_session_ctx.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(ingest_route, "list_sources", return_value={}), \
         patch("app.db.SessionLocal", return_value=fake_session_ctx):
        with pytest.raises(HTTPException) as excinfo:
            await ingest_route.ingest_endpoint("definitely_not_a_source")

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "unknown source"


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_ingest_endpoint_skips_db_check_when_registry_hit():
    """When the name is in the registry, the endpoint should NOT
    open a DB session. The fast path stays fast.
    """
    fake_class = MagicMock()
    fake_class.name = "hn_top"

    expected_summary = {"source": "hn_top", "fetched": 30, "inserted": 30, "duplicates": 0, "error": None}

    with patch.object(ingest_route, "list_sources", return_value={"hn_top": fake_class}), \
         patch.object(ingest_route, "trigger_now", new=AsyncMock(return_value=expected_summary)), \
         patch("app.db.SessionLocal") as mock_session:
        result = await ingest_route.ingest_endpoint("hn_top")

    assert result.fetched == 30
    mock_session.assert_not_called()
