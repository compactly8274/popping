"""Cross-source convergence helper.

One scan over the recent-entry window, grouped by ``title_slug``, that
returns the number of distinct sources mentioning each slug within the
window. Used by:

  - ``/api/foryou`` to compute the convergence boost at query time.
  - the scheduler's ``_check_convergence`` job for periodic alerts.
  - the brief generator's source-overlap logic.

The function used to be copy-pasted across all three call sites with
slight variations; consolidating it here also gives us one place to
add the 30-second TTL cache that the audit flagged as missing.

The cache key is the ``(window_hours,)`` tuple — the SQL doesn't
filter by user or category, so any caller asking for the same window
gets the same answer. We bust the cache by ``time.monotonic``-based
TTL only; the eventual truth source is the entries table, which
changes at ingest time (frequent enough that 30s is plenty fresh).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Entry, Source
from app.scoring import composite as composite_scorer


_TTL_SECONDS = 30.0

# Process-local cache. Single-process deployments (the only shape we
# support today) hit the cache 100% of the time after the first call.
# A multi-worker deploy would have N caches; that's fine because the
# underlying scan is read-only and identical.
_cache: dict[tuple[int, float, int], dict[str, int]] = {}
_cache_lock = asyncio.Lock()


def _cache_key(window_hours: int) -> tuple[int, float, int]:
    """Quantize the TTL so two callers inside the same 30s window
    share a cache entry. Without the floor, every call would have a
    fresh key and never hit. The ``scan_cap`` is part of the key
    so a runtime config change doesn't serve stale data from the
    old shape of the scan.
    """
    return (window_hours, time.monotonic() // _TTL_SECONDS, settings.convergence_scan_cap)


async def counts(
    session: AsyncSession,
    window_hours: int,
) -> dict[str, int]:
    """Map ``title_slug`` → number of distinct sources within the
    window. Only slugs seen in 2+ sources are returned (anything
    with source_count == 1 won't get a boost anyway).
    """
    key = _cache_key(window_hours)
    # Slice 26 (singleflight): the previous code released the lock
    # between the cache read and the SQL scan, so two concurrent
    # callers both saw a cache miss and both ran the scan. With a
    # high-concurrency endpoint like ``/api/foryou`` firing while a
    # brief is being generated, that's a thundering-herd that doubles
    # the convergence-scan DB load on every miss.
    #
    # Hold the lock around the full miss path (read → scan → write)
    # so the first caller does the scan + populates the cache, and
    # concurrent callers see the populated cache and return without
    # scanning. Cost: a brief serialization on the very first miss in
    # a 30s TTL window. Win: every concurrent caller after that hits
    # the cache without touching the DB.
    async with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
        # Cap the row count to the most recent ``convergence_scan_cap``
        # entries in the window. Without this, a 24h window that catches
        # 100k entries (a backlog after a deploy, a news-heavy weekend)
        # pulls every row just to find the last 5k that the convergence
        # boost actually cares about. The boost is a "hot right now"
        # signal; old rows don't move the needle regardless of how
        # many sources cover them, and a per-row ``title_slug`` call in
        # Python (below) is the second cost we're saving. ``scan_cap=0``
        # disables the cap as an escape hatch (a debugging session, a
        # regression that needs the full historical view).
        scan_cap = settings.convergence_scan_cap
        if scan_cap > 0:
            recent = (
                select(Entry.id, Entry.title, Entry.source_id)
                .where(Entry.published_at >= since)
                .order_by(Entry.published_at.desc().nullslast(), Entry.id.desc())
                .limit(scan_cap)
                .subquery()
            )
            stmt = (
                select(recent.c.title, Source.name)
                .join(Source, Source.id == recent.c.source_id)
            )
        else:
            stmt = (
                select(Entry.title, Source.name)
                .join(Source, Entry.source_id == Source.id)
                .where(Entry.published_at >= since)
            )
        rows = (await session.execute(stmt)).all()
        bucket: dict[str, set[str]] = defaultdict(set)
        for title, source_name in rows:
            slug = composite_scorer.title_slug(title)
            if not slug:
                continue
            bucket[slug].add(source_name)
        result = {slug: len(srcs) for slug, srcs in bucket.items() if len(srcs) > 1}

        # Write the cache and evict older entries on each insert. The
        # TTL quantizes keys so there are at most ``1 + a tiny jitter``
        # keys live at any moment, but pruning is cheap insurance.
        _cache[key] = result
        for old in list(_cache.keys()):
            if old[1] < key[1] - 2:  # older than ~60s — drop
                _cache.pop(old, None)
    return result


def invalidate() -> None:
    """Drop the cache. Called from tests; not wired into the ingest
    path because the TTL is short enough that a stale read is
    fine."""
    _cache.clear()