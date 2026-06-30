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
import json
import logging

import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.wikipedia_on_this_day")

_BASE = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/all"
_TIMEOUT = 15.0
_MAX_EVENTS = 50  # plenty; the feed has ~60 events per day
# 5 MB. The feed is ~100 KB. The cap defends against a
# compromised upstream returning a multi-gigabyte body before
# we OOM. Unrelated to the per-thumbnail 2 MB cap in
# ``app.assets`` — that one gates the ``image_path`` write, this
# one gates the JSON parse.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_DEFAULT_HEADERS = {
    "User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)",
    "Accept": "application/json",
}


def _fallback_url(mm: str, dd: str, slug: str) -> str:
    """Build a fallback URL for an event whose linked page is missing.

    Wikipedia's REST feed occasionally ships events with no
    ``pages`` list (rare, but observed on the events-vs-selected
    transition days). The historical code returned the same hard-
    coded ``Wikipedia:Selected_anniversaries`` URL for every
    fallback, which meant 10+ events with no pages collapsed into
    a single dedup row in the entries table — the dashboard
    showed one card for the day's "selected anniversaries" page
    instead of one per event.

    Append a ``#`` fragment derived from the event text so each
    fallback is unique without breaking the URL's
    in-page-anchor semantics. Wikipedia itself uses fragments
    for in-page jumps; we just borrow the convention for
    dedup, and the link is still usable if the user clicks
    through (the fragment will scroll to / not find a match on
    the destination page, but the article body loads fine).
    """
    return f"https://en.wikipedia.org/wiki/Wikipedia:Selected_anniversaries#{slug}"


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
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, headers=_DEFAULT_HEADERS,
                follow_redirects=True, max_redirects=5,
            ) as client:
                # Stream so we can enforce the byte cap on the actual
                # body, not just the advisory Content-Length header.
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > _MAX_RESPONSE_BYTES:
                        raise ValueError(
                            f"wikipedia_on_this_day: response Content-Length "
                            f"{cl} exceeds {_MAX_RESPONSE_BYTES} cap"
                        )
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_RESPONSE_BYTES:
                            raise ValueError(
                                f"wikipedia_on_this_day: response body exceeds "
                                f"{_MAX_RESPONSE_BYTES} cap"
                            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("wikipedia_on_this_day: fetch failed: %s", exc)
            return []
        try:
            data = json.loads(bytes(buf))
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
                    # Use a stable slug derived from the event text
                    # so the fallback URL is unique per event. The
                    # title is already a good differentiator ("1865:
                    # Lincoln…") and is human-meaningful as a
                    # fragment.
                    slug = "".join(
                        c.lower() if c.isalnum() else "-"
                        for c in text[:64]
                    ).strip("-") or "event"
                    page_url = _fallback_url(f"{now.month:02d}", f"{now.day:02d}", slug)
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