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

Auth: soft (``current_user``, not ``require_user``), resolved to a
stable id via ``resolve_user_id``: the session's ``sub`` for real
OIDC, the synthetic ``"local-bypass"`` sub for the LAN bypass, or
the synthetic ``"anonymous"`` id when neither applies. That last
case is the DEFAULT deployment shape — ``OIDC_ENABLED=false`` and
``LOCAL_AUTH_BYPASS=false`` are both off out of the box, so a
fresh ``docker compose up -d`` has no way to ever produce a
session cookie or a bypass grant. Gating these routes on
``require_user`` (the previous behavior) meant every GET/PUT/DELETE
401'd for that default install: read state, hidden entries, saved
entries, and column sort/filter prefs would silently never persist
past the current page load (the frontend only console.warns on a
failed preference write in dev). The soft-auth + anonymous-fallback
pattern mirrors ``app.routes.interactions``, which had the same bug
and was already fixed this way.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, resolve_user_id
from app.db import get_session
from app.models import UserPreference
from app.schemas import UserPreferenceIn, UserPreferenceListOut, UserPreferenceOut

logger = logging.getLogger("popping.routes.preferences")

router = APIRouter(tags=["preferences"])


# Allowlist of the 9 known preference key namespaces the frontend
# actually uses (see ``PREFERENCE_KEYS`` in
# ``frontend/src/lib/preferences.tsx``). Five of them take a
# ``:<columnId>`` suffix (``read_entries``, ``last_viewed``,
# ``column_prefs``, ``column_sections``). Four of them are global
# (no suffix: ``hidden_entries``, ``starred_entries``,
# ``voted_entries``, ``filter_presets``, ``history_group_by``).
# The ``hidden_entries`` etc. keys are also valid WITHOUT a
# suffix because the legacy localStorage keys were the bare
# names; the frontend migrates them on first load.
#
# The columnId suffix is NOT a slug — App.tsx passes the column's
# literal display name (``col.name``) as the columnId, and that
# name is frequently NOT ``[a-z0-9_-]+``: the built-in "Saved" /
# "For You" columns are capitalized, the multi-source filter
# column is literally derived from source names (``multisubColumnName``
# returns things like ``names[0]`` or ``"${names[0]} + 1"`` — a raw
# source display name, which for a Reddit source can contain a
# slash, e.g. ``r/technology``, and for a custom RSS feed can be
# almost any string the user typed when adding it). A charset-
# restricted suffix pattern silently 400s real, common column
# names — this previously shipped as ``[a-z0-9_-]+`` and broke
# persistence for the Saved/For You/multi-source-filter columns.
# So the suffix here is deliberately "anything non-empty"; the
# actual security boundary is the PREFIX allowlist below (a caller
# still can't write to an unrelated key like ``admin_override`` —
# it just won't match any of these 9 prefixes).
#
# Why an allowlist at all: the URL is the source of truth for which
# row gets written. Allowing any string here lets a caller with
# a session pollute the namespace with arbitrary keys
# (``admin_override``, ``__internal_xyz``, etc.) that future
# code might trust as a known shape. Returns 400 with a clear
# message so a typo in the frontend surfaces immediately
# instead of silently writing a row no one reads.
_PREFERENCE_KEY_RE = re.compile(
    r"^(?:" + "|".join(
        # Suffixed names: ``<key>:<anything non-empty>``
        f"(?:{re.escape(name)}:.+)"
        for name in (
            "read_entries",
            "last_viewed",
            "column_prefs",
            "column_sections",
        )
    ) + "|" + "|".join(
        # Global names: bare key, no suffix.
        re.escape(name)
        for name in (
            "hidden_entries",
            "starred_entries",
            "voted_entries",
            "filter_presets",
            "history_group_by",
        )
    ) + r")$"
)


def _validate_key(key: str) -> None:
    """Raise HTTP 400 if ``key`` isn't on the preference allowlist.

    Applied to all 3 single-key endpoints (GET, PUT, DELETE) so a
    caller can read / write / delete exactly the rows the frontend
    writes, and no others. The bulk ``GET /api/preferences`` is
    intentionally not gated — the caller can only read their own
    rows, so pollution on the read side is bounded by what they
    already wrote (which is gated by PUT).
    """
    if not isinstance(key, str) or not _PREFERENCE_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail=(
                f"preference key {key!r} is not on the allowlist; "
                f"expected one of the 9 known namespaces "
                f"(read_entries:<id>, last_viewed:<id>, "
                f"column_prefs:<id>, column_sections:<id>, "
                f"hidden_entries, starred_entries, voted_entries, "
                f"filter_presets, history_group_by)"
            ),
        )


@router.get("/preferences", response_model=UserPreferenceListOut)
async def list_preferences(
    user: dict | None = Depends(current_user),
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
    user_id = resolve_user_id(user)
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
    user: dict | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserPreferenceOut:
    """Return one (key, value) row for the caller. 404 if absent.

    Optional endpoint -- the bulk ``GET /api/preferences`` is the
    common path. This is for callers that only need a single key
    (e.g. a future "did the user dismiss this onboarding hint?"
    check that loads one specific preference on demand).
    """
    _validate_key(key)
    user_id = resolve_user_id(user)
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
    user: dict | None = Depends(current_user),
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
    _validate_key(key)
    user_id = resolve_user_id(user)
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
    user: dict | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a single (key) row. Idempotent: returns 204 whether or
    not the row existed.

    Not currently called by the frontend; included for completeness
    so a future "reset all preferences" button or per-key clear
    action has a server-side target.
    """
    _validate_key(key)
    user_id = resolve_user_id(user)
    await session.execute(
        delete(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.key == key,
        )
    )
    await session.commit()
    logger.debug("preference: user=%s key=%s deleted", user_id, key)
