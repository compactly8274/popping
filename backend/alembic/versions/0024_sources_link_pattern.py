"""sources.link_pattern optional page_links filter prefix

The ``generic_scrape`` source type's discovery chain has two
strategies: ``discover_sitemap_urls`` (page or direct sitemap
URL → trafilatura's sitemap_search) and, when that returns
nothing, ``page_links`` (fetch the page, extract ``<a href>``
links matching a user-specified path prefix).

This migration adds an optional ``sources.link_pattern`` Text
column. When set and the sitemap strategies both fail, the
plugin scrapes ``sources.url`` for matching links and follows
each through the standard per-URL extraction chain.

NULL keeps the original behavior. Path-prefix validation is
enforced at the route layer (must start with ``/`` and must
not be a full URL — same-origin safety check).

Indexing: skipped. The column is read on every plugin
``fetch()`` for ``type='generic_scrape'`` rows but the table
is small and the query is just ``WHERE id = :id`` (PK lookup
on the row the scheduler already loaded).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("link_pattern", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "link_pattern")