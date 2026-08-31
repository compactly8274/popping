"""Abstract base class for source plugins."""

from __future__ import annotations

import html
from abc import ABC, abstractmethod


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

    ``summary`` is lifted out of the passthrough bucket into its own
    top-level key so ``scheduler._embed_text`` can build the designed
    ``title + " — " + summary`` embedding text. Without this, the
    summary lands in ``meta["summary"]`` and every embedding ever
    generated was title-only — the personal scorer, the preference
    vector, and Framing Watch clustering all ran on a weaker signal
    than the design intended.

    A plugin's own ``meta`` dict is merged into the returned meta
    rather than nested under a ``"meta"`` key. Several plugins (HN,
    NVD, CISA KEV, GitHub releases, Wikipedia, dynamic Reddit)
    return items shaped ``{title, url, published_at, meta: {...}}``
    with their source-specific keys inside ``meta``. The previous
    "bucket every unknown key" passthrough nested that dict as
    ``meta["meta"]``, so every consumer reading top-level meta keys
    — ``scoring.engagement`` (``engagement_score`` /
    ``engagement_comments``), ``scheduler._cvss_score``
    (``cvss_score``), the routes layer's ``meta ->> 'reddit_thread_url'``
    projection, ``brief_filter.extract_summary`` — read nothing and
    silently no-op'd: HN/Reddit engagement contributed zero to the
    composite score (25% of its weight), high-CVSS CVE alerts never
    fired, and the "Discussed on Reddit" card footer never rendered.
    Merging instead of nesting restores every one of those paths with
    no per-plugin change. On a key collision the plugin's ``meta``
    value wins — a plugin explicitly setting ``meta.summary`` is a
    deliberate, source-specific statement and should not be clobbered
    by an implicit top-level key with the same name.
    """
    title = raw.get("title")
    url = raw.get("url")
    if not title or not url:
        raise ValueError(f"{name}: item missing title or url: {raw!r}")
    published_at = raw.get("published_at")
    # Pass every remaining top-level key through into meta, then flatten
    # a plugin's own nested ``meta`` dict on top (its keys win on
    # collision). ``image_url`` is popped by the ingest pipeline before
    # this dict reaches the DB, and ``summary`` is handled explicitly
    # below, so neither needs to be excluded here — but both must not
    # collide with the flattened meta. The dict-build below starts
    # from the passthrough bucket so a plugin that does NOT set its
    # own ``meta`` key (BBC via ``fetch_rss``'s top-level shape, RFD
    # via ``_rfd_normalize``) keeps the exact behavior it had before.
    passthrough = {
        k: v
        for k, v in raw.items()
        if k not in ("title", "url", "published_at", "summary", "meta")
    }
    plugin_meta = raw.get("meta")
    if isinstance(plugin_meta, dict):
        flattened = dict(passthrough)
        flattened.update(plugin_meta)
    else:
        flattened = passthrough
    return {
        "title": html.unescape(str(title).strip()),
        "url": str(url).strip(),
        "published_at": published_at,
        "summary": raw.get("summary"),
        "meta": flattened,
    }