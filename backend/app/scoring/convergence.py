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

from app.models import Entry, Source
from app.scoring import composite as composite_scorer


_TTL_SECONDS = 30.0

# Process-local cache. Single-process deployments (the only shape we
# support today) hit the cache 100% of the time after the first call.
# A multi-worker deploy would have N caches; that's fine because the
# underlying scan is read-only and identical.
_cache: dict[tuple[int, float], dict[str, int]] = {}
_cache_lock = asyncio.Lock()


def _cache_key(window_hours: int) -> tuple[int, float]:
    """Quantize the TTL so two callers inside the same 30s window
    share a cache entry. Without the floor, every call would have a
    fresh key and never hit."""
    return (window_hours, time.monotonic() // _TTL_SECONDS)


async def counts(
    session: AsyncSession,
    window_hours: int,
) -> dict[str, int]:
    """Map ``title_slug`` → number of distinct sources within the
    window. Only slugs seen in 2+ sources are returned (anything
    with source_count == 1 won't get a boost anyway).
    """
    key = _cache_key(window_hours)
    async with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
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

    async with _cache_lock:
        _cache[key] = result
        # Evict older entries on each insert. The TTL quantizes keys
        # so there are at most ``1 + a tiny jitter`` keys live at any
        # moment, but pruning is cheap insurance.
        for old in list(_cache.keys()):
            if old != key and _cache[old] is result:
                pass
            if old[1] < key[1] - 2:  # older than ~60s — drop
                _cache.pop(old, None)
    return result


def invalidate() -> None:
    """Drop the cache. Called from tests; not wired into the ingest
    path because the TTL is short enough that a stale read is
    fine."""
    _cache.clear()