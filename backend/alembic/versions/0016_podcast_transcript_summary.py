"""entries.podcast_transcript_summary — LLM summary of a podcast
episode's transcript, cached on first read

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-10 12:00:00.000000

Podcast entries whose feed publishes a Podcasting-2.0
``<podcast:transcript>`` tag get their transcript fetched and
summarized by the configured LLM provider (same one the Brief
generator uses) on the first "Summarize episode" tap. Same NULL /
empty-string / populated cache contract as ``cached_summary`` (see
0011) — this is a sibling column, not a reuse of that one, because
the two cache genuinely different things (the feed's own blurb vs.
an AI-generated summary of the audio content) and an entry could
plausibly want both independently.

Pure additive change — no backfill, no rewrite of existing rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "entries",
        sa.Column("podcast_transcript_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entries", "podcast_transcript_summary")
