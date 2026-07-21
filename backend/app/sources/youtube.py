"""YouTube channel source — row-driven, reuses the RSS fetch path.

A YouTube channel's public video feed IS an Atom feed
(``https://www.youtube.com/feeds/videos.xml?channel_id=...``) — same
reuse-not-reimplement decision podcast support made for its own RSS-
shaped feeds. feedparser already surfaces title/link/published/summary
generically, and ``rss.py``'s ``_pick_image_url`` already prioritizes
``media:thumbnail`` first — which is exactly what YouTube's feed ships
per entry — so video thumbnails flow through with no special-casing.

v1 scope is listing only: title, link, publish date, description
snippet, thumbnail. No duration or view count (that needs the paid-
quota YouTube Data API, or a per-video scrape) and no transcript
summarization — mirrors how podcast support started (Phase 6) before
ASR/transcript summarization was added later.
"""

from __future__ import annotations

from app.models import Source
from app.sources.base import SourcePlugin
from app.sources.rss import fetch_rss


class DynamicYouTubePlugin(SourcePlugin):
    """SourcePlugin bound to a single ``type="youtube_channel"`` Source row.

    Mirrors ``DynamicPodcastPlugin``'s construction — see that class
    for the rationale on mirroring the row's fields onto ``self``.
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
    # (validate_required) — image_url already lands in the entry
    # dict automatically via fetch_rss, no youtube-specific
    # extraction needed here.
