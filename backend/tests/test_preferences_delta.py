"""PATCH /api/preferences/{key}: set keys change by delta, not by
overwrite; GET /api/preferences?since=; and ``{key:path}`` keys.

Two devices (or two calls in one tick) used to PUT whole lists, so the
later write dropped the earlier one's ids. PATCH applies ``add`` /
``remove`` (or ``set`` for votes) to the row under ``SELECT ... FOR
UPDATE`` (taken by ``INSERT ... ON CONFLICT DO UPDATE``). The lock
tests below are deterministic: a second session
holds the lock (or an uncommitted insert) while the PATCH runs as a
task, so the interleaving that loses a write without the lock is the
one that actually happens.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.deps import ANONYMOUS_USER_ID, current_user
from app.db import SessionLocal
from app.models import UserPreference
from app.routes.preferences import SET_KEY_CAPS

REPO = Path(__file__).resolve().parents[2]


def _url(key: str) -> str:
    # What the client sends: ``encodeURIComponent(key)``.
    return "/api/preferences/" + quote(key, safe="")


async def _patch(client, key: str, body: dict):
    return await client.patch(_url(key), json=body)


async def _value(client, key: str):
    resp = await client.get(_url(key))
    assert resp.status_code == 200, resp.text
    return resp.json()["value"]


async def _store(key: str, value) -> None:
    """Write the anonymous row as given, the way a whole-list PUT did
    before PUT refused set keys. Rows like these (stale, over the cap,
    malformed) are still in the database, and PATCH has to cope."""
    async with SessionLocal() as s:
        stmt = pg_insert(UserPreference).values(
            user_id=ANONYMOUS_USER_ID, key=key, value=value
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["user_id", "key"],
                set_={"value": stmt.excluded.value},
            )
        )
        await s.commit()


def _anon_row(key: str):
    return select(UserPreference).where(
        UserPreference.user_id == ANONYMOUS_USER_ID,
        UserPreference.key == key,
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_waits_for_the_row_lock_and_keeps_both_changes(app_client):
    """Session A holds ``SELECT ... FOR UPDATE`` on the row; PATCH B must
    wait for it, then apply its add on top of A's committed value.
    Without the lock (``ON CONFLICT DO NOTHING`` plus a plain SELECT), B
    reads the old value, waits only at its UPDATE, and then overwrites
    A's id."""
    key = "hidden_entries"
    resp = await _patch(app_client, key, {"add": [1]})
    assert resp.status_code == 200, resp.text

    async with SessionLocal() as a:
        await a.execute(_anon_row(key).with_for_update())
        b = asyncio.create_task(_patch(app_client, key, {"add": [3]}))
        try:
            await asyncio.sleep(0.2)
            assert not b.done(), "PATCH must block on the row lock"
            await a.execute(
                update(UserPreference)
                .where(
                    UserPreference.user_id == ANONYMOUS_USER_ID,
                    UserPreference.key == key,
                )
                .values(value=[1, 2])
            )
            await a.commit()
        finally:
            await a.rollback()
            resp = await b

    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["value"]) == [1, 2, 3]
    assert sorted(await _value(app_client, key)) == [1, 2, 3]


@pytest.mark.asyncio
async def test_first_write_waits_for_a_concurrent_insert(app_client):
    """The first-write path: while another transaction's insert of the
    same key is uncommitted, the PATCH waits on the unique index, then
    locks and extends that row."""
    key = "starred_entries"
    async with SessionLocal() as a:
        await a.execute(
            pg_insert(UserPreference)
            .values(user_id=ANONYMOUS_USER_ID, key=key, value=[])
            .on_conflict_do_nothing(index_elements=["user_id", "key"])
        )
        b = asyncio.create_task(_patch(app_client, key, {"add": [2]}))
        try:
            await asyncio.sleep(0.2)
            assert not b.done(), "PATCH must wait for the uncommitted insert"
            await a.execute(
                update(UserPreference)
                .where(
                    UserPreference.user_id == ANONYMOUS_USER_ID,
                    UserPreference.key == key,
                )
                .values(value=[1])
            )
            await a.commit()
        finally:
            await a.rollback()
            resp = await b

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_first", [True, False], ids=["locked", "unlocked"])
async def test_patch_racing_a_delete_of_the_key_recreates_the_row(
    app_client, lock_first
):
    """Reset and Mark all DELETE the key while another device PATCHes
    it. Session A deletes the row and commits while PATCH B waits on it:
    with ``locked``, A held ``FOR UPDATE`` before B started; with
    ``unlocked``, A's uncommitted DELETE is what B waits on. B must not
    find "no row" (that was a 500): it gets a fresh row holding only its
    own change."""
    key = "hidden_entries"
    resp = await _patch(app_client, key, {"add": [1, 2]})
    assert resp.status_code == 200, resp.text

    async with SessionLocal() as a:
        if lock_first:
            await a.execute(_anon_row(key).with_for_update())
        else:
            await a.execute(
                delete(UserPreference).where(
                    UserPreference.user_id == ANONYMOUS_USER_ID,
                    UserPreference.key == key,
                )
            )
        b = asyncio.create_task(_patch(app_client, key, {"add": [3]}))
        try:
            await asyncio.sleep(0.2)
            assert not b.done(), "PATCH must wait for the deleting transaction"
            if lock_first:
                await a.execute(
                    delete(UserPreference).where(
                        UserPreference.user_id == ANONYMOUS_USER_ID,
                        UserPreference.key == key,
                    )
                )
            await a.commit()
        finally:
            await a.rollback()
            resp = await b

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == [3]
    assert await _value(app_client, key) == [3]


@pytest.mark.asyncio
async def test_concurrent_first_patches_on_a_new_key_all_land(app_client):
    key = "seen:Tech"
    results = await asyncio.gather(
        *(_patch(app_client, key, {"add": [i]}) for i in range(1, 9))
    )
    assert [r.status_code for r in results] == [200] * 8
    assert sorted(await _value(app_client, key)) == list(range(1, 9))


# ---------------------------------------------------------------------------
# Applying a change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_is_applied_before_add(app_client):
    key = "hidden_entries"
    await _patch(app_client, key, {"add": [1, 2]})
    resp = await _patch(app_client, key, {"add": [3, 1], "remove": [1, 3]})
    assert resp.status_code == 200, resp.text
    # 1 and 3 are removed, then added back: the add wins.
    assert resp.json()["value"] == [2, 3, 1]


@pytest.mark.asyncio
async def test_add_is_idempotent_and_remove_of_absent_id_is_a_no_op(app_client):
    key = "starred_entries"
    await _patch(app_client, key, {"add": [5, 6]})
    resp = await _patch(app_client, key, {"add": [6, 6, 7], "remove": [99]})
    assert resp.json()["value"] == [5, 6, 7]


@pytest.mark.asyncio
async def test_cap_trims_smallest_ids_first(app_client):
    key = "hidden_entries"
    cap = SET_KEY_CAPS[key]
    # Stored newest-first order is kept for the ids that survive.
    resp = await _patch(app_client, key, {"add": list(range(cap, 0, -1))})
    assert resp.json()["value"] == list(range(cap, 0, -1))
    resp = await _patch(app_client, key, {"add": [5000, 4000]})
    assert resp.status_code == 200, resp.text
    value = resp.json()["value"]
    assert len(value) == cap
    assert 1 not in value and 2 not in value
    assert value[:2] == [cap, cap - 1]
    assert value[-2:] == [5000, 4000]
    # The new ids are not always the survivors: an add older than
    # everything stored is the one that's trimmed.
    resp = await _patch(app_client, key, {"add": [2]})
    assert 2 not in resp.json()["value"]
    assert len(resp.json()["value"]) == cap


@pytest.mark.asyncio
async def test_cap_trims_an_over_cap_row_written_by_an_old_put(app_client):
    key = "read_entries:Tech"
    cap = SET_KEY_CAPS["read_entries:*"]
    await _store(key, list(range(1, cap + 6)))
    resp = await _patch(app_client, key, {"remove": [cap + 5]})
    assert resp.json()["value"] == list(range(5, cap + 5))


@pytest.mark.asyncio
async def test_votes_set_clear_and_cap(app_client):
    key = "voted_entries"
    cap = SET_KEY_CAPS[key]
    resp = await _patch(app_client, key, {"set": {"10": "up", "11": "down"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == {"10": "up", "11": "down"}

    resp = await _patch(app_client, key, {"set": {"10": None, "11": "up", "12": None}})
    assert resp.json()["value"] == {"11": "up"}

    await _store(key, {str(i): "up" for i in range(1, cap + 1)})
    resp = await _patch(
        app_client, key, {"set": {str(cap + 100): "down", str(cap + 101): "up"}}
    )
    value = resp.json()["value"]
    assert len(value) == cap
    # Smallest by number, not by string: "10" sorts before "2".
    assert "1" not in value and "2" not in value
    assert "10" in value
    assert value[str(cap + 100)] == "down"


# ---------------------------------------------------------------------------
# Malformed stored rows are coerced, not a 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, expected",
    [
        ("not a list", [42]),
        ({"1": "up"}, [42]),
        (None, [42]),
        ([1, "2", 3.5, True, None, -4, 0, 2**70, [5], {"6": 6}, 1, 7], [1, 7, 42]),
        # ``true`` is not the id 1.
        ([True, False, 3], [3, 42]),
    ],
)
async def test_malformed_list_row_is_coerced(app_client, stored, expected):
    key = "starred_entries"
    await _store(key, stored)
    resp = await _patch(app_client, key, {"add": [42]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        [1, 2],
        "x",
        {"1": "up", "07": "up", "x": "down", "2": "sideways", "3": None, "4": "down"},
    ],
)
async def test_malformed_votes_row_is_coerced(app_client, stored):
    key = "voted_entries"
    await _store(key, stored)
    resp = await _patch(app_client, key, {"set": {"9": "up"}})
    assert resp.status_code == 200, resp.text
    kept = {"1": "up", "4": "down"} if isinstance(stored, dict) else {}
    assert resp.json()["value"] == {**kept, "9": "up"}


# ---------------------------------------------------------------------------
# 400: keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["column_prefs:Tech", "last_viewed:Tech", "column_sections:Tech",
     "filter_presets", "history_group_by"],
)
async def test_patch_of_a_non_set_key_is_400(app_client, key):
    resp = await _patch(app_client, key, {"add": [1]})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["admin_override", "seen:", "seen", "hidden_entries:x"])
async def test_patch_of_an_unknown_key_is_400(app_client, key):
    resp = await _patch(app_client, key, {"add": [1]})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
async def test_key_longer_than_60_is_400(app_client, method):
    key = "read_entries:" + "x" * 48  # 61 characters
    assert len(key) == 61
    body = {"value": [1]} if method == "PUT" else {"add": [1]}
    resp = await app_client.request(method, _url(key), json=body)
    assert resp.status_code == 400, resp.text
    # 60 is still fine, on a set key (PATCH) and on a PUT key.
    ok = await _patch(app_client, key[:-1], {"add": [1]})
    assert ok.status_code == 200, ok.text
    put_key = "column_prefs:" + "x" * 47
    assert len(put_key) == 60
    ok = await app_client.put(_url(put_key), json={"value": {"sort": "top"}})
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "key",
    ["hidden_entries\n", "read_entries:a\nb", "seen:a\x00b", "hidden_entries\x00"],
)
async def test_newline_or_nul_in_key_is_400(app_client, method, key):
    """``match`` with the allowlist's closing ``$`` accepted a trailing
    newline, and the route pattern's own ``$`` dropped it before the
    handler ran (or 404'd a newline mid-key). A NUL passes the
    allowlist's "anything non-empty" suffix, and Postgres text can't
    hold it, so without its own check it was a 500."""
    body = {"value": [1]} if method == "PUT" else {"add": [1]}
    resp = await app_client.request(method, _url(key), json=body)
    assert resp.status_code == 400, resp.text
    assert _keys(await app_client.get("/api/preferences")) == set()


# ---------------------------------------------------------------------------
# 422: bodies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key, body",
    [
        ("hidden_entries", {"add": [1], "value": [1]}),  # unknown field
        ("hidden_entries", {"add": [0]}),
        ("hidden_entries", {"add": [2**63]}),
        ("hidden_entries", {"remove": [-1]}),
        ("hidden_entries", {"add": ["5"]}),  # strict: no string ids
        ("hidden_entries", {"add": [5.0]}),
        ("hidden_entries", {"add": [True]}),
        ("hidden_entries", {"add": None}),
        ("hidden_entries", {"add": list(range(1, 1002))}),  # over the cap
        ("seen:Tech", {"remove": list(range(1, 1002))}),
        ("hidden_entries", {"set": {"1": "up"}}),  # set on a list key
        ("read_entries:Tech", {"set": {}}),
        ("voted_entries", {"add": [1]}),  # add on votes
        ("voted_entries", {"remove": [1]}),
        ("voted_entries", {"set": {"1": "sideways"}}),
        ("voted_entries", {"set": {"1": 1}}),
        ("voted_entries", {"set": {"07": "up"}}),
        ("voted_entries", {"set": {"0": "up"}}),
        ("voted_entries", {"set": {"abc": "up"}}),
        ("voted_entries", {"set": {str(2**63): "up"}}),
        ("voted_entries", {"set": {str(i): "up" for i in range(1, 2002)}}),
    ],
)
async def test_patch_body_errors_are_422(app_client, key, body):
    resp = await _patch(app_client, key, body)
    assert resp.status_code == 422, resp.text
    # Nothing was written.
    assert (await app_client.get(_url(key))).status_code == 404


@pytest.mark.asyncio
async def test_largest_ids_and_full_lists_are_accepted(app_client):
    resp = await _patch(app_client, "hidden_entries", {"add": [2**63 - 1]})
    assert resp.status_code == 200, resp.text
    resp = await _patch(app_client, "seen:Tech", {"add": list(range(1, 1001))})
    assert resp.status_code == 200, resp.text
    resp = await _patch(
        app_client, "voted_entries", {"set": {str(i): "up" for i in range(1, 2001)}}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "since",
    [
        "2026-10-19T12:00:00",  # naive
        # Aware, but outside year 1 to 9999 once converted to UTC: the
        # driver couldn't bind these (a 500).
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ],
)
async def test_bad_since_is_422(app_client, since):
    resp = await app_client.get("/api/preferences", params={"since": since})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "since", ["0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00"]
)
async def test_since_at_the_ends_of_the_range_is_200(app_client, since):
    await _patch(app_client, "hidden_entries", {"add": [1]})
    resp = await app_client.get("/api/preferences", params={"since": since})
    expected = {"hidden_entries"} if since.startswith("0001") else set()
    assert _keys(resp) == expected


# ---------------------------------------------------------------------------
# Users and suffixed keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_patch_is_stored_under_the_anonymous_id(app_client, db_session):
    resp = await _patch(app_client, "hidden_entries", {"add": [7]})
    assert resp.status_code == 200, resp.text
    row = await db_session.scalar(_anon_row("hidden_entries"))
    assert row is not None and row.value == [7]


@pytest.mark.asyncio
async def test_patch_is_per_user(app_client):
    from app.main import app

    await _patch(app_client, "hidden_entries", {"add": [1]})
    app.dependency_overrides[current_user] = lambda: {"sub": "alice"}
    try:
        resp = await _patch(app_client, "hidden_entries", {"add": [2]})
        assert resp.json()["value"] == [2]
    finally:
        app.dependency_overrides.pop(current_user, None)
    assert await _value(app_client, "hidden_entries") == [1]


@pytest.mark.asyncio
async def test_suffixed_set_keys_are_separate_rows(app_client):
    await _patch(app_client, "read_entries:Tech", {"add": [1]})
    await _patch(app_client, "seen:Tech", {"add": [2]})
    await _patch(app_client, "seen:For You", {"add": [3]})
    assert await _value(app_client, "read_entries:Tech") == [1]
    assert await _value(app_client, "seen:Tech") == [2]
    assert await _value(app_client, "seen:For You") == [3]


# ---------------------------------------------------------------------------
# GET ?since=
# ---------------------------------------------------------------------------


def _keys(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {item["key"] for item in resp.json()["items"]}


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


@pytest.mark.asyncio
async def test_since_returns_only_rows_written_after_it(app_client):
    first = await _patch(app_client, "hidden_entries", {"add": [1]})
    t1 = first.json()["updated_at"]
    await _patch(app_client, "starred_entries", {"add": [2]})

    assert _keys(await app_client.get("/api/preferences")) == {
        "hidden_entries", "starred_entries"
    }
    assert _keys(await app_client.get("/api/preferences", params={"since": t1})) == {
        "starred_entries"
    }
    later = (_ts(t1) + dt.timedelta(hours=1)).isoformat()
    assert _keys(await app_client.get("/api/preferences", params={"since": later})) == set()


@pytest.mark.asyncio
async def test_since_with_overlap_returns_writes_that_commit_out_of_order(app_client):
    """``updated_at`` is the transaction's start time. PATCH 1 starts,
    then waits on a row lock; PATCH 2 (another key) starts later and
    commits first; a poll then sees PATCH 2's timestamp as the newest.
    PATCH 1 commits afterwards with an OLDER timestamp, so a poll from
    exactly that watermark misses it, and the client's 60 s overlap is
    what brings it back."""
    await _patch(app_client, "hidden_entries", {"add": [1]})

    async with SessionLocal() as a:
        await a.execute(_anon_row("hidden_entries").with_for_update())
        p1 = asyncio.create_task(_patch(app_client, "hidden_entries", {"add": [11]}))
        try:
            await asyncio.sleep(0.2)
            assert not p1.done()
            p2 = await _patch(app_client, "starred_entries", {"add": [22]})
            assert p2.status_code == 200, p2.text
            poll = await app_client.get("/api/preferences")
            watermark = max(item["updated_at"] for item in poll.json()["items"])
            assert watermark == p2.json()["updated_at"]
        finally:
            await a.rollback()
            p1_resp = await p1

    assert p1_resp.status_code == 200, p1_resp.text
    assert _ts(p1_resp.json()["updated_at"]) < _ts(watermark)

    exact = await app_client.get("/api/preferences", params={"since": watermark})
    assert "hidden_entries" not in _keys(exact)

    overlap = (_ts(watermark) - dt.timedelta(seconds=60)).isoformat()
    resp = await app_client.get("/api/preferences", params={"since": overlap})
    items = {item["key"]: item["value"] for item in resp.json()["items"]}
    assert set(items) == {"hidden_entries", "starred_entries"}
    assert items["hidden_entries"] == [1, 11]


# ---------------------------------------------------------------------------
# Keys containing "/"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["read_entries:News/Tech", "seen:News/Tech"])
async def test_slash_keys_round_trip_through_every_route(app_client, key):
    """A category can contain ``/``. The client percent-encodes the key,
    the server decodes ``%2F`` before routing, and ``{key:path}`` has to
    match the decoded ``/``. A plain ``{key}`` 404'd these.

    These are set keys, so PUT reaches the handler and is refused with
    a 409 (a 404 would mean the route didn't match);
    ``test_slash_key_put_of_a_non_set_key`` covers a stored PUT."""
    url = _url(key)
    assert "%2F" in url

    resp = await app_client.put(url, json={"value": [1]})
    assert resp.status_code == 409, resp.text
    assert (await app_client.get(url)).status_code == 404

    resp = await app_client.patch(url, json={"add": [1]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == key

    resp = await app_client.patch(url, json={"add": [2]})
    assert resp.status_code == 200, resp.text
    assert resp.json() | {"updated_at": None} == {
        "key": key, "value": [1, 2], "updated_at": None
    }

    resp = await app_client.get(url)
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == [1, 2]
    assert key in _keys(await app_client.get("/api/preferences"))

    resp = await app_client.delete(url)
    assert resp.status_code == 204, resp.text
    assert (await app_client.get(url)).status_code == 404


@pytest.mark.asyncio
async def test_slash_key_put_of_a_non_set_key(app_client):
    key = "column_prefs:News/Tech"
    url = _url(key)
    resp = await app_client.put(url, json={"value": {"sort": "top"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == key
    assert await _value(app_client, key) == {"sort": "top"}
    resp = await app_client.delete(url)
    assert resp.status_code == 204, resp.text
    assert (await app_client.get(url)).status_code == 404


# ---------------------------------------------------------------------------
# Caps pinned against the frontend
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_set_key_caps_match_the_frontend():
    """The server trim is a backstop for the client's own prune, so the
    two must agree. The client's caps live in ``lib/prefSync.ts`` (PR2b),
    which prunes to them; ``MAX_READ_PER_KEY`` covers ``read_entries:*``
    and ``seen:*``."""
    src = (REPO / "frontend/src/lib/prefSync.ts").read_text()

    def ts_number(name: str) -> int:
        m = re.search(rf"export const {name}\s*=\s*(\d+)", src)
        assert m, f"couldn't find {name} in prefSync.ts"
        return int(m.group(1))

    assert SET_KEY_CAPS["hidden_entries"] == ts_number("MAX_HIDDEN")
    assert SET_KEY_CAPS["starred_entries"] == ts_number("MAX_STARRED")
    assert SET_KEY_CAPS["voted_entries"] == ts_number("MAX_VOTED")
    assert SET_KEY_CAPS["read_entries:*"] == ts_number("MAX_READ_PER_KEY")
    assert SET_KEY_CAPS["seen:*"] == ts_number("MAX_READ_PER_KEY")
