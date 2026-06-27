"""For You feed.

Personal top-N feed, ordered by composite_score DESC with a convergence
boost applied. Computed at query time, not at ingest, so cross-source
story clusters get a multiplicative bump as soon as they form.

The convergence SQL is one GROUP BY over the last
``convergence_window_hours``, so it stays cheap even with tens of
thousands of recent entries.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user
from app.config import settings
from app.db import get_session
from app.models import Entry, Source, UserProfile
from app.schemas import EntryOut
from app.scoring import composite as composite_scorer

router = APIRouter(tags=["foryou"])


async def _load_profile(session: AsyncSession) -> UserProfile | None:
    profile = await session.scalar(select(UserProfile).where(UserProfile.id == 1))
    return profile


async def _convergence_counts(
    session: AsyncSession,
    window_hours: int,
) -> dict[str, int]:
    """Map title_slug → number of distinct sources mentioning it within
    the window. Only slugs seen in 2+ sources are returned (saves a
    pass over the candidates that won't get boosted anyway)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    stmt = (
        select(Entry.title, Source.name)
        .join(Source, Entry.source_id == Source.id)
        .where(Entry.published_at >= since)
    )
    rows = (await session.execute(stmt)).all()
    counts: dict[str, int] = defaultdict(set)
    for title, source_name in rows:
        slug = composite_scorer.title_slug(title)
        if not slug:
            continue
        counts[slug].add(source_name)
    return {slug: len(srcs) for slug, srcs in counts.items() if len(srcs) > 1}


@router.get("/foryou", response_model=list[EntryOut])
async def foryou(
    session: AsyncSession = Depends(get_session),
    user: dict | None = Depends(current_user),
    limit: int = Query(default=50, ge=1, le=200),
    category: str | None = Query(default=None, description="Filter by source category"),
) -> list[EntryOut]:
    """Top-N personal feed.

    Order:
      1. Pull a wide candidate set ordered by composite_score DESC.
         We over-fetch (capped at 500) so convergence-boosted entries
         still have room to climb into the result.
      2. Recompute composite_score with the convergence multiplier
         applied (per-row source_count for the entry's title slug).
      3. Re-sort and trim to ``limit``.
    """
    profile = await _load_profile(session)

    over_fetch = min(max(limit * 4, 200), 500)
    stmt = (
        select(Entry)
        .join(Source, Entry.source_id == Source.id)
        .order_by(Entry.composite_score.desc(), Entry.published_at.desc().nullslast())
        .limit(over_fetch)
    )
    if category:
        stmt = stmt.where(Source.category == category)
    candidates = (await session.scalars(stmt)).all()
    if not candidates:
        return []

    conv = await _convergence_counts(session, settings.convergence_window_hours)

    boosted: list[tuple[float, Entry]] = []
    for entry in candidates:
        base = composite_scorer.score(entry, entry.source, profile)
        slug = composite_scorer.title_slug(entry.title)
        mult = composite_scorer.convergence_multiplier(conv.get(slug, 1))
        boosted.append((base * mult, entry))

    boosted.sort(key=lambda pair: (pair[0], pair[1].published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc)), reverse=True)
    top = boosted[:limit]
    return [EntryOut.model_validate(e) for _score, e in top]
