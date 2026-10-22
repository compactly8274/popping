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
    caller. One round-trip on app load. ``?since=<iso>`` returns
    only the rows written after that instant (the delta poll).
  - ``GET    /api/preferences/{key}`` -- one row. Optional; the
    frontend can read from the bulk response.
  - ``PUT    /api/preferences/{key}`` -- upsert. Body is
    ``{"value": <json>}``. Idempotent. Not for the set keys
    (``SET_KEYS``): a whole-list PUT of one is a 409, because it
    would overwrite what other devices added since.
  - ``PATCH  /api/preferences/{key}`` -- apply a change to a set
    key (``{"add": [...], "remove": [...]}``, or ``{"set": {...}}``
    for votes) under a row lock, so concurrent writers merge
    instead of overwriting each other.
  - ``DELETE /api/preferences/{key}`` -- remove the row. Used by
    the "reset all preferences" button (not implemented today;
    included for completeness).

Every single-key route takes the rest of the path as the key
(``{key:preference_key}``, see ``_PreferenceKeyConvertor``): a
category can contain ``/`` (``read_entries:News/Tech``), the client
percent-encodes it, and the server decodes ``%2F`` before routing, so
a plain ``{key}`` (which stops at ``/``) 404'd those keys.

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

import datetime as dt
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AfterValidator, AwareDatetime
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.convertors import Convertor, register_url_convertor

from app.auth.deps import current_user, resolve_user_id
from app.db import get_session
from app.models import UserPreference
from app.schemas import (
    UserPreferenceIn,
    UserPreferenceListOut,
    UserPreferenceOut,
    UserPreferencePatchIn,
)

logger = logging.getLogger("popping.routes.preferences")

router = APIRouter(tags=["preferences"])


class _PreferenceKeyConvertor(Convertor[str]):
    """Starlette's ``path`` convertor, but newlines included.

    ``{key:path}`` is ``.*`` inside a pattern Starlette closes with
    ``$``. ``.`` stops at a newline and ``$`` also matches just before
    a trailing one, so ``hidden_entries%0A`` would route as
    ``hidden_entries`` and a newline mid-key would 404. With ``(?s:)``
    the handler gets the decoded key exactly as sent, and
    ``_validate_key`` refuses it with a 400.
    """

    regex = "(?s:.*)"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("preference_key", _PreferenceKeyConvertor())


# Allowlist of the 11 known preference key namespaces the frontend
# uses (see ``PREFERENCE_KEYS`` in
# ``frontend/src/lib/preferences.tsx``). Five of them take a
# ``:<columnId>`` suffix (``read_entries``, ``last_viewed``,
# ``column_prefs``, ``column_sections``, ``seen``). Six of them are
# global (no suffix: ``hidden_entries``, ``starred_entries``,
# ``voted_entries``, ``filter_presets``, ``history_group_by``,
# ``category_layout``). ``category_layout`` is the tab order and the
# hidden tabs, one whole value written by PUT; the server never reads
# it.
# ``seen:<category>`` is the home for new seen marks, which clients
# write only by PATCH (PUT of a set key is a 409): old bundles PUT
# whole 200-id lists to ``read_entries:<col.name>``, which is the
# same string as a per-category ``read_entries`` key, so new marks
# can't share it.
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
# it just won't match any of these prefixes).
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
            "seen",
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
            "category_layout",
        )
    ) + r")$"
)


# ``user_preferences.key`` is ``String(60)``; a longer key would be a
# Postgres ``DataError`` (a 500), so it's refused up front.
_KEY_MAX = 60


def _validate_key(key: str) -> None:
    """Raise HTTP 400 if ``key`` isn't on the preference allowlist.

    Applied to all 4 single-key endpoints (GET, PUT, PATCH, DELETE)
    so a caller can read / write / delete exactly the rows the
    frontend writes, and no others. The bulk ``GET /api/preferences``
    is intentionally not gated — the caller can only read their own
    rows, so pollution on the read side is bounded by what they
    already wrote (which is gated by PUT and PATCH).

    ``fullmatch``, not ``match``: the pattern ends in ``$``, which
    also matches just before a trailing newline.

    NUL is refused on its own: the suffix is "anything non-empty", so
    the pattern lets ``seen:a\\x00b`` through, and Postgres text can't
    hold a NUL (the bound parameter fails to encode, a 500). No key
    the frontend writes can contain one.
    """
    if not isinstance(key, str) or len(key) > _KEY_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"preference key must be {_KEY_MAX} characters or fewer",
        )
    if "\x00" in key:
        raise HTTPException(
            status_code=400,
            detail="preference key can't contain a NUL character",
        )
    if not _PREFERENCE_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail=(
                f"preference key {key!r} is not on the allowlist; "
                f"expected one of the 11 known namespaces "
                f"(read_entries:<id>, last_viewed:<id>, "
                f"column_prefs:<id>, column_sections:<id>, seen:<id>, "
                f"hidden_entries, starred_entries, voted_entries, "
                f"filter_presets, history_group_by, category_layout)"
            ),
        )


# Set keys: the keys whose value is a set of entry ids (or, for
# ``voted_entries``, a map of entry id -> "up" / "down"). Only these
# take PATCH. ``<name>:*`` is a suffixed namespace. The cap is the
# most the row may hold; PATCH trims down to it, smallest entry id
# first. Clients prune first, so this is a backstop. Pinned against
# the frontend's ``MAX_*`` constants by ``test_preferences_delta.py``.
SET_KEY_CAPS: dict[str, int] = {
    "starred_entries": 1000,
    "hidden_entries": 1000,
    "voted_entries": 2000,
    "read_entries:*": 1000,
    "seen:*": 1000,
}

# The one list of set keys: PATCH takes only these, and PUT refuses
# them with a 409. A bundle older than PR2b PUTs whole lists, and one
# resumed days after launch still holds the lists it read then, so its
# next save, hide or vote would overwrite everything other devices
# added since. Clients from PR2b on write these keys only by PATCH. A
# new set key goes into ``SET_KEY_CAPS`` and so joins both (PR5b adds
# ``muted_sources``). Pinned by ``test_preferences_put_set_keys.py``.
SET_KEYS: tuple[str, ...] = tuple(SET_KEY_CAPS)

SET_KEY_PUT_DETAIL = "Popping was updated. Reload to keep saving."

_VOTES_KEY = "voted_entries"
_ID_MAX = 2**63 - 1
_VOTE_ID_RE = re.compile(r"[1-9][0-9]{0,18}")


def _set_key(key: str) -> str | None:
    """The ``SET_KEYS`` entry an allowlisted ``key`` falls under
    (``seen:Tech`` -> ``seen:*``), or None if it isn't a set key. Call
    after ``_validate_key``: it relies on suffixed names having a
    non-empty suffix and global names having none."""
    name, sep, _ = key.partition(":")
    pattern = f"{name}:*" if sep else name
    return pattern if pattern in SET_KEYS else None


def is_entry_id(value: object) -> bool:
    # ``type(...) is int``: a JSON ``true`` decodes to ``bool``, which
    # ``isinstance(..., int)`` would let through.
    return type(value) is int and 1 <= value <= _ID_MAX


def _coerce_ids(value: object) -> list[int]:
    """The stored id list as clean, de-duplicated ints, in stored order.

    A row written by PUT before set keys were refused holds opaque
    JSON, so it may hold anything: a non-list becomes empty and non-id
    elements are dropped. Without this a malformed row would 500
    every PATCH to its key.
    """
    if not isinstance(value, list):
        return []
    out: list[int] = []
    present: set[int] = set()
    for item in value:
        if is_entry_id(item) and item not in present:
            out.append(item)
            present.add(item)
    return out


def _coerce_votes(value: object) -> dict[str, str]:
    """The stored votes as ``{"<id>": "up" | "down"}``; anything else
    (a non-dict row, a bad id, a bad vote) is dropped."""
    if not isinstance(value, dict):
        return {}
    return {
        k: v
        for k, v in value.items()
        if isinstance(k, str)
        and _VOTE_ID_RE.fullmatch(k)
        and int(k) <= _ID_MAX
        and v in ("up", "down")
    }


def _trim_ids(ids: list[int], cap: int) -> list[int]:
    """Drop the smallest ids until ``cap`` remain, keeping order.
    Entry ids are monotonic, so this drops the oldest stories, as
    the frontend's ``trimVotedEntries`` does."""
    if len(ids) <= cap:
        return ids
    keep = set(sorted(ids)[-cap:])
    return [i for i in ids if i in keep]


def _trim_votes(votes: dict[str, str], cap: int) -> dict[str, str]:
    if len(votes) <= cap:
        return votes
    keep = set(sorted(votes, key=int)[-cap:])
    return {k: v for k, v in votes.items() if k in keep}


async def hidden_entry_ids(session: AsyncSession, user_id: str) -> list[int]:
    """The caller's hidden entry ids, for the list routes to exclude
    (``/api/entries`` and ``/api/foryou``).

    The row is client-written JSON, so it is read through the same
    coercion as PATCH: real ints only (a JSON ``true`` is dropped),
    1 to 2**63-1, de-duplicated, and the newest ``hidden_entries``
    cap (1000) by id. A malformed row reads as fewer ids, never as an
    error, so it can't take down the main feed.
    """
    value = await session.scalar(
        select(UserPreference.value).where(
            UserPreference.user_id == user_id,
            UserPreference.key == "hidden_entries",
        )
    )
    return _trim_ids(_coerce_ids(value), SET_KEY_CAPS["hidden_entries"])


def _apply_patch(
    key: str, current: object, body: UserPreferencePatchIn, cap: int
) -> list[int] | dict[str, str]:
    """The new value for ``key``: the coerced ``current`` value with
    ``body`` applied (``remove`` before ``add``), trimmed to ``cap``."""
    if key == _VOTES_KEY:
        votes = _coerce_votes(current)
        for entry_id, vote in body.set_.items():
            if vote is None:
                votes.pop(entry_id, None)
            else:
                votes[entry_id] = vote
        return _trim_votes(votes, cap)
    removed = set(body.remove)
    ids = [i for i in _coerce_ids(current) if i not in removed]
    present = set(ids)
    for entry_id in body.add:
        if entry_id not in present:
            ids.append(entry_id)
            present.add(entry_id)
    return _trim_ids(ids, cap)


def _since_to_utc(value: dt.datetime) -> dt.datetime:
    """``since`` in UTC, or a 422 when it has no UTC equivalent.

    ``0001-01-01T00:00:00+14:00`` is a valid aware datetime, but in UTC
    it falls before year 1. Left to the driver, that is an
    ``OverflowError`` while binding the parameter (a 500). A
    ``ValueError`` here makes it a validation error instead.
    """
    try:
        return value.astimezone(dt.timezone.utc)
    except OverflowError as exc:
        raise ValueError("since is out of range in UTC") from exc


@router.get("/preferences", response_model=UserPreferenceListOut)
async def list_preferences(
    since: Annotated[AwareDatetime, AfterValidator(_since_to_utc)] | None = Query(
        default=None
    ),
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

    ``since`` (timezone-aware; a naive timestamp, or one with no UTC
    equivalent, is a 422) keeps only rows with ``updated_at > since``. Deletions don't show up in a
    since-response; a full GET is how they arrive.

    ``updated_at`` is ``now()``, the writing transaction's START time,
    and writes to different keys don't block each other. So a write
    that started first can commit after a poll has already returned a
    later row: a client polls with an overlap (``since`` = the newest
    ``updated_at`` it has seen, minus 60 s), not from that timestamp
    itself.
    """
    user_id = resolve_user_id(user)
    stmt = select(UserPreference).where(UserPreference.user_id == user_id)
    if since is not None:
        stmt = stmt.where(UserPreference.updated_at > since)
    rows = await session.scalars(stmt)
    items = [
        UserPreferenceOut(
            key=row.key,
            value=row.value,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    return UserPreferenceListOut(items=items)


@router.get("/preferences/{key:preference_key}", response_model=UserPreferenceOut)
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


@router.put("/preferences/{key:preference_key}", response_model=UserPreferenceOut)
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

    409 for a set key (``SET_KEYS``): those change only by PATCH. A
    whole-list PUT of one comes from a bundle older than PR2b, whose
    lists may be days stale, and storing it would undo saves, hides
    and votes made since on every device. Refused, that bundle loses
    only its own latest change, and shows "Couldn't save" until it
    reloads.

    Implementation note
    --------------------
    We use ``INSERT ... ON CONFLICT (user_id, key) DO UPDATE`` so
    the upsert is one round-trip, not a SELECT-then-INSERT-or-UPDATE.
    This matters for the high-frequency read-state path: marking
    one card read on every visible-card-scroll would otherwise
    burn 2x the round-trips.
    """
    _validate_key(key)
    if _set_key(key) is not None:
        raise HTTPException(status_code=409, detail=SET_KEY_PUT_DETAIL)
    user_id = resolve_user_id(user)
    stmt = (
        pg_insert(UserPreference)
        .values(user_id=user_id, key=key, value=body.value)
        .on_conflict_do_update(
            index_elements=["user_id", "key"],
            # ``func.now()``, not the column's own current value: an
            # ON CONFLICT DO UPDATE SET clause only touches columns
            # explicitly listed here (unlike a plain UPDATE, it does
            # NOT fall back to the column's ``onupdate=func.now()``
            # default for anything omitted), so referencing
            # ``UserPreference.__table__.c.updated_at`` here would
            # compile to ``SET updated_at = user_preferences.updated_at``
            # — a no-op that freezes the timestamp at insert time
            # forever.
            set_={"value": body.value, "updated_at": func.now()},
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


@router.patch("/preferences/{key:preference_key}", response_model=UserPreferenceOut)
async def patch_preference(
    key: str,
    body: UserPreferencePatchIn,
    user: dict | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserPreferenceOut:
    """Apply a change to one set key and return the whole new row.

    400 for a key that isn't a set key (see ``SET_KEYS``). 422 for
    a body that doesn't fit the key: ``set`` on an id-list key,
    ``add`` / ``remove`` on ``voted_entries``, or a list longer than
    the key's cap.

    One transaction, so two devices (or two calls in one tick) both
    land instead of the later whole-list write winning:

      1. ``INSERT ... ON CONFLICT DO UPDATE SET key = EXCLUDED.key
         RETURNING value`` inserts an empty row, or locks the existing
         one and reads its value, in one statement. Without the lock,
         two PATCHes read the same old value and the later UPDATE
         drops the earlier one's change. Two first writes to the same
         key serialize on the unique index. It is one statement, not
         ``ON CONFLICT DO NOTHING`` then ``SELECT ... FOR UPDATE``,
         because a DELETE of the key (Reset, Mark all) committing
         between those two left the SELECT with no row, and the
         UPDATE then matched nothing (a 500). Here Postgres waits for
         a conflicting row's writer, and when that writer deleted the
         row it retries the insert, so a row always comes back, locked.
      2. The locked value is coerced (``_coerce_ids`` /
         ``_coerce_votes``), since a PUT from before set keys were
         refused may have stored anything.
      3. ``remove`` is applied before ``add``, then the result is
         trimmed to the cap, smallest entry id first.
      4. The value is written with ``updated_at = now()``.
    """
    _validate_key(key)
    set_key = _set_key(key)
    if set_key is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"preference key {key!r} doesn't take PATCH; only the "
                f"set keys do ({', '.join(SET_KEYS)})"
            ),
        )
    cap = SET_KEY_CAPS[set_key]
    sent = body.model_fields_set
    if key == _VOTES_KEY:
        if sent & {"add", "remove"}:
            raise HTTPException(
                status_code=422,
                detail=f"{key} takes 'set', not 'add' or 'remove'",
            )
    else:
        if "set_" in sent:
            raise HTTPException(
                status_code=422,
                detail=f"{key} takes 'add' and 'remove', not 'set'",
            )
        if len(body.add) > cap or len(body.remove) > cap:
            raise HTTPException(
                status_code=422,
                detail=f"{key} takes at most {cap} ids per list",
            )

    user_id = resolve_user_id(user)
    where = (UserPreference.user_id == user_id, UserPreference.key == key)
    insert = pg_insert(UserPreference).values(
        user_id=user_id, key=key, value={} if key == _VOTES_KEY else []
    )
    # ``SET key = EXCLUDED.key`` changes nothing; it is there so the
    # conflict path takes the row lock and RETURNING reads the row.
    locked = await session.execute(
        insert.on_conflict_do_update(
            index_elements=["user_id", "key"],
            set_={"key": insert.excluded.key},
        ).returning(UserPreference.value)
    )
    current = locked.scalar_one()
    value = _apply_patch(key, current, body, cap)
    result = await session.execute(
        update(UserPreference)
        .where(*where)
        .values(value=value, updated_at=func.now())
        .returning(UserPreference.updated_at)
    )
    row_updated_at = result.scalar_one()
    await session.commit()
    logger.debug("preference: user=%s key=%s patched", user_id, key)
    return UserPreferenceOut(key=key, value=value, updated_at=row_updated_at)


@router.delete("/preferences/{key:preference_key}", status_code=204)
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
