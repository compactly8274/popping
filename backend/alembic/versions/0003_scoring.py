"""scoring: source_weight + user-profile category fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-27 18:00:00.000000

Phase 2: composite scoring wants to weight sources individually and let
users follow / mute entire categories. Adds three columns:

  - sources.source_weight      Float  default 1.0  (multiplier on raw_score)
  - user_profiles.followed_categories   JSON  nullable
  - user_profiles.muted_categories      JSON  nullable
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "source_weight",
            sa.Float,
            server_default="1.0",
            nullable=False,
        ),
    )
    op.add_column(
        "user_profiles",
        sa.Column("followed_categories", sa.JSON, nullable=True),
    )
    op.add_column(
        "user_profiles",
        sa.Column("muted_categories", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_profiles", "muted_categories")
    op.drop_column("user_profiles", "followed_categories")
    op.drop_column("sources", "source_weight")