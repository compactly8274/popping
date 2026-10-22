"""``category_layout``: the tab order and the hidden tabs (PR7a).

One whole value the frontend PUTs on every change
(``frontend/src/lib/preferences.tsx``, ``setCategoryLayout``). It is on
the key allowlist as a global key: it round-trips as stored, takes no
PATCH (it isn't a set key) and has no ``:<id>`` form.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PREFERENCES_TSX = REPO / "frontend/src/lib/preferences.tsx"

KEY = "category_layout"
LAYOUT = {
    "v": 1,
    "order": ["cat:news", "meta:foryou", "meta:saved", "cat:Hockey Night"],
    "hidden": ["cat:deals"],
}


@pytest.mark.asyncio
async def test_category_layout_round_trips(app_client):
    resp = await app_client.put(f"/api/preferences/{KEY}", json={"value": LAYOUT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == LAYOUT

    resp = await app_client.get(f"/api/preferences/{KEY}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == LAYOUT

    resp = await app_client.get("/api/preferences")
    assert resp.status_code == 200, resp.text
    rows = {item["key"]: item["value"] for item in resp.json()["items"]}
    assert rows[KEY] == LAYOUT

    # A change replaces the whole value.
    moved = {**LAYOUT, "order": ["cat:Hockey Night", *LAYOUT["order"][:-1]], "hidden": []}
    resp = await app_client.put(f"/api/preferences/{KEY}", json={"value": moved})
    assert resp.status_code == 200, resp.text
    resp = await app_client.get(f"/api/preferences/{KEY}")
    assert resp.json()["value"] == moved

    resp = await app_client.delete(f"/api/preferences/{KEY}")
    assert resp.status_code == 204
    resp = await app_client.get(f"/api/preferences/{KEY}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_category_layout_takes_no_patch(app_client):
    resp = await app_client.patch(f"/api/preferences/{KEY}", json={"add": [1]})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["category_layout:x", "category_layouts", "category_layout:"])
async def test_category_layout_has_no_other_form(app_client, key):
    resp = await app_client.put(f"/api/preferences/{key}", json={"value": LAYOUT})
    assert resp.status_code == 400, resp.text


@pytest.mark.no_db
def test_frontend_writes_the_same_key():
    src = PREFERENCES_TSX.read_text()
    assert re.search(r"categoryLayout:\s*'category_layout'", src), (
        "PREFERENCE_KEYS.categoryLayout must be 'category_layout', the key on the allowlist"
    )
