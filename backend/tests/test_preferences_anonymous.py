"""Regression test for the "no-auth deployment can't persist
preferences" bug fixed in PR #6.

Before that fix, ``/api/preferences`` was gated on ``require_user``,
which 401'd unconditionally on the default deployment shape
(``OIDC_ENABLED=false`` and ``LOCAL_AUTH_BYPASS=false``, so there's
never a session cookie). Every read-state / hidden-entry / column-pref
write silently failed. This test drives the routes with no auth
whatsoever — no cookie, no bypass header — the exact request shape a
default ``docker compose up -d`` install sends.
"""

from __future__ import annotations

import pytest

from app.auth.deps import ANONYMOUS_USER_ID, resolve_user_id


def test_resolve_user_id_anonymous_fallback():
    assert resolve_user_id(None) == ANONYMOUS_USER_ID
    assert resolve_user_id({}) == ANONYMOUS_USER_ID


def test_resolve_user_id_prefers_sub():
    assert resolve_user_id({"sub": "alice"}) == "alice"


@pytest.mark.asyncio
async def test_anonymous_round_trip_put_get_list_delete(app_client):
    # PUT with no auth at all must succeed (200), not 401.
    resp = await app_client.put(
        "/api/preferences/read_entries:1",
        json={"value": [101, 102]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == [101, 102]

    resp = await app_client.get("/api/preferences/read_entries:1")
    assert resp.status_code == 200
    assert resp.json()["value"] == [101, 102]

    resp = await app_client.get("/api/preferences")
    assert resp.status_code == 200
    keys = [item["key"] for item in resp.json()["items"]]
    assert "read_entries:1" in keys

    resp = await app_client.delete("/api/preferences/read_entries:1")
    assert resp.status_code == 204

    resp = await app_client.get("/api/preferences/read_entries:1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_anonymous_put_is_idempotent_upsert(app_client):
    first = await app_client.put(
        "/api/preferences/column_prefs:tech",
        json={"value": {"sort": "top"}},
    )
    assert first.status_code == 200

    second = await app_client.put(
        "/api/preferences/column_prefs:tech",
        json={"value": {"sort": "newest"}},
    )
    assert second.status_code == 200
    assert second.json()["value"] == {"sort": "newest"}

    resp = await app_client.get("/api/preferences")
    matching = [i for i in resp.json()["items"] if i["key"] == "column_prefs:tech"]
    assert len(matching) == 1
    assert matching[0]["value"] == {"sort": "newest"}
