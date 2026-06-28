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
    - Daily Brief generation (phase 4)
    - Periodic convergence-check + alert Brief (phase 4)
    - Post-ingest CVE notifications for high-CVSS entries (phase 4)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import defaultdict
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import assets
from app.brief import BriefGenerator
from app.config import settings
from app.db import SessionLocal
from app.embeddings import embedder
from app.models import Brief, Entry, Source, UserProfile
from app.notify import Notifier
from app.scoring import composite as composite_scorer
from app.scoring import recency
from app.sources import list_sources
from app.sources.dynamic_rss import DynamicRssPlugin

logger = logging.getLogger("popping.scheduler")

_scheduler: AsyncIOScheduler | None = None
_brief_generator: Optional[BriefGenerator] = None


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
    newly_inserted: list[tuple[Entry, Source]] = []
    try:
        async with SessionLocal() as session:
            source = await _upsert_source(session, plugin_cls)
            # Lazy favicon fetch: only on first ingest per source. A
            # failed fetch leaves favicon_url NULL and retries next
            # ingest. Runs inside the same transaction so the URL lands
            # in the DB atomically with the source row.
            if source.favicon_url is None:
                try:
                    remote, local = await assets.fetch_favicon(source.url, source.id)
                    if remote and local:
                        source.favicon_url = remote
                        source.favicon_path = local
                        logger.info("favicon cached for %s → %s", source.name, local)
                except Exception:
                    logger.debug("favicon fetch failed for %s", source.name, exc_info=True)
            profile = await _load_profile(session)
            raw_items = await plugin.fetch()
            summary["fetched"] = len(raw_items)
            for raw in raw_items:
                try:
                    norm = plugin.normalize(raw)
                except ValueError as exc:
                    logger.warning("%s: skipping bad item: %s", plugin_cls.name, exc)
                    continue
                # Lift image_url out of meta (the default normalize()
                # buckets it there) so it can land in its own column.
                # A missing image is the common case for non-RSS sources
                # and stays NULL.
                meta = norm.get("meta") or {}
                remote_image_url = meta.pop("image_url", None) or None
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
                        meta=meta or None,
                        image_url=remote_image_url,
                        image_path=None,
                    )
                    .on_conflict_do_nothing(index_elements=["url"])
                    .returning(Entry.id)
                )
                result = await session.execute(stmt)
                if result.rowcount == 1:
                    summary["inserted"] += 1
                    # Re-read the persisted row so the post-hook has a
                    # fully-hydrated Entry (id + meta). Cheap — we just
                    # wrote it.
                    inserted_id = result.scalar_one_or_none()
                    if inserted_id is not None:
                        row = await session.get(Entry, inserted_id)
                        if row is not None:
                            newly_inserted.append((row, source))
                            # Fetch the thumbnail now that we have the
                            # row's id. Set image_path/image_url on the
                            # ORM object — SQLAlchemy emits an UPDATE at
                            # the next flush (the commit below). Never
                            # raises: a failed fetch leaves the row
                            # with image_url set but image_path NULL.
                            if remote_image_url:
                                try:
                                    local_path = await assets.fetch_thumbnail(
                                        remote_image_url, inserted_id
                                    )
                                    if local_path:
                                        row.image_path = local_path
                                        row.image_url = remote_image_url
                                except Exception:
                                    logger.debug(
                                        "thumbnail fetch failed for entry %d",
                                        inserted_id, exc_info=True,
                                    )
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

    # Post-ingest hook: high-CVSS CVE notifications. Only fires when
    # the scheduler actually inserted something (not on every duplicate
    # re-ingest), and only when the notifier is wired up.
    if newly_inserted and _brief_generator is not None:
        await _maybe_notify_cves(newly_inserted)

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


async def _maybe_notify_cves(inserted: list[tuple[Entry, Source]]) -> None:
    """Fire CVE notifications for newly-ingested entries above the
    configured CVSS threshold.

    Dedup: maintain a single rolling ``Brief`` row with
    ``meta.notified_urls``. We don't write a new Brief per CVE — we
    only want one alert per URL across the lifetime of the alerts
    system, and a Brief's GIN-indexed ``meta`` makes the containment
    query cheap.
    """
    if not _brief_generator:
        return
    notifier = _brief_generator.notifier
    if notifier is None:
        return

    threshold = float(settings.cve_notify_min_cvss or 0.0)
    if threshold <= 0:
        return

    cves = [(e, s) for e, s in inserted if _cvss_score(e) >= threshold]
    if not cves:
        return

    try:
        async with SessionLocal() as session:
            already = await _already_notified_urls(session)
            fresh: list[tuple[Entry, Source]] = []
            for entry, src in cves:
                if entry.url in already:
                    continue
                fresh.append((entry, src))
            if not fresh:
                return
            body = "\n\n".join(_format_cve(e, s) for e, s in fresh[:10])
            title = f"🚨 {len(fresh)} high-severity CVE{'s' if len(fresh) != 1 else ''}"
            await notifier.send(title=title, body=body)
            await _record_notified_urls(session, [e.url for e, _ in fresh if e.url])
            await session.commit()
            logger.info("cve notify: %d fresh alert(s) sent", len(fresh))
    except Exception:
        logger.exception("cve notify failed")


async def _check_convergence() -> None:
    """Periodic alert Brief for cross-source convergence clusters.

    Same scan as ``/api/foryou``'s convergence boost, but instead of
    ordering the feed we look for slugs that haven't been alerted on
    today and fire ``BriefGenerator.generate_alert`` for each.
    """
    if not _brief_generator:
        return
    threshold = int(settings.convergence_notify_threshold or 2)
    if threshold < 2:
        return

    try:
        async with SessionLocal() as session:
            conv = await _convergence_counts(session, settings.convergence_window_hours)
            candidates = {slug: count for slug, count in conv.items() if count >= threshold}
            if not candidates:
                return
            already = await _already_alerted_slugs(session)
            new_slugs = {s: c for s, c in candidates.items() if s not in already}
            if not new_slugs:
                return
            for slug, count in list(new_slugs.items())[:5]:  # cap per tick
                try:
                    await _brief_generator.generate_alert(
                        session=session, slug=slug, source_count=count,
                    )
                    await session.commit()
                except Exception:
                    logger.exception("alert brief failed for slug=%s", slug)
                    await session.rollback()
    except Exception:
        logger.exception("convergence check failed")


async def _daily_brief() -> None:
    """Daily scheduled Brief at ``BRIEF_SCHEDULE_HOUR`` UTC."""
    if not _brief_generator:
        return
    try:
        async with SessionLocal() as session:
            brief = await _brief_generator.generate(session=session, tone="terse")
            if brief is not None:
                await session.commit()
                logger.info("daily brief generated id=%d", brief.id)
    except Exception:
        logger.exception("daily brief failed")


async def _convergence_counts(session: AsyncSession, window_hours: int) -> dict[str, int]:
    """Reused logic from /api/foryou — kept inline here so the scheduler
    doesn't depend on the routes package."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    stmt = (
        select(Entry.title, Source.name)
        .join(Source, Entry.source_id == Source.id)
        .where(Entry.published_at >= since)
    )
    rows = (await session.execute(stmt)).all()
    counts: dict[str, set[str]] = defaultdict(set)
    for title, source_name in rows:
        slug = composite_scorer.title_slug(title)
        if not slug:
            continue
        counts[slug].add(source_name)
    return {slug: len(srcs) for slug, srcs in counts.items() if len(srcs) > 1}


async def _already_notified_urls(session: AsyncSession) -> set[str]:
    """Union of all ``meta.notified_urls`` arrays across Brief rows."""
    stmt = select(Brief.meta)
    rows = (await session.execute(stmt)).all()
    out: set[str] = set()
    for (meta,) in rows:
        if not meta:
            continue
        urls = meta.get("notified_urls") or []
        if isinstance(urls, list):
            out.update(u for u in urls if isinstance(u, str))
    return out


async def _already_alerted_slugs(session: AsyncSession) -> set[str]:
    """Union of all ``meta.alert_slugs`` across Brief rows."""
    stmt = select(Brief.meta)
    rows = (await session.execute(stmt)).all()
    out: set[str] = set()
    for (meta,) in rows:
        if not meta:
            continue
        slugs = meta.get("alert_slugs") or []
        if isinstance(slugs, list):
            out.update(s for s in slugs if isinstance(s, str))
    return out


async def _record_notified_urls(session: AsyncSession, urls: list[str]) -> None:
    """Append ``urls`` to the latest Brief row's ``meta.notified_urls``.

    Falls back to creating a marker row if no Brief exists yet — keeps
    dedup working on a cold-start with no daily brief in the DB."""
    if not urls:
        return
    row = await session.scalar(select(Brief).order_by(desc(Brief.generated_at)).limit(1))
    if row is None:
        row = Brief(tone="terse", content="(notification dedup ledger)", meta={"notified_urls": list(urls)})
        session.add(row)
        return
    meta = dict(row.meta or {})
    bucket = list(meta.get("notified_urls") or [])
    bucket.extend(urls)
    # Cap the list — JSON columns don't truncate gracefully and we don't
    # care about CVE URLs from a month ago.
    meta["notified_urls"] = bucket[-500:]
    row.meta = meta


def _cvss_score(entry: Entry) -> float:
    """Read CVSS from ``meta.cvss_score``. Returns 0.0 if absent/invalid."""
    if not entry.meta:
        return 0.0
    val = entry.meta.get("cvss_score")
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _format_cve(entry: Entry, source: Source) -> str:
    score = _cvss_score(entry)
    title = (entry.title or entry.url or "").strip()
    line = f"[{source.name}] {title}"
    if score:
        line += f" (CVSS {score:.1f})"
    if entry.url:
        line += f"\n{entry.url}"
    return line


async def start_scheduler(notifier: Optional[Notifier] = None) -> AsyncIOScheduler:
    """Discover plugins, register one interval job per source, start scheduler.

    Also runs an immediate fetch per source so the dashboard isn't empty
    on a cold start. Schedules the embedding backfill as a fire-and-forget
    task so startup isn't blocked.
    """
    global _scheduler, _brief_generator
    if _scheduler is not None:
        return _scheduler

    # One BriefGenerator for the process. Reads the notifier from
    # outside (lifespan wired it up); pass it here so post-ingest hooks
    # can fire notifications without going through request_state.
    _brief_generator = BriefGenerator(notifier)

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

    # Phase 5: register scheduler jobs for ``Source`` rows that don't
    # have a backing plugin class. Today the only such shape is
    # ``type="rss"`` (served by ``DynamicRssPlugin``); future phases
    # will add ``podcast`` and ``youtube_channel`` and the dispatcher
    # below will pick the right plugin class per row.
    try:
        await _register_dynamic_source_jobs(_scheduler, plugins)
    except Exception:
        logger.exception("scheduler: dynamic-source walk failed — continuing with class-driven sources only")

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

    # Phase 4: scheduled daily Brief at BRIEF_SCHEDULE_HOUR UTC. Set
    # to -1 to disable (manual only). Fires once on startup too if the
    # scheduled hour matches — keeps the dashboard non-empty after a
    # restart mid-day.
    if settings.brief_schedule_hour >= 0:
        _scheduler.add_job(
            _daily_brief,
            trigger=CronTrigger(hour=settings.brief_schedule_hour, minute=0),
            id="brief:daily",
            name="Daily brief",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Phase 4: periodic convergence check + alert Brief. Runs every
    # CONVERGENCE_CHECK_INTERVAL_MINUTES (default 15).
    _scheduler.add_job(
        _check_convergence,
        trigger=IntervalTrigger(minutes=settings.convergence_check_interval_minutes),
        id="brief:convergence",
        name="Convergence check",
        replace_existing=True,
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10),
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.info("scheduler: started")
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler, _brief_generator
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        _brief_generator = None
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


# ---------------------------------------------------------------------------
# Phase 5: dynamic source rows
# ---------------------------------------------------------------------------
#
# ``Source`` rows whose ``name`` is not in the registered plugin
# registry are "dynamic" — they have no class backing them and need a
# runtime-constructed plugin to be fetched. Today this only covers
# ``type="rss"`` (served by ``DynamicRssPlugin``); Phase 6 will add
# ``type="podcast"``, Phase 7 ``type="youtube_channel"``. Both will
# land as their own ``_plugin_for(row)`` branch below.
#
# Each dynamic row gets its own scheduler job keyed by
# ``ingest:dynamic:{row.id}``. This id is what the routes use to
# reschedule / remove the job on PATCH / DELETE.

# Stable id prefix so callers don't have to know the format. Mirrors
# ``ingest:<name>`` used for class-driven sources — different prefix
# (``dynamic:``) so a ``name`` collision with a registered plugin
# name can't mask the class-driven job, and so ``reschedule_job`` /
# ``remove_job`` know they're talking about a row, not a class.
_DYNAMIC_JOB_PREFIX = "ingest:dynamic:"


def _dynamic_job_id(row_id: int) -> str:
    return f"{_DYNAMIC_JOB_PREFIX}{row_id}"


def _plugin_for(row: Source) -> SourcePlugin | None:
    """Dispatch a ``Source`` row to the right plugin instance.

    Returns ``None`` for ``type`` values we don't handle yet (a
    no-op skip; the row stays in the DB but doesn't fetch). Logs a
    debug message so misconfigurations are visible without being
    noisy.
    """
    if row.type == "rss":
        return DynamicRssPlugin(row)
    # Phase 6/7 will add ``podcast`` and ``youtube_channel`` here.
    logger.debug(
        "scheduler: no plugin for source %s (id=%d, type=%s) — skipping",
        row.name, row.id, row.type,
    )
    return None


def _add_or_replace_dynamic_job(scheduler: Any, row: Source) -> bool:
    """Register (or replace) the scheduler job for a dynamic row.

    Returns True if a job was registered, False if the row's type
    has no plugin yet. ``replace_existing=True`` so PATCH refresh-
    interval changes take effect without first removing the job.
    """
    plugin = _plugin_for(row)
    if plugin is None:
        return False
    scheduler.add_job(
        _ingest,
        trigger=IntervalTrigger(seconds=row.refresh_interval_seconds),
        args=[plugin],
        id=_dynamic_job_id(row.id),
        name=f"Ingest {row.name} (dynamic)",
        replace_existing=True,
        next_run_time=dt.datetime.now(dt.timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "scheduler: registered dynamic source id=%d name=%s interval=%ds",
        row.id, row.name, row.refresh_interval_seconds,
    )
    return True


async def _register_dynamic_source_jobs(scheduler: Any, plugins: dict[str, Any]) -> None:
    """Startup walk: register a job for every ``Source`` row whose
    ``name`` is not in the registered plugin registry and whose
    ``active`` is True. Idempotent — APScheduler's
    ``replace_existing=True`` makes a second startup a no-op for
    jobs already registered.

    Disabled rows (``active=False``) are skipped; their job doesn't
    exist and the scheduler won't fetch from them. Re-enabling via
    PATCH goes through ``update_source`` which adds the job back.
    """
    registered_names = set(plugins.keys())
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(Source).where(Source.active == True)  # noqa: E712
            )
        ).all()
    dynamic = [r for r in rows if r.name not in registered_names]
    if not dynamic:
        logger.info("scheduler: no dynamic source rows to register")
        return
    registered_count = 0
    for row in dynamic:
        if _add_or_replace_dynamic_job(scheduler, row):
            registered_count += 1
    logger.info(
        "scheduler: registered %d dynamic source(s) out of %d candidate row(s)",
        registered_count, len(dynamic),
    )


async def add_source(
    session: AsyncSession,
    *,
    name: str,
    type_: str,
    category: str,
    url: str,
    refresh: int,
) -> Source:
    """Create a Source row and register its scheduler job.

    Idempotent on ``name``: if a row with the same name already
    exists, returns it without modification. This matches the
    class-driven upsert in ``_upsert_source`` and lets the POST
    endpoint safely retry without raising 409.

    The scheduler is a module-level singleton; if it isn't running
    (e.g. during tests) the row is still created and will be picked
    up on the next ``start_scheduler`` walk.
    """
    existing = await session.scalar(select(Source).where(Source.name == name))
    if existing is not None:
        return existing
    row = Source(
        name=name,
        type=type_,
        category=category,
        url=url,
        refresh_interval_seconds=refresh,
        active=True,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    if _scheduler is not None:
        _add_or_replace_dynamic_job(_scheduler, row)
    return row


async def update_source(
    session: AsyncSession,
    source_id: int,
    *,
    refresh: int | None = None,
    active: bool | None = None,
    category: str | None = None,
) -> Source | None:
    """Apply a partial update to a Source row and reschedule if needed.

    Returns the updated row, or None if no row exists with that id.

    Only fields the Phase 5 UI exposes are accepted. ``name`` /
    ``url`` changes would invalidate the favicon cache and aren't
    supported — the UI points users to delete + recreate.

    Scheduler effects:
      - ``active`` False → remove the dynamic job. A class-driven
        source (``name in list_sources()``) is left alone; the
        class-driven job continues independently of the row.
      - ``active`` True or ``refresh`` change → re-add the dynamic
        job with the new trigger (``replace_existing=True`` handles
        idempotency).
    """
    row = await session.get(Source, source_id)
    if row is None:
        return None
    refresh_changed = (
        refresh is not None and refresh != row.refresh_interval_seconds
    )
    active_changed = active is not None and active != row.active
    category_changed = category is not None and category != row.category
    if not (refresh_changed or active_changed or category_changed):
        return row
    if refresh is not None:
        row.refresh_interval_seconds = refresh
    if active is not None:
        row.active = active
    if category is not None:
        row.category = category
    await session.commit()
    await session.refresh(row)

    if _scheduler is None:
        return row

    # Class-driven sources (BBC, etc.) own their scheduler job via the
    # plugin registry — touching it here would be wrong. Only manage
    # dynamic jobs. ``list_sources()`` is module-level; safe to call
    # without awaiting.
    registered_names = set(list_sources().keys())
    if row.name in registered_names:
        return row

    if active is False:
        # Disabled — remove the job if it exists. ``remove_job``
        # raises if the id doesn't exist; catch so the caller still
        # gets a successful row update.
        try:
            _scheduler.remove_job(_dynamic_job_id(row.id))
            logger.info("scheduler: removed dynamic job for %s (id=%d)", row.name, row.id)
        except Exception:
            pass
        return row

    # Re-add (covers: enabled, refresh changed, or both).
    _add_or_replace_dynamic_job(_scheduler, row)
    return row


async def delete_source(session: AsyncSession, source_id: int) -> bool:
    """Drop a Source row and its scheduler job. Returns True if a
    row was deleted.

    Class-driven sources (BBC etc.) should not reach here — the
    route layer rejects DELETE for those with a 400 before calling.
    We double-check the registry so a future caller that bypasses
    the route can't accidentally nuke a built-in row.
    """
    row = await session.get(Source, source_id)
    if row is None:
        return False
    name = row.name
    registered_names = set(list_sources().keys())
    if name in registered_names:
        # Defensive: refuse here too. The route's 400 is the
        # user-facing check; this guard catches programmatic callers.
        raise ValueError(f"refusing to delete built-in source {name!r}")
    await session.delete(row)
    await session.commit()
    if _scheduler is not None:
        try:
            _scheduler.remove_job(_dynamic_job_id(source_id))
            logger.info("scheduler: removed dynamic job for %s (id=%d)", name, source_id)
        except Exception:
            pass
    return True
