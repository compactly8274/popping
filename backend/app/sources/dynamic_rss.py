"""Row-driven RSS plugin.

Mirrors the class-driven ``_RssPlugin`` in ``rss.py`` but takes its
config from a ``Source`` DB row instead of class attributes. Used
by the scheduler for any ``Source`` row whose name isn't in the
registered plugin registry (BBC, HN, etc.) — i.e. anything the user
added via ``POST /api/sources`` or seeded by some future external
importer.

Not registered via ``@register_source``. The scheduler instantiates
one of these per row at startup (and on add/update) instead.

Behavior parity with ``_RssPlugin`` is the goal: same HTTP fetch,
same image-picking priority, same summary handling. Anything that
differs (e.g. a future podcast-specific RSS plugin) should be its
own subclass / its own ``type`` value, not a special case here.
"""

from __future__ import annotations

from app.models import Source
from app.sources.base import SourcePlugin
from app.sources.rss import fetch_rss


class DynamicRssPlugin(SourcePlugin):
    """SourcePlugin bound to a single ``Source`` DB row.

    ``name``, ``type``, ``category``, ``url``, and
    ``refresh_interval_seconds`` are read off the row at construction
    time. The scheduler treats these as immutable for the lifetime
    of the job — if the row's refresh interval is updated, the
    scheduler rebuilds the job (reschedule) rather than mutate this
    instance in place.
    """

    def __init__(self, source_row: Source) -> None:
        self._source_row = source_row
        # Mirror the relevant class attrs onto ``self`` so the base
        # contract (``plugin.url`` etc.) works without callers caring
        # whether they're looking at a class-driven or row-driven
        # plugin. ``type`` is locked to "rss" because that's the
        # only branch that lands here today — the scheduler filters
        # out non-RSS rows before constructing.
        self.name = source_row.name
        self.type = source_row.type  # "rss" in v1
        self.category = source_row.category
        self.url = source_row.url
        self.refresh_interval_seconds = source_row.refresh_interval_seconds

    async def fetch(self) -> list[dict]:
        return await fetch_rss(self.url)

    # ``normalize`` comes from the base class — title/url/published_at
    # validation is the same for all RSS-shaped plugins, and the
    # default normalizer's "drop everything except the reserved keys"
    # behavior already captures the summary / image_url into meta.