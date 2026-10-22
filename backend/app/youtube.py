"""Resolve any YouTube channel, handle, video or playlist link to a
YouTube RSS feed URL.

YouTube publishes a free, unauthenticated Atom feed per channel
(``https://www.youtube.com/feeds/videos.xml?channel_id=UC...``) and per
public playlist (``...?playlist_id=PL...``). The channel feed needs the
channel's internal ``UC...`` id, and people paste links in every shape:
``/channel/UC...``, ``@handle``, ``/c/name``, ``/user/name``, a video
link, a ``youtu.be`` short link, the feed link itself, a playlist, or
the bare id copied from the YouTube app.

Shapes that already carry an id resolve with no network call. Every
other shape fetches one YouTube page and reads the channel id from it.
That fetch looks like a browser (Safari User-Agent, Accept-Language,
consent cookies), because YouTube answers a plain bot User-Agent from a
datacenter address with a consent page or a bot check. Every hop goes
through the SSRF guard, and the redirect chain must stay on YouTube and
Google's consent hosts.

Messages raised here are shown to the user as they are, so they are
plain words and never contain exception text, addresses or the guard's
reason.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from app.url_safety import check_url_safe, ssrf_event_hook

logger = logging.getLogger(__name__)

# Matches any youtube.com subdomain (www, m, music, ...) or the
# youtu.be short-link host. Anchored to the end of the hostname so
# "youtube.com.evil.example" doesn't match.
_YOUTUBE_HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be)$", re.IGNORECASE)

# A channel id is "UC" + 22 URL-safe base64 characters, and nothing
# after it: a longer string is not a channel id, so it is never
# silently truncated.
_ID_CHARS = r"[0-9A-Za-z_-]"
_CHANNEL_ID = rf"UC{_ID_CHARS}{{22}}(?!{_ID_CHARS})"
_BARE_CHANNEL_ID_RE = re.compile(rf"UC{_ID_CHARS}{{22}}")
_CHANNEL_PATH_RE = re.compile(rf"(?i:/channel/)(UC{_ID_CHARS}{{22}})(?:/.*)?")
_VIDEO_ID_RE = re.compile(rf"{_ID_CHARS}{{11}}")
_PLAYLIST_ID_RE = re.compile(rf"{_ID_CHARS}+")
_FEED_USER_RE = re.compile(r"[0-9A-Za-z_.-]{1,100}")

# A bare @handle typed on its own. Python's \w is Unicode, so
# non-Latin handles work.
_BARE_HANDLE_RE = re.compile(r"@[\w.·-]{3,30}")

# First path segments that are YouTube's own pages, never a legacy
# vanity channel name.
_RESERVED_SEGMENTS = frozenset(
    {
        "watch", "results", "feed", "feeds", "playlist", "shorts", "live",
        "embed", "channel", "c", "user", "account", "about", "attribution_link",
    }
)

_FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=15.0)

# Test seam. None means the real network; httpx still mounts the
# HTTP(S)_PROXY environment proxies, so the optional egress guard
# keeps applying.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
# Never a Cookie header here: httpx drops it on redirects. The
# consent cookies live on the client's jar instead (see _client).
_PAGE_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_BODY = 3_000_000

MSG_BLOCKED = (
    "YouTube didn't let Popping look this channel up. Paste the channel ID "
    "instead: open the channel in YouTube, tap \u201cmore about this channel\u201d, "
    "then Share channel, then Copy channel ID. It starts with UC."
)
MSG_NOT_FOUND = "YouTube says this channel or video doesn't exist."
MSG_NO_MARKER = (
    "Couldn't find the channel on that YouTube page. Paste the channel ID "
    "instead. It starts with UC."
)
MSG_NETWORK = "Couldn't reach YouTube. Try again in a minute."
MSG_UNREACHABLE = "That address isn't reachable from Popping."
MSG_UNEXPECTED_HOST = (
    "YouTube sent Popping somewhere unexpected. Paste the channel ID "
    "instead. It starts with UC."
)
MSG_NOT_A_CHANNEL = (
    "That isn't a channel link. Paste the channel's page, a video link, "
    "an @handle or the channel ID."
)
MSG_PRIVATE_PLAYLIST = (
    "Private or automatic playlists don't have a feed. Paste the channel's "
    "link instead."
)


def _msg_http_error(code: int) -> str:
    return (
        f"YouTube answered with an error (HTTP {code}). Try again later, "
        "or paste the channel ID. It starts with UC."
    )


class YouTubeResolutionError(RuntimeError):
    """A YouTube link was recognised but could not be turned into a
    feed. The message is safe to show to the user as it is."""


class YouTubeBlockedError(YouTubeResolutionError):
    """YouTube answered with a consent page, a bot check or a 429."""


class YouTubeNotFoundError(YouTubeResolutionError):
    """YouTube answered 404 for the channel or video."""


def is_youtube_url(url: str) -> bool:
    """True if ``url``'s host is youtube.com or youtu.be. Pure, no
    I/O — lets a caller decide whether resolution is even worth
    attempting, mirroring ``apple_podcasts.apple_podcasts_id``."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return bool(_YOUTUBE_HOST_RE.search(host))


def _channel_feed(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _playlist_feed(playlist_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"


def _user_feed(name: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?user={quote(name, safe='')}"


def normalize_youtube_input(text: str, *, allow_bare_handle: bool = False) -> str:
    """Turn what someone pasted into a URL when it is clearly a YouTube
    reference; return anything else unchanged.

    - a bare ``UC...`` id becomes the ``/channel/`` URL;
    - a bare ``@handle`` becomes the handle URL, only when the caller
      knows the input is meant for YouTube (``allow_bare_handle``);
    - a link without ``https://`` gets it, only when the resulting
      host is a YouTube host.
    """
    stripped = text.strip()
    if _BARE_CHANNEL_ID_RE.fullmatch(stripped):
        return f"https://www.youtube.com/channel/{stripped}"
    if allow_bare_handle and _BARE_HANDLE_RE.fullmatch(stripped):
        return "https://www.youtube.com/" + quote(stripped, safe="@")
    if "://" not in stripped and "." in stripped:
        candidate = "https://" + stripped
        if is_youtube_url(candidate):
            return candidate
    return text


def _segments(path: str) -> list[str]:
    return [unquote(seg) for seg in path.split("/") if seg]


def _playlist_target(playlist_id: str) -> str:
    """Feed for a playlist id, or raise for the private and automatic
    kinds (Watch later, Liked, mixes) that have no feed."""
    if not _PLAYLIST_ID_RE.fullmatch(playlist_id):
        raise YouTubeResolutionError(MSG_NOT_A_CHANNEL)
    if playlist_id.startswith("UU") and len(playlist_id) == 24:
        # A channel's uploads playlist: same 22 characters as its id.
        return _channel_feed("UC" + playlist_id[2:])
    if playlist_id.startswith(("PL", "OLAK5uy_", "FL")):
        return _playlist_feed(playlist_id)
    raise YouTubeResolutionError(MSG_PRIVATE_PLAYLIST)


def _classify(url: str) -> tuple[str, str]:
    """Decide what a YouTube URL needs. Returns ``("feed", feed_url)``
    when no network call is needed, or ``("lookup", page_url)`` with
    the page to fetch, rebuilt from parsed parts so tracking parameters
    (``si=``), userinfo and ports never leave the server. Raises
    YouTubeResolutionError for links that are not a channel."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    query = parse_qs(parsed.query)
    segs = _segments(path)

    channel = _CHANNEL_PATH_RE.fullmatch(path)
    if channel:
        return "feed", _channel_feed(channel.group(1))

    if host == "youtu.be" or host.endswith(".youtu.be"):
        if len(segs) == 1 and _VIDEO_ID_RE.fullmatch(segs[0]):
            return "lookup", f"https://www.youtube.com/watch?v={segs[0]}"
        raise YouTubeResolutionError(MSG_NOT_A_CHANNEL)

    first = segs[0].lower() if segs else ""

    if first == "feeds" and len(segs) == 2 and segs[1].lower() == "videos.xml":
        channel_ids = query.get("channel_id", [])
        if channel_ids and _BARE_CHANNEL_ID_RE.fullmatch(channel_ids[0]):
            return "feed", _channel_feed(channel_ids[0])
        users = query.get("user", [])
        if users and _FEED_USER_RE.fullmatch(users[0]):
            return "feed", _user_feed(users[0])
        playlists = query.get("playlist_id", [])
        if playlists:
            return "feed", _playlist_target(playlists[0])
        raise YouTubeResolutionError(MSG_NOT_A_CHANNEL)

    if first == "playlist" and len(segs) == 1 and "v" not in query:
        lists = query.get("list", [])
        if lists:
            return "feed", _playlist_target(lists[0])
        raise YouTubeResolutionError(MSG_NOT_A_CHANNEL)

    video_id = None
    if first in ("watch", "playlist") and len(segs) == 1:
        video_id = (query.get("v") or [None])[0]
    elif first in ("shorts", "live", "embed") and len(segs) >= 2:
        video_id = segs[1]
    if video_id is not None:
        if _VIDEO_ID_RE.fullmatch(video_id):
            return "lookup", f"https://www.youtube.com/watch?v={video_id}"
        raise YouTubeResolutionError(MSG_NOT_A_CHANNEL)

    if segs and segs[0].startswith("@") and len(segs[0]) > 1:
        return "lookup", "https://www.youtube.com/" + quote(segs[0], safe="@")

    if first in ("c", "user") and len(segs) >= 2:
        return "lookup", f"https://www.youtube.com/{first}/{quote(segs[1], safe='')}"

    if len(segs) == 1 and first not in _RESERVED_SEGMENTS and re.fullmatch(r"[\w.-]+", segs[0]):
        # Legacy vanity URL, e.g. youtube.com/marquesbrownlee.
        return "lookup", "https://www.youtube.com/" + quote(segs[0], safe="")

    # The home page, search results, feeds, account pages and anything
    # else. Never looked up: the last page marker would pick whatever
    # channel YouTube happened to recommend.
    raise YouTubeResolutionError(MSG_NOT_A_CHANNEL)


# --- page markers ----------------------------------------------------------

_TAG_RE = {
    "link": re.compile(r"<link\b[^>]*>", re.IGNORECASE),
    "meta": re.compile(r"<meta\b[^>]*>", re.IGNORECASE),
}
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_RSS_HREF_RE = re.compile(rf"/feeds/videos\.xml\?channel_id=({_CHANNEL_ID})")
_CHANNEL_HREF_RE = re.compile(rf"/channel/({_CHANNEL_ID})")
_EXACT_ID_RE = re.compile(rf"({_CHANNEL_ID})")
_VIDEO_DETAILS = '"videoDetails":{"videoId":'
_JSON_MARKERS = (
    ("external_channel_id", re.compile(rf'"externalChannelId":"({_CHANNEL_ID})"')),
    ("external_id", re.compile(rf'"externalId":"({_CHANNEL_ID})"')),
    ("channel_id", re.compile(rf'"channelId":"({_CHANNEL_ID})"')),
)
_CHANNEL_ID_JSON_RE = _JSON_MARKERS[2][1]


def _tags(html: str, name: str) -> list[dict[str, str]]:
    out = []
    for tag in _TAG_RE[name].findall(html):
        attrs = {}
        for key, dq, sq in _ATTR_RE.findall(tag):
            attrs[key.lower()] = dq if dq or not sq else sq
        out.append(attrs)
    return out


def _find_channel_id(html: str) -> tuple[str | None, str | None]:
    """Return ``(channel_id, marker_name)``. First hit wins, in order of
    how specific the marker is to the page's own channel. ``browseId``
    is never used: shelves on a page name other channels."""
    links = _tags(html, "link")
    for attrs in links:
        if attrs.get("rel", "").lower() == "alternate" and "rss" in attrs.get("type", "").lower():
            m = _RSS_HREF_RE.search(attrs.get("href", ""))
            if m:
                return m.group(1), "rss_link"
    for attrs in links:
        if attrs.get("rel", "").lower() == "canonical":
            m = _CHANNEL_HREF_RE.search(attrs.get("href", ""))
            if m:
                return m.group(1), "canonical"
    metas = _tags(html, "meta")
    for attrs in metas:
        if attrs.get("property", "").lower() == "og:url":
            m = _CHANNEL_HREF_RE.search(attrs.get("content", ""))
            if m:
                return m.group(1), "og_url"
    for attrs in metas:
        if attrs.get("itemprop", "") in ("channelId", "identifier"):
            m = _EXACT_ID_RE.fullmatch(attrs.get("content", ""))
            if m:
                return m.group(1), "itemprop"
    start = html.find(_VIDEO_DETAILS)
    if start != -1:
        # A watch page: the video's own channel sits inside
        # videoDetails, close after the video id. A window rather than a
        # brace match, because a title containing { or } would defeat it.
        m = _CHANNEL_ID_JSON_RE.search(html, start, start + 4000)
        if m:
            return m.group(1), "video_details"
    for name, pattern in _JSON_MARKERS:
        m = pattern.search(html)
        if m:
            return m.group(1), name
    return None, None


def channel_id_from_html(html: str) -> str | None:
    """The page's own channel id, or None. Pure."""
    return _find_channel_id(html)[0]


_BOT_CHECK_RE = re.compile(r"confirm you(?:\u2019|'|\\u2019|\\u0027)re not a bot", re.IGNORECASE)


def _blocked_host(host: str, path: str) -> bool:
    host = host.lower()
    return (
        host.endswith("consent.youtube.com")
        or host.endswith("consent.google.com")
        or host == "accounts.google.com"
        or (host in ("google.com", "www.google.com") and path.startswith("/sorry/"))
    )


def _blocked_rule(final_url: str, status: int, html: str) -> str | None:
    parsed = urlparse(final_url)
    if _blocked_host(parsed.hostname or "", parsed.path or ""):
        return "consent"
    if status == 429:
        return "rate_limit"
    if 'action="https://consent.youtube.com' in html or "Before you continue to YouTube" in html:
        return "consent"
    if _BOT_CHECK_RE.search(html):
        return "bot"
    return None


def is_blocked_page(final_url: str, status: int, html: str) -> bool:
    """True when YouTube answered with a consent page, a bot check or a
    rate limit instead of the page. A bare "Sign in to confirm" is not
    a rule: age-gated pages say that too. Pure."""
    return _blocked_rule(final_url, status, html) is not None


def _allowed_hop(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if _YOUTUBE_HOST_RE.search(host):
        return True
    if host in ("consent.google.com", "accounts.google.com"):
        return True
    return host in ("google.com", "www.google.com") and (parsed.path or "").startswith("/sorry/")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:100].strip("_")


def youtube_source_slug(original: str, feed_url: str) -> str:
    """A source name for a YouTube feed: ``<handle>_yt`` for handles,
    ``/c/``, ``/user/`` and ``?user=`` links; ``playlist_<id6>_yt`` for
    playlists; ``channel_<id6>_yt`` otherwise, including handles that
    are empty after slugifying (non-Latin). Pure. The frontend mirrors
    this in ``suggestName``."""
    feed_query = parse_qs(urlparse(feed_url).query)
    name = ""
    normalized = normalize_youtube_input(original, allow_bare_handle=True)
    if is_youtube_url(normalized):
        parsed = urlparse(normalized)
        segs = _segments(parsed.path or "")
        query = parse_qs(parsed.query)
        if segs and segs[0].startswith("@"):
            name = segs[0][1:]
        elif len(segs) >= 2 and segs[0].lower() in ("c", "user"):
            name = segs[1]
        elif query.get("user"):
            name = query["user"][0]
    if not name and feed_query.get("user"):
        name = feed_query["user"][0]
    slug = _slugify(name)
    if slug:
        return f"{slug}_yt"
    # Ids may contain "-", which source names don't allow.
    if feed_query.get("playlist_id"):
        return f"playlist_{_id6(feed_query['playlist_id'][0])}_yt"
    channel = (feed_query.get("channel_id") or [""])[0]
    return f"channel_{_id6(channel)}_yt"


def _id6(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", value[-6:].lower())


# --- network ----------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    jar = httpx.Cookies()
    # SOCS=CAI is the "reject all" consent value open-source YouTube
    # clients use to skip the EU consent wall. YouTube may stop
    # honouring it at any time; blocked-page detection plus the
    # channel-ID message is the fallback. Cookies sit on the jar, not
    # in a header, so they survive the youtube.com -> www -> consent
    # redirects (httpx drops a Cookie header on redirect).
    jar.set("SOCS", "CAI", domain=".youtube.com")
    jar.set("CONSENT", "YES+", domain=".youtube.com")
    return httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        cookies=jar,
        transport=_TRANSPORT,
        # Per-hop SSRF guard (same as rss.py's B7 fix): it fires before
        # every hop, so a redirect to a private or loopback address is
        # refused before the connection opens.
        event_hooks={"request": [ssrf_event_hook]},
    )


def _log(host: str, status: int, result: str) -> None:
    # Host, status and outcome only: never the query string or the body.
    logger.info("youtube: lookup host=%s status=%s result=%s", host, status, result)


async def _lookup(page_url: str) -> str:
    """Fetch one YouTube page and return the feed URL of its channel."""
    page_host = urlparse(page_url).hostname or ""
    safe, _reason = check_url_safe(page_url)
    if not safe:
        _log(page_host, 0, "blocked:ssrf")
        raise YouTubeResolutionError(MSG_UNREACHABLE)
    try:
        async with _client() as client, client.stream("GET", page_url, headers=_PAGE_HEADERS) as resp:
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk[: _MAX_BODY - len(buf)]
                if len(buf) >= _MAX_BODY:
                    break
            body = bytes(buf[:_MAX_BODY])
            chain = [str(r.url) for r in resp.history] + [str(resp.url)]
            final_url = str(resp.url)
            status = resp.status_code
    except httpx.HTTPError as exc:
        if str(exc).startswith("SSRF guard aborted"):
            _log(page_host, 0, "blocked:ssrf")
            raise YouTubeResolutionError(MSG_UNREACHABLE) from None
        _log(page_host, 0, "network")
        raise YouTubeResolutionError(MSG_NETWORK) from None

    final_host = urlparse(final_url).hostname or ""
    if not all(_allowed_hop(u) for u in chain):
        _log(final_host, status, "blocked:host")
        raise YouTubeResolutionError(MSG_UNEXPECTED_HOST)
    safe, _reason = check_url_safe(final_url)
    if not safe:
        _log(final_host, status, "blocked:ssrf")
        raise YouTubeResolutionError(MSG_UNREACHABLE)
    if status == 404:
        _log(final_host, status, "not_found")
        raise YouTubeNotFoundError(MSG_NOT_FOUND)
    if status == 429:
        _log(final_host, status, "blocked:rate_limit")
        raise YouTubeBlockedError(MSG_BLOCKED)
    if status >= 400:
        _log(final_host, status, f"http_{status}")
        raise YouTubeResolutionError(_msg_http_error(status))
    parsed_final = urlparse(final_url)
    if _blocked_host(final_host, parsed_final.path or ""):
        _log(final_host, status, "blocked:consent")
        raise YouTubeBlockedError(MSG_BLOCKED)

    html = body.decode("utf-8", errors="replace")
    # Markers before the blocked-page check, so an age-gated watch page
    # ("Sign in to confirm your age") that still names its channel works.
    channel_id, marker = _find_channel_id(html)
    if channel_id:
        _log(final_host, status, marker or "marker")
        return _channel_feed(channel_id)
    rule = _blocked_rule(final_url, status, html)
    if rule:
        _log(final_host, status, f"blocked:{rule}")
        raise YouTubeBlockedError(MSG_BLOCKED)
    _log(final_host, status, "no_marker")
    raise YouTubeResolutionError(MSG_NO_MARKER)


async def _user_fallback(name: str) -> str:
    """When YouTube blocks the /user/ page, the legacy user feed may
    still answer. Accept it only when it returns at least one item."""
    from app.sources.rss import fetch_rss  # lazy: avoids an import cycle at startup

    feed_url = _user_feed(name)
    try:
        items = await fetch_rss(feed_url)
    except (httpx.HTTPError, ValueError):
        items = []
    if items:
        _log("www.youtube.com", 200, "fallback:user_ok")
        return feed_url
    _log("www.youtube.com", 0, "fallback:user_failed")
    raise YouTubeBlockedError(MSG_BLOCKED)


async def resolve_channel_feed_url(url: str, *, allow_bare_handle: bool = False) -> str:
    """Resolve ``url`` to a YouTube feed URL if it is any shape of
    YouTube link (or a bare channel id, or a bare @handle when
    ``allow_bare_handle``); otherwise return it unchanged. Safe to call
    on every source URL before validation: a no-op for anything that
    isn't YouTube (mirrors ``apple_podcasts.resolve_feed_url``).

    Raises YouTubeResolutionError (or a subclass) with a message that
    can be shown to the user as it is.
    """
    normalized = normalize_youtube_input(url, allow_bare_handle=allow_bare_handle)
    if not is_youtube_url(normalized):
        return url
    kind, target = _classify(normalized)
    if kind == "feed":
        return target
    try:
        return await _lookup(target)
    except YouTubeBlockedError:
        segs = _segments(urlparse(target).path or "")
        if len(segs) == 2 and segs[0] == "user":
            return await _user_fallback(segs[1])
        raise
