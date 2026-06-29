"""Row-driven Reddit per-subreddit plugin.

Mirrors ``DynamicRssPlugin`` but routes its fetch through the user's
Hydra Reddit client-server (see ``app.reddit_client``) instead of a
direct Reddit call. Dispatched by the scheduler when a ``Source`` row
has ``type="reddit"`` — i.e. one the user added via FeedManager's
``Add custom → Subreddit`` tab.

Why row-driven and not registered via ``@register_source``:
each subreddit is its own configurable feed (independent refresh
interval, can be paused/resumed individually, can be deleted). The
class-driven registry is for built-in surfaces that ship one instance
of each (HN top, BBC, etc.). Reddit subs are user content, so they
live as ``Source`` rows the same way user-added RSS rows do.

Failure modes follow the rest of the source plugins:
  - Missing Hydra URL (``settings.reddit_hydra_url == ""``): the
    underlying ``reddit_client.fetch_subreddit`` returns ``[]`` and
    the scheduler's normal "nothing new" path takes over. The user
    sees no Reddit row until they wire up Hydra.
  - Malformed subreddit slug (``r/foo/bar``, empty, weird chars):
    ``normalize_subreddit`` returns None and ``fetch`` short-circuits
    to ``[]`` so a bad row logs DEBUG and is skipped. Mirrors the
    "never raise" pattern of the other plugins.
  - Hydra outage (Hydra VPS down, 5xx, timeouts): ``fetch_subreddit``
    returns ``[]``; the next scheduled tick retries. No crashes.

The canonical engagement keys (``engagement_score`` /
``engagement_comments``) are stamped alongside the source-natural
``score`` / ``comments`` keys — same convention as HN. ``scoring/
engagement.py`` reads the canonical pair; the source-natural pair is
kept for the existing UI code that keys off HN's shape and any future
Reddit-specific card UI.
"""

from __future__ import annotations

import datetime as dt
import logging

from app.models import Source
from app import reddit_client
from app.sources.base import SourcePlugin
from app.sources.reddit import normalize_subreddit

logger = logging.getLogger("popping.sources.dynamic_reddit")


class DynamicRedditPlugin(SourcePlugin):
    """SourcePlugin bound to a single ``Source`` DB row of type ``reddit``.

    ``name``, ``type``, ``category``, ``url``, and
    ``refresh_interval_seconds`` are read off the row at construction
    time. The scheduler treats these as immutable for the lifetime
    of the job — if the row's refresh interval is updated, the
    scheduler rebuilds the job (reschedule) rather than mutate this
    instance in place.

    ``url`` is the human-entered subreddit reference — could be
    ``r/python`` (most common), ``/r/python``, or a full
    ``https://www.reddit.com/r/python`` URL. ``normalize_subreddit``
    parses it into a slug; on a parse failure ``fetch`` returns
    ``[]`` (logged DEBUG) so the next tick can retry after a row edit.
    """

    def __init__(self, source_row: Source) -> None:
        self._source_row = source_row
        # Mirror the relevant class attrs onto ``self`` so the base
        # contract (``plugin.url`` etc.) works without callers caring
        # whether they're looking at a class-driven or row-driven
        # plugin. ``type`` is whatever the row says (locked to
        # ``"reddit"`` by the route layer's type gate).
        self.name = source_row.name
        self.type = source_row.type  # "reddit"
        self.category = source_row.category
        self.url = source_row.url
        self.refresh_interval_seconds = source_row.refresh_interval_seconds

    async def fetch(self) -> list[dict]:
        # Parse the user-entered subreddit reference. Malformed inputs
        # (empty, wrong shape, weird chars) log DEBUG and return ``[]``
        # — matches the "never raise" pattern shared by every other
        # source plugin. The scheduler's normal empty-result path
        # handles the rest.
        sub = normalize_subreddit(self.url)
        if not sub:
            logger.debug(
                "dynamic_reddit: row %r has unparseable subreddit url: %r",
                self.name, self.url,
            )
            return []
        listings = await reddit_client.fetch_subreddit(sub, listing="hot", limit=50)
        if not listings:
            return []
        out: list[dict] = []
        for listing in listings:
            # Self-posts have ``url == ""``; the user-meaningful link
            # then is the thread itself, so fall back to the permalink
            # in that case. Otherwise the listing's outbound URL wins
            # (link posts to external sites).
            outbound_url = listing.get("url") or ""
            permalink = listing.get("permalink") or ""
            if not outbound_url:
                if permalink:
                    outbound_url = f"https://www.reddit.com{permalink}"
                else:
                    # Both empty — drop the row; it would 404 on tap.
                    continue
            # ``created_utc`` is a unix-seconds float. Reddit's API
            # uses float (sub-second precision), so we round-to-int
            # before fromtimestamp to keep datetime pure.
            created_raw = listing.get("created_utc")
            try:
                created_ts = int(float(created_raw)) if created_raw is not None else None
            except (TypeError, ValueError):
                created_ts = None
            published_at = (
                dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).isoformat()
                if created_ts
                else None
            )
            out.append({
                "title": listing.get("title") or "",
                "url": outbound_url,
                "published_at": published_at,
                "summary": "",  # Reddit listings don't ship body text
                "meta": {
                    # Legacy / source-natural keys (mirror HN shape).
                    "score": listing.get("score"),
                    "comments": listing.get("num_comments"),
                    "by": listing.get("author", ""),
                    # Subreddit context (lets the card UI tag "from
                    # r/python" without a second DB lookup).
                    "subreddit": listing.get("subreddit", sub),
                    # Reddit's stable per-post ID (fullname ``t3_xxx``).
                    "reddit_id": listing.get("id", ""),
                    # The thread's permalink (relative path).
                    "thread_permalink": permalink,
                    # Canonical engagement keys consumed by
                    # ``app.scoring.engagement``. Same data as the
                    # legacy keys above; the canonical pair is what
                    # ``composite.score`` reads.
                    "engagement_score": listing.get("score"),
                    "engagement_comments": listing.get("num_comments"),
                },
            })
        return out

    # ``normalize`` falls through to ``SourcePlugin.normalize`` →
    # ``validate_required`` (in ``base.py``). Our ``fetch`` already
    # emits contract-correct shapes (title + url are always set),
    # so no per-source override is needed. Keeping the default makes
    # the contract self-evident: anything custom would go here.

    @property
    def listing(self) -> str:
        # Default listing for the cross-ref/UI surfaces. Reddit
        # changes "hot" too often for a stable default in some
        # subs, but "hot" matches what most users expect from a
        # subreddit feed.
        return "hot"