"""Podcast source — row-driven, reuses the RSS fetch path.

Podcast feeds ARE RSS feeds (RSS 2.0 plus the itunes namespace for
extra metadata) — ``fetch_rss`` already extracts the episode's audio
file (``meta.audio_url``, from the ``<enclosure>`` tag) and duration
(``meta.duration_seconds``, from ``<itunes:duration>``) generically
for any feed that ships them, regardless of ``Source.type``. See
``_pick_audio_enclosure`` / ``_parse_itunes_duration`` in
``rss.py``.

This exists as its own class (rather than just letting
``type="podcast"`` rows use ``DynamicRssPlugin`` directly) so the
scheduler's per-row dispatch and the source list's type label stay
meaningful, and so podcast-specific behavior has a clean place to
land later — e.g. a longer default refresh interval, since episodes
publish far less often than news updates.
"""

from __future__ import annotations

from app.models import Source
from app.sources.base import SourcePlugin
from app.sources.rss import fetch_rss


class DynamicPodcastPlugin(SourcePlugin):
    """SourcePlugin bound to a single ``type="podcast"`` Source row.

    Mirrors ``DynamicRssPlugin``'s construction — see that class for
    the rationale on mirroring the row's fields onto ``self``.
    """

    def __init__(self, source_row: Source) -> None:
        self._source_row = source_row
        self.name = source_row.name
        self.type = source_row.type
        self.category = source_row.category
        self.url = source_row.url
        self.refresh_interval_seconds = source_row.refresh_interval_seconds
        self.source_id = source_row.id

    async def fetch(self) -> list[dict]:
        return await fetch_rss(self.url, headers=self._source_row.custom_headers)

    # normalize() is inherited from SourcePlugin's default
    # (validate_required) — audio_url / duration_seconds already
    # land in meta automatically, no podcast-specific extraction
    # needed here.
