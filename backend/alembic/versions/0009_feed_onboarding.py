"""phase 5: feed onboarding UI + feed recommendations (no schema change)

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-28 12:00:00.000000

Phase 5 lets the user add RSS feeds from the Drawer (POST /api/sources)
and see a curated list of recommendations. The existing ``sources``
table already has every column Phase 5 needs (``type``, ``category``,
``url``, ``refresh_interval_seconds``, ``active``) — no schema
migration is required.

This file exists to keep the alembic revision history linear. A
future reader sees "Phase 5 landed at this revision" and can grep
for the commit that bumped the version. ``upgrade()`` and
``downgrade()`` are explicit no-ops; alembic still records the
revision as applied.
"""

from __future__ import annotations

from typing import Sequence, Union


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No schema change. Phase 5 is a code-only rollout: a new module
    # (backend/app/sources/dynamic_rss.py), a new module
    # (backend/app/feed_recommendations.py), and additions to the
    # sources route + scheduler. The Source table was already
    # runtime-friendly — we just weren't reading from it.
    pass


def downgrade() -> None:
    # No schema change to reverse. Phase 5's runtime additions are
    # removed by reverting the code commit, not by running this
    # downgrade.
    pass