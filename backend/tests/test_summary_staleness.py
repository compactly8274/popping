"""Tests for the entry-summary endpoint's cache semantics,
particularly the new staleness check (cached_summary_fetched_at).

The endpoint is at /api/entries/{id}/summary. It has four
modes:
  1. First call (cached_summary is None) → compute + persist
  2. Cached + cache fresh (cached_summary_fetched_at >=
     fetched_at) → return cached
  3. Cached + cache stale (cached_summary_fetched_at <
     fetched_at) → re-run summary path
  4. Entry not found → 404

The new staleness check is the regression-risk surface. A
test that exercises the (3) path catches the case where the
backfill in migration 0021 doesn't work right, or where a
future refactor drops the timestamp comparison.

Uses the same in-process ASGI transport as
test_http_smoke.py with a FakeEntry mock at the dep level
(no DB needed).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

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
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.main import app  # noqa: E402
from app.db import get_session  # noqa: E402
from app.deps import redis_client  # noqa: E402
from app.routes.entries import (  # noqa: E402
    entry_summary_endpoint,
)


# --- Mock data -------------------------------------------------------------

class FakeEntry:
    """In-memory Entry stand-in. The endpoint reads
    ``cached_summary``, ``cached_summary_fetched_at``,
    ``fetched_at``, ``url``, ``title``, and ``meta``; writes
    ``cached_summary`` + ``cached_summary_fetched_at``. The
    other 20-ish columns aren't read so we don't model them.
    """

    def __init__(
        self,
        id: int = 1,
        url: str = "https://example.com/article",
        title: str = "Test Article",
        meta: Optional[dict] = None,
        body_text: Optional[str] = None,
        cached_summary: Optional[str] = None,
        cached_summary_fetched_at: Optional[dt.datetime] = None,
        fetched_at: Optional[dt.datetime] = None,
    ):
        self.id = id
        self.url = url
        self.title = title
        self.meta = meta or {}
        self.body_text = body_text
        self.cached_summary = cached_summary
        self.cached_summary_fetched_at = cached_summary_fetched_at
        self.fetched_at = fetched_at


class FakeSession:
    """Stand-in for the SQLAlchemy async session. The endpoint
    only calls ``session.get(Entry, id)`` — implements just that.
    """

    def __init__(self, entry: Optional[FakeEntry] = None):
        self._entry = entry

    async def get(self, _model, _id):
        return self._entry

    async def commit(self):
        pass


# --- Tests: cache semantics ------------------------------------------------

class TestCacheHitFresh:
    """cached_summary_fetched_at >= fetched_at → return cached."""

    async def test_returns_cached_when_fresh(self) -> None:
        # The cache was written AFTER the last fetch, so it's
        # still trustworthy. This is the common case (the
        # vast majority of summary requests after the first one).
        entry = FakeEntry(
            id=1,
            cached_summary="Existing summary text.",
            cached_summary_fetched_at=dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc),
            fetched_at=dt.datetime(2026, 7, 24, 11, 0, tzinfo=dt.timezone.utc),
        )
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        assert result.summary == "Existing summary text."
        assert result.cached is True

    async def test_returns_cached_when_equal_timestamps(self) -> None:
        # Edge case: cached_summary_fetched_at == fetched_at
        # (backfilled by migration 0021). The check uses >=
        # so equality counts as fresh.
        t = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc)
        entry = FakeEntry(
            id=1,
            cached_summary="Backfilled summary.",
            cached_summary_fetched_at=t,
            fetched_at=t,
        )
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        assert result.cached is True
        assert result.summary == "Backfilled summary."

    async def test_returns_cached_when_fetched_at_is_null(self) -> None:
        # Defensive: an entry that was never re-ingested has
        # fetched_at = NULL. The cache is the only "we have
        # a summary" signal, so trust it.
        entry = FakeEntry(
            id=1,
            cached_summary="Cached.",
            cached_summary_fetched_at=dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc),
            fetched_at=None,
        )
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        assert result.cached is True


class TestCacheStale:
    """cached_summary_fetched_at < fetched_at → re-run summary path."""

    async def test_re_runs_when_entry_re_ingested(self) -> None:
        # The key bug this test guards against: an entry was
        # re-ingested (fetched_at updated) AFTER its summary
        # was cached. The cached "no summary available" or
        # "got this text" answer is no longer trustworthy.
        # Without the staleness check, the user would see the
        # stale answer forever.
        entry = FakeEntry(
            id=1,
            url="https://example.com/new-article",
            title="Updated Article",
            meta={"summary": "NEW blurb from re-ingest"},
            cached_summary="",  # empty, from before re-ingest
            cached_summary_fetched_at=dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc),
            fetched_at=dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc),
        )
        # No LLM provider configured → falls through to the
        # _extract_fallback_summary path. The fallback reads
        # meta["summary"]. Should return that.
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        # The new fallback text replaces the stale empty cache.
        assert result.cached is False
        assert result.summary == "NEW blurb from re-ingest"
        # The cache was updated to reflect the new state.
        assert entry.cached_summary == "NEW blurb from re-ingest"
        assert entry.cached_summary_fetched_at is not None

    async def test_re_runs_with_no_summary_meta(self) -> None:
        # Entry was re-ingested, no summary in meta, no LLM
        # configured. The cache is rewritten with the empty
        # string (the "asked but nothing" signal). The
        # fetched_at is now newer than the cache write.
        entry = FakeEntry(
            id=1,
            meta={},  # no summary
            cached_summary="old content that no longer applies",
            cached_summary_fetched_at=dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc),
            fetched_at=dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc),
        )
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        # The cache was rewritten (old content replaced with empty).
        assert entry.cached_summary == ""
        assert entry.cached_summary_fetched_at is not None
        # Empty summary returned.
        assert result.summary == ""
        assert result.cached is False


class TestCacheMiss:
    """cached_summary is None → first-call path, run the summary pipeline."""

    async def test_first_call_persists_empty(self) -> None:
        # No LLM, no meta summary → empty string persisted.
        entry = FakeEntry(
            id=1,
            meta={},
            cached_summary=None,
            cached_summary_fetched_at=None,
        )
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        assert result.summary == ""
        assert result.cached is False
        assert entry.cached_summary == ""
        assert entry.cached_summary_fetched_at is not None

    async def test_first_call_persists_meta_summary(self) -> None:
        # No LLM but meta has a summary — that's the fallback
        # path. Persists and returns the meta summary.
        entry = FakeEntry(
            id=1,
            meta={"summary": "From the feed."},
            cached_summary=None,
        )
        result = await entry_summary_endpoint(
            entry_id=1,
            session=FakeSession(entry),
        )
        assert result.summary == "From the feed."
        assert result.cached is False
        assert entry.cached_summary == "From the feed."


class TestNotFound:
    """Entry not found → 404."""

    async def test_404_when_no_entry(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await entry_summary_endpoint(
                entry_id=999,
                session=FakeSession(entry=None),
            )
        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()
