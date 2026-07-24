"""Tests for app.feed_autodiscovery (feed + sitemap discovery) and the
POST /api/sources/auto route.

Same convention as test_article_summary.py / test_feed_discovery.py:
the real "find a working feed/sitemap on a live site" happy path
needs real network access and isn't something a unit test should
depend on. Covered directly instead: SSRF rejection (deterministic,
no network), and the route's name-generation / de-duplication logic
and its two "nothing found" / "no provider" style outcomes.
"""

from __future__ import annotations

import pytest

from app.feed_autodiscovery import discover_feed_url, discover_sitemap_urls
from app.routes import sources as sources_route
from app.routes.sources import _free_source_name, _slugify_hostname
from factories import make_source


# --- app.feed_autodiscovery: SSRF guard (no network needed) ---------------


@pytest.mark.asyncio
async def test_discover_feed_url_rejects_unsafe_url():
    assert await discover_feed_url("http://127.0.0.1:6379/") is None


@pytest.mark.asyncio
async def test_discover_feed_url_rejects_non_http_scheme():
    assert await discover_feed_url("ftp://example.com/") is None


@pytest.mark.asyncio
async def test_discover_sitemap_urls_rejects_unsafe_url():
    assert await discover_sitemap_urls("http://169.254.169.254/latest/meta-data/") == []


@pytest.mark.asyncio
async def test_discover_feed_url_unreachable_host_returns_none():
    # A syntactically-safe URL (passes check_url_safe) but genuinely
    # unreachable in this sandbox — exercises the "fetch_rss raised,
    # find_feed_urls raised too" path without needing real internet
    # access, the same way test_article_summary.py's loopback tests
    # force a deterministic failure.
    assert await discover_feed_url("http://127.0.0.1:1/nope") is None


# --- app.routes.sources._slugify_hostname ----------------------------------


def test_slugify_hostname_strips_www_and_punctuation():
    assert _slugify_hostname("https://www.example.com/some/path") == "example_com"


def test_slugify_hostname_bare_host():
    assert _slugify_hostname("https://blog.example.co.uk/") == "blog_example_co_uk"


def test_slugify_hostname_no_host_falls_back():
    assert _slugify_hostname("not-a-url") == "site"


# --- app.routes.sources._free_source_name ----------------------------------


@pytest.mark.asyncio
async def test_free_source_name_returns_base_when_unused(db_session):
    name = await _free_source_name(db_session, "example_com")
    assert name == "example_com"


@pytest.mark.asyncio
async def test_free_source_name_appends_suffix_on_collision(db_session):
    await make_source(db_session, "example_com")
    name = await _free_source_name(db_session, "example_com")
    assert name == "example_com_2"


@pytest.mark.asyncio
async def test_free_source_name_skips_multiple_taken_suffixes(db_session):
    await make_source(db_session, "example_com")
    await make_source(db_session, "example_com_2")
    name = await _free_source_name(db_session, "example_com")
    assert name == "example_com_3"


# --- POST /api/sources/auto -------------------------------------------------


@pytest.mark.asyncio
async def test_source_auto_route_rejects_invalid_url(app_client):
    resp = await app_client.post("/api/sources/auto", json={"url": "not-a-url"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_source_auto_route_rejects_unsafe_url(app_client):
    # _validate_url (the same SSRF guard every other source-creation
    # route uses) rejects a loopback address before discovery is ever
    # attempted — the row never lands in the DB, and the response is
    # a 422 rather than a 200 "nothing found".
    resp = await app_client.post(
        "/api/sources/auto", json={"url": "http://127.0.0.1:1/nope", "category": "news"}
    )
    assert resp.status_code == 422


# The success paths below monkeypatch the two discovery functions
# (imported into app.routes.sources at module scope) so the route's
# OWN composition logic — which branch wins, name generation +
# de-duplication, the scheduler.add_source call, the response shape —
# gets real coverage without depending on network access or a live
# target site, the same reasoning test_generic_scrape.py's plugin
# tests use.


# check_url_safe does a real DNS lookup as part of validating the
# user-supplied URL (see _validate_url), which happens BEFORE these
# tests' monkeypatched discovery functions are ever reached — so the
# outer URL has to be something that genuinely resolves. example.com
# is the one hostname this whole test suite can rely on resolving
# (see test_feed_discovery.py's equivalent reasoning); what the
# (mocked) discovery step "finds" downstream is unvalidated by this
# route, so it can be any fake URL.
_RESOLVABLE_URL = "https://example.com/"


@pytest.mark.asyncio
async def test_source_auto_route_creates_rss_source_when_feed_found(app_client, db_session, monkeypatch):
    async def fake_discover_feed_url(url):
        return "https://blog.example.com/feed.xml"

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "tech"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["kind"] == "rss"
    assert body["source"]["type"] == "rss"
    assert body["source"]["url"] == "https://blog.example.com/feed.xml"
    # Slugified from the DISCOVERED feed's own host, not the
    # original page URL the user pasted in — that's the host whose
    # feed this source is actually subscribed to.
    assert body["source"]["name"] == "blog_example_com"


@pytest.mark.asyncio
async def test_source_auto_route_creates_generic_scrape_when_only_sitemap_found(
    app_client, db_session, monkeypatch,
):
    async def fake_discover_feed_url(url):
        return None

    async def fake_discover_sitemap_urls(url, limit=1):
        return ["https://example.com/article-1"]

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)
    monkeypatch.setattr(sources_route, "discover_sitemap_urls", fake_discover_sitemap_urls)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "news"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["kind"] == "generic_scrape"
    assert body["source"]["type"] == "generic_scrape"
    assert body["source"]["url"] == _RESOLVABLE_URL


@pytest.mark.asyncio
async def test_source_auto_route_reports_not_found_when_neither_path_works(
    app_client, db_session, monkeypatch,
):
    async def fake_discover_feed_url(url):
        return None

    async def fake_discover_sitemap_urls(url, limit=1):
        return []

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)
    monkeypatch.setattr(sources_route, "discover_sitemap_urls", fake_discover_sitemap_urls)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "news"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"found": False, "kind": None, "source": None}


@pytest.mark.asyncio
async def test_source_auto_route_dedupes_name_on_collision(app_client, db_session, monkeypatch):
    await make_source(db_session, "blog_example_com")

    async def fake_discover_feed_url(url):
        return "https://blog.example.com/feed.xml"

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "tech"}
    )
    assert resp.status_code == 200
    assert resp.json()["source"]["name"] == "blog_example_com_2"
