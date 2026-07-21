"""Resolve any YouTube channel/handle/video link to its channel's
video RSS feed URL.

YouTube publishes a free, unauthenticated Atom feed per channel —
``https://www.youtube.com/feeds/videos.xml?channel_id=UC...`` — but
that URL needs the channel's internal ``UC...`` id, and people paste
channel links in every shape: ``/channel/UC...`` (has the id already),
``@handle``, ``/c/customname``, ``/user/legacyname``, or even a plain
video ``watch?v=...`` URL. Same "obtuse URL hunting" problem the Apple
Podcasts fix solved for podcast show pages — this does the equivalent
for YouTube.

Resolution: a direct ``/channel/UC...`` URL needs no network call —
the id is already in the path. Every other shape requires fetching
the page and pulling the channel id out of the page's embedded JSON
(``"channelId":"UC..."``), which YouTube ships on every page type —
channel, handle, and video pages alike — so one regex covers all of
them without needing the paid-quota YouTube Data API.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from app.url_safety import check_url_safe

# Matches any youtube.com subdomain (www, m, music, ...) or the
# youtu.be short-link host. Anchored to the end of the hostname so
# "youtube.com.evil.example" doesn't match.
_YOUTUBE_HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be)$", re.IGNORECASE)

# A channel id is always "UC" + 22 URL-safe base64 chars.
_CHANNEL_ID_DIRECT_RE = re.compile(r"/channel/(UC[0-9A-Za-z_-]{22})")
_CHANNEL_ID_IN_PAGE_RE = re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{22})"')

_FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=15.0)
_USER_AGENT = "Popping/0.2 (+https://github.com/compactly8274/popping)"


class YouTubeResolutionError(RuntimeError):
    """Raised when a YouTube URL is detected but can't be resolved to
    a channel id — network failure, or a page that doesn't expose one
    (deleted/private/terminated channel)."""


def is_youtube_url(url: str) -> bool:
    """True if ``url``'s host is youtube.com or youtu.be. Pure, no
    I/O — lets a caller decide whether resolution is even worth
    attempting, mirroring ``apple_podcasts.apple_podcasts_id``."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return bool(_YOUTUBE_HOST_RE.search(host))


def _feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


async def resolve_channel_feed_url(url: str) -> str:
    """Resolve ``url`` to a channel's video RSS feed URL if it's any
    shape of YouTube link; otherwise return it unchanged. Safe to
    call unconditionally on every source URL before validation — a
    no-op pass-through for anything that isn't a YouTube host
    (mirrors ``apple_podcasts.resolve_feed_url``'s contract).
    """
    if not is_youtube_url(url):
        return url

    direct = _CHANNEL_ID_DIRECT_RE.search(url)
    if direct:
        return _feed_url(direct.group(1))

    # Every other shape (@handle, /c/name, /user/name, watch?v=...)
    # needs the page fetched and scraped. Real fetch of a user-
    # supplied URL — SSRF-guarded the same way rss.py / podcast_asr.py
    # guard theirs, entry-time and post-redirect.
    safe, reason = check_url_safe(url)
    if not safe:
        raise YouTubeResolutionError(f"couldn't resolve the YouTube link: {reason}")
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT, follow_redirects=True, max_redirects=5,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            final_url = str(resp.url)
            if final_url != url:
                final_safe, final_reason = check_url_safe(final_url)
                if not final_safe:
                    raise YouTubeResolutionError(
                        f"couldn't resolve the YouTube link: {final_reason}"
                    )
    except httpx.HTTPError as exc:
        raise YouTubeResolutionError(f"couldn't resolve the YouTube link: {exc}") from exc

    match = _CHANNEL_ID_IN_PAGE_RE.search(resp.text)
    if not match:
        raise YouTubeResolutionError(
            "couldn't find a channel on this YouTube page — paste a channel, "
            "handle, or video URL"
        )
    return _feed_url(match.group(1))
