"""Curated feed recommendations.

A hand-picked list of feeds the user might want to add. The
FeedManager Drawer surface ("Recommended" tab) shows this list, minus
anything the user has already added. Adding fires ``POST /api/sources``
which uses the dynamic-source path — RSS rows go through
``backend/app/sources/dynamic_rss.py``; ``type="reddit"`` rows go
through ``backend/app/sources/dynamic_reddit.py``.

Updating the static list is a code change + backend restart. That's
deliberate: the curated list is the editorial seed; ``recommendations_for``
re-ranks it dynamically by interaction co-occurrence once the user
has accumulated engagement signals.

Conventions:
    - All URLs are RSS / Atom feeds, or canonical
      ``https://www.reddit.com/r/<sub>`` for ``type="reddit"`` rows.
    - Names are unique, lowercase, [a-z0-9_]+ — matches the regex
      ``POST /api/sources`` validates against. Reddit entries use
      the ``reddit_<sub>`` prefix so they're visually distinct
      from RSS rows in the source list.
    - Categories are loose; the backend stores them verbatim. The
      dashboard groups by category so an unexpected value just
      becomes its own column.
    - ``blurb`` is one line; the Drawer shows it under the name as
      the editorial "why this feed" rationale.
    - Built-in sources (``bbc_news``, ``hn_top``, etc.) are NOT in
      this list — the backend filter strips them anyway because the
      frontend tells us the active source names, but keeping them
      out of the source list avoids confusion if a future change
      drops the server-side filter.
    - Reddit entries are only useful when the user has wired up
      Hydra (``REDDIT_HYDRA_URL``). Without it, the per-subreddit
      plugin's ``fetch`` short-circuits to ``[]`` and the row
      appears in the Source list but produces no entries. The
      curator-shipped Reddit rows surface that gap visually
      because the user can see the empty column; they can then
      either configure Hydra or delete the row.

Ranking (``recommendations_for``):
    When the user has zero interaction rows we serve the list in its
    curated order. Once ``Interaction`` rows land, we aggregate per
    source-category scores from the last 30 days, negate
    ``thumb_down`` / ``never`` events, and squeeze through ``tanh``
    so a single hot category doesn't dominate the ordering. We then
    sort the candidates by ``score desc, curated_index asc`` so ties
    fall back to the editorial order and the list feels stable.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entry, Interaction, Source


# A tuple of (name, category, url, blurb). Storing as a list of dicts
# keeps the JSON serialization for the API endpoint trivial (each row
# becomes one response item) and lets us swap the implementation for
# a DB-backed one in Phase 8 without changing the route.
RECOMMENDATIONS: list[dict] = [
    # --- tech --------------------------------------------------------------
    {
        "name": "the_verge",
        "category": "tech",
        "url": "https://www.theverge.com/rss/index.xml",
        "blurb": "consumer-tech launches, reviews, policy",
    },
    {
        "name": "arstechnica",
        "category": "tech",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "blurb": "deeper tech reporting; Ars' long-form is worth the read",
    },
    {
        "name": "techcrunch",
        "category": "tech",
        "url": "https://techcrunch.com/feed/",
        "blurb": "startups, funding rounds, founder interviews",
    },
    {
        # NOTE: ``hackernews_best`` (hnrss.org/best) was previously
        # here but the upstream serves a Ubiquiti self-signed cert
        # on its public endpoint, so TLS verification fails for
        # any client without that private CA in its trust store.
        # Not something we can work around from this side. The
        # built-in ``hn_top`` plugin (Hacker News via the official
        # firebaseio.com API) is already registered and works;
        # users who specifically want the "best" filtered subset
        # can add a custom source pointing at the endpoint once
        # hnrss.org sorts their cert. The entry is intentionally
        # removed from the curated list rather than shipped with a
        # broken URL — surfacing a feed we know fails costs more
        # user trust than not recommending it at all.
        "name": "lobsters",
        "category": "tech",
        "url": "https://lobste.rs/rss",
        "blurb": "curated tech discussion, fewer memes than HN",
    },
    {
        "name": "github_blog",
        "category": "tech",
        "url": "https://github.blog/feed/",
        "blurb": "GitHub product changes; Copilot / Actions news",
    },
    # --- news --------------------------------------------------------------
    {
        "name": "reuters_top",
        "category": "news",
        "url": "https://feeds.reuters.com/Reuters/worldNews",
        "blurb": "Reuters world wire — wire-service neutrality",
    },
    {
        # NOTE: AP News has been the search for an alternate URL
        # since AP shut down their official RSS feeds in late 2017.
        # The legacy ``feeds.feedburner.com/ap-topnews`` (which used
        # to work via Feedburner's redirector) now resolves to a
        # 200-OK body that is just the move-to-new-host notice —
        # no actual feed. The community AWS mirror at
        # ``associated-press.s3-website-us-east-1.amazonaws.com``
        # is also dead (all files are 55-byte stubs). AP's own
        # ``apnews.com/hub/apf-topnews?format=xml`` is just the
        # HTML hub page (not a feed). No working public RSS exists
        # for AP today. Reuters World is the substitute; if AP
        # ships a real feed later, drop it back in here.
        "name": "the_guardian_world",
        "category": "news",
        "url": "https://www.theguardian.com/world/rss",
        "blurb": "Guardian world — long-running international coverage",
    },
    {
        # CBC's CDN hangs the connection when our default
        # ``Popping/0.2`` User-Agent identifies the request as a
        # scraper. The recommendation ships ``default_headers``
        # with a browser-shaped UA so the Add button is one-tap —
        # the frontend passes it through as ``custom_headers`` at
        # POST time. The cmlink URL is the canonical short link;
        # the 301 resolves to ``/webfeed/rss/rss-topstories``.
        "name": "cbc_top",
        "category": "news",
        "url": "https://www.cbc.ca/cmlink/rss-topstories",
        "blurb": "CBC Top Stories — browser UA pre-applied",
        # Mirror the UA used by ``app.assets._BROWSER_UA``. Kept in
        # sync by code review; the cost of drifting is just CBC
        # coming back blocked, but it's worth surfacing the
        # dependency in this comment. ``default_headers`` is read
        # only by the recommended-add path in FeedManager.
        "default_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        },
    },
    {
        "name": "nyt_world",
        "category": "news",
        "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "blurb": "NYT world (metered; RSS bypasses the wall)",
    },
    {
        "name": "al_jazeera",
        "category": "news",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "blurb": "Al Jazeera English — distinct framing from US/UK wires",
    },
    {
        "name": "economist",
        "category": "news",
        "url": "https://www.economist.com/finance-and-economics/rss.xml",
        "blurb": "Economist finance + economics feed",
    },
    # --- science / space ---------------------------------------------------
    {
        "name": "nasa_breakthrough",
        "category": "science",
        "url": "https://www.nasa.gov/news-release/feed/",
        "blurb": "NASA news releases — mission updates, discoveries",
    },
    {
        "name": "nature",
        "category": "science",
        "url": "https://www.nature.com/nature.rss",
        "blurb": "Nature — primary research highlights",
    },
    {
        "name": "arxiv_cs",
        "category": "science",
        "url": "https://export.arxiv.org/rss/cs",
        "blurb": "arXiv cs — daily CS preprints",
    },
    # --- finance / markets -------------------------------------------------
    {
        "name": "marketwatch_top",
        "category": "finance",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "blurb": "MarketWatch top stories",
    },
    {
        "name": "ft_home",
        "category": "finance",
        "url": "https://www.ft.com/rss/home",
        "blurb": "Financial Times home (metered but useful headlines)",
    },
    {
        "name": "seekingalpha_headlines",
        "category": "finance",
        "url": "https://seekingalpha.com/feed.xml",
        "blurb": "Seeking Alpha headlines",
    },
    # --- security / vulns (in addition to built-in NVD + CISA) -----------
    {
        "name": "krebs_on_security",
        "category": "vulns",
        "url": "https://krebsonsecurity.com/feed/",
        "blurb": "Krebs — investigations, breach writeups",
    },
    {
        "name": "schneier",
        "category": "vulns",
        "url": "https://www.schneier.com/feed/atom/",
        "blurb": "Schneier on Security — crypto + policy analysis",
    },
    {
        "name": "the_hacker_news",
        "category": "vulns",
        "url": "https://feeds.feedburner.com/TheHackersNews",
        "blurb": "The Hacker News — daily vulnerability roundups",
    },
    {
        "name": "sans_isc",
        "category": "vulns",
        "url": "https://isc.sans.edu/rssfeed_full.xml",
        "blurb": "SANS Internet Storm Center — handler diaries",
    },
    # --- policy / gov ------------------------------------------------------
    {
        "name": "eff",
        "category": "policy",
        "url": "https://www.eff.org/rss/updates.xml",
        "blurb": "EFF — digital-rights news and analysis",
    },
    {
        "name": "fcc_daily_digest",
        "category": "policy",
        "url": "https://www.fcc.gov/feeds/daily-digest",
        "blurb": "FCC Daily Digest — official telecom actions",
    },
    # --- longform / interesting -------------------------------------------
    {
        "name": "longreads",
        "category": "longform",
        "url": "https://longreads.com/feed/",
        "blurb": "LongReads — picks of the week's best longform",
    },
    {
        "name": "stratechery",
        "category": "longform",
        "url": "https://stratechery.com/feed/",
        "blurb": "Stratechery — Ben Thompson on tech strategy",
    },
    # --- deals (light) -----------------------------------------------------
    {
        "name": "slickdeals_frontpage",
        "category": "deals",
        "url": "https://slickdeals.net/newsearch.php?mode=frontpage&rss=1",
        "blurb": "Slickdeals front page (rate-limited; refresh conservatively)",
    },
    # --- open source / dev ecosystem ---------------------------------------
    {
        "name": "lwn_net",
        "category": "tech",
        "url": "https://lwn.net/headlines/rss",
        "blurb": "LWN.net — Linux / kernel / free-software deep coverage",
    },
    {
        "name": "rust_blog",
        "category": "tech",
        "url": "https://blog.rust-lang.org/feed.xml",
        "blurb": "Rust language blog — release notes, RFCs",
    },
    # --- Reddit (per-subreddit; requires REDDIT_HYDRA_URL) ---------------
    # The ``type: "reddit"`` discriminator is passed through to
    # ``POST /api/sources`` by the Recommended tab, which routes the
    # row to ``DynamicRedditPlugin`` instead of ``DynamicRssPlugin``.
    # ``url`` is the canonical Reddit thread URL — the route layer
    # also accepts ``r/python`` shorthand but we ship the full URL
    # here so the source list renders a uniform shape. Subreddits
    # are chosen for the same editorial reasons as the RSS rows:
    # general-purpose + a few focused ones that overlap with the
    # existing category structure (tech, news, science).
    {
        "name": "reddit_python",
        "type": "reddit",
        "category": "tech",
        "url": "https://www.reddit.com/r/python",
        "blurb": "r/python — news, discussion, project showcases",
    },
    {
        "name": "reddit_programming",
        "type": "reddit",
        "category": "tech",
        "url": "https://www.reddit.com/r/programming",
        "blurb": "r/programming — language-agnostic dev discussion",
    },
    {
        "name": "reddit_machinelearning",
        "type": "reddit",
        "category": "tech",
        "url": "https://www.reddit.com/r/MachineLearning",
        "blurb": "r/MachineLearning — papers, course announcements, industry",
    },
    {
        "name": "reddit_technology",
        "type": "reddit",
        "category": "tech",
        "url": "https://www.reddit.com/r/technology",
        "blurb": "r/technology — broad tech news + discussion",
    },
    {
        "name": "reddit_news",
        "type": "reddit",
        "category": "news",
        "url": "https://www.reddit.com/r/news",
        "blurb": "r/news — top stories, mainstream aggregation",
    },
    {
        "name": "reddit_worldnews",
        "type": "reddit",
        "category": "news",
        "url": "https://www.reddit.com/r/worldnews",
        "blurb": "r/worldnews — international stories, heavy commentary",
    },
    {
        "name": "reddit_science",
        "type": "reddit",
        "category": "science",
        "url": "https://www.reddit.com/r/science",
        "blurb": "r/science — peer-reviewed discussion + new papers",
    },
]


def recommendations_for(active_source_names: list[str]) -> list[dict]:
    """Strip recommendations the user has already added.

    ``active_source_names`` is the list of source ``name`` strings
    the user currently has rows for (built-in + dynamic). Filtering
    on the server side prevents a stale client from showing
    "Add BBC News" duplicates.

    Returns a fresh list (caller may mutate) in the curated order.
    Names are compared verbatim — the API endpoints also enforce
    lowercase so a case-mismatch can't slip through.
    """
    active = set(active_source_names or [])
    return [r for r in RECOMMENDATIONS if r["name"] not in active]


# Event types that subtract from a category's net score. Listed here
# once so the SQL stays readable; the route layer's InteractionType
# enum shares these names.
_NEGATIVE_TYPES = ("thumb_down", "never")

# Window for the co-occurrence aggregate. Matches the recency decay
# used elsewhere (brief scheduling, brief deduplication); a 30-day
# window means a one-off click from three months ago won't move the
# recommendation needle.
_LOOKBACK = timedelta(days=30)

# Divisor inside tanh: a raw score of 5 in a category maps to
# tanh(5/5) ≈ 0.76; 10 maps to tanh(2) ≈ 0.96. Larger values stay
# close to 1.0 so the ordering stays stable at the top.
_TANH_DIVISOR = 5.0


async def _category_scores(
    session: AsyncSession, user_id: str
) -> dict[str, float]:
    """Return ``{category: net_score}`` for the user, rolled up across
    the last ``_LOOKBACK`` window. ``net_score`` is the sum of
    ``+1`` per positive interaction and ``-1`` per ``thumb_down`` /
    ``never``. Categories with no engagement don't appear.

    Negative types live as constants so the SQL reads cleanly. We do
    the negation in SQL rather than fetching rows and folding in
    Python — cheaper at the typical engagement volume (hundreds per
    user, not millions).
    """
    cutoff = datetime.now(timezone.utc) - _LOOKBACK
    # Build the per-row sign once so the aggregate stays readable.
    # ``case`` translates to a portable ``CASE WHEN`` across both
    # SQLite (tests) and Postgres (prod); ``func.IF`` would be MySQL.
    sign = case(
        (Interaction.type.in_(_NEGATIVE_TYPES), -1.0),  # type: ignore[arg-type]
        else_=1.0,
    )
    net_expr = func.sum(sign)
    stmt = (
        select(Source.category.label("category"), net_expr.label("net_score"))
        .join(Entry, Entry.source_id == Source.id)
        .join(Interaction, Interaction.entry_id == Entry.id)
        .where(Interaction.user_id == user_id)
        .where(Interaction.created_at >= cutoff)
        .group_by(Source.category)
    )
    rows = (await session.execute(stmt)).all()
    return {row.category: float(row.net_score or 0.0) for row in rows}


def _squeeze(raw: float) -> float:
    """Map raw net-score to ``[0, 1]`` via ``tanh`` so a single hot
    category can't pull every recommendation toward it. Output is
    non-negative because all candidates with a net score below 0 are
    sorted by their (already clamped) value."""
    if raw <= 0:
        return 0.0
    return math.tanh(raw / _TANH_DIVISOR)


async def recommendations_for_user(
    session: AsyncSession,
    active_source_names: list[str],
    user_id: str,
) -> list[dict]:
    """Re-ranked recommendation list for a logged-in user.

    Filtering matches ``recommendations_for`` (skip already-added
    sources). Ranking uses the user's last-30-days interaction
    co-occurrence: candidates in a category the user engages with
    float up; candidates in a category the user has thumbed-down
    sink (or stay neutral if no signal). Ties fall back to the
    curated ``RECOMMENDATIONS`` order so the list is stable when
    the user has no data.

    Returns a fresh list — the caller may serialize it directly.
    """
    candidates = recommendations_for(active_source_names)
    if not candidates:
        return candidates

    raw_scores = await _category_scores(session, user_id)
    if not raw_scores:
        # No engagement yet — fall through to curated order, which is
        # already what ``recommendations_for`` returned.
        return candidates

    squeezed = {cat: _squeeze(score) for cat, score in raw_scores.items()}
    # Pre-compute the curated index so ties are deterministic.
    for idx, item in enumerate(candidates):
        item.setdefault("_curated_index", idx)
    # Sort by descending category score, then by curated index ascending.
    candidates.sort(
        key=lambda item: (-squeezed.get(item["category"], 0.0), item["_curated_index"])
    )
    # Strip the helper key before returning — the API serializer
    # doesn't know about it.
    for item in candidates:
        item.pop("_curated_index", None)
    return candidates