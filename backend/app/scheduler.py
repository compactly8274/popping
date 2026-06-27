"""APScheduler wiring for source polling.

`AsyncIOScheduler` runs in the same event loop as FastAPI, so fetches
happen in-process. Phase 1 doesn't have a worker / beat split; if
phase 2 needs to scale fetches across workers, swap in `RedisJobStore`
+ `run_in_apscheduler_role`.

The scheduler owns:
    - One repeating job per registered source plugin
    - Upsert into the entries table (by url — natural primary key for feeds)
    - Embedding the entry text at ingest (phase 2)
    - Composite scoring at ingest (phase 2)
    - One-shot embedding backfill for entries with NULL embedding (phase 2)
    - Source-row bookkeeping (last_fetch_at, last_error, error_count)
    - Periodic purge of expired sessions (DB-backed auth)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.embeddings import embedder
from app.models import Entry, Source, UserProfile
from app.scoring import composite as composite_scorer
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


async def _load_profile(session: AsyncSession) -> UserProfile | None:
    """The single-row user profile. Created on demand so a fresh DB
    doesn't crash ingest."""
    profile = await session.scalar(select(UserProfile).where(UserProfile.id == 1))
    if profile is None:
        profile = UserProfile(id=1)
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
    return profile


async def _embed_text(norm: dict) -> list[float] | None:
    """Build the embed text from a normalized item. Empty → zero vector."""
    title = (norm.get("title") or "").strip()
    summary = (norm.get("summary") or "").strip()
    if not title and not summary:
        return [0.0] * embedder().dim
    text = title
    if summary:
        text = f"{title} — {summary}"
    try:
        return await embedder().embed(text)
    except Exception as exc:
        logger.warning("embedding failed for '%s…': %s", title[:40], exc)
        return None


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
            profile = await _load_profile(session)
            raw_items = await plugin.fetch()
            summary["fetched"] = len(raw_items)
            for raw in raw_items:
                try:
                    norm = plugin.normalize(raw)
                except ValueError as exc:
                    logger.warning("%s: skipping bad item: %s", plugin_cls.name, exc)
                    continue
                # raw_score is the recency-at-ingest — stays interpretable
                # as "how fresh was this when it arrived".
                raw_score = recency.score(norm["published_at"], source.category)
                embedding = await _embed_text(norm)
                composite = composite_scorer.score(
                    _stub_entry(norm, raw_score, embedding),
                    source,
                    profile,
                )
                stmt = (
                    pg_insert(Entry)
                    .values(
                        source_id=source.id,
                        title=norm["title"],
                        url=norm["url"],
                        published_at=norm["published_at"],
                        raw_score=raw_score,
                        personal_score=0.0,
                        composite_score=composite,
                        embedding=embedding,
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


def _stub_entry(norm: dict, raw_score: float, embedding: list[float] | None) -> Entry:
    """Build a transient Entry with only the fields composite_score touches.

    Saves us round-tripping to the DB between insert and composite.
    composite_score is later overwritten anyway.
    """
    e = Entry()
    e.title = norm.get("title") or ""
    e.url = norm.get("url") or ""
    e.published_at = norm.get("published_at")
    e.raw_score = raw_score
    e.personal_score = 0.0
    e.embedding = embedding
    return e


async def _backfill_embeddings(batch_size: int | None = None) -> None:
    """One-shot at startup: embed any existing entries with NULL embedding.

    Runs in batches so we don't OOM the embedder. Logs progress; never
    raises — a failure here is logged and swallowed so the rest of the
    app can start.
    """
    if not embedder().loaded:
        logger.info("embedding backfill: skipping (embedder not loaded)")
        return
    bs = batch_size or settings.embedding_batch_size
    try:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(Entry.id, Entry.title, Entry.body_text).where(Entry.embedding.is_(None))
                )
            ).all()
        if not rows:
            logger.info("embedding backfill: nothing to do")
            return
        logger.info("embedding backfill: %d entries queued (batch=%d)", len(rows), bs)
        for start in range(0, len(rows), bs):
            chunk = rows[start:start + bs]
            texts = [
                ((t or "").strip() + (" — " + (bt or "").strip() if bt else "")).strip()
                or " "
                for _, t, bt in chunk
            ]
            vecs = await embedder().embed_many(texts)
            async with SessionLocal() as session:
                for (entry_id, _t, _bt), vec in zip(chunk, vecs):
                    if vec is None:
                        continue
                    await session.execute(
                        Entry.__table__.update()
                        .where(Entry.id == entry_id)
                        .values(embedding=vec)
                    )
                await session.commit()
            logger.info("embedding backfill: %d / %d", min(start + bs, len(rows)), len(rows))
    except Exception:
        logger.exception("embedding backfill failed (will retry on next startup)")


async def _purge_sessions() -> None:
    """Delete expired session rows. Best-effort; logs on failure."""
    # Imported lazily so module load order is independent of auth availability.
    from app.auth.session import purge_expired

    try:
        async with SessionLocal() as session:
            count = await purge_expired(session)
            if count:
                logger.info("session purge: deleted %d expired row(s)", count)
    except Exception:
        logger.exception("session purge failed")


async def start_scheduler() -> AsyncIOScheduler:
    """Discover plugins, register one interval job per source, start scheduler.

    Also runs an immediate fetch per source so the dashboard isn't empty
    on a cold start. Schedules the embedding backfill as a fire-and-forget
    task so startup isn't blocked.
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

    # Periodic session purge. Runs whenever the scheduler is up; cheap when
    # there's nothing to delete (a single DELETE … WHERE expires_at <= now()).
    _scheduler.add_job(
        _purge_sessions,
        trigger=IntervalTrigger(seconds=settings.session_purge_interval_seconds),
        id="auth:purge_sessions",
        name="Purge expired sessions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Phase 2: fire-and-forget embedding backfill. Runs once shortly after
    # startup so we don't race the model load. If embeddings are disabled
    # or the model failed to load, the backfill is a no-op.
    if settings.embedding_enabled and embedder().loaded:
        _scheduler.add_job(
            _backfill_embeddings,
            trigger=IntervalTrigger(minutes=5),  # re-run periodically to catch missed rows
            id="embed:backfill",
            name="Embedding backfill",
            replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30),
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


async def backfill_now() -> dict:
    """Run the embedding backfill once on demand (e.g. from a debug endpoint)."""
    await _backfill_embeddings()
    return {"ok": True}
