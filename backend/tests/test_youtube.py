"""Tests for app.youtube (resolving any shape of YouTube link to a
YouTube feed URL) and app.sources.youtube (the row-driven plugin
dispatch).

No test here touches the network. An autouse fixture swaps
``app.youtube._TRANSPORT`` for an ``httpx.MockTransport`` whose default
handler fails the test, and records every request, so a test can
assert both the outcome and how many YouTube requests it made. The
real ``check_url_safe`` and ``ssrf_event_hook`` stay in the path;
``fake_dns`` (conftest.py) answers their DNS lookups.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

import app.youtube as youtube
from app.models import Source
from app.scheduler import _plugin_for
from app.sources.youtube import DynamicYouTubePlugin
from app.youtube import (
    YouTubeBlockedError,
    YouTubeResolutionError,
    channel_id_from_html,
    is_blocked_page,
    is_youtube_url,
    normalize_youtube_input,
    resolve_channel_feed_url,
    youtube_source_slug,
)

# Well-formed (UC + 22 chars) but not a real channel — direct
# /channel/ URLs resolve without any network call.
_FAKE_CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
_OTHER_CHANNEL_ID = "UCzyxwvutsrqponmlkjihgfe"
_DIRECT_CHANNEL_URL = f"https://www.youtube.com/channel/{_FAKE_CHANNEL_ID}"
_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={_FAKE_CHANNEL_ID}"

# A handle that can't plausibly exist. Every test that looks it up
# answers with a mocked 404.
_NONEXISTENT_HANDLE_URL = "https://www.youtube.com/@this-channel-definitely-does-not-exist-abc123xyz789"


class _Net:
    """The mock network: records requests; fails the test on any
    request a test didn't expect."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is None:
            pytest.fail(f"unexpected network call {request.url}")
        return self.handler(request)


@pytest.fixture(autouse=True)
def net(monkeypatch):
    n = _Net()
    monkeypatch.setattr(youtube, "_TRANSPORT", httpx.MockTransport(n))
    return n


def _page(channel_id: str = _FAKE_CHANNEL_ID) -> str:
    return f'<html><head><link rel="canonical" href="https://www.youtube.com/channel/{channel_id}"></head></html>'


def _not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, text="<html>404</html>")

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


# --- resolve_channel_feed_url: SSRF hook wiring (found in a repo-wide -----
# --- audit — the scrape-the-page fetch had entry-time and final-URL  -----
# --- check_url_safe calls but no per-hop ssrf_event_hook, so an       -----
# --- intermediate redirect hop to a private/loopback address would   -----
# --- have its connection actually opened before the final check      -----
# --- discarded the response.                                          -----


def test_resolve_channel_feed_url_registers_ssrf_event_hook():
    src = Path(__file__).resolve().parents[1] / "app" / "youtube.py"
    text = src.read_text()
    assert "ssrf_event_hook" in text, (
        "youtube.py must import ssrf_event_hook from app.url_safety"
    )
    assert 'event_hooks={"request": [ssrf_event_hook]}' in text, (
        "resolve_channel_feed_url's httpx.AsyncClient must register "
        "ssrf_event_hook as a per-request hook — without it, a redirect "
        "hop to a private/loopback address is fetched before the "
        "final-URL check ever runs."
    )


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

# --- resolve_channel_feed_url: a looked-up handle that doesn't exist ------


async def test_resolve_channel_feed_url_raises_for_nonexistent_handle(net, fake_dns):
    net.handler = _not_found
    with pytest.raises(YouTubeResolutionError, match="doesn't exist"):
        await resolve_channel_feed_url(_NONEXISTENT_HANDLE_URL)
    assert len(net.requests) == 1


# --- route wiring: POST /api/sources/test ------------------------------------


async def test_test_source_route_youtube_nonexistent_handle_surfaces_resolution_error(app_client, net, fake_dns):
    net.handler = _not_found
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "youtube_channel", "url": _NONEXISTENT_HANDLE_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "invalid_url"
    assert "doesn't exist" in body["error"]

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


async def test_create_source_route_youtube_nonexistent_handle_returns_422(app_client, net, fake_dns):
    net.handler = _not_found
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
    assert "doesn't exist" in resp.json()["detail"]


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


# --- 1. shapes that need no network ------------------------------------------


@pytest.mark.parametrize(
    "pasted, expected",
    [
        (f"  {_FAKE_CHANNEL_ID}  ", _FEED),
        (f"https://www.youtube.com/feeds/videos.xml?channel_id={_FAKE_CHANNEL_ID}&foo=1", _FEED),
        (f"https://www.youtube.com/CHANNEL/{_FAKE_CHANNEL_ID}", _FEED),
        (f"https://studio.youtube.com/channel/{_FAKE_CHANNEL_ID}/videos?si=abc", _FEED),
        (f"https://m.youtube.com/channel/{_FAKE_CHANNEL_ID}", _FEED),
        (f"youtube.com/channel/{_FAKE_CHANNEL_ID}", _FEED),
        ("https://www.youtube.com/playlist?list=UUabcdefghijklmnopqrstuv", _FEED),
        (
            "https://www.youtube.com/playlist?list=PLabcdefghij123456",
            "https://www.youtube.com/feeds/videos.xml?playlist_id=PLabcdefghij123456",
        ),
        (
            "https://www.youtube.com/feeds/videos.xml?user=SomeName",
            "https://www.youtube.com/feeds/videos.xml?user=SomeName",
        ),
    ],
)
async def test_shapes_with_an_id_resolve_without_network(net, pasted, expected):
    assert await resolve_channel_feed_url(pasted) == expected
    assert net.requests == []


@pytest.mark.parametrize(
    "pasted, message",
    [
        ("https://www.youtube.com/playlist?list=WL", "Private or automatic playlists"),
        ("https://www.youtube.com/playlist?list=LL", "Private or automatic playlists"),
        ("https://www.youtube.com/playlist?list=RDabcdef", "Private or automatic playlists"),
        ("https://www.youtube.com/", "That isn't a channel link"),
        ("https://www.youtube.com/results?search_query=x", "That isn't a channel link"),
        ("https://www.youtube.com/feed/subscriptions", "That isn't a channel link"),
        (f"https://www.youtube.com/channel/{_FAKE_CHANNEL_ID}abc", "That isn't a channel link"),
    ],
)
async def test_shapes_that_are_not_a_channel_fail_without_network(net, pasted, message):
    with pytest.raises(YouTubeResolutionError, match=message):
        await resolve_channel_feed_url(pasted)
    assert net.requests == []


@pytest.mark.parametrize(
    "pasted",
    ["https://youtube.com.evil.example/channel/UCxxxx", "https://youtube.com@127.0.0.1/", "r/python"],
)
async def test_non_youtube_input_passes_through(net, pasted):
    assert await resolve_channel_feed_url(pasted) == pasted
    assert net.requests == []


def test_bare_handle_counts_only_when_allowed():
    assert normalize_youtube_input("@somebody") == "@somebody"
    assert normalize_youtube_input("@somebody", allow_bare_handle=True) == "https://www.youtube.com/@somebody"


# --- 2. page markers ------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        f'<link rel="alternate" type="application/rss+xml" title="RSS" href="https://www.youtube.com/feeds/videos.xml?channel_id={_FAKE_CHANNEL_ID}">',
        f'<link href="https://www.youtube.com/channel/{_FAKE_CHANNEL_ID}" rel="canonical">',
        f'<meta content="https://www.youtube.com/channel/{_FAKE_CHANNEL_ID}" property="og:url">',
        f'<meta itemprop="identifier" content="{_FAKE_CHANNEL_ID}">',
        f'"externalChannelId":"{_FAKE_CHANNEL_ID}"',
        f'"externalId":"{_FAKE_CHANNEL_ID}"',
        f'"channelId":"{_FAKE_CHANNEL_ID}"',
    ],
)
def test_each_marker_finds_the_channel(html):
    assert channel_id_from_html(html) == _FAKE_CHANNEL_ID


def test_canonical_link_beats_a_generic_channel_id():
    html = f'"channelId":"{_OTHER_CHANNEL_ID}" <link rel="canonical" href="https://www.youtube.com/channel/{_FAKE_CHANNEL_ID}">'
    assert channel_id_from_html(html) == _FAKE_CHANNEL_ID


def test_watch_page_uses_the_video_details_window():
    html = (
        f'{{"recommended":{{"channelId":"{_OTHER_CHANNEL_ID}"}}}}'
        '"videoDetails":{"videoId":"abcdefghijk","title":"A {weird} title }",'
        f'"lengthSeconds":"10","channelId":"{_FAKE_CHANNEL_ID}"}}'
    )
    assert channel_id_from_html(html) == _FAKE_CHANNEL_ID


def test_browse_id_alone_is_not_a_marker():
    assert channel_id_from_html(f'"browseId":"{_FAKE_CHANNEL_ID}"') is None


# --- 3. blocked pages -------------------------------------------------------------


@pytest.mark.parametrize(
    "final_url, status, html",
    [
        ("https://consent.youtube.com/m?continue=x", 200, ""),
        ("https://consent.google.com/ml", 200, ""),
        ("https://accounts.google.com/ServiceLogin", 200, ""),
        ("https://www.google.com/sorry/index?continue=x", 200, ""),
        ("https://www.youtube.com/@x", 429, ""),
        ("https://www.youtube.com/@x", 200, '<form action="https://consent.youtube.com/save">'),
        ("https://www.youtube.com/@x", 200, "Before you continue to YouTube"),
        ("https://www.youtube.com/@x", 200, "Sign in to confirm you're not a bot"),
        ("https://www.youtube.com/@x", 200, "Sign in to confirm you’re not a bot"),
        ("https://www.youtube.com/@x", 200, "Sign in to confirm you\\u0027re not a bot"),
    ],
)
def test_blocked_pages_are_recognised(final_url, status, html):
    assert is_blocked_page(final_url, status, html) is True


@pytest.mark.parametrize("html", ["<html>a normal page</html>", "Sign in to confirm your age"])
def test_normal_and_age_gated_pages_are_not_blocked(html):
    assert is_blocked_page("https://www.youtube.com/watch?v=abcdefghijk", 200, html) is False


async def test_age_gated_watch_page_with_video_details_resolves(net, fake_dns):
    net.handler = lambda r: httpx.Response(
        200,
        text=f'Sign in to confirm your age "videoDetails":{{"videoId":"abcdefghijk","channelId":"{_FAKE_CHANNEL_ID}"}}',
    )
    assert await resolve_channel_feed_url("https://youtu.be/abcdefghijk") == _FEED


# --- 4. what a lookup sends ---------------------------------------------------------


async def test_lookup_sends_browser_headers_and_consent_cookies_on_every_hop(net, fake_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            return httpx.Response(301, headers={"Location": "https://m.youtube.com/@somebody"})
        return httpx.Response(200, text=_page())

    net.handler = handler
    assert await resolve_channel_feed_url("https://www.youtube.com/@somebody?si=abc") == _FEED
    assert [r.url.host for r in net.requests] == ["www.youtube.com", "m.youtube.com"]
    first = net.requests[0]
    assert "si" not in first.url.params
    assert "Safari" in first.headers["user-agent"]
    assert first.headers.get("accept-language")
    for request in net.requests:
        assert "SOCS=CAI" in request.headers.get("cookie", "")


# --- 5. consent redirect ------------------------------------------------------------


async def test_consent_redirect_is_blocked_with_the_channel_id_hint(net, fake_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            return httpx.Response(302, headers={"Location": "https://consent.youtube.com/m?continue=x"})
        return httpx.Response(200, text="Before you continue to YouTube")

    net.handler = handler
    with pytest.raises(YouTubeBlockedError) as info:
        await resolve_channel_feed_url("https://www.youtube.com/@somebody")
    assert "channel ID" in str(info.value)
    assert "http" not in str(info.value)


# --- 6. /user/ fallback -------------------------------------------------------------


def _consent(request: httpx.Request) -> httpx.Response:
    if request.url.host == "www.youtube.com":
        return httpx.Response(302, headers={"Location": "https://consent.youtube.com/m"})
    return httpx.Response(200, text="Before you continue to YouTube")


async def test_user_link_falls_back_to_the_user_feed_when_it_has_items(net, fake_dns, monkeypatch):
    net.handler = _consent

    async def fake_fetch(url, headers=None):
        return [{"title": "x"}]

    monkeypatch.setattr("app.sources.rss.fetch_rss", fake_fetch)
    result = await resolve_channel_feed_url("https://www.youtube.com/user/SomeName")
    assert result == "https://www.youtube.com/feeds/videos.xml?user=SomeName"


async def test_user_link_stays_blocked_when_the_user_feed_is_empty(net, fake_dns, monkeypatch):
    net.handler = _consent

    async def fake_fetch(url, headers=None):
        return []

    monkeypatch.setattr("app.sources.rss.fetch_rss", fake_fetch)
    with pytest.raises(YouTubeBlockedError):
        await resolve_channel_feed_url("https://www.youtube.com/user/SomeName")


async def test_user_feed_error_is_blocked_not_a_crash(net, fake_dns, monkeypatch, caplog):
    net.handler = _consent

    async def fake_fetch(url, headers=None):
        raise ValueError("not a feed")

    monkeypatch.setattr("app.sources.rss.fetch_rss", fake_fetch)
    with caplog.at_level(logging.INFO, logger="app.youtube"):
        with pytest.raises(YouTubeBlockedError):
            await resolve_channel_feed_url("https://www.youtube.com/user/SomeName")
    assert "fallback:user_failed" in caplog.text


# --- 7-10. status codes, guarded redirects, size cap --------------------------------


async def test_404_says_the_channel_does_not_exist(net, fake_dns):
    net.handler = _not_found
    with pytest.raises(YouTubeResolutionError, match="doesn't exist"):
        await resolve_channel_feed_url("https://www.youtube.com/@somebody")


@pytest.mark.parametrize("target", ["http://127.0.0.1/admin", "http://evil.internal/"])
async def test_redirect_to_a_private_address_is_refused_before_connecting(net, fake_dns, target):
    net.handler = lambda r: httpx.Response(302, headers={"Location": target})
    with pytest.raises(YouTubeResolutionError) as info:
        await resolve_channel_feed_url("https://www.youtube.com/@somebody")
    message = str(info.value)
    assert message == youtube.MSG_UNREACHABLE
    assert "127.0.0.1" not in message and "10.0.0.5" not in message and "denied" not in message
    assert [r.url.host for r in net.requests] == ["www.youtube.com"]


async def test_redirect_to_another_site_is_refused_without_reading_markers(net, fake_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            return httpx.Response(302, headers={"Location": "https://example.org/"})
        return httpx.Response(200, text=_page())

    net.handler = handler
    with pytest.raises(YouTubeResolutionError) as info:
        await resolve_channel_feed_url("https://www.youtube.com/@somebody")
    assert str(info.value) == youtube.MSG_UNEXPECTED_HOST


async def test_body_is_capped_before_markers_are_read(net, fake_dns):
    body = "x" * 3_000_100 + f'"channelId":"{_FAKE_CHANNEL_ID}"' + "y" * 2_000_000
    net.handler = lambda r: httpx.Response(200, text=body)
    with pytest.raises(YouTubeResolutionError) as info:
        await resolve_channel_feed_url("https://www.youtube.com/@somebody")
    assert str(info.value) == youtube.MSG_NO_MARKER


# --- 11. non-Latin handles ----------------------------------------------------------


async def test_non_latin_handle_is_percent_encoded_and_named_by_channel(net, fake_dns):
    net.handler = lambda r: httpx.Response(200, text=_page())
    feed = await resolve_channel_feed_url("@ゆっくり実況", allow_bare_handle=True)
    assert feed == _FEED
    assert b"%E3%82%86" in net.requests[0].url.raw_path
    assert youtube_source_slug("@ゆっくり実況", feed) == "channel_qrstuv_yt"


def test_slugs():
    assert youtube_source_slug("https://www.youtube.com/@Some.Body", _FEED) == "some_body_yt"
    assert youtube_source_slug("https://www.youtube.com/c/Name", _FEED) == "name_yt"
    assert youtube_source_slug(
        "https://www.youtube.com/playlist?list=PLabcdefghij123456",
        "https://www.youtube.com/feeds/videos.xml?playlist_id=PLabcdefghij123456",
    ) == "playlist_123456_yt"
    assert youtube_source_slug("https://youtu.be/abcdefghijk", _FEED) == "channel_qrstuv_yt"
    dashed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnop-r_t-v"
    assert youtube_source_slug("https://youtu.be/abcdefghijk", dashed) == "channel__r_t_v_yt"


# --- 12. the egress proxy still applies ---------------------------------------------


def test_client_mounts_the_environment_proxy(monkeypatch):
    monkeypatch.setattr(youtube, "_TRANSPORT", None)
    monkeypatch.setenv("HTTPS_PROXY", "http://guard-proxy:3128")
    client = youtube._client()
    try:
        assert any(
            str(pattern.pattern).startswith("https") and transport is not None
            for pattern, transport in client._mounts.items()
        )
    finally:
        import asyncio

        asyncio.run(client.aclose())


# --- 13. logging ------------------------------------------------------------------


async def test_lookup_log_has_no_query_string(net, fake_dns, caplog):
    net.handler = lambda r: httpx.Response(200, text=_page())
    with caplog.at_level(logging.INFO, logger="app.youtube"):
        await resolve_channel_feed_url("https://youtu.be/abcdefghijk?si=secret")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("youtube: lookup")]
    assert lines and all("?" not in line and "si=" not in line for line in lines)
    assert "result=canonical" in lines[0]


# --- 15. routes --------------------------------------------------------------------


async def test_test_route_reports_a_consent_page(app_client, net, fake_dns):
    net.handler = _consent
    resp = await app_client.post(
        "/api/sources/test", json={"type": "youtube_channel", "url": "https://www.youtube.com/@somebody"}
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "invalid_url"
    assert "channel ID" in body["error"]


async def test_create_with_a_bare_id_then_a_duplicate(app_client, net, fake_dns):
    payload = {"name": "yt_probe", "type": "youtube_channel", "category": "video", "url": _FAKE_CHANNEL_ID}
    resp = await app_client.post("/api/sources", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == _FEED
    resp = await app_client.post("/api/sources", json={**payload, "name": "yt_probe_again"})
    assert resp.status_code == 409
    assert "already follow" in resp.json()["detail"]
    assert net.requests == []


async def test_auto_adds_a_youtube_channel_once(app_client, fake_dns, monkeypatch):
    async def fake_resolve(url, *, allow_bare_handle=False):
        return _FEED

    monkeypatch.setattr("app.routes.sources.resolve_channel_feed_url", fake_resolve)
    resp = await app_client.post("/api/sources/auto", json={"url": "https://www.youtube.com/@somehandle"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "youtube_channel"
    source = body["source"]
    assert source["type"] == "youtube_channel"
    assert source["name"] == "somehandle_yt"
    assert source["refresh_interval_seconds"] == 21600
    again = (await app_client.post("/api/sources/auto", json={"url": "https://www.youtube.com/@somehandle"})).json()
    assert again["source"]["id"] == source["id"]
    names = [s["name"] for s in (await app_client.get("/api/sources")).json()]
    assert "somehandle_yt_2" not in names


async def test_auto_names_a_playlist(app_client, net, fake_dns):
    resp = await app_client.post(
        "/api/sources/auto", json={"url": "https://www.youtube.com/playlist?list=PLabcdefghij123456"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"]["name"] == "playlist_123456_yt"
    assert net.requests == []


async def test_test_route_reports_invalid_headers(app_client, fake_dns):
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "rss", "url": "https://example.com/feed.xml", "custom_headers": {"Cookie": "x"}},
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "invalid_headers"


async def test_test_route_hides_why_a_private_address_is_refused(app_client):
    body = (await app_client.post("/api/sources/test", json={"type": "rss", "url": "http://127.0.0.1/feed"})).json()
    assert body["error_kind"] == "invalid_url"
    assert "127.0.0.1" not in body["error"] and "denied" not in body["error"]


async def test_test_route_ssrf_branch_uses_the_plain_message(app_client, fake_dns):
    fake_dns["www.reddit.com"] = "10.0.0.5"
    body = (await app_client.post("/api/sources/test", json={"type": "reddit", "url": "r/python"})).json()
    assert body["ok"] is False
    assert body["error"] == "That address isn't reachable from Popping."


@pytest.mark.parametrize(
    "name, kind",
    [("Bad Name", "invalid_url"), ("hn_top", "name_conflict")],
)
async def test_test_route_checks_the_name_before_resolving(app_client, net, name, kind):
    resp = await app_client.post(
        "/api/sources/test",
        json={"type": "youtube_channel", "url": "https://www.youtube.com/@somebody", "name": name},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == kind
    assert net.requests == []
