"""leisure_feed_recommendations: diversify the recommended-feeds pool
beyond work-adjacent categories

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-22 00:30:00.000000

The editorial seed (migration 0017) only covered tech / news / vulns /
science / finance / policy / longform / deals — every one of those
reads as "work reading" for anyone in tech (especially the vulns and
finance rows). This adds five new leisure categories (sports,
entertainment, gaming, food, music) so the Recommended tab has
something to offer that isn't shaped like a work feed. Reuses the
same ``source="editorial"`` convention as the 0017 seed — these are
immediately visible in the pool without depending on the LLM
discovery path (which needs a configured provider) at all, which
also directly addresses "the pool feels static": it grows the very
next time the app is deployed against this migration, no API key or
button click required.

Leans on Reddit feeds where reasonable (``r/sports``, ``r/movies``,
etc.) since that mechanism is already proven by the 0017 seed's
``reddit_*`` rows — same ``fetch_rss``/``dynamic_reddit`` path, no new
plugin code needed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEISURE_SEED: list[dict] = [
    {"name": "reddit_sports", "type": "reddit", "category": "sports", "url": "https://www.reddit.com/r/sports", "blurb": "r/sports — general sports news + discussion across leagues"},
    {"name": "espn_top", "category": "sports", "url": "https://www.espn.com/espn/rss/news", "blurb": "ESPN top headlines"},
    {"name": "reddit_movies", "type": "reddit", "category": "entertainment", "url": "https://www.reddit.com/r/movies", "blurb": "r/movies — news, trailers, discussion"},
    {"name": "reddit_television", "type": "reddit", "category": "entertainment", "url": "https://www.reddit.com/r/television", "blurb": "r/television — show news + episode discussion"},
    {"name": "variety", "category": "entertainment", "url": "https://variety.com/feed/", "blurb": "Variety — film/TV industry news"},
    {"name": "reddit_gaming", "type": "reddit", "category": "gaming", "url": "https://www.reddit.com/r/gaming", "blurb": "r/gaming — general gaming news + discussion"},
    {"name": "polygon", "category": "gaming", "url": "https://www.polygon.com/rss/index.xml", "blurb": "Polygon — games news, reviews, culture"},
    {"name": "ign_games", "category": "gaming", "url": "https://feeds.ign.com/ign/games-all", "blurb": "IGN — games news and reviews"},
    {"name": "reddit_food", "type": "reddit", "category": "food", "url": "https://www.reddit.com/r/food", "blurb": "r/food — food pics, recipes, discussion"},
    {"name": "eater", "category": "food", "url": "https://www.eater.com/rss/index.xml", "blurb": "Eater — food + restaurant news"},
    {"name": "reddit_music", "type": "reddit", "category": "music", "url": "https://www.reddit.com/r/Music", "blurb": "r/Music — music news + discussion"},
    {"name": "pitchfork", "category": "music", "url": "https://pitchfork.com/rss/news/", "blurb": "Pitchfork — music news and reviews"},
]


def upgrade() -> None:
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
        for r in _LEISURE_SEED
    ]
    op.bulk_insert(candidates_table, rows)


def downgrade() -> None:
    names = [r["name"] for r in _LEISURE_SEED]
    candidates_table = sa.table(
        "feed_recommendation_candidates",
        sa.column("name", sa.String),
    )
    op.execute(candidates_table.delete().where(candidates_table.c.name.in_(names)))
