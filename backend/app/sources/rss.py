"""RSS feed source — phase 1's first source, phase 3 widens it.

Fetches any RSS / Atom feed via feedparser-over-httpx. Concrete plugins
are instantiated at module load via the `@register_source` decorator;
the scheduler picks them up by name from `list_sources()`.

BBC News is the default source: no auth, stable URL, exercises the
ingestion path end-to-end.

User-Agent note: many feeds (BBC since 2024, Reddit, etc.) throttle
or 403 the default `python-httpx` UA. We send a descriptive UA and
an Accept header that explicitly asks for RSS/Atom — both cheap and
uncontroversial.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.sources.rss")

_USER_AGENT = "Popping/0.2 (+https://github.com/compactly8274/popping)"
_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": _ACCEPT,
}


def _parse_published(entry: Any) -> dt.datetime | None:
    """Best-effort parse of a feedparser entry's published/updated field.

    Tries ``published_parsed`` / ``updated_parsed`` first (struct_time
    populated by feedparser from RFC-822 dates) and falls back to
    ISO-8601 strings in ``published`` / ``updated`` / ``created`` when
    the struct parse fails. Without the string fallback, an entry that
    ships ``<pubDate>2026-06-28T12:34:56Z</pubDate>`` (non-RFC-822)
    lands with ``published_at=None`` and zero recency contribution —
    silently excluded from the convergence boost.
    """
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    for key in ("published", "updated", "created"):
        s = getattr(entry, key, None)
        if s and isinstance(s, str):
            try:
                return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


# First <img src> in a summary blob. Lazy fallback when the feed
# doesn't ship a structured image field — common in WordPress-style
# HTML summaries.
_IMG_SRC_RE = re.compile(
    r"""<img\s[^>]*?src=["']([^"']+)["']""",
    re.IGNORECASE,
)


def _entry_url(entry: Any) -> str:
    """Best-effort article URL for a feedparser entry.

    Standard ``entry.link`` is the shorthand feedparser sets when
    an item has a single ``<link>`` element. When an item has
    multiple ``<link>`` children (Sportsnet ships 2-3 per item:
    the article URL, a media:enclosure for the thumbnail, and a
    ``type="app-deep-link-field"`` URL for the iOS app), the
    shorthand is empty and the real URL is in ``entry.links[]``.

    Selection priority:
      1. ``entry.link`` if non-empty (the common case — single
         ``<link>`` feeds).
      2. ``entry.links[i].href`` where the link is
         ``rel="alternate"`` and ``type="text/html"`` (the
         canonical web URL per the Atom spec, which Sportsnet
         honours).
      3. Any link with ``rel="alternate"`` regardless of type
         (catches the edge case of a feed that uses a
         non-text/html alternate).
      4. First non-empty ``href`` in ``entry.links`` (last
         resort — picks something, even if it's the wrong thing,
         over returning '' which silently kills the row).

    The 4-level fallback matters: a feed that ships ONLY a
    single ``<link rel="alternate" type="application/pdf">`` (rare
    but real) lands in step 3, while a feed with three unrelated
    ``<link>`` children lands in step 4. Both should produce a
    usable URL rather than an empty string.
    """
    direct = entry.get("link") or ""
    if direct:
        return direct
    links = entry.get("links") or []
    # Step 2: rel="alternate" + type="text/html"
    for L in links:
        if (L.get("rel") == "alternate" and
                (L.get("type") or "").startswith("text/html") and
                L.get("href")):
            return L["href"]
    # Step 3: any rel="alternate"
    for L in links:
        if L.get("rel") == "alternate" and L.get("href"):
            return L["href"]
    # Step 4: first non-empty href
    for L in links:
        if L.get("href"):
            return L["href"]
    return ""


def _pick_image_url(entry: Any) -> str | None:
    """Best thumbnail URL from a feedparser entry, or None.

    Priority matches what real feeds actually ship:
      1. media:thumbnail (Media RSS — most common)
      2. media:content with image/* type
      3. enclosure with image/* type (RSS 2.0)
      4. itunes:image (podcast artwork)
      5. first <img src> regex over summary (HTML summaries)

    Relative ``<img src="/path/...">`` URLs are resolved against the
    entry's ``link`` field so the asset fetcher sends an absolute
    request. Without the resolve, a feed that ships WordPress-style
    relative paths stores ``image_url="/2026/06/x.jpg"``; the fetcher
    GETs that path against the source's host (often wrong) and
    receives a 404, so the thumbnail never lands and ``image_path``
    stays NULL forever. Absolute URLs from media:thumbnail /
    media:content / enclosure / itunes:image pass through unchanged
    (those branches already ship absolute URLs from real-world
    feeds).
    """
    # 1. media:thumbnail
    mt = entry.get("media_thumbnail")
    if mt:
        url = mt[0].get("url") if isinstance(mt, list) else mt.get("url")
        if url:
            return url
    # 2. media:content
    mc = entry.get("media_content")
    if mc:
        items = mc if isinstance(mc, list) else [mc]
        for m in items:
            ct = (m.get("type") or "").lower()
            if ct.startswith("image/") and m.get("url"):
                return m["url"]
    # 3. enclosure
    for enc in entry.get("enclosures") or []:
        ct = (enc.get("type") or "").lower()
        if ct.startswith("image/") and enc.get("href"):
            return enc["href"]
    # 4. itunes:image
    ii = entry.get("image")
    if isinstance(ii, dict) and ii.get("href"):
        return ii["href"]
    # 5. inline <img src> in summary — resolve relative paths
    # against the entry's URL so the asset fetcher doesn't 404.
    summary = entry.get("summary") or ""
    if summary:
        m = _IMG_SRC_RE.search(summary)
        if m:
            entry_url = _entry_url(entry)
            return urljoin(entry_url, m.group(1)) if entry_url else m.group(1)
    return None


def _pick_audio_enclosure(entry: Any) -> str | None:
    """The episode's audio file URL from the RSS <enclosure> tag, or
    None. Podcast feeds ship exactly one audio enclosure per item —
    a plain article feed has none, which is the common case and
    correctly returns None rather than a guess (same "no signal
    beats a guessed one" rule ``_pick_image_url`` and the RFD
    engagement extractor already follow).
    """
    for enc in entry.get("enclosures") or []:
        ct = (enc.get("type") or "").lower()
        if ct.startswith("audio/") and enc.get("href"):
            return enc["href"]
    return None


def _parse_itunes_duration(entry: Any) -> int | None:
    """Episode duration in seconds from <itunes:duration>, or None.

    The tag isn't consistently formatted across podcast hosts — some
    ship plain seconds ("3723"), others "H:MM:SS" or "MM:SS". Handles
    all three; returns None (not 0) when the field is absent or
    unparseable so a missing duration doesn't render as "0:00".
    """
    raw = entry.get("itunes_duration")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    else:
        return None
    return h * 3600 + m * 60 + s


# Podcasting 2.0 namespace — https://podcastindex.org/namespace/1.0.
# feedparser doesn't recognize this namespace (it's newer than
# feedparser's built-in itunes/media/dc/content handling), so
# <podcast:transcript> tags are silently dropped by the feedparser
# pass. We separately walk the raw XML with ElementTree to pick them
# up. Content-type preference: JSON (Podcast Index's own schema —
# structured, cheapest to turn into clean text), then plain text,
# then HTML, then the caption formats (VTT/SRT — need timing-cue
# stripping before they're useful as summarization input).
_PODCAST_NS = "https://podcastindex.org/namespace/1.0"
_TRANSCRIPT_TYPE_PRIORITY = {
    "application/json": 0,
    "text/plain": 1,
    "text/html": 2,
    "text/vtt": 3,
    "application/srt": 4,
    "text/srt": 4,
}


def _extract_podcast_transcripts(xml_text: str) -> dict[str, tuple[str, str]]:
    """Map each ``<item>``'s link/guid to its best ``<podcast:transcript>``
    (url, content_type), for feeds that ship one. Returns {} for feeds
    that don't use the podcast namespace or fail to parse as strict
    XML — feedparser's lenient parse already succeeded by this point
    (this function only runs after that), so a strict-parse failure
    here just means "no transcripts", not "the feed is broken".

    Keyed by both the item's <link> text and <guid> text (whichever
    the caller's feedparser entry exposes) since either can be the
    identifier ``fetch_rss`` looks the result up by.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    ns = {"podcast": _PODCAST_NS}
    result: dict[str, tuple[str, str]] = {}
    for item in root.iter("item"):
        candidates: list[tuple[str, str]] = []
        for t in item.findall("podcast:transcript", ns):
            url = t.get("url")
            ctype = (t.get("type") or "").lower().strip()
            if url:
                candidates.append((url, ctype))
        if not candidates:
            continue
        candidates.sort(key=lambda c: _TRANSCRIPT_TYPE_PRIORITY.get(c[1], 99))
        best = candidates[0]
        link_el = item.find("link")
        guid_el = item.find("guid")
        for el in (link_el, guid_el):
            if el is not None and el.text:
                result[el.text.strip()] = best
    return result


# Per-stage timeouts for the RSS fetch. ``connect`` is the TCP /
# TLS handshake budget — failing fast here is correct, because a
# host that can't accept the connection in 10s isn't coming back.
# ``read`` is the body budget — feeds on slow CDNs (CBC, NYT
# metered) routinely take 30-60s to start streaming. Using a
# blanket timeout=30 was misleading: a 30s ``httpx.Timeout`` applies
# to every stage including read, which is too tight for these.
# Splitting the two keeps the failure modes honest: connection
# refused → 10s fail; slow body → 60s read window.
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0
_RSS_TIMEOUT = httpx.Timeout(
    connect=_CONNECT_TIMEOUT,
    read=_READ_TIMEOUT,
    write=_READ_TIMEOUT,
    pool=_READ_TIMEOUT,
)


async def fetch_rss(url: str, headers: dict[str, str] | None = None) -> list[dict]:
    """Fetch and parse any RSS/Atom feed at ``url``.

    Module-level helper so the class-driven ``_RssPlugin`` and the
    row-driven ``DynamicRssPlugin`` (see ``dynamic_rss.py``) share
    the same parsing logic. Image picking uses the same priority as
    the BBC plugin: media:thumbnail → media:content (image/*) →
    enclosure (image/*) → itunes:image → first <img src> in summary.
    No DB / scheduler awareness here — this is a pure HTTP→list[dict]
    function that the scheduler's ``_ingest`` consumes through the
    plugin's ``fetch()`` method.

    ``headers`` (optional) is merged on top of ``_DEFAULT_HEADERS``
    so a source that blocks our default User-Agent (CBC) can be
    unblocked by setting ``custom_headers`` on the row. Keys are
    case-insensitive; values are sent verbatim. The route layer
    blocks ``Cookie`` / ``Authorization`` so this can't be used to
    forge another session.

    One retry on transient network errors and 5xx. CBC's CDN in
    particular has been observed to time out the first request of a
    cold connection but respond fine to a follow-up; the same TCP
    socket reuse isn't always available because we open a fresh
    client per ingest. One retry is enough — past that the upstream
    is genuinely degraded and we'd rather skip the tick than hold
    up the scheduler for a feed that's not going to land.

    Empty-result detection: if HTTP 200 but the body doesn't look
    like a feed (wrong content-type or ``feed.bozo`` with zero
    entries), raise so the scheduler's per-source error path writes
    a tooltip with the actual response excerpt — better than silently
    showing zero entries with no explanation.
    """
    # Merge caller's overrides on top of defaults. httpx doesn't
    # raise on collisions — the later key wins — so the order here
    # is the priority order: defaults first, then overrides.
    merged_headers = {**_DEFAULT_HEADERS, **(headers or {})}
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(
                timeout=_RSS_TIMEOUT,
                follow_redirects=True, max_redirects=5,
            ) as client:
                resp = await client.get(url, headers=merged_headers)
                resp.raise_for_status()
                # Re-check the FINAL URL after the redirect chain. The
                # entry-time check in routes/sources.py validates the
                # user-supplied URL, but ``follow_redirects=True`` lets
                # a feed owner 3xx the request to a private / loopback
                # address (e.g. ``public.example.com → 127.0.0.1:6379``
                # or ``169.254.169.254/...``) that the entry check
                # never saw. Without this guard, the response body
                # lands in feedparser and any internal services
                # reachable from the backend get a hit on every
                # ingest. Same shape as the assets._download guard.
                final_url = str(resp.url)
                if final_url != url:
                    final_safe, reason = check_url_safe(final_url)
                    if not final_safe:
                        logger.warning(
                            "rss: %s redirected to denied host %s (%s)",
                            url, final_url, reason,
                        )
                        raise ValueError(
                            f"rss: {url} redirected to a denied host"
                        )
            feed = feedparser.parse(resp.text)
            transcripts_by_key = _extract_podcast_transcripts(resp.text)
            items: list[dict] = []
            for entry in feed.entries:
                image_url = _pick_image_url(entry)
                entry_url = _entry_url(entry)
                item: dict[str, Any] = {
                    "title": entry.get("title", ""),
                    "url": entry_url,
                    "published_at": _parse_published(entry),
                    "summary": entry.get("summary", ""),
                    # Top-level so the ingest pipeline can pop it out of
                    # meta cleanly. NULL when the feed ships no image.
                    "image_url": image_url,
                }
                # Podcast fields. Only added when present — unlike
                # image_url (which the ingest pipeline always pops,
                # so it needs a stable key) these have no dedicated
                # Entry column and just flow into meta, so an absent
                # key is cheaper than an always-present null (matches
                # the RFD engagement extractor's convention).
                audio_url = _pick_audio_enclosure(entry)
                if audio_url:
                    item["audio_url"] = audio_url
                duration_seconds = _parse_itunes_duration(entry)
                if duration_seconds is not None:
                    item["duration_seconds"] = duration_seconds
                # Transcript, if the feed publishes one via the
                # Podcasting 2.0 <podcast:transcript> tag. Looked up
                # by link first (matches entry_url, the common case),
                # falling back to the guid (feedparser's ``id``) for
                # feeds whose <link> and <guid> differ.
                transcript = transcripts_by_key.get(entry_url) or transcripts_by_key.get(
                    entry.get("id") or ""
                )
                if transcript:
                    item["transcript_url"], item["transcript_type"] = transcript
                items.append(item)
            # HTTP 200 but the body doesn't look like an RSS/Atom
            # feed: Cloudflare challenges, paywall interstitials,
            # JavaScript-rendered shells, etc. all return HTML that
            # feedparser silently accepts with ``bozo=True`` and zero
            # entries. Surface it as an error so the scheduler can
            # record a useful tooltip instead of silently emptying
            # the dashboard.
            #
            # Also guard the "feedparser happily parsed garbage"
            # case: a truncated XML body can yield a non-empty
            # ``entries`` list whose items have empty ``title`` /
            # ``url``. Without this check, the pipeline lands
            # phantom rows with ``title=''`` and ``url=<feed url>``
            # that pollute the dashboard. We treat "any entry is
            # missing title or url" as malformed and surface a
            # useful tooltip instead of silently accepting the
            # damage.
            all_ok = bool(items) and all(
                e.get("title") and e.get("url") for e in items
            )
            if not all_ok and (bool(feed.bozo) or not _looks_like_feed(resp)):
                snippet = resp.text[:200].replace("\n", " ").strip()
                raise ValueError(
                    f"rss: {url} returned malformed entries "
                    f"(bozo={feed.bozo}, ok={all_ok}, count={len(items)}, "
                    f"content-type={resp.headers.get('content-type')!r}, "
                    f"excerpt={snippet!r})"
                )
            return items
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout, httpx.TooManyRedirects) as exc:
            last_exc = exc
            logger.warning(
                "rss: %s attempt %d failed (%s); retrying", url, attempt, exc,
            )
            continue
        except httpx.HTTPStatusError as exc:
            # 5xx is "server had a hiccup" — retry once. 4xx is
            # permanent (auth failure, gone, not-found, paywall
            # block) and there's no point in another attempt; let it
            # bubble so the user sees a precise reason in the
            # tooltip.
            if 500 <= exc.response.status_code < 600 and attempt == 1:
                last_exc = exc
                logger.warning(
                    "rss: %s attempt %d failed (%s); retrying", url, attempt, exc,
                )
                continue
            raise
    # Both attempts failed. Bubble the last error so the scheduler's
    # per-source try/except logs it as the ingest failure.
    assert last_exc is not None
    raise last_exc


def _looks_like_feed(resp: httpx.Response) -> bool:
    """Best-effort sniff: does the response Content-Type look like an
    RSS/Atom document? Used alongside ``feed.bozo`` to catch
    Cloudflare / paywall HTML bodies that return 200 but aren't
    actually feeds."""
    ctype = resp.headers.get("content-type", "").lower()
    return (
        "rss" in ctype
        or "atom" in ctype
        or "xml" in ctype
        or ctype.startswith("text/")
    )


class _RssPlugin(SourcePlugin):
    """Generic RSS/Atom fetcher. Subclasses set name/url/refresh.

    Thin wrapper around ``fetch_rss`` so the class-driven plugins
    (BBC today; future built-ins if any) and the row-driven
    ``DynamicRssPlugin`` share one implementation.
    """

    type = "rss"
    category = "news"

    async def fetch(self) -> list[dict]:
        return await fetch_rss(self.url)


@register_source
class BbcNews(_RssPlugin):
    name = "bbc_news"
    url = "https://feeds.bbci.co.uk/news/rss.xml"
    refresh_interval_seconds = 3600  # 1 hour
