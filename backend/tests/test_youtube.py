"""Tests for app.youtube (resolving any shape of YouTube link to its
channel's video RSS feed URL) and app.sources.youtube (the row-driven
plugin dispatch).

Same dual-environment-determinism concern as test_apple_podcasts.py:
a test that only passes with or without real outbound network access
is a flaky test wearing a deterministic test's clothes. The direct
``/channel/UC...`` resolution path needs no network call at all (the
id is already in the URL) so those tests are unconditionally
deterministic. For the scrape-the-page path, a handle that can't
plausibly exist is used — the request either fails outright (network-
blocked sandbox) or succeeds with a page that has no ``channelId`` to
find (network-reachable CI, YouTube's "this page isn't available"
response) — both raise ``YouTubeResolutionError``.
"""

from __future__ import annotations

import pytest

from app.models import Source
from app.scheduler import _plugin_for
from app.sources.youtube import DynamicYouTubePlugin
from app.youtube import (
    YouTubeResolutionError,
    is_youtube_url,
    resolve_channel_feed_url,
)

# Well-formed (UC + 22 chars) but not a real channel — direct
# /channel/ URLs resolve without any network call, so this is
# deterministic regardless of environment.
_FAKE_CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
_DIRECT_CHANNEL_URL = f"https://www.youtube.com/channel/{_FAKE_CHANNEL_ID}"

# A handle that can't plausibly exist — see module docstring for why
# this is deterministic in both a network-reachable and a network-
# blocked environment.
_NONEXISTENT_HANDLE_URL = "https://www.youtube.com/@this-channel-definitely-does-not-exist-abc123xyz789"


# --- is_youtube_url -------------------------------------------------------


def test_is_youtube_url_matches_www_host():
    assert is_youtube_url("https://www.youtube.com/channel/UCxxxx") is True


def test_is_youtube_url_matches_short_link_host():
    assert is_youtube_url("https://youtu.be/dQw4w9WgXcQ") is True


def test_is_youtube_url_matches_bare_host_no_www():
    assert is_youtube_url("https://youtube.com/@handle") is True


def test_is_youtube_url_rejects_ordinary_host():
    assert is_youtube_url("https://example.com/feed.xml") is False


def test_is_youtube_url_rejects_spoofed_subdomain():
    # "youtube.com" as a subdomain of an attacker-controlled domain
    # must not match — only an actual youtube.com/youtu.be host does.
    assert is_youtube_url("https://youtube.com.evil.example/channel/UCxxxx") is False


# --- resolve_channel_feed_url: direct /channel/ path (no network) --------


async def test_resolve_channel_feed_url_direct_channel_id_no_network_needed():
    result = await resolve_channel_feed_url(_DIRECT_CHANNEL_URL)
    assert result == f"https://www.youtube.com/feeds/videos.xml?channel_id={_FAKE_CHANNEL_ID}"


async def test_resolve_channel_feed_url_passthrough_for_non_matching_url():
    url = "https://example.com/feed.xml"
    assert await resolve_channel_feed_url(url) == url


async def test_resolve_channel_feed_url_passthrough_for_reddit_reference():
    # Sanity: resolve_channel_feed_url is called unconditionally on
    # every source URL by the route layer, including reddit ones —
    # must be a true no-op for shapes that were never going to match.
    url = "r/python"
    assert await resolve_channel_feed_url(url) == url


# --- resolve_channel_feed_url: scrape path (network-dependent shape) -----


async def test_resolve_channel_feed_url_raises_for_nonexistent_handle():
    # Deterministic in both a network-reachable and a network-blocked
    # environment — see module docstring.
    with pytest.raises(YouTubeResolutionError):
        await resolve_channel_feed_url(_NONEXISTENT_HANDLE_URL)


# --- route wiring: POST /api/sources/test ------------------------------------


async def test_test_source_route_youtube_nonexistent_handle_surfaces_resolution_error(app_client):
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "youtube_channel", "url": _NONEXISTENT_HANDLE_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "invalid_url"


async def test_test_source_route_direct_channel_url_sets_resolved_url(app_client):
    # resolved_url reflects the direct-channel-id resolution
    # deterministically regardless of whether the (fake) feed URL is
    # actually reachable — the fetch outcome (ok true/false) isn't
    # asserted here, only that resolution happened before the fetch.
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "youtube_channel", "url": _DIRECT_CHANNEL_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_url"] == f"https://www.youtube.com/feeds/videos.xml?channel_id={_FAKE_CHANNEL_ID}"


async def test_test_source_route_ordinary_url_unaffected_by_youtube_resolver(app_client):
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "rss", "url": "not-a-valid-url"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["resolved_url"] is None


# --- route wiring: POST /api/sources ------------------------------------------


async def test_create_source_route_youtube_nonexistent_handle_returns_422(app_client):
    resp = await app_client.post(
        "/api/sources",
        json={
            "name": "youtube_probe",
            "type": "youtube_channel",
            "category": "video",
            "url": _NONEXISTENT_HANDLE_URL,
        },
    )
    assert resp.status_code == 422


# --- scheduler dispatch ---------------------------------------------------


def test_plugin_for_dispatches_youtube_channel_type_to_dynamic_youtube_plugin():
    row = Source(
        id=1,
        name="some_channel",
        type="youtube_channel",
        category="video",
        url=f"https://www.youtube.com/feeds/videos.xml?channel_id={_FAKE_CHANNEL_ID}",
        refresh_interval_seconds=21600,
    )
    plugin = _plugin_for(row)
    assert isinstance(plugin, DynamicYouTubePlugin)
    assert plugin.url == row.url
    assert plugin.source_id == row.id
