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
from app.schemas import InteractionBatchIn, InteractionIn

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