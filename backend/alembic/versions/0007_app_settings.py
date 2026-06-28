"""app_settings key/value store

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-24 12:00:00.000000

Backs the runtime settings UI. Keys known today: ``llm.provider``,
``llm.model_brief``, ``llm.model_scoring``. The table is intentionally
free-form (TEXT value) so future settings can be added without a
migration — schema-level validation lives in the route handler that
accepts PUTs.

Read precedence (per ``app.runtime_settings.get``):
    1. Row in ``app_settings`` with that key (if present and non-empty).
    2. ``settings.<env_var>`` field (cold start / first boot).
    3. Hardcoded fallback in the consumer.

Seeding happens once at first boot via ``runtime_settings.seed_from_env``
inside the app lifespan. After that the table is authoritative — env
edits don't silently override the user's UI choice.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")