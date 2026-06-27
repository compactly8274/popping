"""APScheduler wiring for source polling.

`AsyncIOScheduler` runs in the same event loop as FastAPI, so fetches
happen in-process. Phase 1 doesn't have a worker / beat split; if
phase 2 needs to scale fetches across workers, swap in `RedisJobStore`
+ `run_in_apscheduler_role`.

The scheduler owns:
    - One repeating job per registered source plugin
    - Upsert into the entries table (by url — natural primary key for feeds)
    - Source-row bookkeeping (last_fetch_at, last_error, error_count)
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Entry, Source
from app.scoring import recency
from app.sources import list_sources

logger = logging.getLogger("popping.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _upsert_source(session: AsyncSession, plugin_cls: Any) -> Source:
    """Make sure the sources table has a row for this plugin. Idempotent."""
    existing = await session.scalar(
        select(Source).where(Source.name == plugin_cls.name)
    )
    if existing is not None:
        return existing
    row = Source(
        name=plugin_cls.name,
        type=plugin_cls.type,
        category=plugin_cls.category,
        url=plugin_cls.url,
        refresh_interval_seconds=plugin_cls.refresh_interval_seconds,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _ingest(plugin_cls: Any) -> dict:
    """Fetch from a plugin, write entries to DB, update source bookkeeping.

    Returns a small summary dict the manual /ingest endpoint surfaces.
    Catches all exceptions per-source so one broken source can't take
    the scheduler down.
    """
    summary = {"source": plugin_cls.name, "fetched": 0, "inserted": 0, "duplicates": 0, "error": None}
    plugin = plugin_cls()
    try:
        async with SessionLocal() as session:
            source = await _upsert_source(session, plugin_cls)
            raw_items = await plugin.fetch()
            summary["fetched"] = len(raw_items)
            for raw in raw_items:
                try:
                    norm = plugin.normalize(raw)
                except ValueError as exc:
                    logger.warning("%s: skipping bad item: %s", plugin_cls.name, exc)
                    continue
                score = recency.score(norm["published_at"])
                stmt = (
                    pg_insert(Entry)
                    .values(
                        source_id=source.id,
                        title=norm["title"],
                        url=norm["url"],
                        published_at=norm["published_at"],
                        raw_score=score,
                        personal_score=0.0,
                        composite_score=score,
                        meta=norm.get("meta"),
                    )
                    .on_conflict_do_nothing(index_elements=["url"])
                )
                result = await session.execute(stmt)
                if result.rowcount == 1:
                    summary["inserted"] += 1
                else:
                    summary["duplicates"] += 1
            source.last_fetch_at = dt.datetime.now(dt.timezone.utc)
            source.last_error = None
            source.error_count = 0
            await session.commit()
    except Exception as exc:
        logger.exception("ingest failed for %s", plugin_cls.name)
        summary["error"] = f"{type(exc).__name__}: {exc}"
        try:
            async with SessionLocal() as session:
                source = await session.scalar(
                    select(Source).where(Source.name == plugin_cls.name)
                )
                if source is not None:
                    source.last_error = summary["error"]
                    source.error_count = (source.error_count or 0) + 1
                    await session.commit()
        except Exception:
            logger.exception("could not record error for %s", plugin_cls.name)
    return summary


async def start_scheduler() -> AsyncIOScheduler:
    """Discover plugins, register one interval job per source, start scheduler.

    Also runs an immediate fetch per source so the dashboard isn't empty
    on a cold start.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")
    plugins = list_sources()
    logger.info("scheduler: discovered %d source plugin(s): %s", len(plugins), ", ".join(plugins))

    for name, plugin_cls in plugins.items():
        _scheduler.add_job(
            _ingest,
            trigger=IntervalTrigger(seconds=plugin_cls.refresh_interval_seconds),
            args=[plugin_cls],
            id=f"ingest:{name}",
            name=f"Ingest {name}",
            replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc),  # fire once on startup
            max_instances=1,
            coalesce=True,
        )

    _scheduler.start()
    logger.info("scheduler: started")
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler: stopped")


async def trigger_now(plugin_name: str) -> dict:
    """Run a single plugin once on demand. Used by the /ingest endpoint."""
    plugins = list_sources()
    if plugin_name not in plugins:
        return {"source": plugin_name, "error": "unknown source", "fetched": 0, "inserted": 0, "duplicates": 0}
    return await _ingest(plugins[plugin_name])