"""Per-user preferences (read state, last-viewed, column sort/filter).

The dashboard used to keep three things in ``localStorage``:

  - ``readEntries`` -- which entry ids the user manually marked read
  - ``lastViewed`` -- per-column "I last saw this column at" timestamps
  - ``columnPrefs`` -- per-column sort/filter preferences

All three are visibility-state, not per-device ergonomics, so they
belong on the server. Without this, two devices on the same LAN
(computer + phone) see different read state, different "+N new" chip
counts, and different column sort orders.

The replacement is a per-user, per-key JSONB value store. The
frontend uses namespaced keys (``read_entries:<source_id>``,
``last_viewed:<source_id>``, ``column_prefs:<source_id>``) but the
server treats the key as opaque -- a new preference type can be
added without a backend migration. See
``alembic/versions/0015_user_preferences.py`` for the schema and
the rationale for why this isn't a column on ``user_profiles``.

Endpoints
---------

  - ``GET    /api/preferences`` -- all (key, value) rows for the
    caller. One round-trip on app load.
  - ``GET    /api/preferences/{key}`` -- one row. Optional; the
    frontend can read from the bulk response.
  - ``PUT    /api/preferences/{key}`` -- upsert. Body is
    ``{"value": <json>}``. Idempotent.
  - ``DELETE /api/preferences/{key}`` -- remove the row. Used by
    the "reset all preferences" button (not implemented today;
    included for completeness).

All four endpoints require a logged-in user (``require_user``).
The user_id is sourced from the session's ``sub`` (real OIDC) or
the synthetic ``"local-bypass"`` sub (LAN bypass) -- both are
opaque strings here, so the migration to "real" OIDC later
(where multiple users share a deployment) works without changes
to this route.

Auth: requires ``require_user``. The OIDC case is the long-term
shape; the bypass case is the current "I just want this on my
LAN" shape. Both produce a stable ``sub`` string the
``user_preferences`` row is keyed by.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.config import settings
from app.db import get_session
from app.models import UserPreference
from app.schemas import UserPreferenceIn, UserPreferenceListOut, UserPreferenceOut

logger = logging.getLogger("popping.routes.preferences")

# Match the pattern in routes/interactions.py: when OIDC is on, the
# router-level dep list is non-empty and every endpoint requires a
# logged-in user. When OIDC is off, the dep is applied per-handler so
# the route still works in single-user / bypass deployments.
_route_deps = [Depends(require_user)] if settings.oidc_enabled else []

router = APIRouter(tags=["preferences"], dependencies=_route_deps)


@router.get("/preferences", response_model=UserPreferenceListOut)
async def list_preferences(
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> UserPreferenceListOut:
    """Return every (key, value) row for the caller.

    One round-trip on app load -- the dashboard rebuilds its
    ``readEntries`` / ``lastViewed`` / ``columnPrefs`` state from
    this single response. Arbitrary order; the frontend treats
    ``key`` as opaque.

    Empty list on a fresh user (no rows yet). The frontend falls
    back to the localStorage seed in that case -- see the
    one-way migration doc in the frontend's lib/preferences.ts.
    """
    user_id = user["sub"]
    rows = await session.scalars(
        select(UserPreference).where(UserPreference.user_id == user_id)
    )
    items = [
        UserPreferenceOut(
            key=row.key,
            value=row.value,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    return UserPreferenceListOut(items=items)


@router.get("/preferences/{key}", response_model=UserPreferenceOut)
async def get_preference(
    key: str,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> UserPreferenceOut:
    """Return one (key, value) row for the caller. 404 if absent.

    Optional endpoint -- the bulk ``GET /api/preferences`` is the
    common path. This is for callers that only need a single key
    (e.g. a future "did the user dismiss this onboarding hint?"
    check that loads one specific preference on demand).
    """
    user_id = user["sub"]
    row = await session.scalar(
        select(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.key == key,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"preference {key!r} not set for user",
        )
    return UserPreferenceOut(
        key=row.key,
        value=row.value,
        updated_at=row.updated_at,
    )


@router.put("/preferences/{key}", response_model=UserPreferenceOut)
async def put_preference(
    key: str,
    body: UserPreferenceIn,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> UserPreferenceOut:
    """Upsert one (key, value) row for the caller. Idempotent.

    The key is in the URL (not the body) so the route can validate
    "the URL key matches what the caller is writing" if we ever
    need to (we don't today -- the URL is the source of truth).

    The ``value`` field is opaque; the route passes it through to
    the JSONB column without coercion. The frontend's TS types
    are the source of truth for what shape each key holds.

    Implementation note
    --------------------
    We use ``INSERT ... ON CONFLICT (user_id, key) DO UPDATE`` so
    the upsert is one round-trip, not a SELECT-then-INSERT-or-UPDATE.
    This matters for the high-frequency read-state path: marking
    one card read on every visible-card-scroll would otherwise
    burn 2x the round-trips.
    """
    user_id = user["sub"]
    stmt = (
        pg_insert(UserPreference)
        .values(user_id=user_id, key=key, value=body.value)
        .on_conflict_do_update(
            index_elements=["user_id", "key"],
            set_={"value": body.value, "updated_at": UserPreference.__table__.c.updated_at},
        )
        .returning(UserPreference.updated_at)
    )
    result = await session.execute(stmt)
    row_updated_at = result.scalar_one()
    await session.commit()
    logger.debug(
        "preference: user=%s key=%s updated",
        user_id,
        key,
    )
    return UserPreferenceOut(
        key=key,
        value=body.value,
        updated_at=row_updated_at,
    )


@router.delete("/preferences/{key}", status_code=204)
async def delete_preference(
    key: str,
    user: dict = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a single (key) row. Idempotent: returns 204 whether or
    not the row existed.

    Not currently called by the frontend; included for completeness
    so a future "reset all preferences" button or per-key clear
    action has a server-side target.
    """
    user_id = user["sub"]
    await session.execute(
        delete(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.key == key,
        )
    )
    await session.commit()
    logger.debug("preference: user=%s key=%s deleted", user_id, key)
