"""feed_recommendation_candidates: DB-backed recommended-feeds pool

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-11 09:00:00.000000

Replaces the hardcoded ``RECOMMENDATIONS`` list in
``app/feed_recommendations.py`` with a table, so the pool can grow
(via ``app.feed_discovery``'s LLM-suggested rows) without a code
change + backend restart. This migration creates the table and seeds
it with the original 28 editorial rows, marked ``source="editorial"``
to distinguish them from future ``source="llm"`` rows.

``embedding`` starts NULL for every seeded row — the ranking path in
``app.feed_recommendations`` backfills it lazily on first read (same
as the old in-memory ``_CANDIDATE_EMBEDDINGS`` cache, just persisted
now instead of living only for the process lifetime).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The 28 editorial rows previously hardcoded in
# app/feed_recommendations.py's ``RECOMMENDATIONS`` list, carried over
# verbatim (including the CBC browser-UA header) so this migration is
# a pure storage-location change with zero behavior difference on
# upgrade.
_EDITORIAL_SEED: list[dict] = [
    {"name": "the_verge", "category": "tech", "url": "https://www.theverge.com/rss/index.xml", "blurb": "consumer-tech launches, reviews, policy"},
    {"name": "arstechnica", "category": "tech", "url": "https://feeds.arstechnica.com/arstechnica/index", "blurb": "deeper tech reporting; Ars' long-form is worth the read"},
    {"name": "techcrunch", "category": "tech", "url": "https://techcrunch.com/feed/", "blurb": "startups, funding rounds, founder interviews"},
    {"name": "lobsters", "category": "tech", "url": "https://lobste.rs/rss", "blurb": "curated tech discussion, fewer memes than HN"},
    {"name": "github_blog", "category": "tech", "url": "https://github.blog/feed/", "blurb": "GitHub product changes; Copilot / Actions news"},
    {"name": "reuters_top", "category": "news", "url": "https://feeds.reuters.com/Reuters/worldNews", "blurb": "Reuters world wire — wire-service neutrality"},
    {"name": "the_guardian_world", "category": "news", "url": "https://www.theguardian.com/world/rss", "blurb": "Guardian world — long-running international coverage"},
    {
        "name": "cbc_top",
        "category": "news",
        "url": "https://www.cbc.ca/cmlink/rss-topstories",
        "blurb": "CBC Top Stories — browser UA pre-applied",
        "default_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        },
    },
    {"name": "nyt_world", "category": "news", "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "blurb": "NYT world (metered; RSS bypasses the wall)"},
    {"name": "al_jazeera", "category": "news", "url": "https://www.aljazeera.com/xml/rss/all.xml", "blurb": "Al Jazeera English — distinct framing from US/UK wires"},
    {"name": "economist", "category": "news", "url": "https://www.economist.com/finance-and-economics/rss.xml", "blurb": "Economist finance + economics feed"},
    {"name": "nasa_breakthrough", "category": "science", "url": "https://www.nasa.gov/news-release/feed/", "blurb": "NASA news releases — mission updates, discoveries"},
    {"name": "nature", "category": "science", "url": "https://www.nature.com/nature.rss", "blurb": "Nature — primary research highlights"},
    {"name": "arxiv_cs", "category": "science", "url": "https://export.arxiv.org/rss/cs", "blurb": "arXiv cs — daily CS preprints"},
    {"name": "marketwatch_top", "category": "finance", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "blurb": "MarketWatch top stories"},
    {"name": "ft_home", "category": "finance", "url": "https://www.ft.com/rss/home", "blurb": "Financial Times home (metered but useful headlines)"},
    {"name": "seekingalpha_headlines", "category": "finance", "url": "https://seekingalpha.com/feed.xml", "blurb": "Seeking Alpha headlines"},
    {"name": "krebs_on_security", "category": "vulns", "url": "https://krebsonsecurity.com/feed/", "blurb": "Krebs — investigations, breach writeups"},
    {"name": "schneier", "category": "vulns", "url": "https://www.schneier.com/feed/atom/", "blurb": "Schneier on Security — crypto + policy analysis"},
    {"name": "the_hacker_news", "category": "vulns", "url": "https://feeds.feedburner.com/TheHackersNews", "blurb": "The Hacker News — daily vulnerability roundups"},
    {"name": "sans_isc", "category": "vulns", "url": "https://isc.sans.edu/rssfeed_full.xml", "blurb": "SANS Internet Storm Center — handler diaries"},
    {"name": "eff", "category": "policy", "url": "https://www.eff.org/rss/updates.xml", "blurb": "EFF — digital-rights news and analysis"},
    {"name": "fcc_daily_digest", "category": "policy", "url": "https://www.fcc.gov/feeds/daily-digest", "blurb": "FCC Daily Digest — official telecom actions"},
    {"name": "longreads", "category": "longform", "url": "https://longreads.com/feed/", "blurb": "LongReads — picks of the week's best longform"},
    {"name": "stratechery", "category": "longform", "url": "https://stratechery.com/feed/", "blurb": "Stratechery — Ben Thompson on tech strategy"},
    {"name": "slickdeals_frontpage", "category": "deals", "url": "https://slickdeals.net/newsearch.php?mode=frontpage&rss=1", "blurb": "Slickdeals front page (rate-limited; refresh conservatively)"},
    {"name": "lwn_net", "category": "tech", "url": "https://lwn.net/headlines/rss", "blurb": "LWN.net — Linux / kernel / free-software deep coverage"},
    {"name": "rust_blog", "category": "tech", "url": "https://blog.rust-lang.org/feed.xml", "blurb": "Rust language blog — release notes, RFCs"},
    {"name": "reddit_python", "type": "reddit", "category": "tech", "url": "https://www.reddit.com/r/python", "blurb": "r/python — news, discussion, project showcases"},
    {"name": "reddit_programming", "type": "reddit", "category": "tech", "url": "https://www.reddit.com/r/programming", "blurb": "r/programming — language-agnostic dev discussion"},
    {"name": "reddit_machinelearning", "type": "reddit", "category": "tech", "url": "https://www.reddit.com/r/MachineLearning", "blurb": "r/MachineLearning — papers, course announcements, industry"},
    {"name": "reddit_technology", "type": "reddit", "category": "tech", "url": "https://www.reddit.com/r/technology", "blurb": "r/technology — broad tech news + discussion"},
    {"name": "reddit_news", "type": "reddit", "category": "news", "url": "https://www.reddit.com/r/news", "blurb": "r/news — top stories, mainstream aggregation"},
    {"name": "reddit_worldnews", "type": "reddit", "category": "news", "url": "https://www.reddit.com/r/worldnews", "blurb": "r/worldnews — international stories, heavy commentary"},
    {"name": "reddit_science", "type": "reddit", "category": "science", "url": "https://www.reddit.com/r/science", "blurb": "r/science — peer-reviewed discussion + new papers"},
]


def upgrade() -> None:
    op.create_table(
        "feed_recommendation_candidates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("blurb", sa.Text, nullable=False),
        sa.Column("type", sa.String(20), nullable=True),
        sa.Column("default_headers", postgresql.JSONB, nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="editorial"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column(
            "discovered_from_source_id",
            sa.Integer,
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    candidates_table = sa.table(
        "feed_recommendation_candidates",
        sa.column("name", sa.String),
        sa.column("category", sa.String),
        sa.column("url", sa.Text),
        sa.column("blurb", sa.Text),
        sa.column("type", sa.String),
        sa.column("default_headers", postgresql.JSONB),
        sa.column("source", sa.String),
    )
    rows = [
        {
            "name": r["name"],
            "category": r["category"],
            "url": r["url"],
            "blurb": r["blurb"],
            "type": r.get("type"),
            "default_headers": r.get("default_headers"),
            "source": "editorial",
        }
        for r in _EDITORIAL_SEED
    ]
    op.bulk_insert(candidates_table, rows)


def downgrade() -> None:
    op.drop_table("feed_recommendation_candidates")
