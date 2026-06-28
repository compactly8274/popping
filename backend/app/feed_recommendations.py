"""Curated feed recommendations.

A hand-picked list of RSS/Atom feeds the user might want to add. The
FeedManager Drawer surface ("Recommended" tab) shows this list, minus
anything the user has already added. Adding fires ``POST /api/sources``
which uses the dynamic-RSS path — see ``backend/app/sources/dynamic_rss.py``.

Updating the list is a code change + backend restart. That's deliberate:
Phase 5's "personalization" IS the editorial pick. A DB-backed
recommendations table would let the user curate their own, which is
exactly what Phase 8 wires up once ``Interaction`` rows start landing.

Conventions:
    - All URLs are RSS / Atom feeds.
    - Names are unique, lowercase, [a-z0-9_]+ — matches the regex
      ``POST /api/sources`` validates against.
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
"""

from __future__ import annotations


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
        "name": "hackernews_best",
        "category": "tech",
        "url": "https://hnrss.org/best",
        "blurb": "HN front page filtered to high-vote posts",
    },
    {
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
        "name": "ap_top",
        "category": "news",
        "url": "https://feeds.feedburner.com/ap-topnews",
        "blurb": "Associated Press top stories",
    },
    {
        "name": "the_guardian_world",
        "category": "news",
        "url": "https://www.theguardian.com/world/rss",
        "blurb": "Guardian world — long-running international coverage",
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