"""Add sources.display_name.

The free-text label the user gives a feed ("CPABC Newsroom"). ``name``
stays the internal slug; NULL means the app derives a label from it.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("display_name", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "display_name")
