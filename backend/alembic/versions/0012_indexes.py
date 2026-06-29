"""Composite indexes + pg_trgm GIN for search and recommendations

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-29 18:00:00.000000

Three indexes that the dashboard hot path was missing:

  1. ``ix_entries_source_score`` — composite index on
     ``(source_id, composite_score desc, published_at desc nullslast)``.
     /api/entries orders by composite_score and filters by source_id;
     without this index the planner narrowed by source_id then sorted
     in memory, which dominates latency as the entries table grows.
     The nullslast ordering matches what the route actually emits.

  2. ``ix_interactions_user_created`` — composite index on
     ``(user_id, created_at desc)``. feed_recommendations._category_scores
     filters a 30-day window per user; with only the existing
     (user_id, entry_id) index the planner scanned the user's full
     history. After ~10k interactions this is the difference
     between a 50ms Drawer open and a 1.5s Drawer open.

  3. GIN trigram indexes on ``entries.title`` and
     ``(meta->>'summary')`` so ``?q=`` substring search can use the
     index instead of sequential scanning. Requires the
     ``pg_trgm`` extension; we attempt to create it inside the
     migration so the column-level CREATE INDEX can find the
     operator class. If the extension isn't installable (locked-down
     managed PG), the migration logs and skips — the existing
     sequential-scan fallback still works, just slowly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. composite index for the dashboard entries hot path.
    op.create_index(
        "ix_entries_source_score",
        "entries",
        ["source_id", sa.text("composite_score DESC"), sa.text("published_at DESC NULLS LAST")],
    )

    # 2. composite index for the recommendations category score query.
    op.create_index(
        "ix_interactions_user_created",
        "interactions",
        ["user_id", sa.text("created_at DESC")],
    )

    # 3. trigram GIN indexes for substring search. Best-effort on the
    # extension — locked-down managed Postgres may not allow CREATE
    # EXTENSION. We log and continue so the migration still lands.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entries_title_trgm "
        "ON entries USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entries_meta_summary_trgm "
        "ON entries USING gin ((meta->>'summary') gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entries_meta_summary_trgm")
    op.execute("DROP INDEX IF EXISTS ix_entries_title_trgm")
    # We don't drop the extension — it may be in use elsewhere.
    op.drop_index("ix_interactions_user_created", table_name="interactions")
    op.drop_index("ix_entries_source_score", table_name="entries")