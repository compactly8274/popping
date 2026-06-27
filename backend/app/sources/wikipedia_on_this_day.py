"""Wikipedia On This Day.

Fetches the events/holidays/selected-anniversaries for the current
UTC date from Wikipedia's REST API. The data only changes daily so
we refresh every 12 h.

Each ``Event`` has ``text`` (the event description, ~1-2 sentences)
and ``pages`` (Wikipedia articles it links to). The first linked
page becomes the entry URL; the ``text`` is the title. ``year`` is
stored in meta so the UI can render "1865: …" if it wants.
"""

from __future__ import annotations

import datetime as dt
import logging

import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.wikipedia_on_this_day")

_BASE = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/all"
_TIMEOUT = 15.0
_MAX_EVENTS = 50  # plenty; the feed has ~60 events per day
_DEFAULT_HEADERS = {
    "User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)",
    "Accept": "application/json",
}


def _fallback_url(mm: str, dd: str) -> str:
    return f"https://en.wikipedia.org/wiki/Wikipedia:Selected_anniversaries"


@register_source
class WikipediaOnThisDay(SourcePlugin):
    name = "wikipedia_on_this_day"
    type = "api"
    category = "news"
    url = f"{_BASE}/01/01"  # canonical "primary" date; actual date is per-fetch
    refresh_interval_seconds = 43200  # 12 h

    async def fetch(self) -> list[dict]:
        now = dt.datetime.now(dt.timezone.utc)
        url = f"{_BASE}/{now.month:02d}/{now.day:02d}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_DEFAULT_HEADERS) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("wikipedia_on_this_day: fetch failed: %s", exc)
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.warning("wikipedia_on_this_day: non-JSON response")
            return []
        items: list[dict] = []
        # The feed splits content by type; we use ``events`` for
        # historical events and ``selected`` for the curated anniversaries.
        # ``holidays`` and ``births``/``deaths`` are skipped — they're
        # noise for a personal feed.
        for kind in ("events", "selected"):
            for ev in (data.get(kind) or [])[:_MAX_EVENTS]:
                text = (ev.get("text") or "").strip()
                if not text:
                    continue
                year = ev.get("year")
                # First linked page is the canonical link for this event.
                pages = ev.get("pages") or []
                first_page = pages[0] if pages else None
                page_url = None
                if first_page:
                    # REST API returns ``content_urls.desktop.page`` for
                    # the canonical article; fall back to titles.
                    content_urls = first_page.get("content_urls") or {}
                    desktop = content_urls.get("desktop") or {}
                    page_url = desktop.get("page")
                    if not page_url:
                        title = first_page.get("titles") or {}
                        normalized = title.get("normalized") or title.get("display")
                        if normalized:
                            page_url = (
                                f"https://en.wikipedia.org/wiki/{normalized.replace(' ', '_')}"
                            )
                if not page_url:
                    page_url = _fallback_url(f"{now.month:02d}", f"{now.day:02d}")
                title = f"{year}: {text}" if year else text
                items.append(
                    {
                        "title": title,
                        "url": page_url,
                        "published_at": now,  # the "freshness" is when the day rolled over
                        "summary": text,
                        "meta": {
                            "year": year,
                            "kind": kind,  # events | selected
                            "wiki_title": (first_page or {}).get("title"),
                        },
                    }
                )
        return items