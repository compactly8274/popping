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

Source-shape branches
---------------------
``DynamicRssPlugin`` is the row-driven catch-all for ``type="rss"``.
A few feed shapes warrant specialized extraction on top of the
generic RSS path — RFD forum feeds ship engagement counts in their
HTML summary that the generic normalizer drops, so when the row's
URL is RFD-shaped we delegate to ``rfd.rfd_normalize_dynamic`` for
the normalizer step. Adding more feed shapes here (e.g. a future
podcast-specific dynamic source) follows the same pattern.
"""

from __future__ import annotations

from app.models import Source
from app.sources.base import SourcePlugin
from app.sources.rfd import _is_rfd_url, rfd_normalize_dynamic
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
        # ``custom_headers`` overrides the default User-Agent for
        # feeds whose CDN blocks ``Popping/0.2`` (CBC). The route
        # layer validates that headers are str→str and blocks
        # ``Cookie`` / ``Authorization``, so we don't need to defend
        # against abuse here.
        return await fetch_rss(self.url, headers=self._source_row.custom_headers)

    def normalize(self, raw: dict) -> dict:
        # RFD-shaped sources get engagement extraction via the
        # dedicated normalizer. Sniff the URL rather than the row's
        # ``name`` so user-added ``rfd_*`` rows created before
        # ``rfd_hot_deals`` was added (e.g. ``rfd_all``, ``rfd_focued``
        # — typos and all) still light up. Cost is a single ``in``
        # check on the URL string per item.
        if _is_rfd_url(self.url):
            return rfd_normalize_dynamic(self.name, raw)
        return super().normalize(raw)