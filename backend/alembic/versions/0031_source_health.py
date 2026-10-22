"""Add sources.last_attempt_at, last_item_count and last_new_count.

Feed health. ``last_attempt_at`` is when the feed's last check
finished, whatever came of it: a success, a failure, or a setup state
(``[no list]``, ``[reddit off]``). ``last_item_count`` is how many items
the last successful check listed, before the ingest rules cut them, and
``last_new_count`` is how many of those it stored as new stories.

All three are nullable with no default and no backfill: an existing
row keeps NULL until its next check, and the app falls back to
``last_fetch_at``. A nullable column with no default is a catalog-only
change in Postgres (no table rewrite).

Rollback: prefer reverting the app code and keeping this file. The
columns are nullable, so older code ignores them and the head stays at
0031. See docs/ux-notes/PR9a.md for the steps when the schema really
has to go.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("last_item_count", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("last_new_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "last_new_count")
    op.drop_column("sources", "last_item_count")
    op.drop_column("sources", "last_attempt_at")
