"""Tests for app.apple_podcasts: resolving an Apple Podcasts catalog
link (podcasts.apple.com/.../id<N>) to its actual RSS feed URL.

The live "lookup succeeds" happy path isn't covered — needs real
network access to itunes.apple.com, which this sandboxed test
environment doesn't have (same tradeoff made throughout this
codebase). What IS covered: the pure id-extraction regex, the
no-op pass-through for non-matching URLs (no network call at all,
safe to test directly), and — usefully — that a failed lookup call
raises the app's own PodcastResolutionError rather than a raw httpx
exception leaking out, since the sandbox's blocked network gives a
real (if not the intended) failure to exercise that path against.
"""

from __future__ import annotations

import pytest

from app.apple_podcasts import PodcastResolutionError, apple_podcasts_id, resolve_feed_url


# --- apple_podcasts_id ---------------------------------------------------


def test_apple_podcasts_id_extracts_from_full_url():
    url = "https://podcasts.apple.com/us/podcast/how-did-this-get-made/id409287913"
    assert apple_podcasts_id(url) == "409287913"


def test_apple_podcasts_id_extracts_without_locale():
    assert apple_podcasts_id("https://podcasts.apple.com/podcast/id409287913") == "409287913"


def test_apple_podcasts_id_no_match_for_ordinary_feed_url():
    assert apple_podcasts_id("https://example.com/feed.xml") is None


def test_apple_podcasts_id_no_match_for_non_apple_host():
    assert apple_podcasts_id("https://example.com/podcasts.apple.com/id123") is None


# --- resolve_feed_url ---------------------------------------------------


async def test_resolve_feed_url_passthrough_for_non_matching_url():
    url = "https://example.com/feed.xml"
    assert await resolve_feed_url(url) == url


async def test_resolve_feed_url_passthrough_for_reddit_reference():
    # Sanity: resolve_feed_url is called unconditionally on every
    # source URL by the route layer, including reddit ones — must be
    # a true no-op for shapes that were never going to match.
    url = "r/python"
    assert await resolve_feed_url(url) == url


async def test_resolve_feed_url_raises_podcast_resolution_error_on_lookup_failure():
    # No real network access in this sandbox, so the lookup call
    # itself fails — exercises the error-wrapping path: a raw
    # httpx exception must not escape, only PodcastResolutionError.
    url = "https://podcasts.apple.com/us/podcast/how-did-this-get-made/id409287913"
    with pytest.raises(PodcastResolutionError):
        await resolve_feed_url(url)


# --- route wiring: POST /api/sources/test ------------------------------------


async def test_test_source_route_apple_podcasts_url_surfaces_resolution_error(app_client):
    # No network in this sandbox — the lookup fails, which should
    # surface as an ordinary ok=False test result (not a 500), same
    # as any other unreachable-URL test failure.
    resp = await app_client.post(
        "/api/sources/test",
        json={
            "type": "podcast",
            "url": "https://podcasts.apple.com/us/podcast/how-did-this-get-made/id409287913",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "invalid_url"
    assert "409287913" in body["error"]


async def test_test_source_route_ordinary_url_unaffected(app_client):
    # Regression guard: a normal feed URL (no Apple Podcasts pattern)
    # must not go anywhere near the resolver — resolved_url stays
    # null, and behavior is whatever it was before this feature.
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "rss", "url": "not-a-valid-url"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["resolved_url"] is None


# --- route wiring: POST /api/sources ------------------------------------------


async def test_create_source_route_apple_podcasts_url_returns_422(app_client):
    resp = await app_client.post(
        "/api/sources",
        json={
            "name": "hdtgm_probe",
            "type": "podcast",
            "category": "podcast",
            "url": "https://podcasts.apple.com/us/podcast/how-did-this-get-made/id409287913",
        },
    )
    assert resp.status_code == 422
    assert "409287913" in resp.json()["detail"]
