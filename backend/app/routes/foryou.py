"""For You feed.

Personal top-N feed, ordered by composite_score DESC with a convergence
boost applied. Computed at query time, not at ingest, so cross-source
story clusters get a multiplicative bump as soon as they form.

The convergence SQL is one GROUP BY over the last
``convergence_window_hours``, cached at the process level for 30s —
see ``app.scoring.convergence`` for the helper.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.deps import current_user
from app.config import settings
from app.db import get_session
from app.models import Entry, Source, UserProfile
from app.schemas import EntryListOut
from app.scoring import composite as composite_scorer
from app.scoring import convergence

router = APIRouter(tags=["foryou"])


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
      2. Recompute composite_score with the convergence multiplier
         applied (per-row source_count for the entry's title slug).
      3. Re-sort and trim to ``limit``.
    """
    profile = await session.scalar(select(UserProfile).where(UserProfile.id == 1))

    over_fetch = min(max(limit * 4, 200), 500)
    # ``selectinload(Entry.source)`` eagerly fetches the related
    # ``Source`` rows in a single follow-up SELECT and populates them
    # on each entry before we leave the async session. Without it,
    # ``composite_scorer.score(entry, entry.source, ...)`` below
    # triggers a lazy load on the SyncSession-bridged async session,
    # which raises ``MissingGreenlet: greenlet_spawn has not been
    # called`` — the symptom was a 500 on every /api/foryou call
    # once a recent /api/entries?source=… call had primed enough
    # rows. ``selectinload`` issues one IN(…) query regardless of
    # candidate count, so the cost is bounded.
    #
    # Slim SELECT: we only need the columns the frontend renders plus
    # the source join. ``embedding`` (Vector(384)) is ~3KB serialized
    # and unused at this layer, ``meta`` JSONB is ~500B and unused
    # here (the per-card summary endpoint pulls it on demand).
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
    # Hydrate into ORM-ish objects the score loop can read. We can't
    # use scalars() + .all() here because we project specific columns
    # (no Entry row); re-attach the source via a follow-up IN query
    # so composite_scorer.score() can read entry.source.
    source_ids = list({r.source_id for r in rows})
    sources_by_id = {
        s.id: s
        for s in (
            await session.scalars(select(Source).where(Source.id.in_(source_ids)))
        ).all()
    }

    conv = await convergence.counts(session, settings.convergence_window_hours)

    class _Row:
        __slots__ = (
            "id", "source_id", "title", "url", "published_at", "fetched_at",
            "composite_score", "personal_score", "raw_score",
            "image_url", "image_path", "cached_summary", "source",
            "reddit_thread_url", "reddit_comment_count",
        )
        def __init__(self, raw, source):
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
            self.source = source
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

    candidates = [_Row(r, sources_by_id.get(r.source_id)) for r in rows]

    boosted: list[tuple[float, _Row]] = []
    for entry in candidates:
        base = composite_scorer.score(entry, entry.source, profile)
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
