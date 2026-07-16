"""story_clusters: Framing Watch same-story/different-headline clustering

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-15 12:00:00.000000

Adds the ``story_clusters`` table and two columns on ``entries``:
``story_cluster_id`` (FK, nullable — an entry belongs to at most one
cluster) and ``framing_tone`` (nullable headline-tone label, set once
per entry by a batched LLM call — see ``app.framing``).

Membership is modeled as a plain FK on ``entries`` rather than an
array column or a join table: an entry can only ever be in one
cluster, so a many-to-one FK is the normalized shape and matches
every other relationship in this schema (``entries.source_id``, etc.).
No join table needed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "story_clusters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("wire_source", sa.String(40), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "entries",
        sa.Column(
            "story_cluster_id",
            sa.Integer,
            sa.ForeignKey("story_clusters.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_entries_story_cluster_id", "entries", ["story_cluster_id"])
    op.add_column("entries", sa.Column("framing_tone", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("entries", "framing_tone")
    op.drop_index("ix_entries_story_cluster_id", table_name="entries")
    op.drop_column("entries", "story_cluster_id")
    op.drop_table("story_clusters")
