"""Drop ix_entries_fetched_at_desc.

0025 created it as a plain ascending B-tree on ``entries.fetched_at``,
which is byte-for-byte the same index as ``ix_entries_fetched_at`` from
0008. Postgres scans a B-tree in either direction, so the duplicate only
costs write amplification (the rescore job updates entries every 15
minutes) and disk.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entries_fetched_at_desc")


def downgrade() -> None:
    op.create_index("ix_entries_fetched_at_desc", "entries", ["fetched_at"])
