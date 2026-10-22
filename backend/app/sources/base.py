"""Abstract base class for source plugins."""

from __future__ import annotations

import html
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

# A stored-URL lookup ``scheduler._ingest`` hands to a plugin with
# ``SourcePlugin.takes_ingest_hints``: given candidate URLs, returns the
# ones already stored as entries. The scheduler runs the query, so the
# plugin stays DB-free and can skip those pages before fetching them.
KnownUrls = Callable[[list[str]], Awaitable[set[str]]]


# Errors a plugin's ``fetch`` raises for the scheduler to record as
# their own kind (``scheduler._classify_error``), so a feed's
# ``last_error`` says what is wrong and which fix applies, instead of a
# bare ``[error]`` or a silently empty success.


class NotAFeedError(ValueError):
    """The address answered, but with something that isn't a feed (an
    HTML page, JSON, an empty body). Recorded as ``[not a feed]``.

    A ``ValueError`` whose message says "parse", so the Test route
    (``routes/sources._test_source_impl``) still reports it as
    ``parse_error``."""


class NoArticleListError(Exception):
    """A website feed found no list of articles to read: no sitemap,
    no matching links on the page. Recorded as ``[no list]``, a setup
    state: it never counts toward ``error_count`` and never stops the
    feed, because the fix is to point the feed at a section."""


class SourcePlugin(ABC):
    """Contract every Popping source plugin implements.

    Class attributes declare identity / config. The instance method `fetch`
    returns raw items as plain dicts; the scheduler applies `normalize` to
    turn each dict into something ready to write to the entries table.

    Subclasses are typically stateless — `fetch` constructs any client it
    needs (httpx.AsyncClient etc.) inside the call. If a plugin needs
    persistent state (e.g. a session cookie), it lives on the instance and
    the scheduler constructs one instance per source.
    """

    # --- identity (required) ----------------------------------------------
    name: str = ""                       # unique registry key, e.g. "bbc_news"
    type: str = ""                       # "rss" | "api" | "scrape"
    category: str = ""                   # "news" | "deals" | "vulns" | ...
    url: str = ""                        # canonical feed/API/page URL
    refresh_interval_seconds: int = 3600  # default 1h
    # Set by row-driven (dynamic) plugins (DynamicRssPlugin,
    # DynamicRedditPlugin) to the backing Source row's id, so the
    # scheduler can compute the job id (``ingest:dynamic:<id>``) for
    # backoff rescheduling without a class-vs-instance branch at
    # every call site. None for class-driven built-ins, which use
    # ``ingest:<name>`` instead.
    source_id: int | None = None
    # Set by row-driven plugins to the address they were built to
    # fetch: the backing row's ``(url, sitemap_url, link_pattern)`` at
    # construction (``row_fetch_target``). A job keeps its plugin
    # instance until ``update_source`` replaces it, so after an address
    # change an older instance can still be fetching the old address;
    # ``scheduler._ingest`` compares this with the row it commits
    # against and discards a batch that came from an address the row
    # no longer has. None for class-driven built-ins, whose address
    # can't be edited; ``_ingest`` snapshots their row instead.
    fetch_target: tuple[str | None, str | None, str | None] | None = None
    # True for a plugin whose ``fetch`` takes the keyword arguments
    # ``known_urls`` (a ``KnownUrls`` lookup), ``max_undated``
    # (``ingest.max_undated_per_poll``) and ``backlog_before`` (the
    # date before which a page is old backlog on this poll, None on a
    # first fetch), which ``scheduler._ingest`` then passes. Only
    # ``GenericScrapePlugin`` does: it fetches a page per candidate, so
    # skipping stored ones before the fetch matters, and its items never
    # include stored URLs, so the ingest rules' undated cap has to be
    # applied inside it. Every other plugin's ``fetch`` takes no
    # arguments.
    takes_ingest_hints: bool = False

    @abstractmethod
    async def fetch(self) -> list[dict]:
        """Fetch raw items from the source.

        Each dict should at minimum contain `title`, `url`, `published_at`
        (ISO 8601 string or datetime). Other keys are passed through into
        Entry.meta as JSON.
        """
        raise NotImplementedError

    def normalize(self, raw: dict) -> dict:
        """Default normalizer: validate required keys, coerce types.

        Plugins can override to do source-specific cleanup (e.g. strip
        tracking params from URLs, decode HTML entities in titles).
        """
        return validate_required(self.name, raw)


def row_fetch_target(source_row) -> tuple[str | None, str | None, str | None]:
    """The address a ``Source`` row points a plugin at: its URL, sitemap
    URL and link pattern, the three fields whose change re-points a
    feed (see ``scheduler.update_source``). Every row-driven plugin
    records the full tuple, whatever its type, so it compares equal to
    the same tuple read from the row. ``getattr`` with a default keeps
    stand-in rows without the newer columns working."""
    return (
        source_row.url,
        getattr(source_row, "sitemap_url", None),
        getattr(source_row, "link_pattern", None),
    )


def validate_required(name: str, raw: dict) -> dict:
    """Validate the universal contract every entry must satisfy.

    Raises ``ValueError`` when ``title`` or ``url`` is missing. Returns
    the normalized dict on success. Lives as a free function (not a
    method) so callers that need validation without a plugin instance
    — ``_rfd_normalize`` in ``rfd.py``, future per-source normalizers —
    don't have to instantiate the abstract ``SourcePlugin`` (which
    raises ``TypeError`` because ``fetch`` is unimplemented). Keeping
    the same body as ``SourcePlugin.normalize`` means any change to
    the contract flows through both call sites.

    ``title`` is HTML-unescaped. Some feeds (WordPress-generated ones
    in particular — The Verge is one) double-encode their titles, so
    the XML parse only resolves the outer layer and leaves a literal
    ``&#8217;`` etc. sitting in the text (e.g. ``"Nomad&#8217;s
    accessories"`` instead of ``"Nomad's accessories"``). A no-op on
    a title that was already clean.

    A plugin's own ``meta`` dict is flattened into the returned meta
    rather than nested under a ``"meta"`` key. Several plugins (HN,
    NVD, CISA KEV, GitHub releases, Wikipedia, dynamic Reddit) return
    items shaped ``{title, url, published_at, summary, meta: {...}}``
    with their source-specific keys inside ``meta``. The previous
    "bucket every unknown top-level key" passthrough nested that dict
    as ``meta["meta"]``, so every consumer reading top-level meta
    keys — ``scoring.engagement`` (``engagement_score`` /
    ``engagement_comments``), ``scheduler._cvss_score``
    (``cvss_score``), the routes layer's ``meta ->> 'reddit_thread_url'``
    projection, ``brief_filter.extract_summary`` — read nothing and
    silently no-op'd: HN/Reddit engagement contributed zero to the
    composite score (25% of its weight), high-CVSS CVE alerts never
    fired, and the "Discussed on Reddit" card footer never rendered.
    Flattening restores every one of those paths with no per-plugin
    change. On a key collision the plugin's ``meta`` value wins —
    ``dict.update`` semantics — a plugin explicitly setting e.g.
    ``meta["summary"]`` is a deliberate, source-specific statement
    and should not be clobbered by the passthrough bucket. A non-dict
    ``meta`` value (no plugin ships one; defensive) stays in the
    passthrough under the ``meta`` key rather than being dropped.

    Plugins that do NOT set their own ``meta`` key (BBC via
    ``fetch_rss``'s top-level shape, RFD via ``_rfd_normalize``,
    generic_scrape) keep the exact behavior they had before —
    ``summary`` / ``image_url`` / ``audio_url`` land in meta via the
    passthrough bucket and the flatten step is a no-op.
    """
    title = raw.get("title")
    url = raw.get("url")
    if not title or not url:
        raise ValueError(f"{name}: item missing title or url: {raw!r}")
    published_at = raw.get("published_at")
    # Start from the full passthrough (title/url/published_at excluded —
    # they have their own return slots). When the item carries its own
    # nested ``meta`` dict, merge that dict's keys on top of the
    # passthrough (they win on collision) and drop the nested key so it
    # doesn't survive as a ``meta["meta"]`` blob. A non-dict ``meta``
    # value (no plugin ships one; defensive) stays in the passthrough
    # under the ``meta`` key like every other unknown top-level key —
    # ``get`` here (NOT ``pop``) is what preserves it. The previous
    # commit used ``pop``, which removes the key unconditionally and
    # silently dropped the value; the regression suite caught the
    # discrepancy in the backend-tests CI run.
    passthrough = {
        k: v
        for k, v in raw.items()
        if k not in ("title", "url", "published_at")
    }
    plugin_meta = passthrough.get("meta")
    if isinstance(plugin_meta, dict):
        del passthrough["meta"]
        passthrough.update(plugin_meta)
    return {
        "title": html.unescape(str(title).strip()),
        "url": str(url).strip(),
        "published_at": published_at,
        "meta": passthrough,
    }