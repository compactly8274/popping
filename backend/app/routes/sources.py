"""Source listing + management endpoints.

GET endpoints have always been public-read. Phase 5 adds POST/PATCH/
DELETE that mutate the ``sources`` table, gated by the same auth
dependency the manual ``/api/ingest/{name}`` route uses — wide-open
when OIDC is off (single-user LAN deployment), login-required when
it's on.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, require_user
from app.config import settings
from app.db import get_session
from app.feed_recommendations import aggregation_user_ids, recommendations_for_user
from app.models import Source
from app.schemas import (
    FeedRecommendation,
    SourceCreate,
    SourceOut,
    SourceTestRequest,
    SourceTestResult,
    SourceUpdate,
)
from app.sources import list_sources as registered_plugin_names
from app.sources.reddit import normalize_subreddit
from app.url_safety import check_url_safe
from app import scheduler

router = APIRouter(tags=["sources"])

# Auth: matches the pattern in ``routes/ingest.py``. When OIDC is on,
# POST/PATCH/DELETE require a logged-in user; GETs stay open. When
# OIDC is off (single-user LAN), the bypass grants the same identity
# the manual ingest endpoint already accepts.
_write_deps = [Depends(require_user)] if settings.oidc_enabled else []


# --- Validation helpers --------------------------------------------------

# Source names are user-facing (column headers, filter chips, error
# messages) so the regex is conservative: lowercase letters, digits,
# and underscore only, 1-120 chars. Matches the existing built-in
# plugin names ("bbc_news", "hn_top", etc.) so the UI doesn't render
# a row whose name has a different shape than the rest.
_NAME_RE = re.compile(r"^[a-z0-9_]{1,120}$")

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

# Mirrors ``Source.category``'s ``String(40)``. A PATCH / POST with a
# longer string would crash on the DB layer with a Postgres
# ``value too long for type character varying(40)`` — surfaced as a
# 500 to the client. Catching it here returns a clean 422.
_CATEGORY_MAX = 40

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
_HEADER_DENYLIST_EXTRA = frozenset(
    {
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "x-original-url",
        "x-rewrite-url",
    }
)
_HEADER_DENYLIST_ALL = _HEADER_DENYLIST | _HEADER_DENYLIST_EXTRA

# Bound the size of an override map. Real UAs are ~120 chars; Allow
# some headroom for ``Accept-Language`` etc. but reject obvious
# abuse (a multi-MB blob in the DB).
_CUSTOM_HEADERS_MAX = 20
_HEADER_VALUE_MAX = 512

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
    if not _NAME_RE.match(name):
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


def _validate_category(value: str) -> None:
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
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(
                status_code=422,
                detail="custom_headers keys and values must be strings",
            )
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


@router.get("/sources", response_model=list[SourceOut])
async def list_sources_endpoint(
    session: AsyncSession = Depends(get_session),
) -> list[SourceOut]:
    rows = (await session.scalars(select(Source).order_by(Source.category, Source.name))).all()
    return [SourceOut.model_validate(r) for r in rows]


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source_endpoint(
    source_id: int,
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    row = await session.get(Source, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return SourceOut.model_validate(row)


# --- POST / PATCH / DELETE (Phase 5) -------------------------------------


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

    v1 accepts ``type="rss"`` and ``type="reddit"`` (``"podcast"`` /
    ``"youtube_channel"`` land in later phases and route to their
    own plugin dispatchers via ``scheduler._plugin_for``).
    """
    _validate_name(body.name)
    # Type-aware URL validation. Reddit rows accept a subreddit
    # reference (``r/python``) and get rewritten to the canonical
    # Reddit thread URL; RSS rows take a plain http(s) feed URL.
    if body.type == "reddit":
        url = _validate_reddit_url(body.url)
    else:
        _validate_url(body.url)
        url = body.url
    if body.type not in ("rss", "reddit"):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported type {body.type!r} (only 'rss' and 'reddit' are accepted in this build)",
        )
    _validate_category(body.category)
    # ``refresh_interval_seconds`` is optional in the schema; default
    # to a per-type sensible value when absent. Reddit moves faster
    # than news RSS so 15 min vs 1h is the right baseline.
    if body.refresh_interval_seconds is None:
        refresh = _REDDIT_DEFAULT_REFRESH if body.type == "reddit" else _REFRESH_MIN * 60
    else:
        refresh = _validate_refresh(body.refresh_interval_seconds)
    headers = _validate_custom_headers(body.custom_headers)
    row = await scheduler.add_source(
        session,
        name=body.name,
        type_=body.type,
        category=body.category,
        url=url,
        refresh=refresh,
        custom_headers=headers,
    )
    return SourceOut.model_validate(row)


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
    if body.url is not None:
        # PATCH URL is also type-aware: re-validate as a subreddit
        # reference when the row is a Reddit row (so a typo'd edit
        # doesn't store a malformed URL the plugin can't fetch).
        # For non-Reddit rows fall through to the plain URL check.
        if pre.type == "reddit":
            body.url = _validate_reddit_url(body.url)
        else:
            _validate_url(body.url)
    if body.refresh_interval_seconds is not None:
        body.refresh_interval_seconds = _validate_refresh(body.refresh_interval_seconds)
    if body.category is not None:
        _validate_category(body.category)
    headers = (
        _validate_custom_headers(body.custom_headers)
        if body.custom_headers is not None
        else None
    )

    # Built-in rows can be paused / re-categorized (these are
    # row-level state, not class-level). Renaming or re-URLing a
    # built-in is not allowed — the registry key is fixed, and the
    # URL is bound to the plugin class's fetch logic.
    if pre.name in registered_plugin_names() and (
        body.name is not None or body.url is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"built-in source {pre.name!r} cannot be renamed or re-URLed",
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
    return SourceOut.model_validate(row)


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

    Works for both dynamic and built-in rows. For built-ins (BBC,
    HN, etc.) the plugin class stays registered in memory until
    the backend restarts — at which point the plugin
    re-registers itself as a fresh row. The 400-on-built-in
    guard that used to live here was removed so the user can
    actually silence a built-in they don't want (the old
    workaround was to set ``active=false``, which kept the
    scheduler job and the row but stopped fetches — a poor
    substitute for actual removal).
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
    if body.type not in ("rss", "reddit"):
        return SourceTestResult(
            ok=False,
            error_kind="unsupported_type",
            error=f"unsupported type {body.type!r} (only 'rss' and 'reddit' are accepted)",
        )

    # Name is optional on Test, but if provided it must be valid AND
    # not collide with an existing source or built-in. We deliberately
    # do this BEFORE the URL fetch so a name conflict doesn't waste
    # an upstream roundtrip.
    if body.name is not None and body.name.strip():
        try:
            _validate_name(body.name)
        except HTTPException as exc:
            return SourceTestResult(
                ok=False,
                error_kind="invalid_url",  # closest match; user sees the message
                error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            )
        # Reserved-name guard — same set ``create_source_endpoint`` enforces.
        if body.name in registered_plugin_names():
            return SourceTestResult(
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
                ok=False,
                error_kind="name_conflict",
                error=f"a source named {body.name!r} already exists",
            )

    # URL validation. Same path Add uses; on failure, surface the
    # validation error verbatim (e.g. "url must start with http(s)://")
    # so the user knows what to fix.
    if body.type == "reddit":
        try:
            url = _validate_reddit_url(body.url)
        except HTTPException as exc:
            return SourceTestResult(
                ok=False,
                error_kind="invalid_url",
                error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            )
    else:
        try:
            _validate_url(body.url)
        except HTTPException as exc:
            return SourceTestResult(
                ok=False,
                error_kind="invalid_url",
                error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            )
        url = body.url

    # URL safety check — SSRF guard runs here too. ``check_url_safe``
    # returns (bool, reason) rather than raising, so the result has
    # to be checked explicitly — report a rejection as invalid_url
    # so the user understands the URL was rejected, not the network.
    safe, reason = check_url_safe(url)
    if not safe:
        return SourceTestResult(
            ok=False,
            error_kind="invalid_url",
            error=reason,
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
            ok=False,
            error_kind="unknown",
            error=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
        )

    # Fetch. We dispatch through the same plugin classes the
    # scheduler uses, but instantiate a transient plugin here so we
    # don't need a DB row. This means the test path uses exactly the
    # same parse + normalize logic the live ingest uses — a
    # "works on test" result is a true "Add will work" signal.
    try:
        if body.type == "rss":
            from app.sources.rss import fetch_rss
            items = await fetch_rss(url, headers=headers)
        else:  # "reddit"
            from app.sources.reddit import fetch_subreddit
            items = await fetch_subreddit(url, headers=headers)
    except httpx.TimeoutException as exc:
        return SourceTestResult(
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
            ok=False,
            status_code=code,
            error_kind=kind,
            error=f"upstream returned HTTP {code}",
        )
    except httpx.HTTPError as exc:
        # Connection refused, DNS failure, TLS, etc.
        return SourceTestResult(
            ok=False,
            error_kind="network_error",
            error=str(exc),
        )
    except asyncio.TimeoutError:
        return SourceTestResult(
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
            ok=False,
            error_kind=kind,
            error=msg or exc.__class__.__name__,
        )

    # The plugin returned items — success. ``items`` is a list of
    # raw dicts (no normalization at this layer; the Add flow would
    # do that on the way into the DB). We just want a count and a
    # few titles for the UI.
    return SourceTestResult(
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

