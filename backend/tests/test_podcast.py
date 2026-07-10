"""Unit tests for podcast source support (``app.sources.rss``'s
enclosure/duration extraction and ``app.sources.podcast``).

Pure functions fed plain dicts standing in for feedparser entries —
``FeedParserDict`` supports ``.get()`` like a normal dict, so a plain
dict is a faithful enough stand-in for these extractors, same
approach the RFD engagement tests already use.
"""

from __future__ import annotations

from app.models import Source
from app.scheduler import _plugin_for
from app.sources.podcast import DynamicPodcastPlugin
from app.sources.rss import _parse_itunes_duration, _pick_audio_enclosure


def test_pick_audio_enclosure_finds_audio_type():
    entry = {
        "enclosures": [
            {"href": "https://example.com/ep1.mp3", "type": "audio/mpeg"},
        ]
    }
    assert _pick_audio_enclosure(entry) == "https://example.com/ep1.mp3"


def test_pick_audio_enclosure_ignores_non_audio_enclosures():
    entry = {
        "enclosures": [
            {"href": "https://example.com/cover.jpg", "type": "image/jpeg"},
        ]
    }
    assert _pick_audio_enclosure(entry) is None


def test_pick_audio_enclosure_no_enclosures_returns_none():
    assert _pick_audio_enclosure({}) is None
    assert _pick_audio_enclosure({"enclosures": []}) is None


def test_parse_itunes_duration_plain_seconds():
    assert _parse_itunes_duration({"itunes_duration": "3723"}) == 3723


def test_parse_itunes_duration_hms():
    assert _parse_itunes_duration({"itunes_duration": "1:02:03"}) == 3723


def test_parse_itunes_duration_ms():
    assert _parse_itunes_duration({"itunes_duration": "62:03"}) == 62 * 60 + 3


def test_parse_itunes_duration_absent_or_unparseable_returns_none():
    assert _parse_itunes_duration({}) is None
    assert _parse_itunes_duration({"itunes_duration": ""}) is None
    assert _parse_itunes_duration({"itunes_duration": "not a duration"}) is None


def test_plugin_for_dispatches_podcast_type_to_dynamic_podcast_plugin():
    row = Source(
        id=1,
        name="some_podcast",
        type="podcast",
        category="podcast",
        url="https://example.com/podcast/feed.xml",
        refresh_interval_seconds=21600,
    )
    plugin = _plugin_for(row)
    assert isinstance(plugin, DynamicPodcastPlugin)
    assert plugin.url == row.url
    assert plugin.source_id == row.id
