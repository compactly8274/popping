"""PUT /api/preferences/{key} refuses the set keys with a 409.

A bundle older than the PATCH client PUTs whole lists for saves, hides,
votes and read marks. One resumed days after launch still holds the
lists it read then, so its next PUT used to overwrite everything other
devices had added since, and their next full sync adopted the stale
list. Set keys now change only by PATCH; a whole-list PUT of one is a
409 "Popping was updated. Reload to keep saving.", and the stored row
stays as it was.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from app.routes.preferences import SET_KEY_CAPS, SET_KEYS

# One example per set key, and for the suffixed ones also a column
# name with a space and a category with a "/". Each is checked below
# to cover every entry of ``SET_KEYS``.
SET_KEY_EXAMPLES = [
    "starred_entries",
    "hidden_entries",
    "voted_entries",
    "read_entries:Tech",
    "read_entries:For You",
    "read_entries:News/Tech",
    "seen:Tech",
    "seen:News/Tech",
]

PUT_KEY_EXAMPLES = [
    "column_prefs:Tech",
    "column_prefs:News/Tech",
    "last_viewed:Tech",
    "column_sections:Tech",
    "filter_presets",
    "history_group_by",
    "category_layout",
]


def _url(key: str) -> str:
    return "/api/preferences/" + quote(key, safe="")


def _pattern(key: str) -> str:
    name, sep, _ = key.partition(":")
    return f"{name}:*" if sep else name


def _change(key: str) -> dict:
    # A PATCH body that stores entry 5 under ``key``.
    return {"set": {"5": "up"}} if key == "voted_entries" else {"add": [5]}


def _stale_whole_list(key: str):
    return {"1": "down"} if key == "voted_entries" else [1, 2, 3]


@pytest.mark.no_db
def test_set_keys_are_pinned():
    """The one list of set keys. A new one (``muted_sources``, PR5b)
    is added to ``SET_KEY_CAPS``, which also makes it a PATCH key, and
    to the pin and examples here."""
    assert set(SET_KEYS) == {
        "starred_entries",
        "hidden_entries",
        "voted_entries",
        "read_entries:*",
        "seen:*",
    }
    assert len(SET_KEYS) == len(set(SET_KEYS))
    # Shared with the PATCH allowlist: PATCH takes exactly these.
    assert set(SET_KEYS) == set(SET_KEY_CAPS)
    # Every set key has a 409 case below.
    assert {_pattern(k) for k in SET_KEY_EXAMPLES} == set(SET_KEYS)
    assert not {_pattern(k) for k in PUT_KEY_EXAMPLES} & set(SET_KEYS)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SET_KEY_EXAMPLES)
async def test_put_of_a_set_key_is_409_and_writes_nothing(app_client, key):
    resp = await app_client.put(_url(key), json={"value": _stale_whole_list(key)})
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": "Popping was updated. Reload to keep saving."}
    assert (await app_client.get(_url(key))).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SET_KEY_EXAMPLES)
async def test_put_of_a_set_key_keeps_the_stored_row(app_client, key):
    """The case the 409 is for: another device's change, made by
    PATCH, survives an old bundle's whole-list PUT."""
    resp = await app_client.patch(_url(key), json=_change(key))
    assert resp.status_code == 200, resp.text
    before = resp.json()

    resp = await app_client.put(_url(key), json={"value": _stale_whole_list(key)})
    assert resp.status_code == 409, resp.text

    resp = await app_client.get(_url(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SET_KEY_EXAMPLES)
async def test_patch_and_delete_of_a_set_key_are_unchanged(app_client, key):
    resp = await app_client.patch(_url(key), json=_change(key))
    assert resp.status_code == 200, resp.text
    resp = await app_client.delete(_url(key))
    assert resp.status_code == 204, resp.text
    assert (await app_client.get(_url(key))).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("key", PUT_KEY_EXAMPLES)
async def test_put_of_other_keys_is_still_stored(app_client, key):
    resp = await app_client.put(_url(key), json={"value": {"sort": "top"}})
    assert resp.status_code == 200, resp.text
    resp = await app_client.get(_url(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == {"sort": "top"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["admin_override", "hidden_entries:x", "seen:", "starred_entries\n"]
)
async def test_put_of_an_unknown_key_is_still_400(app_client, key):
    """The allowlist runs first: a key that isn't a preference at all
    is a 400, not a request to reload."""
    resp = await app_client.put(_url(key), json={"value": [1]})
    assert resp.status_code == 400, resp.text
