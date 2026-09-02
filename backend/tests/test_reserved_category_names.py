"""Regression test for a category-name collision found in a repo-wide
audit: ``App.tsx``'s ``allSubsColumns`` always prepends a "Saved" and
a "For You" column ahead of the per-category ones, and those columns
are looked up by name in a plain ``Map``. A user-created category
named (case-insensitively) "Saved" or "For You" would collide with —
and silently overwrite — the real reserved column in that Map.

``_validate_category`` is the one place both the create and PATCH
routes funnel through, so rejecting the reserved names there closes
the hole for both paths.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("name", ["Saved", "saved", "SAVED", "For You", "for you", "FOR YOU"])
async def test_create_source_rejects_reserved_category_name(app_client, name):
    resp = await app_client.post(
        "/api/sources",
        json={
            "name": "reserved_category_probe",
            "type": "rss",
            "category": name,
            "url": "https://example.com/feed.xml",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_create_source_accepts_ordinary_category(app_client):
    resp = await app_client.post(
        "/api/sources",
        json={
            "name": "ordinary_category_probe",
            "type": "rss",
            "category": "tech",
            "url": "https://example.com/feed.xml",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "tech"


async def test_patch_source_rejects_reserved_category_name(app_client):
    create = await app_client.post(
        "/api/sources",
        json={
            "name": "reserved_category_patch_probe",
            "type": "rss",
            "category": "tech",
            "url": "https://example.com/feed.xml",
        },
    )
    assert create.status_code == 200, create.text
    source_id = create.json()["id"]

    resp = await app_client.patch(
        f"/api/sources/{source_id}",
        json={"category": "For You"},
    )
    assert resp.status_code == 422, resp.text
