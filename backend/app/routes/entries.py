"""Entry listing endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Entry, Source
from app.schemas import EntryOut, EntrySummaryOut

router = APIRouter(tags=["entries"])


# When ``q`` is set we cap results tighter than the default 50 — the
# dashboard search only ever shows one column of results, and an
# unscoped ILIKE across millions of rows is a future footgun. 50 is
# also enough that "search for X, scroll a page" feels responsive.
_SEARCH_LIMIT_CAP = 50

# Per-card summary length cap. ~800 chars is ~3-4 lines in the
# dashboard card with line-clamp-3, which is the right balance
# between "useful digest" and "wall of text the user has to
# scroll past to see the next headline". Bigger than this and the
# user is better off just opening the article.
_SUMMARY_MAX_CHARS = 800

# Tag-strip regex. ``<[^>]+>`` matches every HTML opening / closing
# tag and self-closing tag; we replace with a single space rather
# than "" so consecutive ``<p>foo</p><p>bar</p>`` doesn't fuse into
# ``foobar``. Trailing ``\s+`` collapse keeps the final string
# compact. Far cheaper than pulling in ``bleach`` for what is
# effectively "remove angle-bracket pairs" — the feeds' summaries
# are near-plain-text with occasional HTML wrappers.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_summary(raw: str | None) -> str:
    """Strip HTML tags, collapse whitespace, trim.

    Returns "" when ``raw`` is empty / None — the caller
    distinguishes "" (asked, none) from "cache hit" via the
    ``cached_summary`` column directly. The length cap is applied
    by the caller (so the same function can be reused for non-card
    contexts later without surprising truncation)."""
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


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


@router.post(
    "/entries/{entry_id}/summary",
    response_model=EntrySummaryOut,
)
async def entry_summary_endpoint(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> EntrySummaryOut:
    """Return (or compute + cache) the per-card summary for an entry.

    Tap-the-chevron-from-the-dashboard path. The frontend calls this
    on the first expansion of a card; the result lands inline under
    the card title. Subsequent calls hit the ``cached_summary``
    column without re-extracting.

    Cache semantics:
      - ``cached_summary is None``   → first call for this row → run
                                       the extract path → persist.
      - ``cached_summary == ""``     → asked before, no usable text
                                       (feed shipped nothing). Return
                                       empty without re-extracting.
      - ``cached_summary == "..."``  → asked before, return verbatim.

    Text extraction order (first non-empty wins):
      1. ``Entry.meta.summary`` — the feed's own deck, what the user
         saw when they tapped the chevron the first time.
      2. ``Entry.body_text`` — populated by the LLM embedder when it
         walked the article. Fall back here when the feed shipped
         no summary (HN top stories, Wikipedia OTD entries).

    Truncation: cap at ``_SUMMARY_MAX_CHARS`` per the card layout —
    3-line clamp needs ~250-300 chars on a 320px column; 800 leaves
    headroom for richer feeds (Verge, NYT deck paragraphs) without
    forcing the user past the next headline.
    """
    row = await session.get(Entry, entry_id)
    if row is None:
        # 404 is the only way the frontend finds out the entry was
        # purged between page load and tap. The Card component
        # shows "couldn't load summary" inline; chevron stays
        # clickable so a retry is one tap away.
        raise HTTPException(status_code=404, detail="entry not found")

    if row.cached_summary is not None:
        # Cache hit. ``""`` means we already determined the source
        # shipped nothing usable — return that as ``summary=""`` so
        # the frontend can distinguish "no summary" from "summary
        # loaded".
        return EntrySummaryOut(summary=row.cached_summary, cached=True)

    # First call: build the text. ``meta`` can be None for entries
    # ingested before the JSONB column existed; default to {} so
    # the chained .get() doesn't AttributeError. ``body_text``
    # fallback is in the same try-block so an empty ``meta.summary``
    # doesn't immediately give up.
    meta = row.meta or {}
    raw = meta.get("summary") if isinstance(meta.get("summary"), str) else ""
    if not raw and row.body_text:
        raw = row.body_text

    cleaned = _clean_summary(raw)
    # Cap at the dashboard's visual budget. Truncating on a word
    # boundary reads better than mid-word cutoff so we walk back to
    # the last space if we'd land mid-word.
    if len(cleaned) > _SUMMARY_MAX_CHARS:
        cleaned = cleaned[:_SUMMARY_MAX_CHARS]
        if " " in cleaned:
            cleaned = cleaned.rsplit(" ", 1)[0]
        cleaned = cleaned.rstrip() + "…"

    # Persist even when empty — that's the cache-hit signal for
    # next time. Without the persist, every chevron tap would
    # re-run the regex / fallback chain.
    row.cached_summary = cleaned
    await session.commit()

    return EntrySummaryOut(summary=cleaned, cached=False)
