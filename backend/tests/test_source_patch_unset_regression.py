"""Regression tests for two related bugs found in a repo-wide audit,
both in ``routes/sources.py``'s handling of ``sitemap_url`` /
``link_pattern`` / ``custom_headers`` on ``generic_scrape`` sources:

1. ``create_source_endpoint`` validated ``body.sitemap_url`` with
   ``_validate_url`` (which returns ``None`` on success, raises on
   failure) and then assigned its return value straight to
   ``sitemap_url_value`` — so every created row got ``sitemap_url =
   NULL`` regardless of what the user submitted.

2. ``update_source_endpoint`` defaulted ``sitemap_url_value`` /
   ``link_pattern_value`` / ``headers`` to a bare ``None`` and only
   ever passed that concrete ``None`` (never the ``_UNSET`` sentinel
   ``scheduler.update_source`` uses to mean "field omitted, don't
   touch") down to ``scheduler.update_source``. Since ``None`` means
   "explicitly clear" to that function, *any* PATCH that didn't
   happen to include ``sitemap_url``/``link_pattern``/
   ``custom_headers`` in its JSON body — e.g. just flipping
   ``active`` or renaming the source — silently wiped those fields
   back to NULL.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _create_generic_scrape(app_client, name: str) -> dict:
    resp = await app_client.post(
        "/api/sources",
        json={
            "name": name,
            "type": "generic_scrape",
            "category": "news",
            "url": "https://example.com",
            "sitemap_url": "https://example.com/sitemap-news.xml",
            "link_pattern": "/news/",
            "custom_headers": {"X-Test-Header": "1"},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_create_source_persists_sitemap_url(app_client):
    """Bug 1: the create route must actually store the submitted
    ``sitemap_url``, not silently drop it to NULL."""
    row = await _create_generic_scrape(app_client, "sitemap_create_regress")
    assert row["sitemap_url"] == "https://example.com/sitemap-news.xml"
    assert row["link_pattern"] == "/news/"
    assert row["custom_headers"] == {"X-Test-Header": "1"}


async def test_unrelated_patch_field_does_not_wipe_sitemap_url(app_client):
    """Bug 2: PATCHing a field that has nothing to do with
    ``sitemap_url``/``link_pattern``/``custom_headers`` must leave
    those fields untouched — they should only change when the PATCH
    body actually names them.
    """
    row = await _create_generic_scrape(app_client, "sitemap_patch_regress")
    source_id = row["id"]

    resp = await app_client.patch(
        f"/api/sources/{source_id}",
        json={"category": "tech"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["category"] == "tech"
    assert updated["sitemap_url"] == "https://example.com/sitemap-news.xml"
    assert updated["link_pattern"] == "/news/"
    assert updated["custom_headers"] == {"X-Test-Header": "1"}


async def test_patch_explicit_null_still_clears_sitemap_url(app_client):
    """The ``_UNSET`` vs ``None`` distinction must still work the
    other way: an explicit ``"sitemap_url": null`` in the body is a
    real clear request, not a no-op."""
    row = await _create_generic_scrape(app_client, "sitemap_clear_regress")
    source_id = row["id"]

    resp = await app_client.patch(
        f"/api/sources/{source_id}",
        json={"sitemap_url": None},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["sitemap_url"] is None
    # Unrelated fields from creation are still untouched.
    assert updated["link_pattern"] == "/news/"
    assert updated["custom_headers"] == {"X-Test-Header": "1"}


async def test_patch_new_sitemap_url_value_is_applied(app_client):
    row = await _create_generic_scrape(app_client, "sitemap_update_regress")
    source_id = row["id"]

    resp = await app_client.patch(
        f"/api/sources/{source_id}",
        json={"sitemap_url": "https://example.com/sitemap-other.xml"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["sitemap_url"] == "https://example.com/sitemap-other.xml"
