"""Regression test: deleting a built-in (class-driven) source must
stick — not get silently recreated by the next scheduled ingest tick
or a backend restart.

Before this fix, ``scheduler.delete_source`` only removed the DB row;
the plugin's permanent APScheduler job (registered once per class at
``start_scheduler()``, never per-row) kept firing on its own interval
and, on the very next tick, ``_upsert_source`` saw no existing row and
recreated it from scratch. The reported symptom: a user deletes
"Wikipedia On This Day" from Settings, and it's back the next time the
backend restarts (or the next scheduled tick, whichever comes first).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import scheduler
from app.models import AppSetting, Source
from app.sources.wikipedia_on_this_day import WikipediaOnThisDay


@pytest.mark.asyncio
async def test_deleting_a_builtin_source_is_not_recreated_by_upsert(db_session):
    # Arrange: the plugin's row exists, as it would after a normal
    # ingest tick or backend startup.
    source = await scheduler._upsert_source(db_session, WikipediaOnThisDay)
    assert source is not None
    source_id = source.id

    # Act: the user deletes it via the same path the DELETE
    # /api/sources/{id} endpoint uses.
    deleted = await scheduler.delete_source(db_session, source_id)
    assert deleted is True
    assert (await db_session.get(Source, source_id)) is None

    # The deletion must be durably recorded, not just "row is gone".
    marker = await db_session.get(
        AppSetting, f"deleted_builtin_source:{WikipediaOnThisDay.name}"
    )
    assert marker is not None

    # Assert: exactly what the next scheduled tick (or a fresh
    # ``start_scheduler()`` on restart) would do — call
    # ``_upsert_source`` again for the same plugin class. It must NOT
    # recreate the row.
    result = await scheduler._upsert_source(db_session, WikipediaOnThisDay)
    assert result is None
    still_gone = await db_session.scalar(
        select(Source).where(Source.name == WikipediaOnThisDay.name)
    )
    assert still_gone is None


@pytest.mark.asyncio
async def test_upsert_source_still_creates_a_never_deleted_builtin(db_session):
    # A plugin that was never deleted must still get its row created
    # normally — the deletion marker is per-name and shouldn't affect
    # unrelated (or not-yet-deleted) sources.
    result = await scheduler._upsert_source(db_session, WikipediaOnThisDay)
    assert result is not None
    assert result.name == WikipediaOnThisDay.name

    # Idempotent: calling it again just returns the same row.
    again = await scheduler._upsert_source(db_session, WikipediaOnThisDay)
    assert again is not None
    assert again.id == result.id
