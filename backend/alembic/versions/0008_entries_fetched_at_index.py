"""index entries.fetched_at

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-28 12:00:00.000000

The brief generator filters entries by ``fetched_at`` (when the row
landed in our DB) — not ``published_at``, so historical content like
Wikipedia "on this day" doesn't pollute the daily digest. The column
is unindexed today, which means a sequential scan on every brief
generation (scheduled + manual) as the table grows.

This index makes the brief query a constant-time seek. It also helps
the convergence-alert path (``_select_entries_by_slug``) which uses
the same filter. The existing ``ix_entries_published_at`` index
stays — it's still useful for the dashboard's browse view.

The index is on the column as-is, not partial, because:
  - Postgres can use it for both equality and range scans.
  - The "last 24h" range query is selective enough that a plain B-tree
    index beats a partial one with no maintenance cost.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_entries_fetched_at",
        "entries",
        ["fetched_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_entries_fetched_at", table_name="entries")
