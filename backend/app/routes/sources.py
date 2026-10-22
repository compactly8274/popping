"""Source listing + management endpoints.

GET endpoints have always been public-read. Phase 5 adds POST/PATCH/
DELETE that mutate the ``sources`` table, gated by the same auth
dependency the manual ``/api/ingest/{name}`` route uses — wide-open
when OIDC is off (single-user LAN deployment), login-required when
it's on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Sequence
from urllib.parse import urlparse

import httpx

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.apple_podcasts import PodcastResolutionError, resolve_feed_url
from app.youtube import (
    YouTubeResolutionError,
    is_youtube_url,
    normalize_youtube_input,
    resolve_channel_feed_url,
    youtube_source_slug,
)
from app.auth.deps import current_user, require_user
from app.config import settings
from app.db import SessionLocal, get_session
from app.feed_autodiscovery import discover_feed_url, discover_sitemap_urls
from app.feed_discovery import discover_candidates, recent_llm_candidate_count
from app.feed_recommendations import (
    aggregation_user_ids,
    recommendations_for_user,
    top_category_for_user,
)
from app.models import Entry, Interaction, Source
from app.schemas import (
    CheckNowRequest,
    CheckNowResult,
    FeedDiscoverRequest,
    FeedDiscoverResult,
    FeedRecommendation,
    SourceAutoRequest,
    SourceAutoResult,
    SourceCreate,
    SourceOut,
    SourceTestRequest,
    SourceTestResult,
    SourceUpdate,
)
from app.sources import list_sources as registered_plugin_names
from app.sources.reddit import normalize_subreddit
from app.url_safety import check_url_safe
from app import reddit_client, scheduler

router = APIRouter(tags=["sources"])

logger = logging.getLogger("popping.routes.sources")

# Auth: matches the pattern in ``routes/ingest.py``. When OIDC is on,
# POST/PATCH/DELETE require a logged-in user; GETs stay open. When
# OIDC is off (single-user LAN), the bypass grants the same identity
# the manual ingest endpoint already accepts.
_write_deps = [Depends(require_user)] if settings.oidc_enabled else []

# Strong references to in-flight auto-discovery tasks — same reason
# ``routes/brief.py``'s ``_RUNNING_TASKS`` exists: ``asyncio.create_task``
# returns a weakly-held Task, so a fire-and-forget discovery call could
# be garbage-collected mid-flight. Each task removes itself on completion.
_DISCOVERY_TASKS: set[asyncio.Task] = set()

# Skip scheduling the auto-discovery background call once this many
# LLM-sourced candidates already landed in the category recently (see
# ``app.feed_discovery.recent_llm_candidate_count`` for the window).
# Bounds LLM spend if the user adds several feeds in the same
# category in quick succession — each add would otherwise fire its
# own discovery call.
_AUTO_DISCOVERY_COOLDOWN_CAP = 3


async def _run_auto_discovery(category: str, context: str, discovered_from_source_id: int) -> None:
    """Background: ask the LLM for a few more feeds like the one the
    user just added. Uses its own session (the request-scoped one is
    closed by the time this runs). Errors are logged and swallowed —
    this is best-effort enrichment of the Recommended pool, never
    something the Add-source call should fail on.
    """
    try:
        async with SessionLocal() as session:
            if await recent_llm_candidate_count(session, category) >= _AUTO_DISCOVERY_COOLDOWN_CAP:
                return
            await discover_candidates(
                session,
                category=category,
                context=context,
                discovered_from_source_id=discovered_from_source_id,
            )
    except Exception:
        logger.exception("auto feed discovery failed for category=%s", category)
    finally:
        task = asyncio.current_task()
        if task is not None:
            _DISCOVERY_TASKS.discard(task)


# --- Validation helpers --------------------------------------------------

# Source names are user-facing (column headers, filter chips, error
# messages) so the regex is conservative: lowercase letters, digits,
# and underscore only, 1-120 chars. Matches the existing built-in
# plugin names ("bbc_news", "hn_top", etc.) so the UI doesn't render
# a row whose name has a different shape than the rest.
# Used with ``fullmatch``: ``^...$`` with ``match`` accepted a trailing
# newline ("abc\n"), which could then reach a job id or a log line.
_NAME_RE = re.compile(r"[a-z0-9_]{1,120}")

# Refresh intervals are clamped so a typo can't accidentally turn a
# feed into "refresh every 1 second" (DB spam, rate-limit hits). 60s
# is the lower bound — anything tighter than that should be a cron
# job, not a polling loop. 24h is the upper bound — feeds slower than
# that don't justify a per-row scheduler job.
_REFRESH_MIN = 60
_REFRESH_MAX = 86_400

# Default refresh for ``type="reddit"`` rows. Reddit moves faster
# than news but slower than HN — 15 min matches the per-subreddit
# plugin's hard-coded default and gives the user a sane starting
# point. The inline editor can override per-row.
_REDDIT_DEFAULT_REFRESH = 900

# Default refresh for ``type="podcast"`` rows. Episodes publish
# weekly/daily at most — polling every hour (the plain-RSS default)
# just wastes a scheduler tick and a request to the host for months
# at a time between episodes. 6h is generous headroom for same-day
# discovery without hammering the feed.
_PODCAST_DEFAULT_REFRESH = 21_600

# Default refresh for ``type="youtube_channel"`` rows. Same rationale
# as podcasts — most channels post far less than hourly, so the
# plain-RSS default would just poll a feed that isn't changing.
_YOUTUBE_DEFAULT_REFRESH = 21_600

# Default refresh for ``type="generic_scrape"`` rows (see
# app.sources.generic_scrape). A single poll can mean up to
# _MAX_NEW_PER_POLL real page fetches against the target site, not
# just one lightweight feed request — considerably heavier per-tick
# than every other source type, so this defaults much longer (2h)
# out of courtesy to sites that never opted into being polled the
# way a real feed's operator implicitly did.
_GENERIC_SCRAPE_DEFAULT_REFRESH = 7_200

# Mirrors ``Source.category``'s ``String(40)``. A PATCH / POST with a
# longer string would crash on the DB layer with a Postgres
# ``value too long for type character varying(40)`` — surfaced as a
# 500 to the client. Catching it here returns a clean 422.
_CATEGORY_MAX = 40

# Column names the frontend reserves for non-category columns (see
# ``App.tsx``'s ``allSubsColumns``, which always prepends a "Saved"
# and a "For You" entry ahead of the per-category ones). Those
# columns are keyed by name in a ``Map``, so a user-created category
# with one of these names would silently collide with — and
# overwrite — the real Saved/For You column. Case-insensitive
# because a differently-cased duplicate ("saved") is just as
# confusing even though it wouldn't literally collide. Underscores,
# dashes and runs of spaces count as one space too, because the
# frontend's label turns "for_you" into "For You" (see
# ``_category_fold``).
_RESERVED_CATEGORY_NAMES = {"saved", "for you"}

# Headers we refuse to let users override via ``custom_headers``.
# ``Cookie`` / ``Authorization`` would let a multi-tenant LAN user
# forge another user's session; ``Host`` is set by httpx from the
# URL. The rest are accepted but rarely useful — keep the denylist
# short and obvious so the validator is easy to audit.
_HEADER_DENYLIST = frozenset({"cookie", "authorization", "host"})

# Headers we also reject because they're request-shaping rather
# than request-data: ``X-Forwarded-For`` would let a user spoof the
# client IP seen by the upstream, and ``X-Real-IP`` / ``X-Original-URL``
# are reverse-proxy conventions. None of these are useful for
# bypassing CDN UA blocks (the legitimate use case for the override
# map) and they all change the trust surface.
#
# The second group is message framing and hop-by-hop headers. A
# user-set Content-Length or Transfer-Encoding on a GET can make the
# HTTP client fail or mis-frame the request, and Proxy-* names would
# reach the optional egress guard-proxy for plain-http feeds.
_HEADER_DENYLIST_EXTRA = frozenset(
    {
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "x-original-url",
        "x-rewrite-url",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "expect",
        "proxy-authorization",
        "proxy-connection",
    }
)
_HEADER_DENYLIST_ALL = _HEADER_DENYLIST | _HEADER_DENYLIST_EXTRA

# Bound the size of an override map. Real UAs are ~120 chars; Allow
# some headroom for ``Accept-Language`` etc. but reject obvious
# abuse (a multi-MB blob in the DB).
_CUSTOM_HEADERS_MAX = 20
_HEADER_VALUE_MAX = 512
_HEADER_NAME_MAX = 100

# Header names must be RFC 7230 tokens. Checked before the deny list,
# so "Coo kie" is refused as an invalid name, not a denied one.
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# Stored in their usual spelling. The fetchers merge custom headers
# into their defaults with a case-sensitive dict (sources/rss.py and
# article_extract.py), so a lower-case "user-agent" would otherwise
# send two User-Agent headers.
_CANONICAL_HEADER_NAMES = {
    "user-agent": "User-Agent",
    "accept": "Accept",
    "accept-language": "Accept-Language",
}

# Characters disallowed in header VALUES. httpx / urllib will pass
# arbitrary bytes through; if a value contains ``\r\n`` the upstream
# HTTP parser can interpret them as additional headers, which is a
# classic CRLF injection / response-splitting primitive (CWE-93 /
# CWE-113). Reject anything outside printable ASCII plus tab.
#
# Real header values are ASCII text; rejecting non-ASCII keeps the
# door closed on smuggling tricks that exploit encoders that map
# non-ASCII bytes to ASCII control codes (e.g. ISO-2022-JP). ``\0``
# is also banned because some libraries truncate on NUL and a
# truncated value can become a different header in the log/trace.
import re as _re

_BAD_HEADER_VALUE_RE = _re.compile(r"[^\t\x20-\x7e]")
"""Match any byte outside tab and printable ASCII (0x20-0x7e)."""


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail="name must match ^[a-z0-9_]{1,120}$ (lowercase letters, digits, underscore)",
        )


def _validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(status_code=422, detail="url is not a valid URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="url must be http or https")
    if not parsed.netloc:
        raise HTTPException(status_code=422, detail="url must include a host")
    # SSRF guard. Reject URLs whose host resolves (or is a literal
    # for) any private / loopback / link-local / metadata range.
    # Done at the route layer so the row never lands in the DB with a
    # hostile URL — the scheduler then has no chance of triggering it.
    safe, reason = check_url_safe(url)
    if not safe:
        # Don't echo the reason back to the user — it leaks which
        # internal subnets we care about. Generic "url is not
        # reachable" is enough for the UI to surface a clean error.
        raise HTTPException(
            status_code=422,
            detail="url is not reachable (must be a public host)",
        )


def _validate_reddit_url(url: str) -> str:
    """Validate a subreddit reference and return the canonical
    ``https://www.reddit.com/r/<slug>`` URL.

    Accepts the same loose inputs ``app.sources.reddit.normalize_subreddit``
    does (``r/python``, ``/r/python``, ``https://reddit.com/r/python``,
    bare slugs). Rejects everything else with 422. The canonical URL
    is what's stored in ``Source.url`` so the user sees a uniform
    shape in the source list and so any future cross-ref against the
    row knows exactly what URL the user added (vs whatever the user
    typed in).

    Mirrors ``_validate_url``'s 422-on-failure shape — no separate
    error code for "this looks like an RSS feed" (a Reddit URL also
    parses as http/https) because the route layer dispatches by
    ``body.type`` before calling either validator. The canonical
    Reddit URL is always a public host (``reddit.com``) so it clears
    ``check_url_safe`` without an explicit call.
    """
    sub = normalize_subreddit(url)
    if not sub:
        raise HTTPException(
            status_code=422,
            detail=(
                "reddit url must be a subreddit reference "
                "(e.g. 'r/python' or 'https://www.reddit.com/r/python')"
            ),
        )
    return f"https://www.reddit.com/r/{sub}"


def _validate_refresh(value: int) -> int:
    clamped = max(_REFRESH_MIN, min(_REFRESH_MAX, value))
    if clamped != value:
        # The user asked for something out of range; silently clamp
        # rather than 422 — the UI sends what the user picked from a
        # preset dropdown, so a mismatch means a stale preset, not
        # malice. Log via the response is overkill; the value is
        # visible in the returned row.
        return clamped
    return value


def _category_fold(value: str) -> str:
    """Lower-case, and treat runs of underscores, dashes and spaces as
    one space. Same rule as ``fold`` in the frontend's categories lib,
    so the phone refuses exactly what the server refuses."""
    return re.sub(r"[_\s-]+", " ", value.lower()).strip()


def _validate_category(value: str) -> str:
    """Return the stripped category, or raise 422. Callers store the
    returned value, so " Tech " is saved as "Tech". No lower-casing:
    entries are grouped by the raw string."""
    stripped = value.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="category cannot be empty")
    if len(stripped) > _CATEGORY_MAX:
        # Mirror the column constraint instead of letting Postgres
        # raise ``value too long for type character varying(40)`` —
        # that path leaks as a 500 to the user.
        raise HTTPException(
            status_code=422,
            detail=f"category must be {_CATEGORY_MAX} characters or fewer",
        )
    if _category_fold(stripped) in _RESERVED_CATEGORY_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"category {stripped!r} is reserved for a built-in column",
        )
    return stripped


def _validate_custom_headers(value: dict | None) -> dict | None:
    """Validate the ``custom_headers`` payload. Returns a fresh dict
    (or None) ready for persistence. Keys are case-insensitive —
    httpx sends them verbatim but most servers normalize on lookup.
    Empty dict becomes NULL so an explicit "clear the override" PATCH
    doesn't keep a useless empty map in the DB."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=422, detail="custom_headers must be a JSON object"
        )
    if len(value) > _CUSTOM_HEADERS_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"custom_headers may have at most {_CUSTOM_HEADERS_MAX} entries",
        )
    out: dict[str, str] = {}
    seen: set[str] = set()
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(
                status_code=422,
                detail="custom_headers keys and values must be strings",
            )
        if not _HEADER_NAME_RE.fullmatch(k):
            raise HTTPException(
                status_code=422,
                detail=f"header name {k!r} is not a valid HTTP header name",
            )
        if len(k) > _HEADER_NAME_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"header name {k!r} is longer than {_HEADER_NAME_MAX} characters",
            )
        k = _CANONICAL_HEADER_NAMES.get(k.lower(), k)
        if k.lower() in seen:
            raise HTTPException(
                status_code=422,
                detail=f"header {k!r} is listed twice",
            )
        seen.add(k.lower())
        if k.lower() in _HEADER_DENYLIST_ALL:
            raise HTTPException(
                status_code=422,
                detail=f"header {k!r} cannot be overridden per-source",
            )
        if len(v) > _HEADER_VALUE_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"header {k!r} value exceeds {_HEADER_VALUE_MAX} chars",
            )
        # CRLF / control-char check (CWE-93 / CWE-113). Anything
        # outside tab + printable ASCII is rejected — httpx will
        # happily send it but an upstream parser may interpret
        # ``\r\n`` as a header boundary, smuggling a second header
        # through the request.
        if _BAD_HEADER_VALUE_RE.search(v):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"header {k!r} value contains disallowed characters "
                    "(must be printable ASCII or tab)"
                ),
            )
        out[k] = v
    return out or None


# --- GETs (read-only, public) --------------------------------------------


async def _net_vote_scores(session: AsyncSession) -> dict[int, int]:
    """Net thumbs score per source: sum(thumb_up) - sum(thumb_down)
    across all of a source's entries. Powers the FeedManager's
    per-row score badge so the user can spot a source they
    consistently downvote and clean it up — the exact "if I
    downvote 5 times it's -5, upvote 5 more and it's back to 0"
    semantics, computed straight off the same ``interactions`` log
    the vote buttons already write to (no new table).

    A source with no thumb_up/thumb_down interactions at all is
    simply absent from the returned dict — callers default to 0.
    One query, aggregated in SQL rather than pulled row-by-row.
    """
    stmt = (
        select(
            Entry.source_id,
            func.sum(
                case(
                    (Interaction.type == "thumb_up", 1),
                    (Interaction.type == "thumb_down", -1),
                    else_=0,
                )
            ),
        )
        .select_from(Interaction)
        .join(Entry, Entry.id == Interaction.entry_id)
        .where(Interaction.type.in_(("thumb_up", "thumb_down")))
        .group_by(Entry.source_id)
    )
    rows = (await session.execute(stmt)).all()
    return {source_id: int(score or 0) for source_id, score in rows}


# The windows ``_health_counts`` counts stories in.
_STORIES_DAY = dt.timedelta(hours=24)
_STORIES_WEEK = dt.timedelta(days=7)


async def _health_counts(
    session: AsyncSession, ids: Sequence[int] | None
) -> dict[int, dict]:
    """Story counts per source id: ``stories_24h``, ``stories_7d``,
    ``newest_story_at`` and ``has_stories``, the computed half of
    ``SourceOut``'s feed-health fields. Every endpoint that returns a
    source fills them (``_source_out``), so a row read back after a
    save carries the same counts as the list, and ``GET
    /sources/{id}`` (polled while a Check now runs) stays one cheap
    query. ``ids`` None counts every feed (the list); otherwise only
    those ids, which are absent from the result when they have no row.

    One round trip. The counts are one ``GROUP BY`` over the last 7
    days of ``fetched_at`` (``ix_entries_fetched_at``): the 24-hour
    count is a ``FILTER`` on it and ``newest_story_at`` is its
    ``max(fetched_at)``, so it is null when nothing arrived this week.
    An all-time ``max`` would walk the whole index for a feed with no
    rows at all. ``has_stories`` is a correlated ``EXISTS`` per source
    on ``entries.source_id``.

    The list passes None rather than every id: an ``IN`` of every feed
    filters nothing, but the planner can still pair it with the week's
    range as a bitmap over the whole ``ix_entries_source_id`` index,
    which costs more than the week's scan itself once the week's rows
    are spread across the table.
    """
    if ids is not None and not ids:
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    week = (
        select(
            Entry.source_id.label("source_id"),
            func.count().filter(Entry.fetched_at >= now - _STORIES_DAY).label("stories_24h"),
            func.count().label("stories_7d"),
            func.max(Entry.fetched_at).label("newest_story_at"),
        )
        .where(Entry.fetched_at >= now - _STORIES_WEEK)
        .group_by(Entry.source_id)
    )
    if ids is not None:
        week = week.where(Entry.source_id.in_(ids))
    counts = week.subquery()
    stmt = select(
        Source.id,
        counts.c.stories_24h,
        counts.c.stories_7d,
        counts.c.newest_story_at,
        exists().where(Entry.source_id == Source.id).label("has_stories"),
    ).outerjoin(counts, counts.c.source_id == Source.id)
    if ids is not None:
        stmt = stmt.where(Source.id.in_(ids))
    out: dict[int, dict] = {}
    for source_id, day, seven, newest, has_stories in (await session.execute(stmt)).all():
        out[source_id] = {
            "stories_24h": int(day or 0),
            "stories_7d": int(seven or 0),
            "newest_story_at": newest,
            "has_stories": bool(has_stories),
        }
    return out


def _source_out(row: Source, health: dict[int, dict]) -> SourceOut:
    """``SourceOut`` for ``row`` with its story counts from
    ``_health_counts`` (the defaults when ``row`` isn't in it)."""
    item = SourceOut.model_validate(row)
    for key, value in health.get(row.id, {}).items():
        setattr(item, key, value)
    return item


async def _one_source_out(session: AsyncSession, row: Source) -> SourceOut:
    """``SourceOut`` for one row, story counts included: what GET,
    POST, PATCH and ``/auto`` return."""
    return _source_out(row, await _health_counts(session, [row.id]))


@router.get("/sources", response_model=list[SourceOut])
async def list_sources_endpoint(
    session: AsyncSession = Depends(get_session),
) -> list[SourceOut]:
    rows = (await session.scalars(select(Source).order_by(Source.category, Source.name))).all()
    scores = await _net_vote_scores(session)
    health = await _health_counts(session, None)
    out: list[SourceOut] = []
    for r in rows:
        item = _source_out(r, health)
        item.net_vote_score = scores.get(r.id, 0)
        out.append(item)
    return out


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source_endpoint(
    source_id: int,
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    row = await session.get(Source, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return await _one_source_out(session, row)


# --- POST / PATCH / DELETE (Phase 5) -------------------------------------


def _clean_display_name(value: str | None) -> str | None:
    """Trim a user-typed feed label; blank means "no label" (NULL)."""
    if value is None:
        return None
    value = " ".join(value.split())
    if len(value) > 120:
        raise HTTPException(status_code=422, detail="Keep the name to 120 characters or fewer.")
    return value or None


@router.post(
    "/sources",
    response_model=SourceOut,
    dependencies=_write_deps,
)
async def create_source_endpoint(
    body: SourceCreate,
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    """Add a new dynamic source.

    v1 accepts ``type="rss"``, ``type="reddit"``, ``type="podcast"``
    (podcast feeds are RSS-shaped — see ``app.sources.podcast``), and
    ``type="youtube_channel"`` (a channel's video feed is Atom-shaped
    RSS too — see ``app.sources.youtube``).
    """
    _validate_name(body.name)
    # Apple Podcasts catalog links (podcasts.apple.com/.../id<N>)
    # aren't a feed — resolve to the real RSS URL before validating.
    # No-op for anything that doesn't match the pattern.
    try:
        body.url = await resolve_feed_url(body.url)
    except PodcastResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Same idea for any shape of YouTube link (channel, handle, video,
    # playlist, feed link or bare channel id) — resolve to the feed.
    # No-op for anything that isn't YouTube. A bare @handle only counts
    # when the user picked the YouTube type.
    try:
        body.url = await resolve_channel_feed_url(
            body.url, allow_bare_handle=(body.type == "youtube_channel")
        )
    except YouTubeResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Type-aware URL validation. Reddit rows accept a subreddit
    # reference (``r/python``) and get rewritten to the canonical
    # Reddit thread URL; RSS, podcast, and YouTube channel rows take
    # a plain http(s) feed URL.
    if body.type == "reddit":
        url = _validate_reddit_url(body.url)
    else:
        _validate_url(body.url)
        url = body.url
    if body.type not in ("rss", "reddit", "podcast", "youtube_channel", "generic_scrape"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported type {body.type!r} (only 'rss', 'reddit', 'podcast', "
                "'youtube_channel', and 'generic_scrape' are accepted in this build)"
            ),
        )
    category = _validate_category(body.category)
    # Following the same YouTube feed twice would make two sources
    # with the same videos. Say which one already has it.
    if is_youtube_url(url) and "/feeds/videos.xml" in url:
        existing = await session.scalar(select(Source).where(Source.url == url))
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"You already follow this channel as {existing.name}.",
            )
    # ``refresh_interval_seconds`` is optional in the schema; default
    # to a per-type sensible value when absent. Reddit moves faster
    # than news RSS so 15 min vs 1h is the right baseline; podcasts
    # and YouTube channels are the opposite — episodes/videos are
    # infrequent, so 6h avoids polling a feed that won't change for
    # days.
    if body.refresh_interval_seconds is None:
        if body.type == "reddit":
            refresh = _REDDIT_DEFAULT_REFRESH
        elif body.type == "podcast":
            refresh = _PODCAST_DEFAULT_REFRESH
        elif body.type == "youtube_channel":
            refresh = _YOUTUBE_DEFAULT_REFRESH
        elif body.type == "generic_scrape":
            refresh = _GENERIC_SCRAPE_DEFAULT_REFRESH
        else:
            refresh = _REFRESH_MIN * 60
    else:
        refresh = _validate_refresh(body.refresh_interval_seconds)
    headers = _validate_custom_headers(body.custom_headers)
    # Slice 20: validate ``sitemap_url`` and ``link_pattern`` at
    # creation time. These are optional and only meaningful for
    # ``type='generic_scrape'`` rows — other types ignore them
    # silently. ``sitemap_url`` is parsed as an http(s) URL (same
    # validator the PATCH route uses); ``link_pattern`` must be a
    # leading-slash path prefix, never a full URL (same rule).
    sitemap_url_value = None
    if body.sitemap_url is not None:
        _validate_url(body.sitemap_url)
        sitemap_url_value = body.sitemap_url
    link_pattern_value = None
    if body.link_pattern is not None:
        candidate = body.link_pattern
        if not candidate.startswith("/"):
            raise HTTPException(
                status_code=422,
                detail="link_pattern must start with '/' (path prefix, not a full URL)",
            )
        if "://" in candidate:
            raise HTTPException(
                status_code=422,
                detail="link_pattern must be a path prefix, not a full URL",
            )
        link_pattern_value = candidate
    # Checked before the row exists, so a bad label can't leave a feed behind.
    display_name = _clean_display_name(body.display_name)
    row = await scheduler.add_source(
        session,
        name=body.name,
        type_=body.type,
        category=category,
        url=url,
        refresh=refresh,
        sitemap_url=sitemap_url_value,
        link_pattern=link_pattern_value,
        custom_headers=headers,
    )
    if display_name is not None:
        row.display_name = display_name
        await session.commit()
        await session.refresh(row)
    # Auto-expand the Recommended pool from what the user just added.
    # Reddit isn't in scope for LLM feed discovery (it only suggests
    # RSS/Atom URLs), so this only fires for rss/podcast rows.
    # Fire-and-forget: the Add call shouldn't wait on an LLM roundtrip.
    if body.type in ("rss", "podcast"):
        task = asyncio.create_task(
            _run_auto_discovery(
                category=row.category,
                context=f"For context, the user just added a feed named {row.name!r} ({row.url}).",
                discovered_from_source_id=row.id,
            )
        )
        _DISCOVERY_TASKS.add(task)
    return await _one_source_out(session, row)


def _slugify_hostname(url: str) -> str:
    """Turn a URL's hostname into a ``^[a-z0-9_]{1,120}$`` source
    name — the same shape ``_validate_name`` requires. Used only by
    the auto-feed flow below, where there's no user-supplied name to
    work with yet (unlike the regular "Add custom" form)."""
    host = (urlparse(url).hostname or "site").lower()
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return slug[:100] or "site"


async def _free_source_name(session: AsyncSession, base: str) -> str:
    """``base``, or ``base_2``, ``base_3``, ... — whichever is the
    first not already taken by an existing row or a built-in plugin
    name. ``scheduler.add_source`` is idempotent-on-name (a colliding
    name silently returns the EXISTING row instead of creating a new
    one), which is the right behavior for a literal retry but wrong
    here: two different sites whose hostnames happen to slugify the
    same way would otherwise silently collapse into one source."""
    candidate = base
    suffix = 2
    reserved = set(registered_plugin_names())
    while True:
        if candidate not in reserved:
            existing = await session.scalar(select(Source).where(Source.name == candidate))
            if existing is None:
                return candidate
        candidate = f"{base}_{suffix}"
        suffix += 1


@router.post(
    "/sources/auto",
    response_model=SourceAutoResult,
    dependencies=_write_deps,
)
async def source_auto_endpoint(
    body: SourceAutoRequest,
    session: AsyncSession = Depends(get_session),
) -> SourceAutoResult:
    """"Auto feed" — paste any URL, get a source added in one step.

    Tries, in order:
      1. An existing RSS/Atom feed, either the URL itself or one it
         links to (``app.feed_autodiscovery.discover_feed_url``). If
         found, creates a normal ``type="rss"`` source — indistinguishable
         from one the user found and added manually.
      2. The site's sitemap (``app.feed_autodiscovery.
         discover_sitemap_urls``) — if it yields at least one URL,
         creates a ``type="generic_scrape"`` source instead, which
         periodically re-checks the sitemap and extracts new articles
         via ``app.sources.generic_scrape`` even though the site never
         published a feed at all.

    Returns ``found=False`` (not an error status) when neither path
    turns up anything — a dead URL, an unreachable host, or a site
    genuinely too dynamic/unusual for sitemap-based discovery to
    work on isn't a bug in this endpoint, just an honest "couldn't
    find anything here."

    The source name is derived from the URL's hostname (there's
    nothing else to name it from at this point) and de-duplicated
    against existing rows — see ``_free_source_name``. The user can
    rename it afterward from the My feeds tab like any other source.

    Any YouTube link, bare channel id or bare @handle takes its own
    branch first: it resolves to the YouTube feed and creates a
    ``youtube_channel`` source (no discovery task). When a source
    already follows that feed, it is returned and nothing is created.
    """
    youtube_input = normalize_youtube_input(body.url, allow_bare_handle=True)
    if is_youtube_url(youtube_input):
        category = _validate_category(body.category)
        try:
            yt_feed = await resolve_channel_feed_url(youtube_input, allow_bare_handle=True)
        except YouTubeResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        _validate_url(yt_feed)
        existing = await session.scalar(select(Source).where(Source.url == yt_feed))
        if existing is not None:
            return SourceAutoResult(
                found=True, kind="youtube_channel", source=await _one_source_out(session, existing)
            )
        name = await _free_source_name(session, youtube_source_slug(youtube_input, yt_feed))
        row = await scheduler.add_source(
            session,
            name=name,
            type_="youtube_channel",
            category=category,
            url=yt_feed,
            refresh=_YOUTUBE_DEFAULT_REFRESH,
        )
        return SourceAutoResult(found=True, kind="youtube_channel", source=await _one_source_out(session, row))

    _validate_url(body.url)
    category = _validate_category(body.category)

    feed_url = await discover_feed_url(body.url)
    if feed_url:
        name = await _free_source_name(session, _slugify_hostname(feed_url))
        row = await scheduler.add_source(
            session,
            name=name,
            type_="rss",
            category=category,
            url=feed_url,
            refresh=_REFRESH_MIN * 60,
        )
        task = asyncio.create_task(
            _run_auto_discovery(
                category=row.category,
                context=f"For context, the user just auto-added a feed named {row.name!r} ({row.url}).",
                discovered_from_source_id=row.id,
            )
        )
        _DISCOVERY_TASKS.add(task)
        return SourceAutoResult(found=True, kind="rss", source=await _one_source_out(session, row))

    sitemap_urls = await discover_sitemap_urls(body.url, limit=1)
    if sitemap_urls:
        name = await _free_source_name(session, _slugify_hostname(body.url))
        row = await scheduler.add_source(
            session,
            name=name,
            type_="generic_scrape",
            category=category,
            url=body.url,
            refresh=_GENERIC_SCRAPE_DEFAULT_REFRESH,
        )
        return SourceAutoResult(found=True, kind="generic_scrape", source=await _one_source_out(session, row))

    return SourceAutoResult(found=False)


@router.patch(
    "/sources/{source_id}",
    response_model=SourceOut,
    dependencies=_write_deps,
)
async def update_source_endpoint(
    source_id: int,
    body: SourceUpdate,
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    # Fetch the existing row FIRST. The URL-validator block below
    # needs ``pre.type`` to decide which validator to run, and the
    # built-in guard at the bottom needs ``pre.name`` and
    # ``pre.type`` to enforce the "no rename / no re-URL on built-ins"
    # invariant. Fetching here means a missing row returns 404 before
    # we run any other validator (so the user sees a clean error
    # rather than a cascade of 422s on a PATCH aimed at a deleted
    # source).
    pre = await session.get(Source, source_id)
    if pre is None:
        raise HTTPException(status_code=404, detail="source not found")

    # Pre-validate before hitting the scheduler. The row-level guards
    # (built-in rejection, name collision) need a DB lookup; do them
    # here so the user sees a clean 400/409 rather than a generic 500
    # from a SQLAlchemy IntegrityError or a defensive guard deeper in
    # the scheduler.
    if body.name is not None:
        _validate_name(body.name)
        # Reject names that collide with a registered plugin. A
        # dynamic row that takes a built-in name would silently become
        # "class-driven" at the next scheduler check (see
        # ``scheduler.update_source``'s ``row.name in registered_names``
        # branch), so the route layer enforces the invariant.
        if body.name in registered_plugin_names():
            raise HTTPException(
                status_code=400,
                detail=f"name {body.name!r} is reserved for a built-in source",
            )
    # Built-in rows can be paused / re-categorized (these are
    # row-level state, not class-level). Renaming or re-URLing a
    # built-in is not allowed — the registry key is fixed, and the
    # URL is bound to the plugin class's fetch logic. Checked before
    # any URL resolution so a refused PATCH costs no network call.
    if pre.name in registered_plugin_names() and (
        body.name is not None or body.url is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"built-in source {pre.name!r} cannot be renamed or re-URLed",
        )
    if body.url is not None:
        # Apple Podcasts catalog links aren't a feed — resolve to the
        # real RSS URL before validating. No-op for anything that
        # doesn't match the pattern.
        try:
            body.url = await resolve_feed_url(body.url)
        except PodcastResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        # Same idea for any shape of YouTube link — no-op for
        # anything that isn't YouTube. A bare @handle only counts on a
        # YouTube row.
        try:
            body.url = await resolve_channel_feed_url(
                body.url, allow_bare_handle=(pre.type == "youtube_channel")
            )
        except YouTubeResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        # PATCH URL is also type-aware: re-validate as a subreddit
        # reference when the row is a Reddit row (so a typo'd edit
        # doesn't store a malformed URL the plugin can't fetch).
        # For non-Reddit rows fall through to the plain URL check.
        if pre.type == "reddit":
            body.url = _validate_reddit_url(body.url)
        else:
            _validate_url(body.url)
    # ``sitemap_url`` is an optional override for generic_scrape
    # sources only. ``None`` means "clear back to homepage-based
    # discovery"; any string is validated as an http(s) URL. The
    # plugin only reads this on ``generic_scrape`` rows; setting it
    # on other types is silently stored-but-ignored. (``custom_headers``
    # is different: generic_scrape does send them, on its article page
    # fetches.)
    # ``update_source`` uses the ``_UNSET`` sentinel to distinguish
    # "field missing from the request body, don't touch" from
    # "explicitly set to None, clear it". Default to ``_UNSET`` here
    # and only override it when the field was actually present in
    # the body — passing a bare ``None`` for an omitted field would
    # wipe out the row's existing value on every unrelated PATCH.
    sitemap_url_value: str | None = scheduler._UNSET
    if "sitemap_url" in body.model_fields_set:
        if body.sitemap_url is not None:
            _validate_url(body.sitemap_url)
        sitemap_url_value = body.sitemap_url
    # ``link_pattern`` is a path-prefix filter for the page_links
    # fallback. Strict validation: must be a leading-slash path
    # prefix (e.g. ``/library/``), NEVER a full URL — accepting a
    # full URL would let the user bypass the same-origin safety
    # check that ``_extract_links_from_html`` enforces.
    link_pattern_value: str | None = scheduler._UNSET
    if "link_pattern" in body.model_fields_set:
        if body.link_pattern is not None:
            candidate = body.link_pattern
            if not candidate.startswith("/"):
                raise HTTPException(
                    status_code=422,
                    detail="link_pattern must start with '/' (path prefix, not a full URL)",
                )
            if "://" in candidate:
                raise HTTPException(
                    status_code=422,
                    detail="link_pattern must be a path prefix, not a full URL",
                )
            link_pattern_value = candidate
        else:
            link_pattern_value = None
    if body.refresh_interval_seconds is not None:
        body.refresh_interval_seconds = _validate_refresh(body.refresh_interval_seconds)
    if body.category is not None:
        body.category = _validate_category(body.category)
    headers: dict | None = scheduler._UNSET
    if "custom_headers" in body.model_fields_set:
        headers = (
            _validate_custom_headers(body.custom_headers)
            if body.custom_headers is not None
            else None
        )

    try:
        row = await scheduler.update_source(
            session,
            source_id,
            refresh=body.refresh_interval_seconds,
            active=body.active,
            category=body.category,
            name=body.name,
            url=body.url,
            sitemap_url=sitemap_url_value,
            link_pattern=link_pattern_value,
            custom_headers=headers,
        )
    except IntegrityError:
        # Most likely cause: ``Source.name`` has a UNIQUE constraint
        # (``models.py:44``) and the new name collides with another
        # row. Surface as 409 so the client knows it's a recoverable
        # conflict, not a server fault.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"source name {body.name!r} is already in use",
        )
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    # A label only: no rescheduling, so it's set here rather than in
    # scheduler.update_source.
    if "display_name" in body.model_fields_set:
        row.display_name = _clean_display_name(body.display_name)
        await session.commit()
        await session.refresh(row)
    return await _one_source_out(session, row)


@router.delete(
    "/sources/{source_id}",
    status_code=204,
    dependencies=_write_deps,
)
async def delete_source_endpoint(
    source_id: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Drop a source row and its scheduler job.

    Works for both dynamic and built-in rows. The 400-on-built-in
    guard that used to live here was removed so the user can
    actually silence a built-in they don't want (the old
    workaround was to set ``active=false``, which kept the
    scheduler job and the row but stopped fetches — a poor
    substitute for actual removal). For built-ins (BBC, HN, etc.)
    the plugin class stays registered in memory for the rest of
    this process's life, but ``scheduler.delete_source`` records
    the deletion in ``app_settings`` and removes the scheduler
    job, so neither the next scheduled tick nor a backend restart
    resurrects the row — see ``scheduler._upsert_source``.
    """
    row = await session.get(Source, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        deleted = await scheduler.delete_source(session, source_id)
    except ValueError as exc:
        # Defensive guard from ``scheduler.delete_source``.
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="source not found")


# --- Test source (no persist) ------------------------------------------


async def _test_source_impl(
    body: SourceTestRequest,
    session: AsyncSession,
) -> SourceTestResult:
    """Shared implementation for ``POST /api/sources/test``.

    Validates the request shape the same way ``create_source_endpoint``
    does (so the test result reflects exactly what the Add call will
    accept), then dispatches the fetch to the same plugin the live
    source would use. Catches the failures ``httpx`` and the parsers
    raise and maps them to ``error_kind`` strings the frontend
    branches on.

    ``name`` is optional. If provided, it must pass the same regex
    and uniqueness checks the Add flow uses — a name_conflict error
    lets the user fix the name before they Add (the form would have
    surfaced a 409 on Add, but the test catches it earlier so the
    user doesn't have to look at the Add button to learn it's
    rejected).

    ``custom_headers`` is honored for the test fetch so the user can
    see "this feed works with a browser UA" without first adding the
    source.
    """
    # Reject unsupported types up front — same set Add accepts. The
    # validation here is intentionally a subset of the create flow's
    # (no DB write, no scheduler registration) so a Test never has
    # side effects beyond the upstream fetch.
    if body.type not in ("rss", "reddit", "podcast", "youtube_channel", "generic_scrape"):
        return SourceTestResult(
            ok=False,
            error_kind="unsupported_type",
            error=(
                f"unsupported type {body.type!r} (only 'rss', 'reddit', 'podcast', "
                "'youtube_channel', and 'generic_scrape' are accepted)"
            ),
        )

    # Apple Podcasts catalog links (podcasts.apple.com/.../id<N>) and
    # any shape of YouTube link aren't feeds — resolve to the real
    # RSS URL before anything below touches ``body.url``. A no-op for
    # any URL that matches neither pattern (``resolved_url`` stays
    # None). Every SourceTestResult returned past this point carries
    # ``resolved_url`` so the frontend can show / adopt the real URL
    # regardless of whether the rest of the test succeeds.
    resolved_url: str | None = None

    # Name is optional on Test, but if provided it must be valid AND
    # not collide with an existing source or built-in. We deliberately
    # do this BEFORE resolving the URL so a bad name costs no YouTube or
    # Apple lookup and no upstream roundtrip.
    if body.name is not None and body.name.strip():
        try:
            _validate_name(body.name)
        except HTTPException as exc:
            return SourceTestResult(
                resolved_url=resolved_url,
                ok=False,
                error_kind="invalid_url",  # closest match; user sees the message
                error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            )
        # Reserved-name guard — same set ``create_source_endpoint`` enforces.
        if body.name in registered_plugin_names():
            return SourceTestResult(
                resolved_url=resolved_url,
                ok=False,
                error_kind="name_conflict",
                error=f"name {body.name!r} is reserved for a built-in source",
            )
        # Live-row collision check.
        existing = await session.execute(
            select(Source).where(Source.name == body.name)
        )
        if existing.scalar_one_or_none() is not None:
            return SourceTestResult(
                resolved_url=resolved_url,
                ok=False,
                error_kind="name_conflict",
                error=f"a source named {body.name!r} already exists",
            )

    try:
        new_url = await resolve_feed_url(body.url)
        new_url = await resolve_channel_feed_url(
            new_url, allow_bare_handle=(body.type == "youtube_channel")
        )
    except (PodcastResolutionError, YouTubeResolutionError) as exc:
        return SourceTestResult(ok=False, error_kind="invalid_url", error=str(exc))
    if new_url != body.url:
        resolved_url = new_url
        body.url = new_url

    # URL validation. Same path Add uses; on failure, surface the
    # validation error verbatim (e.g. "url must start with http(s)://")
    # so the user knows what to fix.
    if body.type == "reddit":
        try:
            url = _validate_reddit_url(body.url)
        except HTTPException as exc:
            return SourceTestResult(
                resolved_url=resolved_url,
                ok=False,
                error_kind="invalid_url",
                error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            )
    else:
        try:
            _validate_url(body.url)
        except HTTPException as exc:
            return SourceTestResult(
                resolved_url=resolved_url,
                ok=False,
                error_kind="invalid_url",
                error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            )
        url = body.url

    # URL safety check — SSRF guard runs here too. ``check_url_safe``
    # returns (bool, reason) rather than raising, so the result has
    # to be checked explicitly — report a rejection as invalid_url
    # so the user understands the URL was rejected, not the network.
    # The reason names internal addresses, so it goes to the log only
    # (url_safety.check_url_safe's contract), never to the user.
    safe, reason = check_url_safe(url)
    if not safe:
        logger.info(
            "sources/test: refused by the SSRF guard host=%s reason=%s",
            urlparse(url).hostname,
            reason,
        )
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            error_kind="invalid_url",
            error="That address isn't reachable from Popping.",
        )

    # Validate custom_headers the same way Add does, but only if
    # provided. None or empty map → use defaults.
    try:
        headers = (
            _validate_custom_headers(body.custom_headers)
            if body.custom_headers is not None
            else None
        )
    except HTTPException as exc:
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            error_kind="invalid_headers",
            error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
        )

    # Fetch. We dispatch through the same plugin classes the
    # scheduler uses, but instantiate a transient plugin here so we
    # don't need a DB row. This means the test path uses exactly the
    # same parse + normalize logic the live ingest uses — a
    # "works on test" result is a true "Add will work" signal.
    try:
        if body.type in ("rss", "podcast", "youtube_channel"):
            # Podcast feeds and YouTube channel feeds are both
            # RSS/Atom-shaped — same fetcher, which already extracts
            # audio_url / duration_seconds when present for podcasts
            # (see app.sources.podcast) and picks up media:thumbnail
            # generically for YouTube (see app.sources.youtube).
            from app.sources.rss import fetch_rss
            items = await fetch_rss(url, headers=headers)
        elif body.type == "generic_scrape":
            # No Source row to construct a real GenericScrapePlugin
            # from yet — probe() is the row-free equivalent of its
            # fetch(), sitemap-discovering and extracting a small
            # sample directly. custom_headers isn't honored here
            # (the underlying fetch is app.article_extract.fetch_html,
            # not this route's httpx client) — a rare enough need for
            # a scrape-fallback source that it's not worth threading
            # through yet.
            from app.sources.generic_scrape import probe as probe_generic_scrape
            items = await probe_generic_scrape(url)
        else:  # "reddit"
            # Same client the scheduled ingest uses
            # (``DynamicRedditPlugin.fetch``). It takes the subreddit
            # name, not the canonical URL ``_validate_reddit_url``
            # built, and custom_headers don't apply: Reddit fetches go
            # through ``reddit_client``'s own shared clients.
            if reddit_client.is_disabled():
                return SourceTestResult(
                    resolved_url=resolved_url,
                    ok=False,
                    error_kind="unsupported_type",
                    error="Reddit isn't available on this server.",
                )
            sub = normalize_subreddit(url)
            try:
                items = await reddit_client.fetch_subreddit(sub, limit=25)
            except reddit_client.RedditFetchThrottled as exc:
                # Caught here, before the generic ``except Exception``
                # below would call a 429 a parse error.
                return SourceTestResult(
                    resolved_url=resolved_url,
                    ok=False,
                    status_code=exc.status,
                    error_kind="network_error",
                    error=f"Reddit is busy (HTTP {exc.status}). Try again in a minute.",
                )
            if not items:
                # ``fetch_subreddit`` turns HTTP errors (403 for a
                # private or banned sub, 404) and unparseable bodies
                # into ``[]``, so an empty listing is the only signal
                # it gives for all of them.
                return SourceTestResult(
                    resolved_url=resolved_url,
                    ok=False,
                    error_kind="not_found",
                    error=f"r/{sub} returned no posts.",
                )
    except httpx.TimeoutException as exc:
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            error_kind="timeout",
            error=f"feed took too long to respond ({exc})",
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else None
        kind = (
            "not_found" if code in (404, 410)
            else "forbidden" if code in (401, 403)
            else "unknown"
        )
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            status_code=code,
            error_kind=kind,
            error=f"upstream returned HTTP {code}",
        )
    except httpx.HTTPError as exc:
        # Connection refused, DNS failure, TLS, etc.
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            error_kind="network_error",
            error=str(exc),
        )
    except asyncio.TimeoutError:
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            error_kind="timeout",
            error="feed took too long to respond",
        )
    except Exception as exc:
        # Parse errors, schema validation, anything else. The plugin
        # contract is "return a list of dicts"; if it raises, the
        # response wasn't a feed.
        msg = str(exc)
        kind = "parse_error" if "parse" in msg.lower() or "missing" in msg.lower() else "unknown"
        return SourceTestResult(
            resolved_url=resolved_url,
            ok=False,
            error_kind=kind,
            error=msg or exc.__class__.__name__,
        )

    # The plugin returned items — success. ``items`` is a list of
    # raw dicts (no normalization at this layer; the Add flow would
    # do that on the way into the DB). We just want a count and a
    # few titles for the UI.
    return SourceTestResult(
        resolved_url=resolved_url,
        ok=True,
        item_count=len(items),
        sample_titles=[str(it.get("title", ""))[:120] for it in items[:3]],
    )


@router.post(
    "/sources/test",
    response_model=SourceTestResult,
    dependencies=_write_deps,
)
async def test_source_endpoint(
    body: SourceTestRequest,
    session: AsyncSession = Depends(get_session),
) -> SourceTestResult:
    """Probe a URL with the same plugin Add would use, no DB write.

    Returns ``ok=True`` with a small item count + sample titles on
    success, or ``ok=False`` with an ``error_kind`` enum the
    frontend maps to a friendly message. The full ``error`` string
    is included so power users can see the underlying exception
    without re-running with curl.
    """
    return await _test_source_impl(body, session)


# --- Check now (pull to refresh) ------------------------------------------


@router.post(
    "/sources/check-now",
    response_model=CheckNowResult,
    dependencies=_write_deps,
)
async def check_now_endpoint(body: CheckNowRequest) -> CheckNowResult:
    """Ask for up to 20 feeds to be checked in the next minute.

    Nothing is fetched in the request: ``scheduler.queue_check`` moves
    each feed's job up, unless the feed was checked within the
    cooldown, is paused, or is failing and still inside its backoff
    (``force`` skips that gate). The response says which feeds were
    queued and when, so the caller can tell when they're done.
    """
    result = await scheduler.queue_check(body.source_ids, force=body.force)
    return CheckNowResult(**result)


# --- Recommendations (Phase 5) -------------------------------------------


@router.get("/feed-recommendations", response_model=list[FeedRecommendation])
async def feed_recommendations_endpoint(
    session: AsyncSession = Depends(get_session),
    user: dict | None = Depends(current_user),
) -> list[FeedRecommendation]:
    """Curated list of feeds the user might want to add, re-ranked by
    the user's last-30-days interaction co-occurrence. Falls back to
    the static editorial order when there's no engagement history yet
    (including for a fully anonymous caller). See
    ``backend/app/feed_recommendations.py`` for the ranking math,
    ``aggregation_user_ids`` for how a request maps to the interaction
    rows that count as "this user's", and how to update the curated
    list.
    """
    rows = (await session.scalars(select(Source.name))).all()
    active = list(rows)
    recs = await recommendations_for_user(session, active, aggregation_user_ids(user))
    return [FeedRecommendation(**r) for r in recs]


# Cold-start fallback when POST /discover is called with no category
# and the user has no engagement history yet to infer one from.
_DISCOVER_DEFAULT_CATEGORY = "tech"


@router.post(
    "/feed-recommendations/discover",
    response_model=FeedDiscoverResult,
    dependencies=_write_deps,
)
async def feed_recommendations_discover_endpoint(
    body: FeedDiscoverRequest,
    session: AsyncSession = Depends(get_session),
    user: dict | None = Depends(current_user),
) -> FeedDiscoverResult:
    """"Find more feeds" — the Recommended tab's on-demand LLM
    discovery button. Synchronous (unlike the auto-trigger on Add):
    a single LLM call + a handful of validation fetches is a few
    seconds, well within a normal request budget, and the user is
    actively waiting on this one rather than it happening as a side
    effect of something else.

    Runs for one category per call, either the one the caller names
    or (when omitted) the user's top-scoring category by interaction
    history — falling back to ``_DISCOVER_DEFAULT_CATEGORY`` cold
    start. See ``app.feed_discovery.discover_candidates`` for the
    validation pipeline; ``added`` can legitimately be 0.
    """
    if body.category is not None:
        category = _validate_category(body.category)
    else:
        category = await top_category_for_user(
            session, aggregation_user_ids(user)
        ) or _DISCOVER_DEFAULT_CATEGORY

    created, note = await discover_candidates(
        session,
        category=category,
        context="The user tapped \"find more feeds\" for this category in their dashboard.",
    )
    return FeedDiscoverResult(category=category, added=len(created), note=note)

