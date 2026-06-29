"""RedFlagDeals forum source — RSS shape, engagement-rich content.

RFD publishes per-forum RSS feeds (e.g. ``/feed/forum/9`` for Hot
Deals). The entries carry vote / reply / view counts that the generic
RSS normalizer drops on the floor, which is a real loss: a deal
that's gotten 200 thumbs up and 80 replies is qualitatively different
from one with zero interaction, and the composite scorer now has a
fourth component (``engagement``) that wants exactly that signal.

The feed ships engagement in its entry HTML summary as text like
"42 votes, 8 replies, 1.2k views". We extract those counts with three
targeted regexes and write whichever ones land. When nothing matches
the entry contributes zero engagement (which is correct — no signal
beats a guessed one).

The composite scorer's ``app.scoring.engagement`` module reads
canonical ``engagement_score`` and ``engagement_comments`` keys, so
we emit those plus the RFD-shaped names (``votes``, ``comments``,
``views``) for backwards compat with anything that already reads
``meta.votes``.

This module is also imported by ``dynamic_rss.py`` so that any
user-added ``Source`` row whose URL is an RFD feed gets engagement
extraction without needing to migrate ``type`` to a new value. The
class-driven plugin below is registered the normal way
(``@register_source``) so new users picking "Add RFD" from feed-
recommendations get the same treatment as built-ins.

NOTE: as of 2026 RFD started gating some feed endpoints behind a
Tollbit paywall (HTTP 402 from the canonical
``forums.redflagdeals.com`` host). When that happens the underlying
``fetch_rss`` raises and the scheduler logs the failure — engagement
extraction only runs on feeds that successfully fetched. Adding a
fallback would require a non-RSS source (scrape), which is out of
scope here.
"""

from __future__ import annotations

import logging
import re

from app.sources import register_source
from app.sources.base import SourcePlugin, validate_required
from app.sources.rss import fetch_rss

logger = logging.getLogger("popping.sources.rfd")


# --- Engagement extraction -----------------------------------------------

# RFD renders vote/reply counts into the entry's HTML summary as
# text like "42 votes, 8 replies, 1.2k views". The regexes below are
# intentionally conservative — match digits (with optional thousands
# separators), then a keyword, capture the FIRST occurrence of each
# kind. If the feed echoes the same number twice we don't double-count.
_VOTE_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:votes?|thumbs?\s*up|upvotes?|likes?)",
    re.IGNORECASE,
)
_REPLY_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:replies?|comments?|responses?)",
    re.IGNORECASE,
)
_VIEW_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:views?|reads?)",
    re.IGNORECASE,
)


def _strip_thousands(n: str) -> int:
    """``'1,234'`` → ``1234``. ``'1234'`` → ``1234``. Pure int."""
    return int(n.replace(",", ""))


def _first_int(pattern: re.Pattern[str], text: str) -> int | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return _strip_thousands(m.group(1))
    except ValueError:
        return None


def _extract_from_haystack(
    summary: str | None,
    content: str | None = None,
) -> tuple[int | None, int | None, int | None]:
    """Best-effort vote + reply + view counts from the entry's text.

    Returns ``(votes, replies, views)``; any may be ``None`` when the
    corresponding pattern didn't match. The regex runs against the
    raw HTML without stripping — ``42 votes`` lives inside text
    nodes, so HTML tags around it don't interfere.

    Why regex rather than reading feedparser's namespace elements?
    The shared ``fetch_rss`` path doesn't pass the feedparser entry
    through — it only ships title/url/summary/image_url. A plugin
    author who fetches the feed directly can call this with the
    entry's text and get the same extraction.
    """
    parts: list[str] = []
    if summary:
        parts.append(str(summary))
    if content:
        parts.append(str(content))
    haystack = " ".join(parts)
    if not haystack:
        return None, None, None
    return (
        _first_int(_VOTE_RE, haystack),
        _first_int(_REPLY_RE, haystack),
        _first_int(_VIEW_RE, haystack),
    )


def _summary_from_raw(raw: dict) -> tuple[str | None, str | None]:
    """Pull ``summary`` and ``content`` text out of a raw ``fetch_rss``
    item. ``fetch_rss`` passes ``summary`` as a string and ``content``
    as either a string or a list of ``{value, type}`` dicts (Atom
    style); we normalize to ``(summary_str, content_str)``."""
    summary = raw.get("summary")
    summary_str = summary if isinstance(summary, str) else None

    content = raw.get("content")
    content_str: str | None = None
    if isinstance(content, str):
        content_str = content
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            content_str = str(first.get("value") or "") or None
    return summary_str, content_str


def _is_rfd_url(url: str) -> bool:
    """True when ``url`` points at a RedFlagDeals forum RSS endpoint.

    Used by ``dynamic_rss.py`` to decide whether to apply RFD-style
    engagement extraction to a user-added row. Match is on host
    substring rather than the full URL because RFD has shifted hosts
    (forums.redflagdeals.com → tollbit.redflagdeals.com, etc.) over
    the years and the path component varies by forum id.
    """
    if not url:
        return False
    return "redflagdeals.com" in url.lower()


# --- Plugin classes ------------------------------------------------------


class _RssBase(SourcePlugin):
    """Common fetch() for RSS-shape sources. Subclasses pick the
    normalizer (default for vanilla RSS, RFD override for engagement
    extraction). Mirrors ``_RssPlugin`` in ``rss.py`` but lives here
    so the RFD-specific bits can live in the same module without
    dragging engagement logic into the BBC plugin."""

    type = "rss"
    category = "deals"

    async def fetch(self) -> list[dict]:
        return await fetch_rss(self.url)


@register_source
class RfdHotDeals(_RssBase):
    """Built-in RFD Hot Deals source. Points at the canonical
    ``/feed/forum/9`` endpoint, which is the forum ID for "Hot Deals".
    Other forums (e.g. Freebies, Electronics) ship at ``/feed/forum/<id>``
    and can be added through the UI as additional dynamic sources —
    they'll get the same engagement treatment via the URL sniff in
    ``dynamic_rss.normalize``.
    """
    name = "rfd_hot_deals"
    url = "https://forums.redflagdeals.com/feed/forum/9"
    refresh_interval_seconds = 900  # 15 min — deals move fast

    def normalize(self, raw: dict) -> dict:
        return _rfd_normalize(self.name, raw)


def rfd_normalize_dynamic(name: str, raw: dict) -> dict:
    """Normalizer for dynamic ``Source`` rows whose URL is RFD-shaped.

    ``dynamic_rss.py`` calls this after detecting ``redflagdeals.com``
    in the row URL. We don't override ``DynamicRssPlugin.normalize``
    directly because that would couple the dynamic path to the RFD
    module — a free function keeps the dependency arrow pointing
    from dynamic → rfd (one direction).
    """
    return _rfd_normalize(name, raw)


def _rfd_normalize(name: str, raw: dict) -> dict:
    """Shared normalize() for both the class-driven plugin and dynamic
    RFD rows. Validates required keys (delegated to the base) and then
    layers on engagement extraction.

    Why ``validate_required`` and not ``base = SourcePlugin()``: the
    base class declares ``fetch`` as ``@abstractmethod`` so direct
    instantiation raises ``TypeError``. Calling the free function
    gets the same contract — title/url required — without needing a
    plugin instance. (Previously the indirection called
    ``SourcePlugin().normalize(...)`` which crashed on every entry;
    that bug shipped zero entries per RFD tick.)
    """
    normalized = validate_required(name, raw)

    summary, content = _summary_from_raw(raw)
    votes, replies, views = _extract_from_haystack(summary, content)

    if votes is None and replies is None and views is None:
        # No engagement signal at all. Log at debug so the operator
        # can see whether the regex needs tuning for a particular
        # forum skin without spamming info-level logs every refresh.
        logger.debug(
            "rfd: %s — no engagement data found in %r",
            name, (raw.get("title") or "")[:80],
        )

    meta = dict(normalized.get("meta") or {})
    if votes is not None:
        # Canonical key consumed by ``app.scoring.engagement``. Same
        # data as the legacy key below; the canonical pair is what
        # ``composite.score`` reads.
        meta["engagement_score"] = votes
        # Legacy key for backwards compat (matches the HN plugin's
        # convention so existing UI / queries that read ``meta.votes``
        # keep working).
        meta["votes"] = votes
    if replies is not None:
        meta["engagement_comments"] = replies
        meta["comments"] = replies
    if views is not None:
        # Views don't drive engagement scoring today — there's no
        # canonical "engagement_views" key — but they're cheap to
        # keep in meta in case a future component wants them.
        meta["views"] = views

    return {
        "title": normalized["title"],
        "url": normalized["url"],
        "published_at": normalized["published_at"],
        "meta": meta,
    }