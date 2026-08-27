"""For You feed.

Personal top-N feed, ordered by composite_score DESC with a convergence
boost applied. Computed at query time, not at ingest, so cross-source
story clusters get a multiplicative bump as soon as they form.

The convergence SQL is one GROUP BY over the last
``convergence_window_hours``, cached at the process level for 30s —
see ``app.scoring.convergence`` for the helper.

Scoring strategy
----------------

The endpoint reads the stored ``composite_score`` column (which already
blends recency, personal, source weight, and engagement — refreshed
every 10 min by the scheduler's rescore tick) and applies only the
convergence multiplier at query time.  The previous implementation
pulled ``Entry.embedding`` (Vector(384)) for 500 candidate rows
(~1.5MB of wire data per request) to recompute ``personal_score`` in
Python via cosine similarity, but the stored value is already current
within a 10-minute window — the live recompute was pure overhead.

The convergence multiplier is the only signal that can change between
rescore ticks (a story can enter a multi-source cluster at any
moment), so applying it at query time is the correct and sufficient
live adjustment.  The brief generator uses the same pattern.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user
from app.config import settings
from app.db import get_session
from app.models import Entry, Source, UserProfile
from app.schemas import EntryListOut
from app.scoring import composite as composite_scorer
from app.scoring import convergence

router = APIRouter(tags=["\"foryou\""])


@router.get("/foryou", response_model=list[EntryListOut])
async def foryou(
    session: AsyncSession = Depends(get_session),
    user: dict | None = Depends(current_user),
    limit: int = Query(default=50, ge=1, le=200),
    category: str | None = Query(default=None, description="Filter by source category"),
) -> list[EntryListOut]:
    """Top-N personal feed.

    Order:
      1. Pull a wide candidate set ordered by composite_score DESC.
         We over-fetch (capped at 500) so convergence-boosted entries
         still have room to climb into the result.
      2. Apply the convergence multiplier to the stored composite_score.
      3. Re-sort and trim to ``limit``.
    """
    profile = await session.scalar(select(UserProfile).where(UserProfile.id == 1))

    over_fetch = min(max(limit * 4, 200), 500)
    # Slim SELECT: we project only the columns the frontend renders plus
    # the source fields needed for the convergence slug lookup.
    # ``Entry.embedding`` (Vector(384)) is deliberately excluded — it
    # was ~3KB serialized per row (~1.5MB for 500 candidates), and the
    # stored ``composite_score`` already incorporates the personal
    # component (refreshed every 10 min by the scheduler rescore tick).
    # The only live adjustment is the convergence multiplier, which
    # uses the entry title (already in the projection) and the cached
    # convergence counts (one GROUP BY, 30s TTL).
    #
    # JOIN Source in the same query (instead of a follow-up IN query)
    # so we can read source.category for the category filter and avoid
    # a second DB round-trip.  Projecting source fields the scorer might
    # need keeps the door open for future re-weighting without re-
    # introducing the N+1 pattern.
    stmt = (
        select(
            Entry.id,
            Entry.source_id,
            Entry.title,
            Entry.url,
            Entry.published_at,
            Entry.fetched_at,
            Entry.composite_score,
            Entry.personal_score,
            Entry.raw_score,
            Entry.image_url,
            Entry.image_path,
            Entry.cached_summary,
            # Reddit cross-reference footer. Same projection as
            # ``/api/entries`` — ``->>`` returns unescaped text;
            # NULL when the key is absent. Coerced to int in the
            # _Row builder below (see comment there).
            Entry.meta.op("->>")("reddit_thread_url").label("reddit_thread_url"),
            Entry.meta.op("->>")("reddit_comment_count").label("reddit_comment_count_text"),
            # Source fields needed for category filter and potential
            # future re-weighting.  Joining here eliminates the second
            # round-trip the previous code did to hydrate Source rows.
            Source.category.label("source_category"),
            Source.source_weight.label("source_weight"),
        )
        .join(Source, Entry.source_id == Source.id)
        .order_by(Entry.composite_score.desc(), Entry.published_at.desc().nullslast())
        .limit(over_fetch)
    )
    if category:
        stmt = stmt.where(Source.category == category)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    conv = await convergence.counts(session, settings.convergence_window_hours)

    class _Row:
        __slots__ = (
            "id", "source_id", "title", "url", "published_at", "fetched_at",
            "composite_score", "personal_score", "raw_score",
            "image_url", "image_path", "cached_summary",
            "reddit_thread_url", "reddit_comment_count",
        )
        def __init__(self, raw):
            self.id = raw.id
            self.source_id = raw.source_id
            self.title = raw.title
            self.url = raw.url
            self.published_at = raw.published_at
            self.fetched_at = raw.fetched_at
            self.composite_score = raw.composite_score
            self.personal_score = raw.personal_score
            self.raw_score = raw.raw_score
            self.image_url = raw.image_url
            self.image_path = raw.image_path
            self.cached_summary = raw.cached_summary
            self.reddit_thread_url = raw.reddit_thread_url
            # ``reddit_comment_count`` projects as text from JSONB
            # (``->>`` is always text). Coerce to int so the
            # ``EntryListOut`` validator accepts it; fall back to
            # None on a parse error so a bad migration doesn't 422
            # the whole /foryou call.
            raw_count = getattr(raw, "reddit_comment_count_text", None)
            try:
                self.reddit_comment_count = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                self.reddit_comment_count = None

    candidates = [_Row(r) for r in rows]

    # Apply the convergence multiplier to the stored composite_score.
    # The stored composite_score already blends recency, personal,
    # source weight, and engagement (refreshed every 10 min by the
    # scheduler's rescore tick).  The convergence multiplier is the
    # only signal that can change between ticks, so applying it here
    # is sufficient.  The brief generator uses the same pattern.
    boosted: list[tuple[float, _Row]] = []
    for entry in candidates:
        base = entry.composite_score or 0.0
        slug = composite_scorer.title_slug(entry.title)
        mult = composite_scorer.convergence_multiplier(conv.get(slug, 1))
        boosted.append((base * mult, entry))

    boosted.sort(
        key=lambda pair: (
            pair[0],
            pair[1].published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        ),
        reverse=True,
    )
    top = boosted[:limit]
    return [EntryListOut.model_validate(e) for _score, e in top]