"""Entry listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Entry, Source
from app.schemas import EntryOut

router = APIRouter(tags=["entries"])


# When ``q`` is set we cap results tighter than the default 50 — the
# dashboard search only ever shows one column of results, and an
# unscoped ILIKE across millions of rows is a future footgun. 50 is
# also enough that "search for X, scroll a page" feels responsive.
_SEARCH_LIMIT_CAP = 50


@router.get("/entries", response_model=list[EntryOut])
async def list_entries(
    session: AsyncSession = Depends(get_session),
    category: str | None = Query(default=None, description="Filter by source category"),
    # ``source`` is now repeated: ``?source=bbc&source=reuters``. FastAPI
    # gives us a list of values. We use ``Source.name.in_(...)`` rather
    # than OR'ing equality predicates — same plan shape, clearer intent.
    source: list[str] | None = Query(
        default=None,
        description="Filter by one or more source names (repeat the param)",
    ),
    # Free-text search across ``Entry.title`` and ``meta.summary``
    # (cast via ``.astext`` so the JSONB string field is searchable
    # without a separate tsvector column). Case-insensitive substring
    # match — adequate for a personal dashboard; a proper FTS index
    # with ranking is a future enhancement. ``limit`` is overridden to
    # ``_SEARCH_LIMIT_CAP`` when ``q`` is set so a careless ``limit=500``
    # can't turn this into a full-table scan.
    q: str | None = Query(
        default=None,
        description="Substring search across title and meta.summary (case-insensitive)",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[EntryOut]:
    stmt = (
        select(Entry)
        .join(Source, Entry.source_id == Source.id)
        .order_by(Entry.composite_score.desc(), Entry.published_at.desc().nullslast())
    )
    if category:
        stmt = stmt.where(Source.category == category)
    if source:
        # ``in_`` with an empty list matches nothing; treat it as "no
        # filter" so a frontend bug that sends ``source=`` doesn't
        # silently zero the response.
        if len(source) == 1:
            stmt = stmt.where(Source.name == source[0])
        else:
            stmt = stmt.where(Source.name.in_(source))
    if q:
        # JSONB ``.astext`` gives us the underlying string so ILIKE
        # works. Postgres needs the cast to be explicit; ``meta``
        # alone is a jsonb value and ILIKE on a jsonb fails.
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Entry.title.ilike(pattern),
                Entry.meta["summary"].astext.ilike(pattern),
            )
        )
        # Override the user-supplied limit to the search cap. We still
        # accept the param (and clamp via ``ge=1``) for shape parity
        # but ignore larger values — a search for a common substring
        # could otherwise try to return tens of thousands of rows.
        limit = min(limit, _SEARCH_LIMIT_CAP)
    stmt = stmt.limit(limit)
    rows = (await session.scalars(stmt)).all()
    return [EntryOut.model_validate(r) for r in rows]
