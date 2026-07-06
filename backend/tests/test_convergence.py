"""Integration tests for ``app.scoring.convergence``.

Real Postgres — the query groups by joined ``Source.name`` and filters
on ``published_at``, which sqlite can't exercise faithfully (no
``ROW_NUMBER``/timezone-aware datetime parity guarantees).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.scoring import convergence
from factories import make_entry, make_source


@pytest.fixture(autouse=True)
def _reset_convergence_cache():
    # The cache is a module-level dict keyed on a monotonic-time
    # bucket, so it happily leaks across tests in the same process
    # unless cleared before/after each one.
    convergence.invalidate()
    yield
    convergence.invalidate()


@pytest.mark.asyncio
async def test_counts_only_includes_slugs_seen_in_multiple_sources(db_session):
    bbc = await make_source(db_session, "bbc", category="news")
    reuters = await make_source(db_session, "reuters", category="news")
    solo = await make_source(db_session, "solo_blog", category="news")

    now = dt.datetime.now(dt.timezone.utc)
    await make_entry(db_session, bbc, "Big Story Breaks Overnight", published_at=now)
    await make_entry(db_session, reuters, "Big Story Breaks Overnight", published_at=now)
    await make_entry(db_session, solo, "Nobody Else Covered This", published_at=now)

    result = await convergence.counts(db_session, window_hours=24)

    assert result.get("big story breaks overnight") == 2
    assert "nobody else covered this" not in result


@pytest.mark.asyncio
async def test_counts_excludes_entries_outside_window(db_session):
    bbc = await make_source(db_session, "bbc", category="news")
    reuters = await make_source(db_session, "reuters", category="news")

    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    await make_entry(db_session, bbc, "Old Overlapping Headline", published_at=old)
    await make_entry(db_session, reuters, "Old Overlapping Headline", published_at=old)

    result = await convergence.counts(db_session, window_hours=24)

    assert "old overlapping headline" not in result


@pytest.mark.asyncio
async def test_counts_result_is_cached_within_ttl(db_session):
    bbc = await make_source(db_session, "bbc", category="news")
    reuters = await make_source(db_session, "reuters", category="news")
    now = dt.datetime.now(dt.timezone.utc)

    first = await convergence.counts(db_session, window_hours=24)
    assert first == {}

    # A second overlapping entry lands after the first (cached) read —
    # within the TTL window the cached (stale) empty result should
    # still come back, proving the cache is actually consulted rather
    # than re-querying every call.
    await make_entry(db_session, bbc, "Cache Me If You Can", published_at=now)
    await make_entry(db_session, reuters, "Cache Me If You Can", published_at=now)

    cached = await convergence.counts(db_session, window_hours=24)
    assert cached == {}

    convergence.invalidate()
    fresh = await convergence.counts(db_session, window_hours=24)
    assert fresh.get("cache me if you can") == 2
