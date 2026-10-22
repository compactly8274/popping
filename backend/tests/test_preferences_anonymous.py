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
        "/api/preferences/column_prefs:1",
        json={"value": {"sort": "top"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == {"sort": "top"}

    resp = await app_client.get("/api/preferences/column_prefs:1")
    assert resp.status_code == 200
    assert resp.json()["value"] == {"sort": "top"}

    resp = await app_client.get("/api/preferences")
    assert resp.status_code == 200
    keys = [item["key"] for item in resp.json()["items"]]
    assert "column_prefs:1" in keys

    resp = await app_client.delete("/api/preferences/column_prefs:1")
    assert resp.status_code == 204

    resp = await app_client.get("/api/preferences/column_prefs:1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_anonymous_round_trip_patch_get_list_delete(app_client):
    """Read state is a set key, which changes by PATCH (a PUT of it is
    a 409, see ``test_preferences_put_set_keys.py``). With no auth at
    all, that must succeed too."""
    resp = await app_client.patch(
        "/api/preferences/read_entries:1",
        json={"add": [101, 102]},
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
async def test_column_id_with_uppercase_and_space_is_accepted(app_client):
    """Regression test: the preference-key allowlist added for the
    "arbitrary key pollution" fix originally used a lowercase-only
    ``[a-z0-9_-]+`` suffix charset. App.tsx passes a column's literal
    display name as the columnId — including the built-in "Saved" /
    "For You" columns and the multi-source filter column (which is
    derived from raw source names, e.g. a Reddit source's "r/technology").
    A lowercase-only charset silently 400'd every one of those, so
    read-state / last-viewed / column-prefs never persisted for them.
    """
    # A columnId containing a literal ``/`` (e.g. "r/technology", or a
    # category like "News/Tech") is covered by
    # ``test_preferences_delta.py::test_slash_keys_round_trip_through_every_route``.
    for key in (
        "last_viewed:Saved",
        "column_sections:For You",
        "column_prefs:Filtering",
    ):
        resp = await app_client.put(f"/api/preferences/{key}", json={"value": "x"})
        assert resp.status_code == 200, f"{key} -> {resp.status_code} {resp.text}"

        resp = await app_client.get(f"/api/preferences/{key}")
        assert resp.status_code == 200
        assert resp.json()["value"] == "x"

    # Read state for those columns is a set key, written by PATCH.
    for key in ("read_entries:For You", "seen:For You"):
        resp = await app_client.patch(f"/api/preferences/{key}", json={"add": [7]})
        assert resp.status_code == 200, f"{key} -> {resp.status_code} {resp.text}"

        resp = await app_client.get(f"/api/preferences/{key}")
        assert resp.status_code == 200
        assert resp.json()["value"] == [7]


@pytest.mark.asyncio
async def test_unknown_key_is_still_rejected(app_client):
    """The allowlist's security boundary is the PREFIX, not the
    suffix charset — an unrelated key must still 400."""
    resp = await app_client.put("/api/preferences/admin_override", json={"value": "x"})
    assert resp.status_code == 400


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


@pytest.mark.asyncio
async def test_updated_at_advances_on_second_put(app_client):
    """Regression test: the upsert's ON CONFLICT DO UPDATE SET clause
    used to reference the column's own current value
    (``UserPreference.__table__.c.updated_at``) instead of
    ``func.now()``, which compiles to ``SET updated_at =
    user_preferences.updated_at`` — a no-op. After the first PUT,
    ``updated_at`` was frozen forever regardless of how many times
    the key was subsequently written."""
    first = await app_client.put(
        "/api/preferences/column_prefs:updated_at_probe",
        json={"value": {"sort": "top"}},
    )
    assert first.status_code == 200
    first_updated_at = first.json()["updated_at"]

    second = await app_client.put(
        "/api/preferences/column_prefs:updated_at_probe",
        json={"value": {"sort": "newest"}},
    )
    assert second.status_code == 200
    second_updated_at = second.json()["updated_at"]

    assert second_updated_at > first_updated_at, (
        f"updated_at must advance on a second write to the same key: "
        f"{first_updated_at!r} -> {second_updated_at!r}"
    )
