"""Unit tests for RFD engagement extraction (``app.sources.rfd``).

Regex-based, no network/DB — feeding hand-written HTML summary
fragments in the exact shape RFD's feed ships and checking the
extracted vote/reply/view counts end up on the right ``meta`` keys.
"""

from __future__ import annotations

from app.sources.rfd import _extract_from_haystack, _is_rfd_url, _rfd_normalize


def test_extract_all_three_signals():
    summary = "<p>42 votes, 8 replies, 1,234 views</p>"
    votes, replies, views = _extract_from_haystack(summary)
    assert (votes, replies, views) == (42, 8, 1234)


def test_extract_partial_signals():
    # Only a vote count in this one — replies/views should come back None,
    # not zero (zero would be indistinguishable from "0 replies").
    votes, replies, views = _extract_from_haystack("<p>7 thumbs up</p>")
    assert votes == 7
    assert replies is None
    assert views is None


def test_extract_no_signal_returns_all_none():
    assert _extract_from_haystack("<p>no numbers here</p>") == (None, None, None)
    assert _extract_from_haystack(None) == (None, None, None)


def test_is_rfd_url():
    assert _is_rfd_url("https://forums.redflagdeals.com/feed/forum/9")
    assert _is_rfd_url("https://www.redflagdeals.com/rss/deals.php?city=chilliwack")
    assert not _is_rfd_url("https://example.com/feed")
    assert not _is_rfd_url("")


def test_rfd_normalize_populates_canonical_and_legacy_meta_keys():
    raw = {
        "title": "50% off widgets",
        "url": "https://forums.redflagdeals.com/widgets-123",
        "published_at": "2026-01-01T00:00:00Z",
        "summary": "12 votes, 3 replies, 500 views",
    }
    normalized = _rfd_normalize("rfd_hot_deals", raw)
    meta = normalized["meta"]
    assert meta["engagement_score"] == 12
    assert meta["votes"] == 12
    assert meta["engagement_comments"] == 3
    assert meta["comments"] == 3
    assert meta["views"] == 500


def test_rfd_normalize_no_engagement_signal_omits_meta_keys():
    raw = {
        "title": "Some deal",
        "url": "https://forums.redflagdeals.com/some-deal-1",
        "published_at": "2026-01-01T00:00:00Z",
        "summary": "no counts in this one",
    }
    normalized = _rfd_normalize("rfd_hot_deals", raw)
    engagement_keys = {"engagement_score", "votes", "engagement_comments", "comments", "views"}
    assert engagement_keys.isdisjoint(normalized["meta"])
