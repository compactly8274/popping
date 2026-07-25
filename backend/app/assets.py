"""Asset cache: download remote favicons/thumbnails once at ingest,
serve from a local StaticFiles mount.

Why local cache? Hot-path feeds render many entries per page; remote
fetches per render would hammer source CDNs and break offline. Cache
invalidates when the underlying Entry/Source row's remote URL changes
— the ingest pipeline overwrites the file in that case.

Atomicity: writes go to ``<dest>.<ext>.part`` first and are renamed
into place via ``os.replace`` so a partial write never leaves a
corrupt file served under a real name.

Failure handling: fetchers never raise. They log DEBUG and return
None / (None, None) so a transient network blip on one source can't
break ingest. The pipeline retries on the next ingest automatically
(rows keep their NULL image_path / favicon_url until success).

Favicon discovery: modern sites advertise their icon via
``<link rel="icon">`` (or apple-touch-icon / shortcut icon) in the
HTML ``<head>`` rather than serving a root ``/favicon.ico``. We probe
the homepage first, parse the link tags with a small ``re`` regex
(no bs4 dependency), and only fall back to ``/favicon.ico`` when no
link tag is found.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.assets")

_ASSETS_DIR = Path(settings.assets_dir)
_FAVICON_DIR = _ASSETS_DIR / "favicons"
_THUMBNAIL_DIR = _ASSETS_DIR / "thumbnails"

# Shared HTTP client. One client per process — connection pooling
# across fetches amortises the TCP/TLS handshake (a 50-item ingest
# would otherwise pay 150+ handshakes). Different headers/timeouts
# per call-site are passed as kwargs to ``client.stream`` rather
# than via separate clients. Set on lifespan startup; closed on
# lifespan teardown so connection pools don't leak across reloads.
_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    """Build the shared client. Idempotent — a second call replaces
    the existing client (and closes the old one)."""
    global _client
    if _client is not None:
        return
    # Per-call headers/timeout go through ``client.stream(..., headers=...)``
    # so the shared client only needs the defaults that are common to
    # every call (follow_redirects, base timeout).
    _client = httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _APP_UA},
    )


async def close_client() -> None:
    """Tear down the shared client. Called from FastAPI lifespan exit
    so connection pools don't leak across ``uvicorn --reload`` cycles."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _get_client() -> httpx.AsyncClient:
    if _client is None:
        # Defensive — if a route fires before lifespan startup (e.g.
        # during tests), build a one-off. The shared pool is the
        # common case.
        return httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _APP_UA},
        )
    return _client

# Favicons are tiny (5-15 KB); thumbnails can be 300-500 KB on news
# sites and 800+ KB on social-media embeds. Splitting the cap means a
# misbehaving source can't fill the volume, but legitimate thumbnails
# don't get silently truncated to "image failed".
_MAX_FAVICON_BYTES = 256 * 1024
# 2 MB. NYT / Verge / Wired hero images routinely land in the
# 800 KB-1.5 MB range; 1 MB silently dropped them and ``image_path``
# stayed NULL forever. The audit found this matches real-world
# payloads. Keep the streaming cap (don't trust Content-Length);
# just raise the threshold.
_MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
# Default per-stream timeout for the shared client's connection /
# read. Per-call overrides (``_FAVICON_TIMEOUT`` / ``_THUMBNAIL_TIMEOUT``)
# apply to the per-stream read window when set; this fallback covers
# the ``init_client`` and any test paths that don't override.
_TIMEOUT = 30.0
# Favicon stays at 10s (5-15 KB downloads are trivial).
# Thumbnail bumps to 30s because news CDNs routinely stream at
# 50 KB/s when cold — a 1.5 MB hero image needs a real read window.
# Using a single 30s blanket was misleading for the favicon path,
# so we split them: 10s for the small file, 30s for the large one.
_FAVICON_TIMEOUT = 10.0
_THUMBNAIL_TIMEOUT = 30.0
# Cap on the homepage HTML probe — icons never need more than the
# <head>. Keeps a chatty site from running us out of memory before we
# even see the link tags.
_HTML_PROBE_BYTES = 64 * 1024

# App-shaped UA — used for the favicon HTML probe and falls back to
# /favicon.ico. Most sites accept this without 403.
_APP_UA = "Popping/0.2 (+https://github.com/compactly8274/popping)"
# Browser-shaped UA — used for the actual image download. Cloudflare,
# Imgur, GitHub-raw and a handful of other CDNs 403 non-browser UAs
# for image fetches even when the same UA fetched the page HTML fine.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_IMAGE_ACCEPT = "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8"
_HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"

# Common image content-types → file extension. feedparser may hand us
# variants ("image/jpg", "image/x-icon"); cover the ones seen in the
# wild. Anything we don't recognise falls back to "bin" so we still
# cache the bytes rather than dropping them.
_EXT_BY_CT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/bmp": "bmp",
    "image/avif": "avif",
}

# Match a ``<link rel="…icon…" href="…">`` tag, tolerating attribute
# order and quoting style. Captures the rel + href values separately
# so we can rank candidates by rel quality (apple-touch-icon > icon >
# shortcut icon > anything with "icon" in the rel).
_LINK_ICON_RE = re.compile(
    r"""<link\b([^>]*?)\bhref=["']([^"']+)["']([^>]*?)/?>""",
    re.IGNORECASE | re.DOTALL,
)
_REL_ATTR_RE = re.compile(r"""\brel\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# Match any ``<meta ...>`` tag so we can inspect its attribute bag for
# og:image / twitter:image regardless of attribute order (some sites
# emit ``content`` before ``property``, others after) — same "capture
# the whole tag, search within it" approach ``_LINK_ICON_RE`` uses for
# favicons.
_META_TAG_RE = re.compile(r"""<meta\b([^>]*?)/?>""", re.IGNORECASE | re.DOTALL)
_META_PROPERTY_RE = re.compile(
    r"""\b(?:property|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_META_CONTENT_RE = re.compile(r"""\bcontent\s*=\s*["']([^"']*)["']""", re.IGNORECASE)

# Lower is better. og:image is the de-facto standard (set by nearly
# every CMS for link-preview cards); secure_url is a same-image HTTPS
# variant some sites also emit. twitter:image is the fallback for the
# minority of sites that only implement Twitter Cards.
_OG_IMAGE_PROPERTY_PRIORITY: dict[str, int] = {
    "og:image": 0,
    "og:image:secure_url": 0,
    "og:image:url": 0,
    "twitter:image": 1,
    "twitter:image:src": 1,
}


def _ext_from_content_type(ct: str | None) -> Optional[str]:
    """Map a Content-Type header to a file extension. Returns None for
    content types we won't store — the /assets mount serves files
    same-origin under the dashboard's origin, so a non-image
    response saved as ``<id>.html`` would render as HTML in the
    browser and execute embedded scripts (XSS via favicon cache).
    Anything that isn't on the image allowlist returns None and the
    caller aborts the download."""
    if not ct:
        return None
    # Strip params: "image/png; charset=utf-8" → "image/png".
    bare = ct.split(";", 1)[0].strip().lower()
    if bare in _EXT_BY_CT:
        return _EXT_BY_CT[bare]
    # Reject anything that's not on the image allowlist — even
    # ``mimetypes.guess_extension`` results. ``text/html`` would map
    # to ``.html``, ``application/javascript`` to ``.js``, etc.;
    # allowing those through the cache turns a benign asset fetch
    # into a stored XSS vector. If a CDN serves a real favicon
    # behind a generic content-type, it still fails the allowlist
    # and the asset is skipped — a small loss vs. an XSS hole.
    return None


def _origin(url: str) -> Optional[str]:
    """scheme://host[:port] — used to derive a source's /favicon.ico and
    to resolve relative ``<link>`` hrefs against the page URL."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def _link_rel_priority(rel: str) -> int:
    """Lower is better. Favicon discovery preference:
       0 = exact rel="icon" (modern, explicit, preferred)
       1 = any rel containing "icon" as a token (catches
           rel="icon shortcut", rel="apple-touch-icon", etc.)
       99 = rel doesn't contain "icon" — caller filters these out
            upstream, so this branch only exists as a safety net.
    """
    r = rel.strip().lower()
    if r == "icon":
        return 0
    if "icon" in r:
        # Demoted below the canonical rel="icon" so a noisy site
        # with multiple link tags still picks the canonical one.
        return 1
    return 99


async def _pick_favicon_url(page_url: str) -> Optional[str]:
    """Fetch ``page_url`` and pick the best favicon URL from its
    ``<head>`` link tags. Returns None if no link tag is found OR the
    HTML fetch itself fails.

    Never raises — caller falls back to ``/favicon.ico`` on None.
    """
    # SSRF guard. The page URL was user-supplied (a Source row) or
    # scraped from one (a thumb URL in an RSS feed). Reject anything
    # that resolves to a private / loopback / link-local range
    # *before* we open the socket. Defense in depth — the route
    # layer's validator catches the obvious case at POST/PATCH time
    # but a feed whose RSS XML points at a 10.x image wouldn't be
    # stopped by that.
    safe, _reason = check_url_safe(page_url)
    if not safe:
        logger.debug("assets: HTML probe rejected: %s", page_url)
        return None
    try:
        client = _get_client()
        async with client.stream(
            "GET", page_url, headers={"Accept": _HTML_ACCEPT},
        ) as resp:
            if resp.status_code >= 400:
                logger.debug("assets: %s → HTTP %s (icon probe)", page_url, resp.status_code)
                return None
            # Re-check the FINAL URL after the redirect chain — same
            # per-hop guard as ``_download``. The entry-time check
            # above only covers ``page_url`` itself; the shared
            # client follows redirects, so a chain like
            # ``public.example.com → 169.254.169.254/`` clears entry
            # (public host) but must be caught here (private hop).
            final_url = str(resp.url)
            if final_url != page_url:
                final_safe, _reason = check_url_safe(final_url)
                if not final_safe:
                    logger.debug(
                        "assets: %s redirected to denied host %s", page_url, final_url,
                    )
                    return None
            ct = resp.headers.get("content-type") or ""
            if "html" not in ct.lower() and "xml" not in ct.lower():
                # RSS / JSON / etc — no <link> tags. Don't waste
                # bytes parsing it.
                return None
            buf = bytearray()
            async for chunk in resp.aiter_bytes(chunk_size=16 * 1024):
                buf.extend(chunk)
                if len(buf) >= _HTML_PROBE_BYTES:
                    break
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("assets: HTML probe %s failed: %s", page_url, exc)
        return None

    html = bytes(buf).decode("utf-8", errors="replace")

    best_url: Optional[str] = None
    best_priority = 999
    for match in _LINK_ICON_RE.finditer(html):
        href = match.group(2)
        if not href:
            continue
        # ``rel`` may be in either the pre-href or post-href attribute
        # bag — search the whole tag.
        rel_match = _REL_ATTR_RE.search(match.group(1) + " " + match.group(3))
        rel = (rel_match.group(1) if rel_match else "").strip().lower()
        if "icon" not in rel:
            continue
        # Resolve relative hrefs (``/icon.svg``, ``icon.png``) against
        # the page URL; absolute (``https://cdn/icon.png``) pass
        # through; protocol-relative (``//cdn/icon.png``) get a scheme.
        resolved = urljoin(page_url, href)
        priority = _link_rel_priority(rel)
        if priority < best_priority:
            best_url = resolved
            best_priority = priority
            if priority == 0:
                # Can't do better than a canonical rel="icon" — bail
                # out of the scan.
                break

    if best_url is None:
        logger.debug("assets: no <link rel=icon> in %s", page_url)
    return best_url


async def _pick_og_image_url(page_url: str) -> Optional[str]:
    """Fetch ``page_url`` and pick its og:image (or twitter:image)
    meta tag, if any. Returns None if no tag is found OR the HTML
    fetch itself fails — same shape and same SSRF guard as
    ``_pick_favicon_url``, reused as the fallback thumbnail source
    for entries whose feed didn't ship an image of its own (a large
    share of non-RSS sources — HN, GitHub Releases, CISA/NVD
    advisories — and plenty of RSS feeds that just don't bother with
    media:thumbnail).

    Never raises — caller (``fetch_og_image_fallback``) treats None
    as "no fallback available", same as a missing feed image.
    """
    safe, _reason = check_url_safe(page_url)
    if not safe:
        logger.debug("assets: og:image probe rejected: %s", page_url)
        return None
    try:
        client = _get_client()
        async with client.stream(
            "GET", page_url, headers={"Accept": _HTML_ACCEPT},
        ) as resp:
            if resp.status_code >= 400:
                logger.debug("assets: %s → HTTP %s (og:image probe)", page_url, resp.status_code)
                return None
            # Same per-hop SSRF re-check as the favicon probe — the
            # entry-time check above only covers ``page_url`` itself.
            final_url = str(resp.url)
            if final_url != page_url:
                final_safe, _reason = check_url_safe(final_url)
                if not final_safe:
                    logger.debug(
                        "assets: %s redirected to denied host %s", page_url, final_url,
                    )
                    return None
            ct = resp.headers.get("content-type") or ""
            if "html" not in ct.lower():
                # Not an article page (e.g. the "link" is itself a
                # PDF, a direct image, a video file) — no meta tags
                # to find.
                return None
            buf = bytearray()
            async for chunk in resp.aiter_bytes(chunk_size=16 * 1024):
                buf.extend(chunk)
                if len(buf) >= _HTML_PROBE_BYTES:
                    break
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("assets: og:image probe %s failed: %s", page_url, exc)
        return None

    html = bytes(buf).decode("utf-8", errors="replace")
    best_url = _extract_og_image_url(html, page_url)
    if best_url is None:
        logger.debug("assets: no og:image/twitter:image in %s", page_url)
    return best_url


def _extract_og_image_url(html: str, page_url: str) -> Optional[str]:
    """Pure parsing step: scan ``html`` for the best og:image /
    twitter:image meta tag and resolve it against ``page_url``.
    Split out from ``_pick_og_image_url`` so the regex/priority logic
    is unit-testable without a network fetch.
    """
    best_url: Optional[str] = None
    best_priority = 999
    for match in _META_TAG_RE.finditer(html):
        attrs = match.group(1)
        prop_match = _META_PROPERTY_RE.search(attrs)
        if not prop_match:
            continue
        prop = prop_match.group(1).strip().lower()
        priority = _OG_IMAGE_PROPERTY_PRIORITY.get(prop)
        if priority is None or priority >= best_priority:
            continue
        content_match = _META_CONTENT_RE.search(attrs)
        content = content_match.group(1).strip() if content_match else ""
        if not content:
            continue
        best_url = urljoin(page_url, content)
        best_priority = priority
        if priority == 0:
            break
    return best_url


async def _download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    timeout: Optional[float] = None,
    headers: Optional[dict] = None,
) -> Optional[str]:
    """GET ``url`` and write to ``dest``. Returns the relative path under
    the assets root on success, None on failure.

    Streams the body and aborts past ``max_bytes`` so we never trust
    the Content-Length header (which can lie or be missing).

    ``timeout`` / ``headers`` override the shared client's defaults
    on a per-call basis — favicon probes use a 10s read window while
    thumbnails need 30s; image fetches set a browser UA and image/*
    Accept header to bypass CDN 403s. ``dest`` is a path WITHOUT an
    extension — we derive the extension from the response's
    Content-Type (e.g. "favicons/3" → "favicons/3.png").

    SSRF: the URL is allowlist-checked at entry AND at every redirect
    hop. ``httpx`` exposes the final URL on the response after
    ``follow_redirects=True`` has walked the chain; we re-check that
    final URL in case any hop redirected to a private / loopback
    address. The hop-by-hop check is what catches the
    ``public.example.com → 127.0.0.1/admin`` attack that the entry
    check alone would miss.
    """
    # Entry-time SSRF check. The URL is user-controlled (favicon
    # link from RSS / scraped <link> tag) so a sloppy check at the
    # route layer isn't enough — the user-supplied Source URL has
    # already been validated, but ``_download`` is also called for
    # thumbnail URLs that came from the feed's XML.
    safe, _reason = check_url_safe(url)
    if not safe:
        logger.debug("assets: %s rejected by URL safety check", url)
        return None
    try:
        async with client.stream(
            "GET", url, follow_redirects=True,
            headers=headers, timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                logger.debug("assets: %s → HTTP %s", url, resp.status_code)
                return None
            # Re-check the FINAL URL after the redirect chain. This
            # is the per-hop SSRF guard — a chain
            # ``public.example.com → 192.168.0.1/admin`` clears
            # entry (public host) but fails here (private hop).
            # ``resp.url`` reflects the last hop's URL; ``httpx``
            # only sets it once the response object exists, so
            # checking it after the status_code branch is fine.
            final_url = str(resp.url)
            if final_url != url:
                final_safe, _reason = check_url_safe(final_url)
                if not final_safe:
                    logger.debug(
                        "assets: %s redirected to denied host %s", url, final_url,
                    )
                    return None
            ct = resp.headers.get("content-type")
            ext = _ext_from_content_type(ct)
            if ext is None:
                # Reject anything that isn't on the image allowlist.
                # The /assets mount serves same-origin; saving an
                # HTML or JS response here would be a stored-XSS
                # vector (see _ext_from_content_type for the why).
                logger.debug("assets: %s rejected content-type %r", url, ct)
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + f".{ext}.part")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            n = 0
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=16 * 1024):
                        n += len(chunk)
                        if n > max_bytes:
                            f.close()
                            tmp.unlink(missing_ok=True)
                            logger.debug("assets: %s exceeds %d bytes", url, max_bytes)
                            return None
                        f.write(chunk)
                final = dest.with_name(dest.name + f".{ext}")
                os.replace(tmp, final)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            return f"{final.parent.name}/{final.name}"
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("assets: download %s failed: %s", url, exc)
        return None


async def fetch_favicon(source_url: str, source_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Discover + download the source's favicon.

    Discovery order:
      1. Probe the source's homepage with a small HTML fetch and
         extract the best ``<link rel="icon">`` (or apple-touch-icon
         / shortcut icon) href. Resolve relative/protocol-relative
         hrefs against the page URL.
      2. Fall back to ``{origin}/favicon.ico`` if the probe returns
         no link tag OR the HTML fetch fails.

    The favicon file lands at ``assets/favicons/<id>.<ext>`` and the
    extension is derived from the response Content-Type (ico, png,
    svg, etc.).

    Returns ``(remote_url, local_relative_path)``. Either or both may
    be None on failure — both None means "tried, gave up". Idempotent:
    re-running writes the same file on retry.
    """
    origin = _origin(source_url)
    if not origin:
        return None, None

    # Try the HTML link-tag probe first. If we find a candidate, that's
    # the URL we actually download from.
    chosen_url = await _pick_favicon_url(source_url)
    if chosen_url is None:
        chosen_url = f"{origin}/favicon.ico"

    # Browser UA + image/* Accept. Two separate attempts so a 403 from
    # the chosen URL can be retried with a fresh socket — some CDNs
    # rotate per-connection and a stale 200+403 sequence can look like
    # "the asset is fine" when it isn't. The shared client handles
    # connection pooling, so attempts share the same underlying pool.
    last_err: Optional[str] = None
    client = _get_client()
    for attempt in (1, 2):
        rel = await _download(
            client, chosen_url, _FAVICON_DIR / str(source_id),
            max_bytes=_MAX_FAVICON_BYTES,
            timeout=_FAVICON_TIMEOUT,
            headers={"User-Agent": _BROWSER_UA, "Accept": _IMAGE_ACCEPT},
        )
        if rel is not None:
            return (chosen_url, rel)
        last_err = "download returned None"
        if attempt == 1:
            logger.debug("assets: favicon %s attempt 1 failed; retrying", chosen_url)
    logger.debug("assets: favicon gave up after 2 attempts: %s (%s)", chosen_url, last_err)
    return None, None


async def fetch_thumbnail(remote_url: str, entry_id: int) -> Optional[str]:
    """Download an entry's thumbnail to ``assets/thumbnails/<id>.<ext>``.

    Returns the local relative path (e.g. ``"thumbnails/1234.jpg"``) on
    success, None on failure. Never raises. Uses the 2 MB cap so
    news-site thumbnails up to ~2 MB land (NYT, Verge, Wired routinely
    ship 800 KB-1.5 MB hero images); bigger images are skipped rather
    than truncated.
    """
    last_err: Optional[str] = None
    client = _get_client()
    for attempt in (1, 2):
        # Thumbnail path: 30s read window because news CDNs routinely
        # stream at 50 KB/s when cold — a 1.5 MB hero image needs a
        # real read window. Re-running on a freshly-opened socket
        # often clears a transient hiccup.
        rel = await _download(
            client, remote_url, _THUMBNAIL_DIR / str(entry_id),
            max_bytes=_MAX_THUMBNAIL_BYTES,
            timeout=_THUMBNAIL_TIMEOUT,
            headers={"User-Agent": _BROWSER_UA, "Accept": _IMAGE_ACCEPT},
        )
        if rel is not None:
            return rel
        last_err = "download returned None"
        if attempt == 1:
            logger.debug("assets: thumbnail %s attempt 1 failed; retrying", remote_url)
    logger.debug("assets: thumbnail gave up after 2 attempts: %s (%s)", remote_url, last_err)
    return None


async def fetch_og_image_fallback(article_url: str, entry_id: int) -> Optional[Tuple[str, str]]:
    """Best-effort thumbnail for an entry whose feed didn't supply
    ``image_url`` — probe the article's own page for an og:image (or
    twitter:image) meta tag and, if found, download it through the
    same ``fetch_thumbnail`` path a feed-supplied image uses.

    Returns ``(remote_url, local_relative_path)`` on success, None if
    no og:image tag was found or the download failed — the caller
    treats this exactly like "no image available", not an error. Two
    network round-trips (HTML probe, then the image itself) only
    happen for entries that would otherwise render with no thumbnail
    at all, so the extra cost only lands where it adds coverage.
    """
    og_url = await _pick_og_image_url(article_url)
    if og_url is None:
        return None
    local = await fetch_thumbnail(og_url, entry_id)
    if local is None:
        return None
    return og_url, local


def ensure_dirs() -> None:
    """Create the favicons/ and thumbnails/ directories. Called from
    lifespan startup so a fresh volume doesn't 404 the StaticFiles mount."""
    _FAVICON_DIR.mkdir(parents=True, exist_ok=True)
    _THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)


def delete_favicon(source_id: int) -> bool:
    """Unlink the cached favicon for a source row. Returns True when
    a file was removed, False when nothing was cached.

    The favicon file is keyed by source id (not URL), so the
    extension is the only thing that varies — ``<id>.png`` / ``.ico``
    / ``.svg`` / etc. Glob the directory for any extension; only
    files whose stem is exactly ``str(source_id)`` belong to this
    row, so a stray file with the same prefix won't be touched.

    Called from ``scheduler.update_source`` when the row's URL
    changes — the next ingest will redownload into the same path via
    ``os.replace`` (atomic overwrite, see ``_download``). Caller is
    expected to also clear ``favicon_url``/``favicon_path`` on the
    row; this helper only touches the filesystem.
    """
    stem = str(source_id)
    removed = False
    # ``iterdir`` is more portable than ``glob`` here — we're matching
    # exact stem + any suffix, so a glob of ``<stem>.*`` would also
    # catch ``12345.tmp`` style leftovers from a partial download.
    # Iterdir + name check is the precise form.
    try:
        for entry in _FAVICON_DIR.iterdir():
            if entry.is_file() and entry.name.split(".", 1)[0] == stem:
                entry.unlink(missing_ok=True)
                removed = True
    except OSError as exc:
        # Don't raise — a failed unlink shouldn't break the PATCH.
        # Logged at debug so an operator chasing a stale favicon sees
        # the cause without it spamming info-level logs.
        logger.debug("assets: delete_favicon(%d) failed: %s", source_id, exc)
    return removed