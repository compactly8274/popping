"""sources.favicon_url/path + entries.image_url/path

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-28 12:00:00.000000

Direct columns for cached asset URLs/paths. The frontend renders
``<img src=/assets/{image_path}>`` and ``<img src=/assets/{favicon_path}>``;
the backend populates these from a local cache populated at ingest
(see ``app/assets.py``). No JSON bag — image is first-class data,
every row has at most one.

The ``_path`` columns store the local relative path (under the StaticFiles
mount at ``/assets``); the ``_url`` columns store the original remote URL
for audit / future re-fetch. Favicon path is needed separately because
the cache stores ``<id>.<ext>`` where ext varies (`.ico`, `.png`, `.svg`).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("favicon_url", sa.Text, nullable=True))
    op.add_column("sources", sa.Column("favicon_path", sa.Text, nullable=True))
    op.add_column("entries", sa.Column("image_url", sa.Text, nullable=True))
    op.add_column("entries", sa.Column("image_path", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("entries", "image_path")
    op.drop_column("entries", "image_url")
    op.drop_column("sources", "favicon_path")
    op.drop_column("sources", "favicon_url")
