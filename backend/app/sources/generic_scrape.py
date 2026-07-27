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

import datetime as dt
import logging
from typing import Optional

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
        # Trafilatura returns ``data.get("date")`` as a string
        # in one of several shapes — ISO-8601 with offset
        # (``"2026-07-27T08:59:10-07:00"``), bare ISO date
        # (``"2026-07-27"``), or with a trailing ``Z`` (the
        # Z-suffix form). The downstream ``recency.score`` calls
        # ``.tzinfo`` on the value, so a raw string crashes the
        # whole ingest. Normalize via ``_parse_published_str``
        # which handles all three shapes and returns ``None`` on
        # anything it can't parse (a missing or unparseable date
        # yields zero recency contribution — the entry still
        # lands in the DB, just without the convergence boost).
        "published_at": _parse_published_str(data.get("date")),
        "summary": summary,
        "image_url": data.get("image"),
    }


def _parse_published_str(raw: object) -> Optional[dt.datetime]:
    """Normalize a trafilatura ``date`` value to a UTC datetime.

    Trafilatura's ``bare_extraction`` returns ``data["date"]`` in
    several shapes — depends on what metadata the page exposed:

    - ``"2026-07-27T08:59:10-07:00"`` (ISO-8601 with offset)
    - ``"2026-07-27"`` (date only — no time component)
    - ``"2026-07-27T08:59:10Z"`` (Z-suffix; some HTML metadata)
    - ``"Mon, 27 Jul 2026 15:59:10 +0000"`` (RFC-822, rare)
    - ``None`` (page didn't expose any date metadata)

    Returns a timezone-aware ``datetime`` in UTC for the parseable
    cases, or ``None`` for ``None`` / unparseable strings. Plain
    dates get their time component set to midnight UTC (the
    earliest moment of that day) so the recency score is at least
    representative — a publication-day accuracy is fine for a
    "freshness" signal, a "publication second" wouldn't add value
    the RSS path doesn't already have.

    Mirrors the spirit of ``app.sources.rss._parse_published``
    (which handles ``feedparser`` entries) but operates on a raw
    string instead, since trafilatura doesn't expose a struct_time
    the way feedparser does.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Shape 1: ISO-8601 with offset, possibly Z-suffix.
    # Python 3.11+ ``fromisoformat`` accepts the Z suffix; on
    # 3.10 and earlier we'd have to substitute. We're on
    # 3.12 (matches the deploy image), so this is safe; if
    # 3.10 ever comes back, replace ``Z`` with ``+00:00``
    # first as a belt-and-braces measure.
    try:
        # Belt-and-braces: handle Z on every Python version
        # by substituting before fromisoformat. fromisoformat
        # rejects the literal 'Z' on 3.10.
        normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
        parsed = dt.datetime.fromisoformat(normalized)
        # Naive datetime (date-only or no offset) → assume UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        pass
    # Shape 2: RFC-822 (rare, e.g. ``"Mon, 27 Jul 2026 15:59:10 +0000"``)
    # ``email.utils.parsedate_to_datetime`` is the canonical
    # stdlib helper for this; it raises ``TypeError`` on bad
    # input rather than returning a sentinel.
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(s)
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


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
        # Optional direct sitemap URL. When set, the plugin skips
        # trafilatura's homepage-based sitemap discovery (which
        # doesn't work on every site — e.g. theprogress.com's
        # sitemap_index has 1100+ cross-domain entries that all
        # 404) and instead parses the user-specified URL directly
        # with ``trafilatura.sitemaps.sitemap_search``. Same code
        # path, same SSRF guard, same dedup; just a more specific
        # entry point. None falls back to the original behavior
        # so old rows aren't affected by the migration.
        self.sitemap_url: Optional[str] = getattr(source_row, "sitemap_url", None)
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
        # If the row carries an explicit sitemap_url, use it
        # directly — ``trafilatura.sitemaps.sitemap_search`` will
        # parse it as a sitemap (returning the article URLs) when
        # given a direct sitemap URL, but it tries to *find* the
        # sitemap first when given a homepage URL, which is
        # fragile (e.g. theprogress.com's sitemap_index is
        # 1.1MB of cross-domain entries that all 404). Letting
        # the user point at a known-good sitemap URL side-steps
        # the discovery heuristic entirely.
        sitemap_input = self.sitemap_url or self.url
        candidates = await discover_sitemap_urls(sitemap_input, limit=_MAX_SITEMAP_CANDIDATES)
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
