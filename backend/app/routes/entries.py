"""Entry listing endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import reddit_client
from app.article_extract import fetch_article_text
from app.article_summary import summarize_article
from app.auth.deps import require_user
from app.config import settings
from app.db import get_session
from app.llm import router as llm_router
from app.models import Entry, Source, StoryCluster
from app.podcast_asr import asr_available, transcribe_audio
from app.podcast_transcript import fetch_transcript_text, summarize_transcript
from app.reddit_client import fetch_thread_comments
from app.reddit_comment_summary import summarize_comments
from app.schemas import (
    EntryListOut,
    EntryOut,
    EntryPodcastSummaryOut,
    EntryRedditCommentSummaryOut,
    EntrySummaryOut,
    FramingArticleOut,
    FramingClusterOut,
)

router = APIRouter(tags=["entries"])


# Same auth-gating pattern as ``app.routes.sources._write_deps`` and
# ``app.routes.brief._route_deps``: require auth when OIDC is on, allow
# unauthenticated when OIDC is off (the single-user LAN case). The
# three summary endpoints below are POSTs that fetch a URL and may
# run an LLM call (and write to the DB), so an attacker who can reach
# the API could otherwise DoS the LLM budget, pollute the
# cached_summary column with attacker-controlled text, or just
# cause a lot of outbound network traffic. The OIDC-on case must
# require auth; the OIDC-off case keeps the existing single-user
# behavior (the dashboard itself has no auth, the local network
# is the only access boundary).
_write_deps = [Depends(require_user)] if settings.oidc_enabled else []


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
    return _LIKE_ESCAPE_RE.sub(r"\\\\\\1", term)


# ---------------------------------------------------------------------------
# Per-entry in-flight lock for the three summary endpoints
# ---------------------------------------------------------------------------
#
# All three summary endpoints (``/api/entries/{id}/summary``,
# ``/podcast_summary``, ``/reddit_comment_summary``) share the same
# race: two concurrent requests for the same entry both see
# "cache empty", both run the (expensive) fetch + LLM chain, and
# both write back the same answer. The user pays for two LLM
# calls and the slower of the two wins the write. The behavior
# was previously correct-but-wasteful.
#
# Fix: a per-entry lock guards the entire "check cache →
# fetch → LLM → write" body. A second request that arrives
# while the first is in flight blocks on the lock; once it
# acquires the lock, the cache has been populated by request
# 1, so the second-check inside the critical section sees a
# populated cache and returns immediately. This is the
# double-checked locking pattern: cheap fast-path (no lock)
# for the common case of a populated cache, expensive slow-
# path (lock + re-check) for the cache-miss case.
#
# The cap is a leak guard, not a memory bound. ``asyncio.Lock``
# is a few hundred bytes; 256 entries × ~500 B is ~125 KB,
# which is fine. The cap prevents a pathological workload
# (an attacker hammering 10k distinct entry ids) from
# accumulating one lock per id forever. When the cap is hit
# we return a throwaway lock — the lock still serializes
# locally for the requesting task (so two concurrent
# requests for the SAME id beyond the cap still serialize),
# but two requests for DIFFERENT ids beyond the cap each get
# their own lock and can race. The trade-off (mild stampede
# for the 257+ distinct id) is strictly better than leaking
# locks per request.
#
# This mirrors ``app.auth.oidc._get_discovery_lock`` — same
# dict-of-locks + guard + cap pattern, kept identical for
# readability.
_SUMMARY_LOCKS_MAX = 256
_summary_locks: dict[int, asyncio.Lock] = {}
_summary_locks_guard = asyncio.Lock()


async def _get_summary_lock(entry_id: int) -> asyncio.Lock:
    """Return a process-wide ``asyncio.Lock`` for ``entry_id``.

    First call for a given id allocates a lock and stores it.
    Subsequent calls return the same lock. When the cap is
    hit, returns a throwaway untracked lock (a fresh
    ``asyncio.Lock()`` per call) — see the module-level
    comment for the trade-off.
    """
    # Fast path: the common case is a single concurrent request
    # per entry. Reading the dict under no lock is safe because
    # dict.__getitem__ is atomic in CPython, and a missing-key
    # race just falls through to the slow path.
    lock = _summary_locks.get(entry_id)
    if lock is not None:
        return lock
    async with _summary_locks_guard:
        lock = _summary_locks.get(entry_id)
        if lock is not None:
            return lock
        if len(_summary_locks) >= _SUMMARY_LOCKS_MAX:
            # Return a fresh, untracked lock. The caller still
            # serializes locally for THIS call, but doesn't
            # share the lock with any other in-flight request.
            # Mild cold-start stampede is possible for ids
            # beyond the cap; preferred over leaking locks.
            return asyncio.Lock()
        lock = asyncio.Lock()
        _summary_locks[entry_id] = lock
        return lock


def _should_retry_empty_cache(
    fetched_at: Optional[dt.datetime],
    retry_hours: float,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Decide whether an empty-string ``cached_summary`` is still
    fresh enough to skip a retry.

    Returns True when the cache is "old enough to retry"; the
    endpoint uses this in a negated form (``not
    _should_retry_empty_cache(...)``) so a True return means
    "fall through to the fetch / extract / LLM chain".

    Rules:
      - ``fetched_at is None`` (pre-migration rows that pre-date
        the column) → True. The first chevron tap after deploy
        refetches and re-caches every empty-string hit. One-time
        burst cost; subsequent calls land in the normal
        cache-or-retry decision via the real timestamp.
      - ``fetched_at`` is older than ``retry_hours`` ago → True.
        The cache has been empty long enough that a transient
        failure (the source's article URL was temporarily 404,
        the LLM provider was momentarily over-quota) is unlikely
        to still be in effect; retrying is worthwhile.
      - ``fetched_at`` is within ``retry_hours`` of ``now`` → False.
        Recent empty cache: trust it, don't burn the LLM budget
        on a constant retry loop.

    Pure function (no DB), so unit-testable without a real
    backend. ``now`` is injectable for deterministic testing.
    """
    if fetched_at is None:
        # Pre-migration row. Treat as "old enough to retry" so
        # the deploy's first tap after upgrade refreshes stale
        # empty-string caches.
        return True
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    # fetched_at is a tz-aware UTC timestamp from the model; guard
    # against a naive value just in case the DB driver ever drops
    # tz info (some MySQL drivers do — Postgres's asyncpg preserves
    # it, but a future driver swap shouldn't silently break the
    # retry window).
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
    return (now - fetched_at).total_seconds() >= retry_hours * 3600


def _summary_cache_response(row) -> Optional[EntrySummaryOut]:
    """Decide whether ``row`` is already cached, and if so build
    the response for the fast-path return.

    Returns ``None`` when the cache is empty OR when the empty
    cache is "old enough to retry" (the caller falls through to
    the fetch / extract / LLM chain). Otherwise returns a
    pre-built ``EntrySummaryOut(summary=row.cached_summary,
    cached=True)``.

    Pure function: no DB, no LLM. The double-checked locking
    pattern in ``entry_summary_endpoint`` calls this TWICE —
    once before acquiring the per-entry lock (fast path) and
    once after acquiring the lock with a freshly-refreshed row
    (the second half of the double check). Keeping the rule
    in a single function means a future change to the cache
    semantics only needs to update one place.
    """
    if row.cached_summary is None:
        return None
    if row.cached_summary == "" and _should_retry_empty_cache(
        row.cached_summary_fetched_at,
        retry_hours=settings.cached_summary_retry_hours,
    ):
        # Empty cache + still inside the retry window → return
        # the empty string as "we already determined no summary
        # is available". Outside the window (the
        # _should_retry_empty_cache returned False) → fall
        # through and re-run the chain.
        return None
    return EntrySummaryOut(summary=row.cached_summary, cached=True)


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
        # Framing Watch cluster membership — a real column, no JSONB
        # extraction needed. Non-null only for entries grouped with
        # 2+ other outlets' coverage of the same story (see
        # app.framing). Drives the card's "Related coverage" button.
        Entry.story_cluster_id,
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


def _truncate_summary(text: str) -> str:
    """Cap at ``_SUMMARY_MAX_CHARS``, truncating on a word boundary
    rather than mid-word. Applied to both summary paths below — the
    LLM path is prompted for 3-4 sentences (usually well under the
    cap) but this is the safety net against a model that ignores the
    instruction, same as the old extract-only path always had."""
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    truncated = text[:_SUMMARY_MAX_CHARS]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.rstrip() + "…"


def _extract_fallback_summary(row: Entry) -> str:
    """The original, LLM-free summary: the feed's own blurb (or
    ``body_text``, though that column is never populated anywhere in
    this schema today — see ``app.article_extract``'s module
    docstring), cleaned and truncated. Used when no LLM provider is
    configured, the article fetch fails, or the LLM call itself
    fails — this always has *something* to fall back to, unlike the
    LLM path which can legitimately come back empty."""
    meta = row.meta or {}
    raw = meta.get("summary") if isinstance(meta.get("summary"), str) else ""
    if not raw and row.body_text:
        raw = row.body_text
    return _truncate_summary(_clean_summary(raw))


def _is_reddit_url(url: str | None) -> bool:
    """True if ``url`` is a Reddit thread permalink or comment permalink.

    Matches both ``reddit.com`` and ``www.reddit.com`` hostnames. Does
    NOT match ``old.reddit.com`` or any other host — the helper is
    specifically for routing Reddit URLs into the Reddit-specific
    article-fetch path (which fetches the thread's ``.rss`` and uses
    the post body as the article text). Non-Reddit URLs are handled
    by ``fetch_article_text`` (HTML fetch + trafilatura).
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        host = url.split("/", 3)[2] if "://" in url else ""
    except IndexError:
        return False
    return host in ("reddit.com", "www.reddit.com")


async def _fetch_reddit_post_body(url: str) -> str:
    """Fetch a Reddit thread's post body via its ``.rss`` feed.

    Reddit's main site is a client-rendered SPA — ``fetch_article_text``
    (which uses trafilatura to extract from raw HTML) returns empty
    for any ``reddit.com`` URL because the actual post body never
    lands in the initial HTML payload. Reddit's ``.rss`` feed
    sidesteps this: the first ``<entry>`` in the thread's RSS IS
    the post itself (the OP's body text), rendered as a
    ``<content type="html">`` element that the standard XML parser
    reads directly.

    Returns the cleaned text of the first entry, or empty string on
    any failure (Reddit down, rate-limited, malformed feed, empty
    body). The same comments-fetch path is used elsewhere, so the
    rate-limit semantics align with the existing direct-mode
    bucket: a rate-limited call here doesn't block the
    cross-reference sweep or the scheduled ingest jobs.
    """
    try:
        comments = await fetch_thread_comments(url)
    except Exception:
        return ""
    if not comments:
        return ""
    # The thread's RSS includes the post as the first entry, then
    # the comments in tree order. The first entry is the OP.
    return _clean_summary(comments[0].get("text", ""))


@router.post(
    "/entries/{entry_id}/summary",
    response_model=EntrySummaryOut,
    dependencies=_write_deps,
)
async def entry_summary_endpoint(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> EntrySummaryOut:
    """Return (or compute + cache) the per-card summary for an entry.

    Tap-the-chevron-from-the-dashboard path. The frontend calls this
    on the first expansion of a card; the result lands inline under
    the card title. Subsequent calls hit the ``cached_summary``
    column without re-extracting or re-fetching.

    Cache semantics:
      - ``cached_summary is None``   → first call for this row → run
                                       the summary path → persist.
      - ``cached_summary == ""``     → asked before, no usable text
                                       (feed shipped nothing AND the
                                       article couldn't be fetched).
                                       Return empty without retrying.
      - ``cached_summary == "..."``  → asked before, return verbatim.

    Summary path (first that produces text wins):
      1. LLM summary of the article's own full text — fetch the
         entry's URL, extract the readable body
         (``app.article_extract``), and summarize it in 3-4 sentences
         (``app.article_summary``). Skipped entirely (no fetch) when
         no LLM provider is configured, since it can't be used
         either way.
      2. The feed's own short blurb, cleaned and truncated — the
         original behavior, and the permanent fallback for entries
         whose article can't be fetched (paywalled, blocked, 404'd)
         or when no LLM is configured at all.

    Truncation: cap at ``_SUMMARY_MAX_CHARS`` per the card layout —
    the LLM path is prompted for 3-4 sentences, comfortably under
    this; it's a safety net, not the normal path to hitting the cap.
    """
    row = await session.get(Entry, entry_id)
    if row is None:
        # 404 is the only way the frontend finds out the entry was
        # purged between page load and tap. The Card component
        # shows "couldn't load summary" inline; chevron stays
        # clickable so a retry is one tap away.
        raise HTTPException(status_code=404, detail="entry not found")

    # Fast path: cache hit. ``_summary_cache_response`` is the
    # single source of truth for the "is this cached, and if so
    # is the empty-string retry window still in effect" decision
    # — see its docstring for the rules.
    cached = _summary_cache_response(row)
    if cached is not None:
        return cached

    # Slow path: cache miss. Acquire the per-entry lock so two
    # concurrent requests for the same entry can't both run the
    # fetch + LLM chain. ``session.get`` is on a session-scoped
    # connection, so the row handle ``row`` may be stale by the
    # time we acquire the lock (another request holding the lock
    # for a few seconds could have written the cache in the
    # meantime). Re-read the row inside the critical section —
    # that's the second half of the double-checked locking
    # pattern: the first half was the fast-path check above, the
    # second is this re-read. If the re-read sees a populated
    # cache, return it; otherwise we're the request that owns
    # the work, run the chain, and write.
    async with await _get_summary_lock(entry_id):
        await session.refresh(row)
        cached = _summary_cache_response(row)
        if cached is not None:
            return cached

        final = ""
        if _is_reddit_url(row.url):
            # Reddit's main site is a client-rendered SPA — the
            # standard ``fetch_article_text`` (HTML fetch +
            # trafilatura) returns empty for every ``reddit.com``
            # URL because the post body never lands in the initial
            # HTML payload. Use the thread's ``.rss`` instead: the
            # first entry is the post itself, with full body text
            # in ``<content>``. The cleaned post text becomes the
            # article body for the LLM call. If no LLM is
            # configured, we fall through to the post text
            # directly — the ``_truncate_summary`` step at the end
            # caps it to ``_SUMMARY_MAX_CHARS`` so we never dump
            # thousands of words into a card.
            post_body = await _fetch_reddit_post_body(row.url)
            if post_body:
                if llm_router.providers_for("brief"):
                    llm_summary = await summarize_article(
                        row.title, post_body
                    )
                    if llm_summary:
                        final = _truncate_summary(llm_summary.strip())
                if not final:
                    # No LLM or LLM call failed. The post body
                    # itself is a usable summary — at least it
                    # gives the user the OP's question / context,
                    # which is the whole point of expanding the
                    # card. Truncated to ``_SUMMARY_MAX_CHARS``
                    # below.
                    final = post_body
        elif llm_router.providers_for("brief"):
            article_text = await fetch_article_text(row.url)
            if article_text:
                llm_summary = await summarize_article(row.title, article_text)
                if llm_summary:
                    final = _truncate_summary(llm_summary.strip())

        if not final:
            final = _extract_fallback_summary(row)

        # Persist even when empty — that's the cache-hit signal for
        # next time. Without the persist, every chevron tap would
        # re-run the fetch / extract / LLM chain. ``fetched_at`` is
        # set on every write (including the empty-string write) so
        # the next chevron tap's retry-window check has a real
        # timestamp to compare against.
        row.cached_summary = final
        row.cached_summary_fetched_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()

        return EntrySummaryOut(summary=final, cached=False)


@router.post(
    "/entries/{entry_id}/podcast_summary",
    response_model=EntryPodcastSummaryOut,
    dependencies=_write_deps,
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

    # Fast path: cache hit. ``podcast_transcript_summary is not
    # None`` means we already attempted (success or empty); the
    # per-row column is the source of truth and a populated
    # entry never re-runs the fetch + transcribe + LLM chain.
    if row.podcast_transcript_summary is not None:
        return EntryPodcastSummaryOut(
            summary=row.podcast_transcript_summary, cached=True, available=True,
        )

    meta = row.meta or {}
    transcript_url = meta.get("transcript_url")
    transcript_type = meta.get("transcript_type") or ""
    audio_url = meta.get("audio_url")

    # Pre-lock bail-out: the entry has no transcript AND no
    # ASR-capable audio AND no configured Groq key. There's
    # nothing for the lock to protect — the result is the same
    # no matter how many requests arrive. Bail before the
    # lock so a feed-wide "no transcripts, no audio" sweep
    # doesn't pin 256+ per-entry locks.
    if not (
        (isinstance(transcript_url, str) and transcript_url)
        or (isinstance(audio_url, str) and audio_url and asr_available())
    ):
        return EntryPodcastSummaryOut(summary=None, cached=False, available=False)

    # Slow path: cache miss AND we have something to fetch.
    # Per-entry lock so two concurrent requests for the same
    # episode don't both run the (expensive) transcript fetch
    # + ASR + LLM chain. Re-read inside the lock — a
    # concurrent request that held the lock for the few
    # seconds of the chain will have populated the cache by
    # the time we get here.
    async with await _get_summary_lock(entry_id):
        await session.refresh(row)
        if row.podcast_transcript_summary is not None:
            return EntryPodcastSummaryOut(
                summary=row.podcast_transcript_summary, cached=True, available=True,
            )

        transcript_text = None
        if isinstance(transcript_url, str) and transcript_url:
            transcript_text = await fetch_transcript_text(transcript_url, transcript_type)
        elif isinstance(audio_url, str) and audio_url and asr_available():
            transcript_text = await transcribe_audio(audio_url)
        # The pre-lock check above already short-circuited the
        # "neither transcript nor audio + ASR" case, so we don't
        # need the `else` here.

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


@router.post(
    "/entries/{entry_id}/reddit_comment_summary",
    response_model=EntryRedditCommentSummaryOut,
    dependencies=_write_deps,
)
async def entry_reddit_comment_summary_endpoint(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> EntryRedditCommentSummaryOut:
    """Return (or fetch + generate + cache) an LLM-written summary of
    a Reddit thread's comment discussion.

    Cache semantics mirror ``/podcast_summary``, with one addition:
      - ``reddit_comment_summary is None`` → never attempted → fetch
        the thread's comments and summarize + persist.
      - ``reddit_comment_summary == ""``   → attempted, no usable
        result (fetch/parse failed / no LLM configured / LLM
        returned nothing) → return empty without re-attempting.
      - populated                           → return cached.
      - rate-limited right now (Reddit's direct-mode fetch allows
        only ~1 request/75s — see ``app.reddit_client``) → NOT
        cached, since a retry shortly after could succeed. Reported
        via ``rate_limited=True`` rather than folded into the
        ordinary "no summary" empty-string case, so the frontend can
        tell the user to try again in a moment instead of implying
        there's nothing to discuss.
    """
    row = await session.get(Entry, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")

    # Fast path: cache hit.
    if row.reddit_comment_summary is not None:
        return EntryRedditCommentSummaryOut(
            summary=row.reddit_comment_summary, cached=True, available=True,
        )

    # Pre-lock bail-out: no thread URL stamped on this entry.
    # The cross-reference sweep populates ``reddit_thread_url``
    # for non-Reddit sources that are cross-referenced to a
    # thread, and (since slice 31) the dynamic_reddit source
    # populates it for Reddit-source entries on every ingest. So
    # a missing field only happens for entries ingested BEFORE
    # slice 31 — those rows are still in the DB, and we want the
    # comment-summary path to work for them too. Fall back to
    # ``row.url`` when the entry itself IS a Reddit thread (the
    # thread URL and the entry URL are the same for Reddit-source
    # entries) — same effect, no DB migration needed.
    meta = row.meta or {}
    thread_url = meta.get("reddit_thread_url")
    if (not isinstance(thread_url, str) or not thread_url) and _is_reddit_url(row.url):
        thread_url = row.url
    if not isinstance(thread_url, str) or not thread_url:
        # Nothing to cache — a future cross-reference sweep tick
        # could still stamp this entry with a thread URL later.
        return EntryRedditCommentSummaryOut(summary=None, cached=False, available=False)

    # Slow path: cache miss, thread URL present. Per-entry
    # lock so two concurrent requests for the same entry
    # don't both pay for the Reddit fetch + LLM. Re-read
    # inside the lock — a concurrent request that held the
    # lock for the few seconds of the chain will have
    # populated the cache by the time we get here.
    #
    # Note: the rate-limited short-circuit is INSIDE the
    # lock. We don't cache rate-limited responses (a retry
    # shortly after could succeed), so the lock's only
    # purpose is to deduplicate the work that follows; a
    # rate-limited response from one request doesn't help
    # any other request (they'd all hit the same rate
    # limit). The 2nd-3rd-... requests will queue on the
    # lock and each return their own rate-limited response,
    # which is correct — they all "didn't do the work"
    # for the same reason.
    async with await _get_summary_lock(entry_id):
        await session.refresh(row)
        if row.reddit_comment_summary is not None:
            return EntryRedditCommentSummaryOut(
                summary=row.reddit_comment_summary, cached=True, available=True,
            )

        try:
            comments = await reddit_client.fetch_thread_comments(thread_url)
        except reddit_client.RedditRateLimited:
            return EntryRedditCommentSummaryOut(
                summary=None, cached=False, available=True, rate_limited=True,
            )

        summary = None
        if comments:
            summary = await summarize_comments(row.title, comments)

        # Persist even on failure (empty string) — same rationale as the
        # other summary caches. The rate-limited case above already
        # returned before reaching here, so this only covers genuine
        # failures (no thread found, malformed feed, no LLM configured).
        row.reddit_comment_summary = summary or ""
        await session.commit()

        return EntryRedditCommentSummaryOut(summary=summary, cached=False, available=True)


@router.get(
    "/entries/{entry_id}/related",
    response_model=Optional[FramingClusterOut],
)
async def entry_related_endpoint(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> FramingClusterOut | None:
    """Other outlets' coverage of the same story as this entry, if
    any — reuses Framing Watch's existing clustering
    (``app.framing.cluster_recent_entries``, the same hourly job that
    powers the standalone Framing Watch section) rather than a
    separate "related articles" algorithm or a live similarity
    search. This is the same data, just re-surfaced per-card so a
    reader doesn't have to scroll up and cross-reference the Framing
    Watch section themselves to see how other outlets are covering
    (and titling) the story they're currently reading.

    A GET, not a POST like the summary endpoints — this is a plain
    read (a join against already-computed clustering state), not
    something that fetches, calls an LLM, or writes a cache column,
    so there's nothing to distinguish "cached" from "fresh" and no
    reason to require a mutating verb.

    Returns ``None`` (not 404) when this entry isn't part of a
    detected cluster — the overwhelming majority of entries, since
    clustering only fires for stories multiple configured outlets
    happen to cover in the same window. The frontend uses this to
    hide the "Related coverage" affordance entirely rather than
    showing an empty panel. The current entry itself is excluded
    from the returned ``articles`` — the reader is already looking
    at it; showing it again in its own "other coverage" list would
    be redundant.
    """
    row = await session.get(Entry, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    if row.story_cluster_id is None:
        return None

    stmt = (
        select(
            Entry.id,
            Entry.title,
            Entry.url,
            Entry.published_at,
            Entry.framing_tone,
            Source.name.label("source_name"),
            Source.favicon_path,
            StoryCluster.wire_source,
            StoryCluster.first_seen_at,
        )
        .join(Source, Entry.source_id == Source.id)
        .join(StoryCluster, Entry.story_cluster_id == StoryCluster.id)
        .where(Entry.story_cluster_id == row.story_cluster_id, Entry.id != entry_id)
        .order_by(Entry.published_at.asc().nullslast())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        # A cluster technically exists but every other member has
        # since been deleted/purged — nothing left to show.
        return None

    return FramingClusterOut(
        cluster_id=row.story_cluster_id,
        wire_source=rows[0].wire_source,
        first_seen_at=rows[0].first_seen_at,
        articles=[
            FramingArticleOut(
                entry_id=r.id,
                title=r.title,
                url=r.url,
                source_name=r.source_name,
                favicon_path=r.favicon_path,
                published_at=r.published_at,
                framing_tone=r.framing_tone,
            )
            for r in rows
        ],
    )

