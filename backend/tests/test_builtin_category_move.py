"""Existing installs move Wikipedia On This Day out of ``news`` into its
own ``on_this_day`` tab once, at scheduler start.

``_upsert_source`` returns an existing row unchanged, so the plugin's
new class category alone never reaches a row created before it. The
move runs once (an ``app_settings`` marker) and only while the row is
still in ``news``: a category the user picked, before or after, stays."""

from __future__ import annotations

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app import scheduler
from app.models import AppSetting, Source
from app.scoring import recency
from app.sources.wikipedia_on_this_day import WikipediaOnThisDay
from factories import make_source

_NAME = "wikipedia_on_this_day"
_MARKER = f"builtin_category_moved:{_NAME}"


async def _category(session) -> str | None:
    session.expire_all()
    row = await session.scalar(select(Source).where(Source.name == _NAME))
    return None if row is None else row.category


async def _marker(session) -> AppSetting | None:
    session.expire_all()
    return await session.get(AppSetting, _MARKER)


async def test_moves_a_news_row_once(db_session):
    await make_source(db_session, _NAME, category="news", type="api")
    await scheduler._move_builtin_categories()
    assert await _category(db_session) == "on_this_day"
    assert await _marker(db_session) is not None

    # The user moves it back to news; a restart leaves that alone.
    row = await db_session.scalar(select(Source).where(Source.name == _NAME))
    row.category = "news"
    await db_session.commit()
    await scheduler._move_builtin_categories()
    assert await _category(db_session) == "news"


async def test_leaves_a_category_the_user_picked(db_session):
    await make_source(db_session, _NAME, category="history", type="api")
    await scheduler._move_builtin_categories()
    assert await _category(db_session) == "history"
    assert await _marker(db_session) is not None


async def test_no_row_records_the_move_and_a_new_row_gets_the_new_category(db_session):
    await scheduler._move_builtin_categories()
    assert await _marker(db_session) is not None
    async with scheduler.SessionLocal() as session:
        row = await scheduler._upsert_source(session, WikipediaOnThisDay)
    assert row.category == "on_this_day"


async def test_start_scheduler_runs_the_move(db_session, monkeypatch):
    await make_source(db_session, _NAME, category="news", type="api")

    class _Paused(AsyncIOScheduler):
        def start(self, paused: bool = False) -> None:  # noqa: ARG002 - never dispatch
            super().start(paused=True)

    async def _no_walk(sched, plugins):
        return None

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", _Paused)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_brief_generator", None)
    monkeypatch.setattr(scheduler, "_register_dynamic_source_jobs", _no_walk)
    built = await scheduler.start_scheduler()
    try:
        assert await _category(db_session) == "on_this_day"
    finally:
        built.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._brief_generator = None


@pytest.mark.no_db
def test_on_this_day_half_life_is_6_hours():
    assert recency.half_life_hours("on_this_day") == 6.0
    assert recency.half_life_hours("news") == 6.0
