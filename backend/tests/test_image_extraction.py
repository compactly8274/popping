"""Tests for app.sources.rss._pick_image_url's inline-<img> fallback,
specifically the content-body fallback added so feeds that only embed
a photo in the full content (not the short summary teaser) still get
a thumbnail — RFD's deal threads are the motivating case, but this is
generic to any feed shaped that way.

Pure functions fed plain dicts standing in for feedparser entries —
same approach the RFD engagement tests already use (a FeedParserDict
supports ``.get()`` like a normal dict, so a plain dict is a faithful
enough stand-in).
"""

from __future__ import annotations

from app.sources.rss import _pick_image_url


def test_pick_image_url_finds_img_in_summary():
    entry = {
        "link": "https://example.com/a",
        "summary": '<p>deal details <img src="https://example.com/deal.jpg"></p>',
    }
    assert _pick_image_url(entry) == "https://example.com/deal.jpg"


def test_pick_image_url_falls_back_to_content_when_summary_has_no_image():
    # Summary is a plain-text teaser with no <img> at all; the photo
    # only lives in the full content body — the RFD-shaped case.
    entry = {
        "link": "https://example.com/a",
        "summary": "42 votes, 8 replies, 1.2k views",
        "content": [{"value": '<p>full deal writeup <img src="https://example.com/full.jpg"></p>', "type": "text/html"}],
    }
    assert _pick_image_url(entry) == "https://example.com/full.jpg"


def test_pick_image_url_prefers_summary_image_over_content_image():
    entry = {
        "link": "https://example.com/a",
        "summary": '<img src="https://example.com/summary.jpg">',
        "content": [{"value": '<img src="https://example.com/content.jpg">', "type": "text/html"}],
    }
    assert _pick_image_url(entry) == "https://example.com/summary.jpg"


def test_pick_image_url_content_as_plain_string():
    # Some feedparser entries expose content as a bare string rather
    # than the Atom-style list-of-dicts.
    entry = {
        "link": "https://example.com/a",
        "summary": "",
        "content": '<img src="https://example.com/plain.jpg">',
    }
    assert _pick_image_url(entry) == "https://example.com/plain.jpg"


def test_pick_image_url_resolves_relative_content_image_against_link():
    entry = {
        "link": "https://forums.redflagdeals.com/thread/123",
        "summary": "",
        "content": [{"value": '<img src="/attachments/deal.jpg">', "type": "text/html"}],
    }
    assert _pick_image_url(entry) == "https://forums.redflagdeals.com/attachments/deal.jpg"


def test_pick_image_url_no_image_anywhere_returns_none():
    entry = {"link": "https://example.com/a", "summary": "no photo here", "content": []}
    assert _pick_image_url(entry) is None


def test_pick_image_url_media_thumbnail_still_wins_over_content():
    # Sanity: the new content fallback is step 5 (last resort) —
    # media:thumbnail (step 1) must still take priority.
    entry = {
        "link": "https://example.com/a",
        "media_thumbnail": [{"url": "https://example.com/media-thumb.jpg"}],
        "content": [{"value": '<img src="https://example.com/content.jpg">', "type": "text/html"}],
    }
    assert _pick_image_url(entry) == "https://example.com/media-thumb.jpg"
