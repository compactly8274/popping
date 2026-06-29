"""Subreddit URL normalization helpers.

Accepts the variety of ways a user might enter a subreddit in the
``Add custom`` tab:

  - ``r/python``             — the natural shorthand, what most people type
  - ``/r/python``            — leading slash variant
  - ``https://www.reddit.com/r/python``         — full URL, www
  - ``https://reddit.com/r/python/hot``         — full URL with listing tail
  - ``reddit.com/r/python``                     — no scheme

Returns the subreddit slug (``"python"``) so the plugin can build
``https://www.reddit.com/r/{slug}`` and pass it to Hydra's
``/r/{sub}/{listing}`` endpoint.

Subreddit name constraints:
  - 3-21 characters
  - letters, digits, underscores
  - Reddit rejects anything outside this set; we mirror the constraint
    client-side so a malformed slug gets a clear error rather than a
    vague 404 from Hydra.

A bad / unknown input returns ``None``. The plugin's ``fetch`` treats
``None`` as "skip this row" (logs DEBUG, returns ``[]``) — matches the
"never raise" pattern of the rest of the source plugins.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

# Reddit's published limit on subreddit names. Mirroring here so a
# bad slug surfaces as "invalid subreddit" rather than "Reddit returned
# 404". Modulo a leading ``u/`` for user names, but we only deal with
# subreddits in this plugin.
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{3,21}$")


def normalize_subreddit(value: str) -> Optional[str]:
    """Parse ``value`` into a subreddit slug or return ``None`` on
    malformed input.

    Accepts: ``r/python``, ``/r/python``, full ``https://reddit.com/r/python``
    URLs (with or without trailing path segments), or a bare slug.
    Case-insensitive on input but returned lowercase — Reddit normalises
    subreddit names to lowercase server-side.
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None

    # Bare shorthand ``r/python`` or ``/r/python``.
    if s.startswith("/r/"):
        s = s[3:]
    elif s.startswith("r/") and not s.startswith("http"):
        s = s[2:]
    elif s.startswith("/"):
        s = s.lstrip("/")

    # Full URL? Extract the /r/<slug> path segment.
    if "://" in s or s.startswith("reddit.com"):
        url = s if "://" in s else f"https://{s}"
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        # Path is ``/r/<slug>`` possibly followed by more segments
        # (``/comments/...``, ``/hot``, ``/new``, etc.).
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2 or parts[0].lower() != "r":
            return None
        s = parts[1]

    # Strip any further path junk in case the input was ``r/python/hot``.
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]

    # Reject empty / too-short / too-long / out-of-set slugs up front.
    if not _SUBREDDIT_RE.match(s):
        return None

    return s.lower()


def is_reddit_url(value: str) -> bool:
    """True if ``value`` looks like it points at a Reddit URL or shorthand.
    Used by the source route to decide which URL validator to call —
    keeps the create endpoint's behaviour parallel to the per-subreddit
    plugin's input expectations."""
    if not value:
        return False
    s = value.strip().lower()
    if s.startswith("r/") or s.startswith("/r/"):
        return True
    if "reddit.com" in s:
        return True
    return False