"""sources.sitemap_url optional direct-sitemap field

The ``generic_scrape`` source type uses
``trafilatura.sitemaps.sitemap_search`` to discover article
URLs to scrape. The function does its own internal sitemap
discovery (robots.txt, guessed common paths, nested sitemap
indexes) when called with a homepage URL. On a number of
real sites (e.g. theprogress.com) the homepage's sitemap_index
is broken — 1100+ cross-domain entries that all return 404,
causing ``sitemap_search`` to give up — while a specific
section's sitemap is healthy and returns the actual article
URLs.

This migration adds an optional ``sources.sitemap_url`` Text
column. When set, the ``generic_scrape`` plugin skips
homepage-based discovery and parses the user-specified URL
directly. NULL keeps the original behavior for every existing
row, so this is a no-op for already-deployed sources.

Indexing: skipped. The column is read on every plugin
``fetch()`` but only for rows of ``type='generic_scrape'``;
an index over ``(type, sitemap_url IS NOT NULL)`` would help
if the table grew to millions of rows of mixed types, which
isn't the current scale and isn't worth the write cost on
every fetch-tick right now.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("sitemap_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "sitemap_url")
