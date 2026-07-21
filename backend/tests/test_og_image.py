"""Tests for the og:image fallback thumbnail source
(``app.assets._extract_og_image_url`` / ``fetch_og_image_fallback``).

Purpose: a significant share of entries land with no feed-supplied
image — either the source type has none at all (HN, GitHub Releases,
CISA/NVD advisories) or the RSS feed just doesn't ship
media:thumbnail. ``fetch_og_image_fallback`` probes the article's own
page for an og:image / twitter:image meta tag as a best-effort
substitute, reusing ``fetch_thumbnail`` for the actual download.

The parsing logic (``_extract_og_image_url``) is pure — no network —
so it's tested directly against hand-built HTML fixtures rather than
through the network-dependent ``_pick_og_image_url`` wrapper. The
SSRF guard on the probe itself is exercised via a real loopback-URL
call, matching the deterministic-regardless-of-network convention
used throughout this codebase's asset/URL-safety tests.
"""

from __future__ import annotations

from app.assets import _extract_og_image_url, _pick_og_image_url, fetch_og_image_fallback


# --- _extract_og_image_url (pure parsing) ---------------------------------


def test_extract_og_image_finds_standard_tag():
    html = '<html><head><meta property="og:image" content="https://example.com/hero.jpg"></head></html>'
    assert _extract_og_image_url(html, "https://example.com/article") == "https://example.com/hero.jpg"


def test_extract_og_image_attribute_order_independent():
    # content before property — some CMSes emit it this way.
    html = '<meta content="https://example.com/hero.jpg" property="og:image">'
    assert _extract_og_image_url(html, "https://example.com/article") == "https://example.com/hero.jpg"


def test_extract_og_image_prefers_og_over_twitter():
    html = (
        '<meta name="twitter:image" content="https://example.com/twitter.jpg">'
        '<meta property="og:image" content="https://example.com/og.jpg">'
    )
    assert _extract_og_image_url(html, "https://example.com/article") == "https://example.com/og.jpg"


def test_extract_og_image_falls_back_to_twitter_image():
    html = '<meta name="twitter:image" content="https://example.com/twitter.jpg">'
    assert _extract_og_image_url(html, "https://example.com/article") == "https://example.com/twitter.jpg"


def test_extract_og_image_resolves_relative_url():
    html = '<meta property="og:image" content="/static/hero.jpg">'
    assert _extract_og_image_url(html, "https://example.com/article") == "https://example.com/static/hero.jpg"


def test_extract_og_image_ignores_unrelated_meta_tags():
    html = (
        '<meta charset="utf-8">'
        '<meta name="description" content="an article about things">'
        '<meta property="og:title" content="An Article">'
    )
    assert _extract_og_image_url(html, "https://example.com/article") is None


def test_extract_og_image_skips_empty_content():
    html = '<meta property="og:image" content="">'
    assert _extract_og_image_url(html, "https://example.com/article") is None


def test_extract_og_image_no_meta_tags_returns_none():
    assert _extract_og_image_url("<html><body>hello</body></html>", "https://example.com/article") is None


# --- _pick_og_image_url / fetch_og_image_fallback: SSRF guard ------------


async def test_pick_og_image_url_rejects_loopback_url():
    assert await _pick_og_image_url("http://127.0.0.1:9999/article") is None


async def test_fetch_og_image_fallback_rejects_loopback_url():
    # No network attempted at all when the probe URL fails the SSRF
    # check — deterministic regardless of environment network access.
    assert await fetch_og_image_fallback("http://127.0.0.1:9999/article", entry_id=1) is None
