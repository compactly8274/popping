"""entries.cached_summary_fetched_at — track when the cached_summary
was last populated, so re-ingest can invalidate stale entries.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-25 00:00:00.000000

Before this column, an entry whose source first published a
``meta.summary`` AFTER ``cached_summary`` was set to ``""``
stayed permanently empty: the cache contract was "asked but no
usable text" and the route never re-tried. The new column lets
``/api/entries/{id}/summary`` re-fetch when the row has been
ingested more recently than the cache was populated.

Pure additive change. Existing rows get NULL; the route's
backward-compat branch treats NULL the same as "fetched_at
matches entry.fetched_at" (i.e. trust the cache).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "entries",
        sa.Column("cached_summary_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: assume the existing cached_summary was populated at
    # the same time as the entry's last fetch. This gives the new
    # check the right semantics for all already-cached rows: a
    # re-ingest that touches entry.fetched_at will be considered
    # newer than cached_summary_fetched_at and trigger a re-fetch.
    op.execute(
        "UPDATE entries SET cached_summary_fetched_at = fetched_at "
        "WHERE cached_summary IS NOT NULL AND cached_summary_fetched_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("entries", "cached_summary_fetched_at")
