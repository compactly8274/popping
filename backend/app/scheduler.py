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
    - Periodic rescore of recently-fetched entries so recency decay and
      preference-vector updates actually reach rows already in the table
      instead of only ever applying at the moment of ingest
    - One-shot embedding backfill for entries with NULL embedding (phase 2)
    - Source-row bookkeeping (last_fetch_at, last_error, error_count)
    - Auto-disabling sources after ``_AUTO_DISABLE_THRESHOLD``
      consecutive failures (see comment on the constant)
    - Periodic purge of expired sessions (DB-backed auth)
    - Daily Brief generation (phase 4)
    - Periodic convergence-check + alert Brief (phase 4)
    - Post-ingest CVE notifications for high-CVSS entries (phase 4)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import json
import logging
import re
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import assets
from app.brief import BriefGenerator
from app.config import settings
from app.db import SessionLocal
from app.embeddings import embedder
from app.models import Brief, Entry, Interaction, NotificationDedup, Source, UserProfile
from app.notify import Notifier
from app.scoring import composite as composite_scorer
from app.scoring import convergence as convergence_helper
from app.scoring import personal as personal_scorer
from app.scoring import recency
from app.sources import list_sources
from app.sources.base import SourcePlugin
from app.sources.dynamic_reddit import DynamicRedditPlugin
from app.sources.dynamic_rss import DynamicRssPlugin

logger = logging.getLogger("popping.scheduler")

_scheduler: AsyncIOScheduler | None = None

# Sentinel for ``update_source``'s ``custom_headers`` parameter:
# ``None`` is a meaningful value ("set the column to NULL / clear the
# override") so we need a separate marker for "field absent from the
# PATCH body, leave untouched." The route layer translates an
# explicit ``body.custom_headers == {}`` to ``None`` and anything
# missing to ``_UNSET``.
_UNSET: Any = object()
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


# Favicon retry gate: at most one attempt per source per hour. Catches
# the cold-start-with-unmounted-volume case (asset_dir became writable
# after the first ingest) without hammering a permanently-broken host
# (a 403 stays a 403, no amount of retrying helps). One hour is long
# enough to recover from transient infra issues and short enough that
# the user notices the favicon land within a typical work session.
_FAVICON_RETRY_INTERVAL = dt.timedelta(hours=1)


# After this many consecutive failed ingests, a source is auto-disabled
# (``active=False``). 6 was chosen so a single transient incident
# (CDN hiccup, brief outage, rate-limit window) doesn't disable a
# feed — RSS feeds typically re-poll within minutes, so 6 failures
# spans ~6× the refresh interval for a default-source RSS row.
# Combined with the user's earlier observation that a permanently-
# malformed feed (ap_top) keeps erroring forever, this caps the
# damage: at most ~6 stuck rows in the dashboard before the user
# notices, and each one carries a ``last_error`` tooltip explaining
# why. Reactivating a row (FeedManager toggle) resets ``error_count``
# so the user gets a clean retry slate rather than an immediate
# re-disable on the first hiccup.
_AUTO_DISABLE_THRESHOLD = 6


def _should_retry_favicon(last_fetch_at: dt.datetime | None) -> bool:
    if last_fetch_at is None:
        return True
    # ``last_fetch_at`` is timezone-aware UTC in practice, but tolerate
    # a naive value coming from a pre-upgrade DB row.
    if last_fetch_at.tzinfo is None:
        last_fetch_at = last_fetch_at.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - last_fetch_at) >= _FAVICON_RETRY_INTERVAL


async def _ingest(plugin_cls: Any) -> dict:
    """Fetch from a plugin, write entries to DB, update source bookkeeping.

    Returns a small summary dict the manual /ingest endpoint surfaces.
    Catches all exceptions per-source so one broken source can't take
    the scheduler down.
    """
    summary = {"source": plugin_cls.name, "fetched": 0, "inserted": 0, "duplicates": 0, "error": None}
    # A class-driven plugin arrives as a class — instantiate a fresh one
    # per run so plugin-local state doesn't leak between ingests. A
    # dynamic plugin arrives as an already-constructed
    # ``DynamicRssPlugin`` instance (the scheduler creates one per row
    # and passes that instance to ``add_job``); calling an instance
    # would raise ``TypeError: 'DynamicRssPlugin' object is not callable``
    # which the broad ``except Exception`` below would swallow, leaving
    # the row with a stale ``last_error`` and no entries landing.
    plugin = plugin_cls() if not isinstance(plugin_cls, SourcePlugin) else plugin_cls
    newly_inserted: list[tuple[Entry, Source]] = []
    # Track entries that need a thumbnail pass. Collected here so we
    # can do the network fetches outside the DB session (each
    # ``fetch_thumbnail`` can take up to 20s with the new retry
    # logic — holding a DB transaction for 50 entries × 20s is a
    # pool starvation waiting to happen). The pass runs after the
    # entries commit and writes back via a single bulk UPDATE.
    thumbnail_jobs: list[tuple[int, str]] = []
    try:
        # Decide whether to fetch the favicon BEFORE opening the long
        # ingest transaction. ``fetch_favicon`` makes two network
        # round-trips (HTML probe + icon download, each up to 10s) —
        # running them inside the session would hold an idle DB
        # transaction for ~20s per source on every retry, starving the
        # pool. We snapshot the upserted source's id/url/state, close
        # the session, run the network work, and re-open the session
        # only to persist the result.
        source_id: int | None = None
        source_url: str | None = None
        needs_favicon = False
        async with SessionLocal() as session:
            source = await _upsert_source(session, plugin_cls)
            source_id = source.id
            source_url = source.url
            needs_favicon = (
                source.favicon_url is None
                and _should_retry_favicon(source.last_fetch_at)
            )

        if needs_favicon and source_id is not None and source_url:
            try:
                remote, local = await assets.fetch_favicon(source_url, source_id)
                if remote and local:
                    async with SessionLocal() as session:
                        row = await session.get(Source, source_id)
                        if row is not None:
                            row.favicon_url = remote
                            row.favicon_path = local
                            await session.commit()
                            logger.info("favicon cached for %s → %s", plugin_cls.name, local)
            except Exception:
                logger.debug("favicon fetch failed for %s", plugin_cls.name, exc_info=True)

        async with SessionLocal() as session:
            source = await session.get(Source, source_id) if source_id else None
            if source is None:
                # Source was deleted between sessions — nothing to do.
                return summary
            # Paused (built-in or dynamic): skip the fetch entirely.
            # APScheduler doesn't know about the DB row's active flag,
            # so we have to check here. For dynamic rows, ``update_source``
            # already removes the job on pause, but a class-driven
            # built-in's job is owned by the plugin registry and never
            # goes away — this is the only place the pause can be
            # honored end-to-end. Skipping on every tick (instead of
            # removing the job) keeps the resume path O(1): flip the
            # flag back and the next tick lands entries again.
            if not source.active:
                logger.debug("ingest: %s paused — skipping", source.name)
                return summary
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
                # ``.returning(Entry.id)`` only emits a row on a
                # successful insert; on a conflict (duplicate URL)
                # the result set is empty. ``scalar_one_or_none()``
                # gives us None in that case so we can branch.
                # Note: SQLAlchemy 2.0 async returns a
                # ``ChunkedIteratorResult`` which has no
                # ``.rowcount`` attribute, so the only reliable check
                # is whether ``scalar_one_or_none()`` returned a
                # value.
                result = await session.execute(stmt)
                inserted_id = result.scalar_one_or_none()
                if inserted_id is not None:
                    summary["inserted"] += 1
                    if remote_image_url:
                        # Defer the network fetch to a single pass
                        # after the commit (see below).
                        thumbnail_jobs.append((inserted_id, remote_image_url))
                else:
                    summary["duplicates"] += 1
            source.last_fetch_at = dt.datetime.now(dt.timezone.utc)
            source.last_error = None
            source.error_count = 0
            await session.commit()
            # Re-fetch inserted rows for the post-hook (notification
            # path) after the commit so they have stable ids.
            if thumbnail_jobs:
                ids = [jid for jid, _ in thumbnail_jobs]
                rows = (
                    await session.scalars(select(Entry).where(Entry.id.in_(ids)))
                ).all()
                for row in rows:
                    newly_inserted.append((row, source))
    except Exception as exc:
        logger.exception("ingest failed for %s", plugin_cls.name)
        # Strip newlines (multi-line traceback in a tooltip is
        # unreadable) and cap at 200 chars (native HTML ``title=``
        # balloons truncate past ~400 chars anyway, and the full
        # traceback is already captured via ``logger.exception``
        # above). Without this cap a verbose exception (e.g. an
        # HTML Cloudflare challenge body) renders as a multi-line
        # unreadable tooltip.
        exc_type = type(exc).__name__
        msg = str(exc).strip()
        msg = msg.splitlines()[0][:200] if msg else ""
        summary["error"] = f"{exc_type}: {msg}" if msg else exc_type
        try:
            async with SessionLocal() as session:
                source = await session.scalar(
                    select(Source).where(Source.name == plugin_cls.name)
                )
                if source is not None:
                    source.last_error = summary["error"]
                    source.error_count = (source.error_count or 0) + 1
                    # Auto-disable after _AUTO_DISABLE_THRESHOLD consecutive
                    # failures. The next ingest pass sees ``active=False``
                    # and skips (line ~206), so a permanently-broken feed
                    # stops hammering the host AND stops accumulating log
                    # noise within one failure-cycle. The user reactivates
                    # via FeedManager (which resets error_count, so a
                    # manual retry starts from zero rather than being
                    # immediately re-disabled).
                    #
                    # Log at WARNING so the auto-disable is visible in
                    # ``docker compose logs backend`` even when the user
                    # only has INFO-level logs configured.
                    if (
                        source.active
                        and source.error_count >= _AUTO_DISABLE_THRESHOLD
                    ):
                        source.active = False
                        logger.warning(
                            "scheduler: auto-disabling source %s after %d consecutive failures (last_error=%r)",
                            plugin_cls.name,
                            source.error_count,
                            source.last_error,
                        )
                    await session.commit()
        except Exception:
            logger.exception("could not record error for %s", plugin_cls.name)
        return summary

    # Thumbnail pass. Runs OUTSIDE the ingest session — each
    # ``fetch_thumbnail`` can take up to 20s with the new retry
    # logic, and a typical ingest inserts dozens of entries. Doing
    # the fetches here (post-commit) keeps the DB transaction short
    # and lets us run them concurrently via ``asyncio.gather`` since
    # they're independent of each other. The bulk UPDATE at the end
    # persists all results in one round-trip.
    if thumbnail_jobs:
        results = await asyncio.gather(
            *(assets.fetch_thumbnail(url, eid) for eid, url in thumbnail_jobs),
            return_exceptions=True,
        )
        path_by_id: dict[int, str] = {}
        for (eid, _url), result in zip(thumbnail_jobs, results):
            if isinstance(result, BaseException):
                logger.debug("thumbnail fetch failed for entry %d", eid, exc_info=result)
                continue
            if result is not None:
                path_by_id[eid] = result
        if path_by_id:
            try:
                async with SessionLocal() as session:
                    # One bulk UPDATE per source — N entries become a
                    # single round-trip instead of N. ``CASE WHEN``
                    # picks the right path for each id.
                    from sqlalchemy import case
                    whens = {eid: path for eid, path in path_by_id.items()}
                    ids = list(whens.keys())
                    path_expr = case(whens, value=Entry.id)
                    await session.execute(
                        Entry.__table__.update()
                        .where(Entry.id.in_(ids))
                        .values(image_path=path_expr)
                    )
                    await session.commit()
                    logger.info(
                        "thumbnails cached for %s: %d / %d",
                        plugin_cls.name, len(path_by_id), len(thumbnail_jobs),
                    )
            except Exception:
                logger.exception("thumbnail bulk update failed for %s", plugin_cls.name)

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


# Per-interaction-type contribution to the preference vector.
# Positive = move toward the entry's embedding (more of this in
# the For You feed). Negative = move away (less of this).
# Magnitudes were picked so a single thumb_down outranks a
# single view, but a steady stream of views still moves the
# needle; bookmark is the strongest positive signal (the user
# explicitly said "save for later").
_INTERACTION_WEIGHTS: dict[str, float] = {
    # ``view`` fires on every card scroll-into-view, including
    # ones the user has already seen and scrolled past. It's a
    # "didn't say no" signal, not a "want more of this" signal --
    # the prior weight of 1.0 made it dominate the aggregated
    # vector (3188 views vs 34 ``never`` in one user's data, a
    # 94:1 ratio), so explicit hide/thumb_down events were
    # effectively swamped. Demoted to 0.2 so it still keeps the
    # vector from going stale on a user who never clicks
    # anything, but doesn't drown out the explicit signals.
    "view": 0.2,
    # ``click`` is the strongest "I actually read this" signal
    # we have -- the user opened the article. Was 2.0; bumped
    # to match the positive explicit signals so a heavy clicker
    # can outweigh a moderate ``view`` volume.
    "click": 1.0,
    # ``dwell`` is the per-card read-time signal. The value is
    # already in milliseconds (or seconds) so the weight here is
    # a divisor -- 0.3 means a 10-second read contributes ~3.0
    # to the aggregated vector, comparable to a single ``click``.
    "dwell": 0.3,
    # Explicit positive signals. Bumped from 3.0 to 4.0 so a
    # single thumb-up can outweigh ~20 scroll-by views. The
    # intuition: a user who actually thumbed something is
    # telling us "I want more like this", and that should beat
    # 20 cards they casually scrolled past.
    "thumb_up": 4.0,
    "bookmark": 4.0,
    # Explicit negative signals. Bumped from -2.0/-3.0 to -4.0
    # so a single hide cancels ~20 views' worth of positive
    # signal. The user clicking the eye-icon is a strong "I
    # never want to see this"; it should move the vector
    # meaningfully, not be averaged out by passive browsing.
    "thumb_down": -4.0,
    "never": -4.0,
    # ``share`` is an explicit positive but rare; was 1.5.
    # Bumped to 2.0 -- sharing implies a stronger endorsement
    # than just bookmarking. Still under thumb_up (4.0) so
    # a "I like this" thumb beats an "I shared this" share
    # in the absence of additional thumbs.
    "share": 2.0,
}


# The set of user_ids the recompute should aggregate. In a
# multi-user OIDC deployment, this would resolve to "the
# OIDC user, if any, plus the bypass user" -- the soft-auth
# "anonymous" events would be excluded. In a single-user
# homelab (the current shape), OIDC is off, the bypass is
# on, and the soft-auth path is what records events (because
# the user's browser doesn't always hit from a CIDR that
# matches the bypass list -- e.g. via a port-forward or
# reverse proxy). For the homelab case we aggregate over
# all three ids so the recompute doesn't miss events.
_AGGREGATION_USER_IDS_ALL: tuple[str, ...] = (
    "anonymous",
    "local-bypass",
    "default",  # pre-soft-auth events tagged with the column default
)


async def _resolve_aggregation_user_ids(
    session: AsyncSession,
) -> tuple[str, ...]:
    """Return the user_ids whose interactions feed the recompute.

    Today this is the same tuple regardless of OIDC state
    (the homelab case). When OIDC is enabled in a future
    deployment, this would scope to the OIDC user's sub
    instead. See the function docstring on
    ``_recompute_preference_vector`` for the rationale.
    """
    return _AGGREGATION_USER_IDS_ALL


async def _recompute_preference_vector() -> None:
    """Aggregate the user's recent interactions into
    ``UserProfile.preference_vector``.

    Why this exists
    ---------------

    The personal scorer (``app.scoring.personal``) reads
    ``preference_vector`` to rank For You candidates by cosine
    similarity to the user's taste. Without a non-null vector
    the scorer returns a flat 50 for every entry (the "no
    signal yet" midpoint), so the For You feed looks static
    regardless of what the user has been reading.

    Until this job landed, the vector was never written --
    interactions were stored in the ``interactions`` table
    for the History view, but nothing consumed them. This
    function is the missing consumer.

    Algorithm
    ---------

    1. Pull the last ``pref_vector_window_days`` of interactions
       for user_id 1 (the LAN-bypass user), joined to
       ``entries.embedding``. Interactions against entries with
       a NULL embedding are skipped silently -- they don't move
       the vector.
    2. Sum ``entry.embedding * _INTERACTION_WEIGHTS[type]`` for
       each interaction. The sum is a 384-dim vector.
    3. L2-normalize so the cosine math in personal.py
       (``dot(a, b) / (|a| * |b|)``) produces values in the
       expected range. Without normalization a user with many
       interactions would push the vector to a large norm and
       the cosine would become a less meaningful signal.
    4. Blend with the existing vector: ``v_new =
       blend_new * aggregated + (1 - blend_new) * old``. This
       smooths the recompute -- a single outlier can't yank
       the feed, and a user with no recent activity keeps
       their old vector (so the For You feed doesn't reset to
       50 every tick).
    5. Persist via a single UPDATE on the single-row
       ``user_profiles`` table.

    Edge cases
    ----------

    - No interactions in the window: leave the existing
      vector untouched (don't reset to NULL -- that would
      degrade the feed back to the neutral 50). Log it and
      return.
    - All interactions against entries with NULL embeddings
      (e.g. embedder is disabled or those rows are very
      old): same as above -- leave the vector untouched.
    - Embedder disabled (``settings.embedding_enabled=False``):
      we still run the query, but ``entry.embedding`` will
      always be NULL so the function no-ops. Logged once.
    - Old vector is NULL: treat as zero vector, blend
      collapses to ``blend_new * aggregated`` (i.e. behave
      like a fresh install catching up).
    """
    if not settings.embedding_enabled or not embedder().loaded:
        logger.debug("pref vector: skipping (embedder not enabled/loaded)")
        return
    window = dt.timedelta(days=settings.pref_vector_window_days)
    cutoff = dt.datetime.now(dt.timezone.utc) - window
    try:
        async with SessionLocal() as session:
            # Aggregate via SQL: GROUP BY entry_id, sum the
            # weighted embeddings. We use a Python-side loop
            # rather than a SQL-side sum because pgvector
            # array_add isn't a single operator we can call
            # with a weight; pulling (entry_id, embedding,
            # type) and doing it in Python is simple and
            # bounded by the window size (a heavy reader
            # has thousands of rows over 30 days -- still
            # cheap to iterate in-process).
            #
            # User-id filter
            # --------------
            # The interactions endpoint uses soft auth: it
            # tags every event with the OIDC sub when
            # available, "local-bypass" when the LAN bypass
            # fires (TCP peer in
            # local_bypass_allowed_cidrs), or "anonymous"
            # when neither applies. In a single-user
            # deployment all three collapse to "this user's
            # interactions", so we aggregate over all of them.
            # In a multi-user deployment (OIDC on) the OIDC
            # sub is stable per user and only that user's
            # sub should land in the recompute; the bypass
            # and "anonymous" rows from other users stay
            # out. We resolve the right user set at query
            # time: if there's a resolved OIDC user, scope
            # to their sub; otherwise aggregate everything
            # (single-user homelab). ``_resolve_aggregation_user_id``
            # picks one.
            user_ids = await _resolve_aggregation_user_ids(session)
            if not user_ids:
                logger.debug("pref vector: no user_ids to aggregate, keeping current")
                return
            rows = (
                await session.execute(
                    select(Entry.id, Entry.embedding, Interaction.type)
                    .join(Interaction, Interaction.entry_id == Entry.id)
                    .where(
                        Interaction.user_id.in_(user_ids),
                        Interaction.created_at >= cutoff,
                        Entry.embedding.isnot(None),
                    )
                )
            ).all()
            if not rows:
                logger.debug("pref vector: no in-window interactions with embeddings, keeping current")
                return
            dim = len(rows[0][1]) if rows[0][1] is not None else 0
            if dim == 0:
                return
            agg = [0.0] * dim
            n_used = 0
            for _entry_id, emb, itype in rows:
                if emb is None:
                    continue
                w = _INTERACTION_WEIGHTS.get(itype, 0.0)
                if w == 0.0:
                    continue
                for i in range(dim):
                    agg[i] += w * float(emb[i])
                n_used += 1
            if n_used == 0:
                return
            # L2-normalize the aggregated vector so the
            # cosine math in personal.py is meaningful.
            norm_sq = sum(x * x for x in agg)
            if norm_sq == 0.0:
                return
            inv = 1.0 / math.sqrt(norm_sq)
            agg = [x * inv for x in agg]
            # Load the current vector and blend. NULL →
            # zero vector; the blend collapses to
            # ``blend_new * agg``.
            profile = await _load_profile(session)
            old = profile.preference_vector or [0.0] * dim
            if len(old) != dim:
                # Embedder dim changed between the old
                # vector and now. Drop the old vector
                # rather than crash on the blend; the
                # next tick (10 min later) re-stabilises.
                logger.warning(
                    "pref vector: dim mismatch (old=%d, new=%d), discarding old",
                    len(old),
                    dim,
                )
                old = [0.0] * dim
            blend = max(0.0, min(1.0, settings.pref_vector_blend_new))
            blended = [blend * a + (1.0 - blend) * o for a, o in zip(agg, old)]
            # Re-normalize the blend too, so the persisted
            # vector is always unit length.
            sq = sum(x * x for x in blended)
            if sq > 0.0:
                inv = 1.0 / math.sqrt(sq)
                blended = [x * inv for x in blended]
            await session.execute(
                UserProfile.__table__.update()
                .where(UserProfile.id == profile.id)
                .values(preference_vector=blended)
            )
            await session.commit()
            logger.info(
                "pref vector: recomputed from %d interactions, blend=%.2f",
                n_used,
                blend,
            )
    except Exception:
        logger.exception("pref vector recompute failed (will retry on next tick)")


# How far back to rescore. Long enough to outlast the slowest
# category half-life with room to spare (deals: 48h) so nothing
# still-relevant is skipped; short enough that the batch stays a
# few thousand rows, not the whole table.
_RESCORE_WINDOW_DAYS = 7
_RESCORE_INTERVAL_MINUTES = 15


async def _rescore_recent_entries() -> None:
    """Recompute ``composite_score`` (and ``personal_score``) for
    everything fetched in the last ``_RESCORE_WINDOW_DAYS``.

    Why this exists
    ----------------

    ``_ingest`` sets ``composite_score`` / ``personal_score`` exactly
    once, at insert time, and nothing ever touches them again.
    ``composite_score``'s recency component is ``recency.score()`` —
    an exponential decay meant to fall from 100 toward 0 as an entry
    ages — but since the stored value is never recomputed, it stays
    frozen at whatever it was the moment the row landed. In practice
    every entry looks "fresh" (recency ≈ 100) at its own ingest, so
    the decay this column was designed to express never actually
    happens in the data — an entry ingested a week ago competes in
    ``ORDER BY composite_score DESC`` on equal footing with one
    ingested a minute ago, as long as their INITIAL scores were
    similar.

    The same staleness hits ``personal_score``: it's computed once
    against whatever ``preference_vector`` existed at that moment.
    ``_recompute_preference_vector`` updates the vector itself every
    ``pref_vector_recompute_interval_minutes``, but an entry inserted
    before the user's taste became clear (or before they started
    ignoring a source like Wikipedia On This Day) keeps its
    original, likely-neutral personal_score forever — the profile
    gets smarter, already-scored entries don't.

    Net effect on For You / the dashboard: sources that always look
    "fresh" at ingest (Wikipedia On This Day sets ``published_at`` to
    the fetch time, not the historical event's date — see
    ``app.sources.wikipedia_on_this_day``) keep a permanently
    high-looking composite_score, and never get corrected downward
    even once the user's preference vector clearly disfavors them.
    This job is the periodic correction: recompute both columns
    against the CURRENT profile and the CURRENT wall clock for
    everything young enough to still matter, using the exact same
    scoring functions ingest uses (no duplicated logic to drift).

    ``raw_score`` is deliberately left untouched — see its comment
    at the ``_ingest`` call site: it's a permanent "how fresh was
    this when it arrived" quality marker, not a decaying signal.
    """
    try:
        async with SessionLocal() as session:
            profile = await _load_profile(session)
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_RESCORE_WINDOW_DAYS)
            rows = (
                await session.execute(
                    select(Entry)
                    .options(selectinload(Entry.source))
                    .where(Entry.fetched_at >= cutoff)
                )
            ).scalars().all()
            if not rows:
                return
            updates: list[dict] = []
            for entry in rows:
                new_composite = composite_scorer.score(entry, entry.source, profile)
                new_personal = personal_scorer.score(entry, entry.source, profile)
                if new_composite != entry.composite_score or new_personal != entry.personal_score:
                    updates.append(
                        {"_id": entry.id, "composite_score": new_composite, "personal_score": new_personal}
                    )
            if updates:
                # Bulk UPDATE-by-primary-key: one round-trip for the
                # whole batch via executemany-style bound params,
                # rather than one UPDATE per row. ``Entry.__table__.update()``
                # (Core, not the ORM-mapped ``update(Entry)``) so this
                # doesn't trip SQLAlchemy's "ORM Bulk UPDATE by Primary
                # Key" special-casing, which expects a different calling
                # convention — same pattern the thumbnail-path and
                # preference-vector bulk updates elsewhere in this file
                # already use.
                stmt = (
                    Entry.__table__.update()
                    .where(Entry.id == bindparam("_id"))
                    .values(
                        composite_score=bindparam("composite_score"),
                        personal_score=bindparam("personal_score"),
                    )
                )
                await session.execute(stmt, updates)
                await session.commit()
                logger.info(
                    "rescore: updated %d/%d recent entries",
                    len(updates),
                    len(rows),
                )
    except Exception:
        logger.exception("rescore: failed (will retry next tick)")


async def _prune_notification_dedup() -> None:
    """Prune ``notification_dedup`` rows older than the retention window.

    Each CVE URL or convergence slug that fires once and never again
    leaves a row that sits in the table forever — the
    ``ON CONFLICT DO UPDATE`` only bumps ``last_notified_at`` on
    re-fires, so steady-state ingest on a vuln-heavy mix grows the
    table by one row per unique URL. Over months this hits tens of
    thousands of rows on a single-user install. Reads stay cheap (PK
    lookups) but the index size grows linearly.

    Prune rows whose ``last_notified_at`` is older than
    ``NOTIFICATION_DEDUP_RETENTION_DAYS`` (default 30). 30 days is
    long enough that a CVE re-reported tomorrow still dedups
    against this week's ledger; short enough that the table tops
    out around "two weeks of CVE volume + a few hundred
    convergence slugs".

    Runs daily. Scheduled by ``start_scheduler`` next to the
    session-purge job.
    """
    from app.config import settings as _s  # avoid cycle at module load

    retention_days = getattr(_s, "notification_dedup_retention_days", 30)
    if retention_days <= 0:
        # 0 = unbounded (opt-out for users who want the full history).
        return
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
    try:
        async with SessionLocal() as session:
            stmt = delete(NotificationDedup).where(
                NotificationDedup.last_notified_at < cutoff,
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount:
                logger.info(
                    "scheduler: notification_dedup pruned %d rows older than %d days",
                    result.rowcount, retention_days,
                )
    except Exception:
        logger.exception("notification_dedup prune failed — continuing")


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
            # Record the dedup BEFORE sending the notification.
            # The previous order (send, then record, then commit)
            # had a partial-state hazard: a transient commit failure
            # between the notifier call and the dedup ledger INSERT
            # left the user with a sent notification AND no dedup
            # row, so the next tick re-sent the same alert. Flip the
            # order so the dedup is durable BEFORE the side effect
            # fires. Trade-off: a notifier failure post-commit leaves
            # the dedup row but no notification sent, so the user
            # misses one alert — preferred over duplicate alerts.
            await _record_notified_urls(session, [e.url for e, _ in fresh if e.url])
            await session.commit()
            body = "\n\n".join(_format_cve(e, s) for e, s in fresh[:10])
            title = f"🚨 {len(fresh)} high-severity CVE{'s' if len(fresh) != 1 else ''}"
            await notifier.send(title=title, body=body)
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
            conv = await convergence_helper.counts(session, settings.convergence_window_hours)
            candidates = {slug: count for slug, count in conv.items() if count >= threshold}
            if not candidates:
                return
            already = await _already_alerted_slugs(session)
            new_slugs = {s: c for s, c in candidates.items() if s not in already}
            if not new_slugs:
                return
            for slug, count in list(new_slugs.items())[:5]:  # cap per tick
                try:
                    # Record the dedup ledger FIRST, then dispatch
                    # the alert. The previous order (generate_alert,
                    # then record, then commit) had a partial-state
                    # hazard if the final commit failed: the LLM
                    # call had already run and the notifier had
                    # already fired (side effects), but the dedup
                    # ledger INSERT was uncommitted and rolled back,
                    # so the next tick re-ran the same alert. Flip
                    # the order so the dedup ledger is durable
                    # BEFORE the side effects. A notifier failure
                    # post-commit leaves a dedup row but no alert
                    # sent — the user misses one alert, instead of
                    # receiving the same alert every tick until a
                    # successful send.
                    await _record_alerted_slug(session, slug)
                    await session.commit()
                    await _brief_generator.generate_alert(
                        session=session, slug=slug, source_count=count,
                    )
                    # ``generate_alert`` only flushes the Brief row
                    # (no internal commit), so persist it now. The
                    # dedup ledger from the commit above is the
                    # durable dedup; this commit carries the Brief
                    # row itself. If this second commit fails the
                    # dedup is still durable — the user just won't
                    # see the Brief in the dashboard, which is
                    # preferable to a duplicate alert.
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


# --- Reddit cross-reference sweep ------------------------------------------
#
# Background job that walks every ``Entry`` whose ``meta`` does not
# already have ``reddit_thread_url`` and asks Hydra's
# ``/search?url=<entry.url>`` endpoint whether the URL is discussed on
# Reddit. On a hit, ``meta`` is patched via the Postgres
# ``jsonb || jsonb`` merge so other keys (engagement, image refs,
# ...) survive. The card UI reads the new fields directly off
# ``Entry.meta`` via the ``EntryListOut`` shape.

# Batch size for the sweep. Small enough to finish in a single
# scheduler tick even on a slow Hydra; large enough that the hourly
# cadence keeps up with typical ingest volume (~200 entries/day).
_CROSSREF_BATCH = 50
# Inter-batch pause. 100ms between batches keeps us under ~5 req/s
# average on the Hydra VPS — well within a single-user gateway's
# comfort zone and polite enough that an outage / overload doesn't
# happen just because the sweep ran.
_CROSSREF_BATCH_SLEEP = 0.1


async def _crossref_sweep() -> None:
    """Hourly: stamp ``reddit_thread_url`` onto entries that have a
    matching Reddit thread.

    Skips entries that already have the key (idempotent — re-running
    on a stable set is a no-op). Skips entries from Reddit sources
    themselves (their own URL IS the thread, no separate cross-ref
    to stamp). Batches ``_CROSSREF_BATCH`` entries per round to keep
    Hydra load predictable; sleeps ``_CROSSREF_BATCH_SLEEP`` between
    batches. Never raises — a Hydra outage just means no new stamps
    this tick; the next hour retries.

    Disabled (no-op) when the Reddit client is fully disabled
    (no proxy AND ``reddit_direct_disabled`` is True). In direct
    mode the sweep still runs — the polite in-process rate
    limiter throttles the burst of search calls so a single
    sweep tick spreads over several minutes rather than a tight
    loop.
    """
    # Lazy import: keeps the startup hot path light when the feature
    # is off, and isolates the cross-ref path from the rest of the
    # scheduler imports.
    from app import reddit_client
    from app.models import Source as SourceModel

    if reddit_client.is_disabled():
        # Fully off — no proxy and direct path explicitly disabled.
        # Silent skip; the startup log already mentioned the posture.
        return

    try:
        async with SessionLocal() as session:
            # Pull all candidate entries. We do an in-Python filter for
            # "no reddit_thread_url" + "not from a Reddit source"
            # rather than a GIN-indexed ``NOT (meta ? 'reddit_thread_url')``
            # predicate — the rows-to-stamp set is small after the
            # first sweep, so the scan is cheap. The GIN index added
            # in migration 0012 still helps when the entry count
            # climbs into the tens of thousands.
            stmt = (
                select(Entry.id, Entry.url, Entry.source_id, Entry.meta)
                .order_by(Entry.id.desc())
                .limit(_CROSSREF_BATCH * 4)
            )
            candidates = (await session.execute(stmt)).all()

            # Fetch the set of Source rows that are Reddit-typed so
            # we can skip their entries (their own URL IS the
            # Reddit thread). One small query, in-Python membership
            # check after.
            reddit_source_ids = set((
                await session.scalars(
                    select(SourceModel.id).where(SourceModel.type == "reddit")
                )
            ).all())

            to_check: list[tuple[int, str]] = []
            for row in candidates:
                if row.source_id in reddit_source_ids:
                    continue
                if (row.meta or {}).get("reddit_thread_url"):
                    continue
                if not row.url:
                    continue
                to_check.append((row.id, row.url))

            if not to_check:
                return
            logger.info("crossref sweep: %d entries to check", len(to_check))

            stamped = 0
            # ``to_check`` is already truncated to ``_CROSSREF_BATCH``
            # (the slice below). We sleep per-entry rather than
            # per-batch because the in-batch cost is dominated by
            # the per-entry ``search_thread_by_url`` roundtrip —
            # 100ms between *batches* would bunch all 50 requests
            # into a single 0.5s wall, while 100ms *per entry*
            # spreads the same 5 seconds across the tick and gives
            # Hydra's per-connection throughput a breather.
            for entry_id, entry_url in to_check[:_CROSSREF_BATCH]:
                match = await reddit_client.search_thread_by_url(entry_url)
                if match is None:
                    # Still pause so a Hydra outage doesn't let us
                    # spin through 50 entries in 0ms on the
                    # failure path — same wall-clock cap as a
                    # healthy tick.
                    await asyncio.sleep(_CROSSREF_BATCH_SLEEP)
                    continue
                thread_url = f"https://www.reddit.com{match['permalink']}"
                # jsonb || jsonb merge — preserves any other meta
                # keys the entry already has. The patch goes through
                # raw SQL because SQLAlchemy's JSONB column type
                # doesn't auto-merge on update.
                await session.execute(
                    text(
                        "UPDATE entries "
                        "SET meta = COALESCE(meta, '{}'::jsonb) || :patch::jsonb "
                        "WHERE id = :id"
                    ),
                    {
                        "patch": json.dumps({
                            "reddit_thread_url": thread_url,
                            "reddit_comment_count": int(match["num_comments"]),
                        }),
                        "id": entry_id,
                    },
                )
                stamped += 1
                await asyncio.sleep(_CROSSREF_BATCH_SLEEP)
            if stamped:
                await session.commit()
                logger.info(
                    "crossref sweep: stamped %d entries (of %d candidates)",
                    stamped, len(to_check),
                )
            # If we had more candidates than we processed in this
            # batch, the next hourly tick picks them up. Keeping the
            # per-tick cap bounded so the sweep can't monopolise the
            # scheduler's single thread.
    except Exception:
        logger.exception("reddit crossref sweep failed")


async def _already_notified_urls(session: AsyncSession) -> set[str]:
    """URLs already in the CVE dedup ledger.

    Backed by the ``notification_dedup`` table rather than
    ``Brief.meta.notified_urls`` — the old layout wrote to one Brief
    row and read across all rows, which silently dropped entries past
    the 500-row bucket cap.
    """
    stmt = select(NotificationDedup.key).where(NotificationDedup.kind == "cve_url")
    return {row[0] for row in (await session.execute(stmt)).all()}


async def _already_alerted_slugs(session: AsyncSession) -> set[str]:
    """Slugs already in the convergence alert dedup ledger.

    Same ledger table, different ``kind`` discriminator. Was a union
    across Brief rows; now a direct PK lookup.
    """
    stmt = select(NotificationDedup.key).where(NotificationDedup.kind == "convergence_slug")
    return {row[0] for row in (await session.execute(stmt)).all()}


async def _record_notified_urls(session: AsyncSession, urls: list[str]) -> None:
    """Insert CVE URL dedup rows. ``ON CONFLICT DO NOTHING`` so a
    retried notify doesn't fail on duplicate-key; last_notified_at is
    bumped on conflict via the ``DO UPDATE`` clause so maintenance
    pruning has a useful timestamp.
    """
    if not urls:
        return
    now = dt.datetime.now(dt.timezone.utc)
    stmt = pg_insert(NotificationDedup).values(
        [{"kind": "cve_url", "key": u, "last_notified_at": now} for u in urls if u]
    )
    # ON CONFLICT (kind, key) DO UPDATE bumps last_notified_at — without
    # this, rows would keep their original timestamp and pruning by age
    # would treat re-fires as fresh. The DO NOTHING alternative would
    # also work and cost less; we want the timestamp bump.
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "key"],
        set_={"last_notified_at": now},
    )
    await session.execute(stmt)


async def _record_alerted_slug(session: AsyncSession, slug: str) -> None:
    """Mark a convergence slug as alerted. Same ``ON CONFLICT … DO
    UPDATE`` pattern as ``_record_notified_urls``.
    """
    if not slug:
        return
    now = dt.datetime.now(dt.timezone.utc)
    stmt = pg_insert(NotificationDedup).values(
        [{"kind": "convergence_slug", "key": slug, "last_notified_at": now}]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "key"],
        set_={"last_notified_at": now},
    )
    await session.execute(stmt)


def _cvss_score(entry: Entry) -> float:
    """Read CVSS from ``meta.cvss_score``. Returns 0.0 if absent/invalid."""
    if not entry.meta:
        return 0.0
    val = entry.meta.get("cvss_score")
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# Tags / entities we strip from CVE description text. CVE feeds
# (NVD / CISA KEV) are JSON-typed, so the title is plain text, but
# downstream notification backends (Discord / Slack / Telegram via
# Apprise) sometimes interpret Markdown and HTML; an entry whose
# title happens to contain "<script>…" or "```rm -rf ~```" is
# uncomfortable at best, exploitable at worst. Strip aggressively.
_TAG_RE = re.compile(r"<[^>]+>")
_BB_RE = re.compile(r"```.*?```", re.DOTALL)
# Visible whitespace collapse for the rare CVE feed that ships
# embedded newlines.
_WS_RE = re.compile(r"\s+")


def _sanitize_cve_text(text: str) -> str:
    """Strip HTML tags, code-fence blocks, and collapse whitespace.

    Keeps the message human-readable while removing everything that
    could trigger a Markdown / HTML interpretation in a downstream
    notifier backend. We deliberately do NOT try to escape — a
    missed escape rule becomes an XSS. Strip is safer."""
    if not text:
        return ""
    out = _TAG_RE.sub("", text)
    out = _BB_RE.sub("", out)
    out = _WS_RE.sub(" ", out).strip()
    return out


def _format_cve(entry: Entry, source: Source) -> str:
    score = _cvss_score(entry)
    title = _sanitize_cve_text(entry.title or entry.url or "").strip()
    line = f"[{source.name}] {title}"
    if score:
        line += f" (CVSS {score:.1f})"
    if entry.url:
        # URLs themselves are not stripped — they're not interpreted
        # as HTML by the notifier, and the body is the user-readable
        # copy. Length-cap so a pathological URL doesn't push the
        # body past Pushover's 4KB cap.
        line += f"\n{entry.url[:512]}"
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

    # Preference vector recompute. Aggregates the last
    # ``pref_vector_window_days`` of interactions into
    # ``UserProfile.preference_vector`` so the personal scorer
    # has something to work with. Default cadence: 10 min.
    # Fires once on startup (30s delay) so the vector catches
    # up after a restart without waiting a full interval.
    if settings.embedding_enabled and embedder().loaded:
        _scheduler.add_job(
            _recompute_preference_vector,
            trigger=IntervalTrigger(
                minutes=settings.pref_vector_recompute_interval_minutes,
            ),
            id="pref:recompute",
            name="Preference vector recompute",
            replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30),
            max_instances=1,
            coalesce=True,
        )

    # Rescore recent entries. composite_score / personal_score are
    # otherwise write-once at ingest — see _rescore_recent_entries's
    # docstring for why that means recency decay and preference-vector
    # updates never reach already-ingested rows without this. Runs
    # 15s after the preference-vector recompute above (best-effort
    # ordering, not a hard dependency) so the first pass after a
    # restart scores against the freshly-recomputed vector rather
    # than whatever was persisted before the restart.
    _scheduler.add_job(
        _rescore_recent_entries,
        trigger=IntervalTrigger(minutes=_RESCORE_INTERVAL_MINUTES),
        id="score:rescore_recent",
        name="Rescore recent entries",
        replace_existing=True,
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=45),
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

    # Reddit cross-reference sweep. Walks entries that don't already
    # have ``meta.reddit_thread_url`` and asks Hydra whether the URL
    # is discussed on Reddit. On a hit, the entry's ``meta`` gets a
    # ``reddit_thread_url`` + ``reddit_comment_count`` patch via
    # ``meta || jsonb`` (preserves other keys). No-op when
    # ``REDDIT_HYDRA_URL`` is unset (``reddit_client.is_configured``
    # short-circuits).
    _scheduler.add_job(
        _crossref_sweep,
        trigger=IntervalTrigger(hours=1),
        id="reddit:crossref_sweep",
        name="Reddit cross-reference sweep",
        replace_existing=True,
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )

    # Daily prune of the ``notification_dedup`` ledger. Without it
    # the table grows by one row per unique CVE URL / convergence
    # slug that ever fired — never freed, because
    # ``ON CONFLICT DO UPDATE`` only refreshes the timestamp on
    # re-fires. The audit flagged this as unbounded growth.
    # 24-hour cadence is plenty for a retention window measured in
    # days; the prune itself is a single DELETE bounded by an index.
    _scheduler.add_job(
        _prune_notification_dedup,
        trigger=IntervalTrigger(hours=24),
        id="notify:prune_dedup",
        name="Notification dedup ledger prune",
        replace_existing=True,
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
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
    if row.type == "reddit":
        return DynamicRedditPlugin(row)
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
    custom_headers: dict | None = None,
) -> Source:
    """Create a Source row and register its scheduler job.

    Idempotent on ``name``: if a row with the same name already
    exists, returns it without modification. This matches the
    class-driven upsert in ``_upsert_source`` and lets the POST
    endpoint safely retry without raising 409.

    The scheduler is a module-level singleton; if it isn't running
    (e.g. during tests) the row is still created and will be picked
    up on the next ``start_scheduler`` walk.

    Concurrency: two callers racing on the same name used to TOCTOU
    past the existence check and both reach ``INSERT``, with the
    second crashing on the unique constraint. We catch that and
    re-fetch the row the winner wrote. The race window is the
    roundtrip between ``SELECT`` and ``INSERT`` — vanishingly small
    in practice, but worth closing because the resulting 500 is
    user-facing and easy to trigger by double-clicking the
    Add-source button.
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
        custom_headers=custom_headers,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Lost the race — the unique constraint on ``name`` fired.
        # Roll back, refetch the row the winner wrote, return it.
        # ``expire_all`` drops the now-stale cached state so the
        # subsequent ``refresh`` issues a fresh SELECT.
        await session.rollback()
        winner = await session.scalar(select(Source).where(Source.name == name))
        if winner is None:
            # Should not happen — constraint fired but row vanished?
            # Re-raise as the IntegrityError so the caller can
            # surface a real error rather than a silent miss.
            raise
        return winner
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
    name: str | None = None,
    url: str | None = None,
    custom_headers: dict | None = _UNSET,
) -> Source | None:
    """Apply a partial update to a Source row and reschedule if needed.

    Returns the updated row, or None if no row exists with that id.

    All fields are optional — missing fields are left untouched. The
    route layer enforces name/url constraints (``^[a-z0-9_]+$``,
    URL parse, no built-in collisions, no name collisions with other
    rows); this function assumes the inputs have already passed
    validation.

    Scheduler effects:
      - ``active`` False → remove the dynamic job. A class-driven
        source (``name in list_sources()``) is left alone; the
        class-driven job continues independently of the row.
      - ``active`` True, ``refresh`` change, ``name`` change, or
        ``url`` change → re-add the dynamic job with the new trigger
        (``replace_existing=True`` handles idempotency). ``replace_existing=True``
        rebinds the job's args — for dynamic rows this means the
        next tick uses a freshly-constructed ``DynamicRssPlugin``
        instance built from the renamed row.

    URL changes clear the cached favicon: ``favicon_url`` and
    ``favicon_path`` are NULLed on the row, and the on-disk file
    at ``assets/favicons/<id>.*`` is unlinked via
    ``assets.delete_favicon``. The next ingest re-downloads the new
    favicon into the same path via ``os.replace`` (atomic
    overwrite). The file is keyed by ``source.id`` (stable across
    URL changes) so no rename is needed.
    """
    from app import assets as assets_mod

    row = await session.get(Source, source_id)
    if row is None:
        return None
    refresh_changed = (
        refresh is not None and refresh != row.refresh_interval_seconds
    )
    active_changed = active is not None and active != row.active
    category_changed = category is not None and category != row.category
    name_changed = name is not None and name != row.name
    url_changed = url is not None and url != row.url
    # ``custom_headers`` compares order-insensitively so an idempotent
    # PATCH with the same map doesn't trigger a scheduler rebuild.
    headers_changed = (
        custom_headers is not _UNSET and custom_headers != row.custom_headers
    )
    # ALL six fields participate in the early-return guard. The
    # original three-field version silently dropped name/url-only
    # PATCHes because the commit lives below the guard; the new
    # fields would have suffered the same bug.
    if not (
        refresh_changed
        or active_changed
        or category_changed
        or name_changed
        or url_changed
        or headers_changed
    ):
        return row
    if refresh is not None:
        row.refresh_interval_seconds = refresh
    if active is not None:
        row.active = active
        # Reactivating a source gives it a clean error slate so the
        # first transient hiccup after the user toggles it back on
        # doesn't immediately re-cross ``_AUTO_DISABLE_THRESHOLD`` and
        # flip it off again. The toggle is the user's explicit "I want
        # this back" signal — count that as a reset.
        if active and active_changed:
            row.error_count = 0
    if category is not None:
        row.category = category
    if name is not None:
        row.name = name
    if url_changed:
        row.url = url
        # Capture the OLD favicon metadata BEFORE nulling the
        # columns, then null them in-row so the next commit
        # persists the cleared state. The actual filesystem unlink
        # happens AFTER ``session.commit`` (see block below) so a
        # commit failure can't leave the row pointing at a deleted
        # file. The previous version unlinked first and committed
        # second — a transient DB error (name collision,
        # connection blip) left the row pointing at a missing file,
        # so the next render showed a broken image until the next
        # ingest ran. Commit-first / delete-second flips that:
        # a delete failure leaves a stale favicon on disk (the
        # next ingest's ``os.replace`` overwrites it via
        # ``assets._download``); far better than a dangling path.
        old_favicon_path = row.favicon_path
        row.favicon_url = None
        row.favicon_path = None
    else:
        old_favicon_path = None
    if custom_headers is not _UNSET:
        # Route layer has already normalized this (None → NULL,
        # empty dict → NULL). Storing ``{}`` would be wasteful.
        row.custom_headers = custom_headers
    await session.commit()
    if url_changed and old_favicon_path:
        # Post-commit filesystem cleanup. Only reached on a
        # successful commit, so the row no longer references the
        # cached file; a failed unlink here leaves a stale favicon
        # on disk for one ingest cycle (the next ingest's
        # ``os.replace`` overwrites it via ``assets._download``).
        try:
            assets_mod.delete_favicon(source_id)
        except Exception:
            logger.debug(
                "scheduler: delete_favicon(%d) raised — continuing",
                source_id, exc_info=True,
            )
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

    # Re-add (covers: enabled, refresh / name / url changed, or any
    # combination). ``replace_existing=True`` rebinds the args so
    # the freshly-constructed ``DynamicRssPlugin(row)`` below picks
    # up the new name/url.
    _add_or_replace_dynamic_job(_scheduler, row)
    return row


async def delete_source(session: AsyncSession, source_id: int) -> bool:
    """Drop a Source row and its scheduler job. Returns True if a
    row was deleted. Works for both built-in and dynamic rows.

    Entries belonging to this source are deleted in the SAME
    transaction. The FK is declared ``ON DELETE CASCADE`` so the DB
    would clean them up on its own, but the ORM-level relationship
    doesn't include ``cascade="all, delete-orphan"`` — by default
    SQLAlchemy disassociates children via ``UPDATE ... SET source_id
    = NULL``, which the NOT NULL constraint rejects with a 500.
    Issuing the DELETE explicitly matches the FK cascade semantics
    and avoids a misleading NotNullViolationError surfacing to the
    user as "can't delete feed."
    """
    row = await session.get(Source, source_id)
    if row is None:
        return False
    # Built-in sources (BBC, HN, etc.) ARE deletable now. The
    # row goes away, the scheduler job is removed, the
    # dashboard stops showing the source. The plugin class
    # stays registered in memory until the next backend
    # restart — at which point it re-registers itself as a
    # fresh row. The proper "permanent soft-delete across
    # restarts" fix is a ``deleted_at`` column on Source;
    # for now this matches the user's stated intent with
    # the simplest possible code change.
    # Delete child rows first. ``Entry.interactions`` also has an
    # ON DELETE CASCADE FK, so dropping entries drops their
    # interactions transparently — no need to fan out further.
    await session.execute(
        delete(Entry).where(Entry.source_id == source_id)
    )
    await session.delete(row)
    await session.commit()
    if _scheduler is not None:
        try:
            _scheduler.remove_job(_dynamic_job_id(source_id))
            logger.info("scheduler: removed dynamic job for %s (id=%d)", name, source_id)
        except Exception:
            pass
    return True

