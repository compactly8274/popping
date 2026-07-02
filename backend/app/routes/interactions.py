"""User engagement events.

The frontend POSTs here when the user views a card, clicks through
to the article, thumbs something up or down, etc. The events feed:

  1. The recommendation co-occurrence ranker
     (``app.feed_recommendations``) — counts interactions per
     source category over a 30-day window, with thumb_down / never
     counted as -1, to re-rank the Recommended tab.
  2. (Future) The For You personalization model — interaction
     sequences could replace or augment the static preference vector
     in ``UserProfile``. Not wired today; the table is the raw
     substrate for that work.

Auth: when OIDC is enabled, requires a logged-in user. The
``user_id`` column is sourced from the session's ``sub`` — not a
hardcoded ``"default"`` — so multiple LAN devices sharing one
deployment don't conflate their interaction buckets into a single
co-occurrence profile.

Why both a single-shot and a batch endpoint?

  - Single-shot (``POST /api/interactions``) — for click events.
    Sparse, want immediate feedback. The browser fires one request
    per click and gets back a confirmation.
  - Batch (``POST /api/interactions/batch``) — for view events
    on dashboard render. A typical load has 20-50 visible cards;
    firing 30 individual POSTs would hammer the backend. The
    frontend batches them and flushes on ``requestIdleCallback``
    or ``visibilitychange: hidden`` via ``navigator.sendBeacon``.

``value`` allows negative floats so thumb_down / never can record
a -1.0 contribution; positive events keep the default 1.0.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.config import settings
from app.db import get_session
from app.models import Entry, Interaction
from app.schemas import InteractionBatchIn, InteractionIn, InteractionListOut, InteractionOut

logger = logging.getLogger("popping.routes.interactions")

_route_deps = [Depends(require_user)] if settings.oidc_enabled else []

router = APIRouter(tags=["interactions"], dependencies=_route_deps)


async def _commit_unique(session: AsyncSession, event: Interaction) -> int | None:
    """Persist one ``Interaction`` row, returning the row id on
    success or None if the FK violated (entry was deleted between
    dashboard load and tap).

    Letting the FK fire avoids a pre-flight ``SELECT id FROM entries
    WHERE id IN (...)`` round-trip on every POST/batch — the FK
    constraint is the source of truth, so we let it speak. asyncpg's
    IntegrityError wraps the underlying ``23503`` (foreign_key_violation).
    """
    session.add(event)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    await session.refresh(event)
    return event.id


@router.post("/interactions", status_code=201)
async def post_interaction(
    body: InteractionIn,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record one engagement event. Returns the inserted row id."""
    user_id = user["sub"]
    row = Interaction(
        entry_id=body.entry_id,
        user_id=user_id,
        type=body.type.value,
        value=body.value,
    )
    row_id = await _commit_unique(session, row)
    if row_id is None:
        # Surface as 404 (not 422) so the client can treat it as
        # "the entry is gone, drop the event" rather than a bug.
        raise HTTPException(
            status_code=404,
            detail=f"entry {body.entry_id} not found",
        )
    logger.debug(
        "interaction: user=%s entry=%d type=%s value=%s",
        user_id, body.entry_id, body.type.value, body.value,
    )
    return {"id": row_id}


# Cap the batch so a runaway client can't enqueue thousands of
# events in one request. 50 covers a generous dashboard (5 columns
# × 10 visible cards) with headroom. Anything larger would be a
# misuse — split it client-side.
_BATCH_MAX = 50


async def _existing_entry_ids(
    session: AsyncSession, entry_ids: list[int]
) -> set[int]:
    """Return the subset of ``entry_ids`` that exist. One round-trip
    regardless of batch size. Used only on the batch path; the
    single-shot endpoint lets the FK do the talking — cheaper than
    a SELECT on the hot path of one POST per click.
    """
    if not entry_ids:
        return set()
    rows = await session.scalars(
        select(Entry.id).where(Entry.id.in_(entry_ids))
    )
    return set(rows.all())


@router.post("/interactions/batch", status_code=201)
async def post_interactions_batch(
    body: InteractionBatchIn,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record a batch of engagement events. The single response field
    ``inserted`` reports how many rows landed (after dropping
    invalid entry_ids).

    One pre-flight SELECT filters stale ids (single round-trip);
    the remaining rows land in one INSERT + commit. Compared to
    per-event ``flush()``/FK-catch, this is one round-trip for
    large batches (50 events is the cap) versus ``n`` flushes.
    """
    if not body.events:
        return {"inserted": 0}
    if len(body.events) > _BATCH_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"batch size {len(body.events)} exceeds limit {_BATCH_MAX}",
        )
    entry_ids = list({e.entry_id for e in body.events})
    valid_ids = await _existing_entry_ids(session, entry_ids)
    user_id = user["sub"]
    inserted = 0
    for evt in body.events:
        if evt.entry_id not in valid_ids:
            # Drop silently — a stale view event for a deleted
            # entry shouldn't fail the whole batch.
            continue
        session.add(
            Interaction(
                entry_id=evt.entry_id,
                user_id=user_id,
                type=evt.type.value,
                value=evt.value,
            )
        )
        inserted += 1
    if inserted:
        await session.commit()
    logger.debug(
        "interactions batch: user=%s sent=%d inserted=%d",
        user_id, len(body.events), inserted,
    )
    return {"inserted": inserted}


@router.get("/interactions/recent", response_model=InteractionListOut)
async def get_recent_interactions(
    types: str = "",
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> InteractionListOut:
    """Return the user's recent engagement events with entry
    metadata joined in. Used by the frontend's History view
    (in the Drawer) so the user can review what they've
    marked read vs hidden vs starred.

    Query parameters
      - ``types``: comma-separated list of interaction types
        to include. Empty = all types. Example:
        ``?types=view,never&limit=20`` returns the user's
        20 most recent reads + hides.
      - ``limit``: page size (default 50, max 200).
      - ``offset``: pagination offset (default 0).

    Sort: ``created_at DESC`` so the most recent interactions
    appear first. The user is reviewing their history in
    reverse-chronological order — the "what did I just do"
    pattern is more useful than the "first thing I ever
    did" pattern.

    Total count: a separate count query (so the frontend can
    show "Showing 50 of 1,234" without paging the full
    list). The count uses the same filters as the main
    query so they can't disagree.

    ``user_id`` is sourced from the session's ``sub`` so
    multi-user deployments don't conflate their history.
    Mirrors the write path's user_id handling.
    """
    user_id = user["sub"]
    # Parse the types filter. Empty = all types (omit WHERE
    # filter on type). The split is forgiving: an empty
    # string, a string of commas, or whitespace is treated
    # as "no filter".
    type_list: list[str] = []
    if types.strip():
        type_list = [t.strip() for t in types.split(",") if t.strip()]

    # The hidden dwell events: ``dwell`` fires per-card on
    # mark-read as a "how long was this on screen" signal.
    # The History view shouldn't show those — the user
    # already sees the read event for the same entry. Filter
    # dwell out by default. The frontend doesn't need to
    # know about this; the types filter is optional.
    if "dwell" not in type_list:
        type_list.append("dwell")
    # We use ``NOT IN (dwell)`` so the SQL still hits the
    # (user_id, created_at) index; converting to a set
    # membership check here keeps the query plan cheap.
    exclude_types = {"dwell"}

    # Build the query. Two separate queries (one for the
    # page, one for the total) keep the main query simple
    # and let the SQL planner use the index on
    # (user_id, created_at).
    from sqlalchemy import func, select
    from app.models import Entry, Source

    # Page query
    page_q = (
        select(Interaction, Entry, Source)
        .join(Entry, Entry.id == Interaction.entry_id)
        .join(Source, Source.id == Entry.source_id)
        .where(Interaction.user_id == user_id)
    )
    if type_list:
        page_q = page_q.where(Interaction.type.in_(type_list))
    # Apply the dwell exclusion: NOT IN is cheaper than
    # re-evaluating the type list.
    if exclude_types:
        page_q = page_q.where(Interaction.type.notin_(exclude_types))
    page_q = (
        page_q.order_by(Interaction.created_at.desc())
        .offset(offset)
        .limit(min(limit, 200))
    )
    rows = (await session.execute(page_q)).all()

    # Total count query (same filters, no limit/offset)
    count_q = (
        select(func.count(Interaction.id))
        .where(Interaction.user_id == user_id)
    )
    if type_list:
        count_q = count_q.where(Interaction.type.in_(type_list))
    if exclude_types:
        count_q = count_q.where(Interaction.type.notin_(exclude_types))
    total = (await session.execute(count_q)).scalar_one()

    items = [
        InteractionOut(
            id=inter.id,
            type=inter.type,
            value=inter.value,
            created_at=inter.created_at,
            entry_id=entry.id,
            entry_title=entry.title,
            entry_url=entry.url,
            entry_published_at=entry.published_at,
            source_id=source.id,
            source_name=source.name,
        )
        for (inter, entry, source) in rows
    ]
    return InteractionListOut(
        items=items,
        total=total,
        has_more=(offset + len(items)) < total,
    )

