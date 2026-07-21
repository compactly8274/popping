"""Entry listing endpoints."""

from __future__ import annotations

import html
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Entry, Source
from app.podcast_asr import asr_available, transcribe_audio
from app.podcast_transcript import fetch_transcript_text, summarize_transcript
from app.schemas import EntryListOut, EntryOut, EntryPodcastSummaryOut, EntrySummaryOut

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

# Escape SQL LIKE metacharacters. ``q`` is bound, so this is not a
# SQL-injection concern — it's a correctness one. Without escaping,
# ``q="AI"`` matches every title containing "AI" or "remaining" or
# "fail" (the "ai" substring), and ``q="%"`` matches everything.
# We want literal substring matches, so any ``%`` / ``_`` the user
# typed should be matched literally. ``\`` is the LIKE escape
# character; we use ``ESCAPE '\\'`` in the SQL to make that explicit.
_LIKE_ESCAPE_RE = re.compile(r"([\\%_])")


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards. Called once per request to build the
    search pattern. Cheap (linear scan, single regex)."""
    return _LIKE_ESCAPE_RE.sub(r"\\\1", term)


def _clean_summary(raw: str | None) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace, trim.

    Returns "" when ``raw`` is empty / None — the caller
    distinguishes "" (asked, none) from "cache hit" via the
    ``cached_summary`` column directly. The length cap is applied
    by the caller (so the same function can be reused for non-card
    contexts later without surprising truncation).

    The unescape pass matters for the same reason it's applied to
    titles in ``app.sources.base.validate_required`` — some feeds
    double-encode, so a literal ``&#8217;`` (etc.) survives the XML
    parse and needs a second html-unescape to read as ``'``."""
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


@router.get("/entries", response_model=list[EntryListOut])
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
    per_category_limit: int | None = Query(
        default=None,
        ge=1,
        le=200,
        description=(
            "When set (and category/source/q are all unset), return up to this "
            "many entries PER SOURCE CATEGORY instead of one global top-`limit`. "
            "A flat global limit lets a single high-volume or high-scoring "
            "category (e.g. Hacker News' 5-minute refresh) crowd slower or "
            "lower-scoring categories out of the response entirely once the "
            "table has enough rows — this guarantees every category gets a "
            "fair slice regardless of how the others score. Ignored (falls "
            "back to the flat `limit`) once any of category/source/q narrows "
            "the query to something other than 'the whole dashboard'."
        ),
    ),
) -> list[EntryListOut]:
    # Slim column projection — the list endpoint returns EntryListOut
    # (no meta JSONB blob). The dashboard only ever reads meta.summary
    # lazily via the per-card summary endpoint, so dropping it from
    # the list payload saves ~100 KB per dashboard refresh.
    columns = (
        Entry.id,
        Entry.source_id,
        Entry.title,
        Entry.url,
        Entry.published_at,
        Entry.fetched_at,
        Entry.composite_score,
        Entry.personal_score,
        Entry.raw_score,
        Entry.cached_summary,
        Entry.image_url,
        Entry.image_path,
        # Reddit cross-reference footer. Pulled out of the JSONB blob
        # via the ``->>`` operator so the list payload doesn't have to
        # ship ``meta`` itself — the rest of meta is unused by the
        # list render. ``->>`` returns the unescaped text (vs
        # ``->`` which returns JSON-encoded with quotes); NULL when
        # the key is absent → both columns NULL → ``EntryListOut``
        # defaults both to None and the card skips the footer. The
        # GIN index added in migration 0014 keeps the meta scan
        # cheap on large entry tables.
        Entry.meta.op("->>")("reddit_thread_url").label("reddit_thread_url"),
        Entry.meta.op("->>")("reddit_comment_count").label("reddit_comment_count_text"),
        # Podcast episode audio, same pull-out-of-meta pattern. NULL
        # for every non-podcast entry — the card only renders the
        # "Listen" affordance when audio_url is non-null.
        Entry.meta.op("->>")("audio_url").label("audio_url"),
        Entry.meta.op("->>")("duration_seconds").label("duration_seconds_text"),
        # Podcasting 2.0 transcript URL, when the feed publishes one.
        # NULL for everything else — the card only shows "Summarize
        # episode" when this is non-null (see
        # POST /entries/{id}/podcast_summary).
        Entry.meta.op("->>")("transcript_url").label("transcript_url"),
    )
    stmt = select(*columns).join(Source, Entry.source_id == Source.id)
    if q:
        # When a search query is set, order by recency within the
        # search result set. The default composite_score sort is
        # misleading for search — a high-scored story from last week
        # that happens to mention "AI" would beat a fresh story
        # actually about AI, and the user has no signal that this
        # is the ordering they got.
        stmt = stmt.order_by(Entry.published_at.desc().nullslast())
    else:
        stmt = stmt.order_by(
            Entry.composite_score.desc(), Entry.published_at.desc().nullslast()
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
        # Escape LIKE wildcards so a user query of "%" or "_" doesn't
        # match every row; ``ESCAPE '\'`` tells PG the backslash is
        # the escape character (the default is no escape, which would
        # make the backslash literal — see
        # https://www.postgresql.org/docs/current/functions-matching.html).
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                Entry.title.ilike(pattern, escape="\\"),
                Entry.meta["summary"].astext.ilike(pattern, escape="\\"),
            )
        )
        # Override the user-supplied limit to the search cap. We still
        # accept the param (and clamp via ``ge=1``) for shape parity
        # but ignore larger values — a search for a common substring
        # could otherwise try to return tens of thousands of rows.
        limit = min(limit, _SEARCH_LIMIT_CAP)

    if per_category_limit is not None and not category and not source and not q:
        # Per-category windowed query instead of one global top-`limit`.
        # ``ROW_NUMBER() OVER (PARTITION BY category ORDER BY ...)``
        # ranks each category's own rows independently, so a slow or
        # low-scoring category still gets its top
        # ``per_category_limit`` rows even if every one of them would
        # rank below `limit` in a single cross-category ordering.
        rn = (
            func.row_number()
            .over(
                partition_by=Source.category,
                order_by=(Entry.composite_score.desc(), Entry.published_at.desc().nullslast()),
            )
            .label("rn")
        )
        ranked = (
            select(*columns, rn)
            .join(Source, Entry.source_id == Source.id)
            .subquery()
        )
        column_names = [c.name for c in columns]
        stmt = (
            select(*[ranked.c[name] for name in column_names])
            .where(ranked.c.rn <= per_category_limit)
            .order_by(ranked.c.composite_score.desc(), ranked.c.published_at.desc().nullslast())
        )
    else:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).mappings().all()
    # ``reddit_comment_count`` projects as text (JSONB ``->>`` always
    # returns text). The schema expects Optional[int]; coerce here so
    # pydantic doesn't reject the value with a 422.
    #
    # ``RowMapping`` (the type ``Result.mappings().all()`` yields in
    # SA 2.0) is a read-only ``Mapping`` — no ``pop``, no
    # ``__setitem__``. Copy to a fresh dict per row so we can rename
    # the projected ``..._text`` column to the schema field name and
    # coerce the type in place without fighting the immutable view.
    out: list[EntryListOut] = []
    for r in rows:
        data = dict(r)
        raw_count = data.pop("reddit_comment_count_text", None)
        if raw_count is not None:
            try:
                data["reddit_comment_count"] = int(raw_count)
            except (TypeError, ValueError):
                # Defensive: the JSONB value should always parse as an
                # int (the sweep writes ``int(...)``). If a manual SQL
                # write or a bad migration left a non-int string,
                # null it out rather than 422 the whole list.
                data["reddit_comment_count"] = None
        raw_duration = data.pop("duration_seconds_text", None)
        if raw_duration is not None:
            try:
                data["duration_seconds"] = int(raw_duration)
            except (TypeError, ValueError):
                data["duration_seconds"] = None
        out.append(EntryListOut.model_validate(data))
    return out



@router.get("/entries/by-ids", response_model=list[EntryOut])
async def entries_by_ids(
    session: AsyncSession = Depends(get_session),
    ids: str = Query(
        default="",
        description="Comma-separated entry ids to fetch (max 200).",
    ),
) -> list[EntryOut]:
    """Resolve a list of entry ids to full EntryOut
    rows, joined with source name.

    Used by the Settings overlay's Hidden and
    Starred tabs to render a list of the entries
    the user has hidden or starred (the ids are
    in localStorage; the dashboard's `entries`
    state doesn't include hidden entries).

    The endpoint is unauth'd — same pattern as
    ``/api/entries``. In a homelab / single-user
    deployment, the bypass covers the common
    case; in an OIDC deployment, the row-level
    data isn't sensitive (no PII, just article
    metadata).

    Cap at 200 ids so a careless client can't
    turn this into a full-table scan.

    Returns the entries in the same order as
    the input ids. Ids that don't match a row
    are dropped silently (a deleted entry
    shouldn't cause the whole call to fail).
    """
    if not ids.strip():
        return []
    # Parse + validate ids. The pydantic model
    # will reject non-numeric strings; we use
    # ``int(v)`` inside a list comprehension so
    # a single bad value doesn't fail the whole
    # call (the entry is just dropped).
    raw_ids = [s.strip() for s in ids.split(",") if s.strip()]
    parsed: list[int] = []
    for v in raw_ids:
        try:
            parsed.append(int(v))
        except ValueError:
            continue
    if not parsed:
        return []
    # Cap the count to prevent abuse.
    if len(parsed) > 200:
        parsed = parsed[:200]
    # Query: select entries by id, join source for
    # the name. Same column projection as the
    # list_entries endpoint (no meta JSONB).
    stmt = (
        select(Entry, Source.name)
        .join(Source, Entry.source_id == Source.id)
        .where(Entry.id.in_(parsed))
    )
    result = await session.execute(stmt)
    rows = result.all()
    # Build a map for O(1) lookup, then return in
    # the input order (preserves the user's
    # localStorage ordering, which is recency).
    by_id = {row[0].id: (row[0], row[1]) for row in rows}
    out: list[EntryOut] = []
    for eid in parsed:
        if eid not in by_id:
            continue
        entry, source_name = by_id[eid]
        out.append(
            EntryOut(
                id=entry.id,
                source_id=entry.source_id,
                source_name=source_name,
                title=entry.title,
                url=entry.url,
                published_at=entry.published_at,
                fetched_at=entry.fetched_at,
                composite_score=entry.composite_score,
                personal_score=entry.personal_score,
                raw_score=entry.raw_score,
                meta=entry.meta or {},
            )
        )
    return out


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


@router.post(
    "/entries/{entry_id}/podcast_summary",
    response_model=EntryPodcastSummaryOut,
)
async def entry_podcast_summary_endpoint(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> EntryPodcastSummaryOut:
    """Return (or fetch/transcribe + generate + cache) an LLM-written
    summary of a podcast episode.

    Two paths, tried in cost order:
      1. Podcasting 2.0 ``<podcast:transcript>`` tag (extracted at
         ingest time into ``meta.transcript_url`` / ``meta.
         transcript_type`` — see ``app.sources.rss``). Free — reuses
         a transcript the host already produced.
      2. Real speech-to-text via Groq's hosted Whisper endpoint (see
         ``app.podcast_asr``), when the feed has no transcript tag
         but does have ``meta.audio_url`` (the episode's enclosure)
         and a Groq API key is configured. Costs a fraction of a
         cent per episode; only attempted when path 1 isn't
         available.

    Cache semantics mirror ``/summary`` above:
      - ``podcast_transcript_summary is None``  → never attempted →
        try path 1, then path 2, summarize + persist.
      - ``podcast_transcript_summary == ""``    → attempted, no
        usable result (fetch/transcription failed / no LLM
        configured / LLM returned nothing) → return empty without
        re-attempting.
      - populated                                → return cached.
    """
    row = await session.get(Entry, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")

    if row.podcast_transcript_summary is not None:
        return EntryPodcastSummaryOut(
            summary=row.podcast_transcript_summary, cached=True, available=True,
        )

    meta = row.meta or {}
    transcript_url = meta.get("transcript_url")
    transcript_type = meta.get("transcript_type") or ""
    audio_url = meta.get("audio_url")

    transcript_text = None
    if isinstance(transcript_url, str) and transcript_url:
        transcript_text = await fetch_transcript_text(transcript_url, transcript_type)
    elif isinstance(audio_url, str) and audio_url and asr_available():
        transcript_text = await transcribe_audio(audio_url)
    else:
        # Neither a published transcript nor (audio_url + a
        # configured ASR key) — nothing to cache (a future re-ingest
        # or a later GROQ_API_KEY setup could make this available,
        # so we don't want to permanently record "unavailable" on
        # the row).
        return EntryPodcastSummaryOut(summary=None, cached=False, available=False)

    summary = None
    if transcript_text:
        summary = await summarize_transcript(row.title, transcript_text)

    # Persist even on failure (empty string) — same rationale as
    # cached_summary: without this, a broken transcript URL or an
    # unconfigured LLM provider would re-attempt the fetch + (would-be)
    # LLM call on every single tap.
    row.podcast_transcript_summary = summary or ""
    await session.commit()

    return EntryPodcastSummaryOut(summary=summary, cached=False, available=True)

