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
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger("popping.assets")

_ASSETS_DIR = Path(settings.assets_dir)
_FAVICON_DIR = _ASSETS_DIR / "favicons"
_THUMBNAIL_DIR = _ASSETS_DIR / "thumbnails"

# Cap so a misbehaving source can't fill the volume. Favicons are
# typically 5-15 KB; thumbnails 30-80 KB; 256 KB is generous.
_MAX_BYTES = 256 * 1024
_TIMEOUT = 10.0
_USER_AGENT = "Popping/0.2 (+https://github.com/compactly8274/popping)"

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


def _ext_from_content_type(ct: str | None) -> str:
    if not ct:
        return "bin"
    # Strip params: "image/png; charset=utf-8" → "image/png".
    bare = ct.split(";", 1)[0].strip().lower()
    if bare in _EXT_BY_CT:
        return _EXT_BY_CT[bare]
    guess = mimetypes.guess_extension(bare)
    return guess.lstrip(".") if guess else "bin"


def _origin(url: str) -> Optional[str]:
    """scheme://host[:port] — used to derive a source's /favicon.ico."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> Optional[str]:
    """GET ``url`` and write to ``dest``. Returns the relative path under
    the assets root on success, None on failure.

    Streams the body and aborts past ``_MAX_BYTES`` so we never trust
    the Content-Length header (which can lie or be missing).

    ``dest`` is a path WITHOUT an extension — we derive the extension
    from the response's Content-Type (e.g. "favicons/3" → "favicons/3.png").
    """
    try:
        async with client.stream("GET", url, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                logger.debug("assets: %s → HTTP %s", url, resp.status_code)
                return None
            ct = resp.headers.get("content-type")
            # We accept any content-type here. The size cap is the
            # real safety against abuse — many CDNs return
            # application/octet-stream for .ico files so the content-
            # type filter would reject legitimate favicons.
            ext = _ext_from_content_type(ct)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + f".{ext}.part")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            n = 0
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=16 * 1024):
                        n += len(chunk)
                        if n > _MAX_BYTES:
                            f.close()
                            tmp.unlink(missing_ok=True)
                            logger.debug("assets: %s exceeds %d bytes", url, _MAX_BYTES)
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
    """Download the origin's /favicon.ico to ``assets/favicons/<id>.<ext>``.

    Returns ``(remote_url, local_relative_path)``. Either or both may be
    None on failure — both None means "tried, gave up". Idempotent:
    re-running writes the same file on retry.
    """
    origin = _origin(source_url)
    if not origin:
        return None, None
    remote = f"{origin}/favicon.ico"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        rel = await _download(client, remote, _FAVICON_DIR / str(source_id))
        if rel is None:
            return None, None
        return (remote, rel)


async def fetch_thumbnail(remote_url: str, entry_id: int) -> Optional[str]:
    """Download an entry's thumbnail to ``assets/thumbnails/<id>.<ext>``.

    Returns the local relative path (e.g. ``"thumbnails/1234.jpg"``) on
    success, None on failure. Never raises.
    """
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        return await _download(client, remote_url, _THUMBNAIL_DIR / str(entry_id))


def ensure_dirs() -> None:
    """Create the favicons/ and thumbnails/ directories. Called from
    lifespan startup so a fresh volume doesn't 404 the StaticFiles mount."""
    _FAVICON_DIR.mkdir(parents=True, exist_ok=True)
    _THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
