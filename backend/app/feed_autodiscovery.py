"""Auto-discover a feed for a URL the user pastes into "Add custom" —
either an existing RSS/Atom feed, or (when none exists) a sitemap-
derived list of article URLs suitable for the generic-scrape fallback
source type (``app.sources.generic_scrape``).

Uses trafilatura's own feed/sitemap discovery (``trafilatura.feeds``,
``trafilatura.sitemaps``) rather than reimplementing "search a page
for <link rel=alternate>" or "guess an external service's API shape"
ourselves — well-tested, actively maintained, and already a project
dependency (added in ``app.article_extract`` for full-article LLM
summaries).

SSRF note: trafilatura does its own internal HTTP fetching for
redirects and any secondary URLs it discovers along the way (a
candidate feed URL, a sitemap linked from robots.txt, etc.) — those
calls are NOT individually routed through
``app.url_safety.check_url_safe`` the way every other network call in
this codebase is, because trafilatura has no hook for injecting an
external URL check into its internal fetch layer. We check the
user-supplied entry URL before calling into trafilatura at all (blocks
the direct/obvious case), but accept the internal-fetch gap as a
lower-severity, lower-probability trade for a single-operator
dashboard where the URL always originates from the operator's own
deliberate input, not an adversarial multi-tenant path. The ONGOING
periodic re-fetch of any discovered article URL (the generic_scrape
plugin's scheduled polling, not this one-time discovery step) goes
through ``app.article_extract.fetch_article_text``, which DOES apply
the full SSRF guard on every single call — so the unguarded window is
only this one-shot, user-initiated discovery request.
"""

from __future__ import annotations

import asyncio
import logging

from app.sources.rss import fetch_rss
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.feed_autodiscovery")

_MAX_FEED_CANDIDATES = 5
_MAX_SITEMAP_URLS = 50

# trafilatura's own per-request download timeout — NOT unbounded, as
# the ``_TRAFILATURA_BUDGET`` comment below used to claim. It defaults
# to 30s (``trafilatura.settings`` DEFAULT.DOWNLOAD_TIMEOUT), which is
# a problem for us specifically: ``sitemap_search`` doesn't crawl a
# site with just one request — it tries robots.txt, several guessed
# common paths, and any nested sitemap-index references it finds,
# fetching each one in turn (see ``trafilatura.sitemaps.SitemapObject
# .fetch``, which calls ``fetch_url`` with no override). Our own
# ``_TRAFILATURA_BUDGET`` (30s) wraps that WHOLE multi-request crawl,
# so if trafilatura's internal per-request timeout is ALSO 30s, a
# single slow/hanging host among several candidates can consume the
# entire outer budget by itself — the rest of the crawl (which might
# have found something) never even gets a chance to run. Observed
# directly: a real site's sitemap crawl went completely silent for
# ~28s (one slow request eating nearly the whole 30s budget) before
# our outer wait_for cut it off.
#
# ``fetch_url`` resolves to a shared, mutable ConfigParser object when
# called without an explicit ``config=`` (which is how both
# ``sitemap_search`` and ``determine_feed``'s own fetch call it) — we
# tighten THAT shared object in place so every internal trafilatura
# request this module triggers is bounded to a few seconds instead of
# up to 30, leaving room in our outer budget for the multi-request
# crawl a real site's sitemap discovery actually needs.
_TRAFILATURA_DOWNLOAD_TIMEOUT_S = 8


def _tighten_trafilatura_download_timeout() -> None:
    import inspect

    from trafilatura.downloads import fetch_url as _trafilatura_fetch_url

    # ``config`` is a positional-or-keyword parameter, so its default
    # lives in ``__defaults__`` — but which slot depends on how many
    # other defaulted params precede it, which is exactly the kind of
    # detail a trafilatura version bump could silently shift. Using
    # ``inspect.signature`` instead of indexing ``__defaults__``
    # directly is robust to that: it resolves the actual bound
    # default object regardless of position.
    default_config = inspect.signature(_trafilatura_fetch_url).parameters["config"].default
    default_config.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(_TRAFILATURA_DOWNLOAD_TIMEOUT_S))


# Per-stage timeouts for the autodiscovery flow.
#
# The /api/sources/auto endpoint is synchronous from the user's
# perspective ("click Add Custom, wait for a toast"). The discovery
# chain — initial fetch_rss + trafilatura.find_feed_urls + a per-
# candidate fetch_rss loop — runs WITHOUT an outer endpoint
# timeout, so the worst case is unbounded (``fetch_rss`` uses
# ``_READ_TIMEOUT=60.0`` × 2 attempts = 120s per candidate).
# Observed: a slow CDN makes the endpoint hang 5-10 minutes, which
# the user reads as "the autofeed button is broken".
#
# Bounded here per stage so every link in the chain has a sane
# deadline. The numbers are chosen to be larger than the happy
# path (most feeds discover in <2s) but tight enough that a hung
# host gets returned-as-not-found within a small multiple of the
# happy-path wall time, not minutes:
#
#   _INITIAL_PROBE_BUDGET    10s  the user-supplied URL is a direct feed
#                              (fast probe; if the URL itself isn't a
#                              feed we move on quickly)
#   _TRAFILATURA_BUDGET      30s  one-shot internal HTTP for find_feed_urls
#                              / sitemap_search — a multi-request crawl
#                              (robots.txt, guessed paths, nested
#                              sitemap indexes), each request now
#                              capped at ``_TRAFILATURA_DOWNLOAD_TIMEOUT_S``
#                              (see above) so this budget covers
#                              several of them instead of one
#   _CANDIDATE_PROBE_BUDGET  20s  per-candidate fetch_rss; shorter than
#                              60s because we have many candidates and
#                              the diminishing-return on slow feeds is
#                              steep — if the first candidate takes
#                              >20s, the rest will too, and the user
#                              would rather see a fast "not a feed"
#                              than a 5-min wait
#   _DISCOVERY_TOTAL_BUDGET  60s  outer cap on the whole flow inside
#                              discover_feed_url itself; protects
#                              against the worst-case scenario where
#                              every candidate narrowly misses each
#                              per-stage cap and adds up
_INITIAL_PROBE_BUDGET = 10.0
_TRAFILATURA_BUDGET = 30.0
_CANDIDATE_PROBE_BUDGET = 20.0
_DISCOVERY_TOTAL_BUDGET = 60.0


async def discover_feed_url(page_url: str) -> str | None:
    """Return a working RSS/Atom feed URL for ``page_url``, or None
    if none could be found within the discovery time budget.

    Tries ``page_url`` itself first — the user may have pasted a feed
    URL directly, which is the common case and needs no discovery at
    all — then asks trafilatura for candidate feed URLs on the page
    and tries each in turn until one actually parses with at least
    one item. A candidate merely existing in the page's markup isn't
    enough to trust; feed-detection heuristics false-positive on
    stale/broken `<link>` tags often enough that "does it actually
    parse" is the only real test.

    Every external hop is wrapped in ``asyncio.wait_for`` so a
    host that hangs on connect, read, or any internal trafilatura
    call can't hold the request open for minutes. Timeouts are
    treated the same as a network failure — fall through to the
    next stage. The user-visible behavior is "took too long,
    not a feed after all"; see the per-stage budget module
    docstring for the rationale.
    """
    safe, reason = check_url_safe(page_url)
    if not safe:
        logger.info("feed_autodiscovery: %s rejected by URL safety check (%s)", page_url, reason)
        return None

    async def _probe_initial() -> str | None:
        """Phase 1: a quick probe of the user-supplied URL itself."""
        items = await fetch_rss(page_url)
        return page_url if items else None

    try:
        result = await asyncio.wait_for(_probe_initial(), _INITIAL_PROBE_BUDGET)
        if result:
            return result
    except asyncio.TimeoutError:
        logger.info(
            "feed_autodiscovery: initial probe of %s hit the %ss budget; "
            "falling through to trafilatura",
            page_url, _INITIAL_PROBE_BUDGET,
        )
    except Exception:  # noqa: BLE001 - not itself a feed; fall through to discovery
        pass

    async def _trafilatura_find() -> list[str]:
        # Lazy import — ``trafilatura`` is a heavy dep that loads lxml at
        # import time, so deferring to here keeps the module-load cost
        # off the request hot path when no one is using autofeed.
        from trafilatura.downloads import fetch_url
        from trafilatura.feeds import FeedParameters, determine_feed, get_hostinfo

        _tighten_trafilatura_download_timeout()

        # NOT trafilatura.feeds.find_feed_urls — despite the name, that
        # function does NOT return feed URLs found on the page. It
        # fetches the page, and if the page itself doesn't parse as a
        # feed, it locates a candidate feed (via determine_feed, same
        # as below), fetches THAT too, and returns the individual
        # ARTICLE links extracted from inside the feed — it's a "give
        # me article URLs, using feed discovery as the mechanism"
        # helper, not a "find the feed URL" helper. Feeding its output
        # to fetch_rss() (which is what happens below) means every
        # "candidate" is an individual article page, which never
        # parses as RSS/Atom — so this returned real candidates from
        # trafilatura but discover_feed_url still reported "not
        # found" for effectively every site whose homepage isn't
        # itself the feed (i.e. nearly every real site). Verified via
        # a local mock: find_feed_urls returned the mock feed's two
        # <item> links, not the feed.xml URL itself.
        #
        # determine_feed(html, params) is the actual "<link
        # rel=alternate>-and-common-paths" scanner find_feed_urls
        # calls internally before it goes and dereferences the
        # result — replicate exactly that first half (fetch the page,
        # build the same FeedParameters, call determine_feed) and
        # return ITS output, which is real feed URLs. Same to_thread
        # reasoning as before: fetch_url is synchronous and does its
        # own network I/O, so it runs on a worker thread; wait_for
        # (below) still bounds the total wait since trafilatura has
        # no internal timeout of its own.
        def _sync_determine_feed() -> list[str]:
            domain, baseurl = get_hostinfo(page_url)
            if domain is None:
                logger.info(
                    "feed_autodiscovery: get_hostinfo could not extract a "
                    "domain from %s — giving up before fetching",
                    page_url,
                )
                return []
            downloaded = fetch_url(page_url)
            if downloaded is None:
                # trafilatura's fetch_url swallows the underlying reason
                # (bot-blocked, TLS error, DNS failure, non-2xx status,
                # etc.) and just returns None — this is as much detail
                # as we get without reimplementing its fetch layer.
                logger.info(
                    "feed_autodiscovery: trafilatura could not fetch %s "
                    "(blocked, non-2xx, or a network error) — no page "
                    "content to scan for a <link rel=alternate> tag",
                    page_url,
                )
                return []
            params = FeedParameters(baseurl, domain, page_url, False, None)
            found = determine_feed(downloaded, params)
            logger.info(
                "feed_autodiscovery: determine_feed found %d candidate(s) "
                "for %s: %s",
                len(found), page_url, found[:_MAX_FEED_CANDIDATES],
            )
            return found

        return await asyncio.to_thread(_sync_determine_feed)

    try:
        candidates = await asyncio.wait_for(_trafilatura_find(), _TRAFILATURA_BUDGET)
    except asyncio.TimeoutError:
        logger.info(
            "feed_autodiscovery: determine_feed for %s hit the "
            "%ss budget; giving up",
            page_url, _TRAFILATURA_BUDGET,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - third-party discovery, never let it fail the request
        logger.info("feed_autodiscovery: determine_feed failed for %s: %s", page_url, exc)
        return None

    async def _probe_candidate(candidate: str) -> str | None:
        """Phase 2: probe a single trafilatura-discovered candidate."""
        items = await fetch_rss(candidate)
        return candidate if items else None

    for candidate in candidates[:_MAX_FEED_CANDIDATES]:
        try:
            result = await asyncio.wait_for(_probe_candidate(candidate), _CANDIDATE_PROBE_BUDGET)
            if result:
                return result
            logger.info(
                "feed_autodiscovery: candidate %s for %s fetched but "
                "didn't parse as a feed with any items; trying next candidate",
                candidate, page_url,
            )
        except asyncio.TimeoutError:
            # Log and try the next candidate. A single slow candidate
            # shouldn't take down the whole loop.
            logger.info(
                "feed_autodiscovery: candidate %s for %s hit the %ss budget; "
                "trying next candidate",
                candidate, page_url, _CANDIDATE_PROBE_BUDGET,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "feed_autodiscovery: candidate %s for %s raised %s; "
                "trying next candidate",
                candidate, page_url, exc,
            )
            continue
    return None


async def discover_sitemap_urls(page_url: str, limit: int = _MAX_SITEMAP_URLS) -> list[str]:
    """Return up to ``limit`` article-page URLs from ``page_url``'s
    sitemap, or an empty list if none is found or discovery fails.
    Used as the "this site has no native feed" fallback when adding a
    ``type="generic_scrape"`` source — see
    ``app.sources.generic_scrape``.

    Subject to the same ``_TRAFILATURA_BUDGET`` as the feed
    discovery path: a hung sitemap host returns ``[]``, not
    an unbounded wait, so the Add Custom endpoint can't hang on
    this hop either.
    """
    safe, reason = check_url_safe(page_url)
    if not safe:
        logger.info("feed_autodiscovery: %s rejected by URL safety check (%s)", page_url, reason)
        return []
    async def _trafilatura_sitemap() -> list[str]:
        # Lazy import — see ``_trafilatura_find`` above.
        from trafilatura import sitemaps as trafilatura_sitemaps

        _tighten_trafilatura_download_timeout()

        # Same to_thread reasoning as find_feed_urls above —
        # sitemap_search is synchronous and does its own network I/O.
        # It crawls MULTIPLE URLs internally (robots.txt, guessed
        # common paths, nested sitemap indexes) — see the
        # _TRAFILATURA_DOWNLOAD_TIMEOUT_S comment up top for why each
        # of those needs its own tight cap, not just the one this
        # wait_for applies to the whole crawl.
        return await asyncio.to_thread(trafilatura_sitemaps.sitemap_search, page_url)
    try:
        urls = await asyncio.wait_for(_trafilatura_sitemap(), _TRAFILATURA_BUDGET)
    except asyncio.TimeoutError:
        logger.info(
            "feed_autodiscovery: trafilatura.sitemap_search for %s hit the "
            "%ss budget; returning empty list",
            page_url, _TRAFILATURA_BUDGET,
        )
        return []
    except Exception as exc:  # noqa: BLE001
        logger.info("feed_autodiscovery: sitemap_search failed for %s: %s", page_url, exc)
        return []
    return urls[:limit]  # noqa: RET504 - explicit for the type checker; see dict-route equivalent
