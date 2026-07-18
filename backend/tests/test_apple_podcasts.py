"""Tests for app.apple_podcasts: resolving an Apple Podcasts catalog
link (podcasts.apple.com/.../id<N>) to its actual RSS feed URL.

Network-dependent tests here have to work whether or not the test
environment actually has outbound access to itunes.apple.com — CI
runners do, this repo's sandboxed dev environments often don't, and a
test that only passes under one of those two conditions is a flaky
test wearing a deterministic test's clothes (this bit us once
already: an earlier version of this file assumed the lookup call
always fails, which held in a network-restricted sandbox but broke
the moment CI's real network access let the same call succeed).

The fix: for the "the lookup can't be resolved" tests, use an id
number far outside any real Apple Podcasts collection id
(``_NONEXISTENT_ID``). That's deterministic in both directions —
network reachable: Apple's API returns an empty result set for an id
that doesn't exist, feeding the same "not found" branch;
network unreachable: the request itself fails, feeding the same
error-wrapping branch. Either way, PodcastResolutionError comes out.

The genuine "lookup succeeds for a real id" happy path still isn't
covered by an assertion on the actual feedUrl value (that needs a
real, currently-existing show and a network connection this sandbox
doesn't reliably have) — same tradeoff made throughout this codebase.
The JSON-parsing logic itself was manually verified separately
against the real response shape Apple's API returns.
"""

from __future__ import annotations

import pytest

from app.apple_podcasts import PodcastResolutionError, apple_podcasts_id, resolve_feed_url

# Deliberately far outside any real Apple Podcasts collection id
# (those top out well under this many digits) — see module docstring
# for why this makes the "can't resolve" tests deterministic
# regardless of whether the environment has network access.
_NONEXISTENT_ID = "999999999999"
_NONEXISTENT_URL = f"https://podcasts.apple.com/us/podcast/does-not-exist/id{_NONEXISTENT_ID}"


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


async def test_resolve_feed_url_raises_podcast_resolution_error_for_nonexistent_id():
    # Deterministic in both a network-reachable and a network-blocked
    # environment — see module docstring.
    with pytest.raises(PodcastResolutionError):
        await resolve_feed_url(_NONEXISTENT_URL)


# --- route wiring: POST /api/sources/test ------------------------------------


async def test_test_source_route_apple_podcasts_url_surfaces_resolution_error(app_client):
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "podcast", "url": _NONEXISTENT_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "invalid_url"
    assert _NONEXISTENT_ID in body["error"]


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
            "name": "apple_podcast_probe",
            "type": "podcast",
            "category": "podcast",
            "url": _NONEXISTENT_URL,
        },
    )
    assert resp.status_code == 422
    assert _NONEXISTENT_ID in resp.json()["detail"]
