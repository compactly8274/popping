"""Regression test for a bug found in a repo-wide audit: alembic
migration 0021 adds ``ix_interactions_user_type_created`` (a composite
index on ``(user_id, type, created_at)``) to speed up
``GET /api/interactions/recent`` (the History page), but that index
was never mirrored into ``Interaction.__table_args__`` in models.py —
unlike e.g. ``Entry.fetched_at``'s ``index=True``, which the module
explicitly keeps in sync with its own migration "so
``Base.metadata.create_all()``-based dev setups [stay] consistent
with the migration-managed production schema."

Since this test suite builds its schema via
``Base.metadata.create_all`` (not Alembic — see conftest.py), the
gap meant the index silently didn't exist in any dev/CI/test
environment even though production (migrated via Alembic) had it —
a performance regression on the History endpoint that no test could
catch.
"""
from __future__ import annotations

from sqlalchemy import text


async def test_interaction_user_type_created_index_exists(db_session):
    rows = (
        await db_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'interactions' "
                "AND indexname = 'ix_interactions_user_type_created'"
            )
        )
    ).all()
    assert len(rows) == 1, (
        "ix_interactions_user_type_created must exist on a "
        "Base.metadata.create_all()-built schema, matching alembic "
        "migration 0021 — add it to Interaction.__table_args__ if "
        "missing."
    )
    indexdef = rows[0][0]
    assert "user_id" in indexdef and "type" in indexdef and "created_at" in indexdef, (
        f"index must cover (user_id, type, created_at), got: {indexdef}"
    )
