"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-25 12:00:00.000000

Creates all six core tables: sources, entries, interactions,
watchlist_items, user_profiles, briefs. Embedding columns are Vector(384)
matching all-MiniLM-L6-v2 (the model the embedding pipeline will use in
phase 2). The pgvector extension is created in env.py before this
migration runs.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("category", sa.String(40), nullable=False, index=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("refresh_interval_seconds", sa.Integer, server_default="3600"),
        sa.Column("last_fetch_at", sa.DateTime, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("error_count", sa.Integer, server_default="0"),
        sa.Column("active", sa.Boolean, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "entries",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("published_at", sa.DateTime, nullable=True, index=True),
        sa.Column("raw_score", sa.Float, server_default="0"),
        sa.Column("personal_score", sa.Float, server_default="0"),
        sa.Column("composite_score", sa.Float, server_default="0", index=True),
        sa.Column("body_text", sa.Text, nullable=True),
        sa.Column("body_text_compressed", sa.Boolean, server_default=sa.false()),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("meta", sa.JSON, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("fetched_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "interactions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "entry_id",
            sa.BigInteger,
            sa.ForeignKey("entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(60), server_default="default", nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("value", sa.Float, server_default="1.0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False, index=True),
    )
    op.create_index("ix_interactions_user_entry", "interactions", ["user_id", "entry_id"])

    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("threshold", sa.Float, nullable=True),
        sa.Column("last_checked_at", sa.DateTime, nullable=True),
        sa.Column("last_notified_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("preference_vector", Vector(384), nullable=True),
        sa.Column("interest_clusters", sa.JSON, nullable=True),
        sa.Column("followed_teams", sa.JSON, nullable=True),
        sa.Column("tracked_repos", sa.JSON, nullable=True),
        sa.Column("running_stack", sa.JSON, nullable=True),
        sa.Column("quiet_hours_start", sa.Integer, nullable=True),
        sa.Column("quiet_hours_end", sa.Integer, nullable=True),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("id", name="uq_user_profiles_single_row"),
    )

    op.create_table(
        "briefs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("generated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("tone", sa.String(20), server_default="terse"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("delivered_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("briefs")
    op.drop_table("user_profiles")
    op.drop_table("watchlist_items")
    op.drop_index("ix_interactions_user_entry", table_name="interactions")
    op.drop_table("interactions")
    op.drop_table("entries")
    op.drop_table("sources")