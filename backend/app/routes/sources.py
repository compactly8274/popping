"""Source listing + management endpoints.

GET endpoints have always been public-read. Phase 5 adds POST/PATCH/
DELETE that mutate the ``sources`` table, gated by the same auth
dependency the manual ``/api/ingest/{name}`` route uses — wide-open
when OIDC is off (single-user LAN deployment), login-required when
it's on.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.config import settings
from app.db import get_session
from app.feed_recommendations import recommendations_for
from app.models import Source
from app.schemas import FeedRecommendation, SourceCreate, SourceOut, SourceUpdate
from app.sources import list_sources as registered_plugin_names
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

    v1 accepts ``type="rss"`` only. Phase 6/7 will add ``"podcast"``
    and ``"youtube_channel"`` and route them to their own plugin
    dispatchers (see ``scheduler._plugin_for``).
    """
    _validate_name(body.name)
    _validate_url(body.url)
    if body.type != "rss":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported type {body.type!r} (only 'rss' is accepted in this build)",
        )
    refresh = _validate_refresh(body.refresh_interval_seconds)
    row = await scheduler.add_source(
        session,
        name=body.name,
        type_=body.type,
        category=body.category,
        url=body.url,
        refresh=refresh,
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
    if body.refresh_interval_seconds is not None:
        body.refresh_interval_seconds = _validate_refresh(body.refresh_interval_seconds)
    if body.category is not None and not body.category.strip():
        raise HTTPException(status_code=422, detail="category cannot be empty")
    row = await scheduler.update_source(
        session,
        source_id,
        refresh=body.refresh_interval_seconds,
        active=body.active,
        category=body.category,
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
    """Drop a dynamic source. Built-in sources (BBC, HN, etc.) are
    rejected with a 400 — they're managed by the plugin registry, not
    the DB, and the user has no UI affordance to recreate them. Use
    ``active=false`` to silence a built-in instead."""
    row = await session.get(Source, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    if row.name in registered_plugin_names():
        raise HTTPException(
            status_code=400,
            detail=f"built-in source {row.name!r} cannot be deleted via the API",
        )
    try:
        deleted = await scheduler.delete_source(session, source_id)
    except ValueError as exc:
        # Defensive guard from ``scheduler.delete_source`` — should
        # never reach here because the route-layer check above
        # already filters out built-ins, but a programmatic caller
        # could bypass it.
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="source not found")


# --- Recommendations (Phase 5) -------------------------------------------


@router.get("/feed-recommendations", response_model=list[FeedRecommendation])
async def feed_recommendations_endpoint(
    session: AsyncSession = Depends(get_session),
) -> list[FeedRecommendation]:
    """Curated list of feeds the user might want to add, minus any
    they already have. See ``backend/app/feed_recommendations.py``
    for the editorial rationale and how to update the list.

    The list is static today (Phase 5); Phase 8 will re-rank by
    co-engagement signals once ``Interaction`` rows start landing.
    """
    rows = (await session.scalars(select(Source.name))).all()
    return [FeedRecommendation(**r) for r in recommendations_for(list(rows))]