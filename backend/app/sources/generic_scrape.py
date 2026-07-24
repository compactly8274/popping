"""Generic scrape plugin — periodic ingest for sites with no native
RSS/Atom feed.

Auto-feed (see ``app.feed_autodiscovery``) tries an actual feed
first; this is the fallback the "Add custom" flow wires up when none
exists. Rather than a bespoke "guess which links on this page are
articles" heuristic, this leans on the site's own sitemap (via
``trafilatura.sitemaps.sitemap_search``) for the list of candidate
page URLs, then extracts each one's title/body the same way the
on-demand article-summary feature does (``trafilatura.bare_extraction``,
same library, same SSRF-guarded fetch as ``app.article_extract``).

Not registered via ``@register_source`` — row-driven, same pattern as
``DynamicRssPlugin`` / ``DynamicYouTubePlugin``. The scheduler
instantiates one of these per ``type="generic_scrape"`` Source row
and keeps that instance alive for the process lifetime (see
``app.scheduler._ingest``'s docstring), which is what lets
``_extracted_urls`` below persist across scheduled polls without a
dedicated DB table.
"""

from __future__ import annotations

import logging

from app.article_extract import fetch_html
from app.feed_autodiscovery import discover_sitemap_urls
from app.models import Source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.generic_scrape")

# How many NOT-YET-SEEN candidate URLs to actually fetch+extract per
# poll. Bounds both the load this puts on the target site and the
# backend's own per-poll work — a freshly added source pointed at a
# sitemap with thousands of URLs drains its backlog gradually over
# many poll cycles rather than fetching everything in one burst.
_MAX_NEW_PER_POLL = 10

# How many candidate URLs to even ask the sitemap for. Independent of
# _MAX_NEW_PER_POLL — we want to see enough of the sitemap to find
# _MAX_NEW_PER_POLL genuinely new ones even once the easy/recent
# entries near the top have already been extracted in prior polls.
_MAX_SITEMAP_CANDIDATES = 200

# Extracted body text is stored in meta.summary (same field the
# regular RSS path uses for the feed's own blurb — see
# routes/entries.py's _extract_fallback_summary), truncated the same
# way that endpoint would truncate a long one anyway. Keeps a huge
# extracted article from bloating the entries table.
_SUMMARY_MAX_CHARS = 2000


async def _extract_one(url: str) -> dict | None:
    """Fetch + extract a single candidate URL into a plugin-item
    dict, or None if the fetch or extraction didn't produce anything
    usable. Module-level (not a method) so both ``GenericScrapePlugin.
    fetch`` and the "Test" endpoint's ``probe`` below share the exact
    same extraction logic without one needing a full Source row to
    exercise the other's code path."""
    html = await fetch_html(url)
    if html is None:
        return None
    try:
        import trafilatura

        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, favor_recall=True)
    except Exception:  # noqa: BLE001 - a single page's extraction failing shouldn't sink the whole poll
        logger.debug("generic_scrape: %s: extraction raised", url)
        return None
    if doc is None:
        return None
    data = doc.as_dict()
    title = (data.get("title") or "").strip()
    text = (data.get("text") or "").strip()
    if not title or not text:
        logger.debug("generic_scrape: %s: no usable title/text extracted", url)
        return None
    summary = text[:_SUMMARY_MAX_CHARS]
    return {
        "title": title,
        "url": url,
        "published_at": data.get("date"),
        "summary": summary,
        "image_url": data.get("image"),
    }


async def probe(url: str, limit: int = 3) -> list[dict]:
    """Standalone, row-free version of ``GenericScrapePlugin.fetch``
    for ``POST /api/sources/test`` — Add's "does this actually work"
    preview needs to run before a Source row exists to construct a
    real plugin instance from. Tries a few more sitemap candidates
    than ``limit`` in case the first couple don't extract cleanly
    (e.g. a listing/index page that slipped into the sitemap), so the
    preview isn't overly pessimistic about a genuinely workable site."""
    candidates = await discover_sitemap_urls(url, limit=max(limit * 3, 10))
    items: list[dict] = []
    for candidate in candidates:
        if len(items) >= limit:
            break
        item = await _extract_one(candidate)
        if item is not None:
            items.append(item)
    return items


class GenericScrapePlugin(SourcePlugin):
    """SourcePlugin bound to a single ``type="generic_scrape"`` Source row.

    Construction mirrors ``DynamicYouTubePlugin`` — see that class for
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
        # In-process cache of URLs we've already attempted extraction
        # for — see the module docstring for why this instance
        # persists across polls. Resets on backend restart, which
        # just means one extra pass over already-inserted URLs (the
        # entries table's URL-uniqueness constraint is the real dedup
        # backstop regardless — on_conflict_do_nothing in the
        # scheduler's insert path silently no-ops a re-attempt),
        # not a correctness issue.
        self._extracted_urls: set[str] = set()

    async def fetch(self) -> list[dict]:
        candidates = await discover_sitemap_urls(self.url, limit=_MAX_SITEMAP_CANDIDATES)
        items: list[dict] = []
        attempted = 0
        for url in candidates:
            if url in self._extracted_urls:
                continue
            if attempted >= _MAX_NEW_PER_POLL:
                # Leave the rest for the next poll — see
                # _MAX_NEW_PER_POLL. Deliberately NOT marked as
                # extracted, so they're candidates again next cycle
                # instead of being skipped forever.
                break
            attempted += 1
            item = await _extract_one(url)
            self._extracted_urls.add(url)
            if item is not None:
                items.append(item)
        return items

    # normalize() is inherited from SourcePlugin's default
    # (validate_required) — the dict shape _extract_one returns
    # already matches its contract (title/url required, everything
    # else flows into meta).
