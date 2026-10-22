"""APScheduler wiring for source polling.

`AsyncIOScheduler` runs in the same event loop as FastAPI, so fetches
happen in-process. Phase 1 doesn't have a worker / beat split; if
phase 2 needs to scale fetches across workers, swap in `RedisJobStore`
+ `run_in_apscheduler_role`.

The scheduler owns:
    - One repeating job per registered source plugin
    - Upsert into the entries table (by url — natural primary key for feeds),
      after the ingest rules (``app.ingest_rules``) pick which new items a
      poll stores: a first fetch keeps the newest few plus the last
      couple of days, never a feed's whole back catalogue
    - Embedding the entry text at ingest (phase 2)
    - Composite scoring at ingest (phase 2)
    - Periodic rescore of recently-fetched entries so recency decay and
      preference-vector updates actually reach rows already in the table
      instead of only ever applying at the moment of ingest
    - One-shot embedding backfill for entries with NULL embedding (phase 2)
    - Source-row bookkeeping (last_fetch_at, last_error, error_count,
      and for feed health last_attempt_at, last_item_count,
      last_new_count)
    - Exponential backoff on consecutive failures (widens the job's own
      interval instead of retrying at full cadence) and a short error
      kind (``ERROR_KINDS``: blocked / not found / network / not a
      feed / ...) prefixed onto ``last_error`` so the feed's status and
      its fix are diagnosable without reading backend logs
    - Auto-disabling sources after ``_AUTO_DISABLE_THRESHOLD``
      consecutive failures (see comment on the constant). Setup states
      (``SETUP_STATE_KINDS``: no article list, Reddit off) are recorded
      but never count as failures
    - Periodic purge of expired sessions (DB-backed auth)
    - Daily Brief generation (phase 4)
    - Periodic convergence-check + alert Brief (phase 4)
    - Post-ingest CVE notifications for high-CVSS entries (phase 4)
    - Check now (``queue_check``): pull to refresh moves a tab's feed
      jobs up, with a per-feed cooldown, without pushing a failing
      feed past its backoff
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import math
import json
import logging
import re
import time
from collections import Counter
from typing import Any, Callable, Optional, Sequence

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import bindparam, delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import assets, framing, ingest_rules, reddit_client, runtime_settings
from app.brief import BriefGenerator
from app.config import settings
from app.db import SessionLocal
from app.embeddings import embedder
from app.models import AppSetting, Entry, Interaction, NotificationDedup, Source, UserProfile
from app.notify import Notifier
from app.reddit_client import RedditDisabledError
from app.schemas import CHECK_NOW_MAX_IDS
from app.scoring import composite as composite_scorer
from app.scoring import convergence as convergence_helper
from app.scoring import personal as personal_scorer
from app.scoring import recency
from app.sources import list_sources
from app.sources.base import NoArticleListError, NotAFeedError, SourcePlugin
from app.sources.dynamic_reddit import DynamicRedditPlugin
from app.sources.dynamic_rss import DynamicRssPlugin
from app.sources.generic_scrape import GenericScrapePlugin
from app.sources.podcast import DynamicPodcastPlugin
from app.sources.youtube import DynamicYouTubePlugin

logger = logging.getLogger("popping.scheduler")

_scheduler: AsyncIOScheduler | None = None

# Scheduler-wide job defaults (see ``start_scheduler``). 120 s of
# misfire grace lets a run that starts late still run. With
# ``coalesce`` and every job's ``max_instances=1``, a grace longer
# than a job's interval (feeds can poll every 60 s) still never stacks
# runs: the missed fire times collapse into the one late run.
_JOB_DEFAULTS: dict[str, Any] = {"misfire_grace_time": 120, "coalesce": True}

# ``brief:daily`` fires once a day, so it can afford to run up to an
# hour late rather than miss the day entirely.
_BRIEF_DAILY_MISFIRE_GRACE_SECONDS = 3600

# Sentinel for ``update_source``'s ``custom_headers`` parameter:
# ``None`` is a meaningful value ("set the column to NULL / clear the
# override") so we need a separate marker for "field absent from the
# PATCH body, leave untouched." The route layer translates an
# explicit ``body.custom_headers == {}`` to ``None`` and anything
# missing to ``_UNSET``.
_UNSET: Any = object()
_brief_generator: Optional[BriefGenerator] = None


# Key prefix for the ``app_settings`` marker that records a
# deliberately-deleted built-in (class-driven) source — see
# ``_upsert_source`` and ``delete_source``. Built-in plugins register
# one permanent APScheduler job per class at every ``start_scheduler()``
# call (unlike dynamic rss/reddit/podcast/youtube rows, whose jobs are
# added/removed per DB row), so deleting the row alone doesn't stop
# the next tick — or the immediate startup tick on the next backend
# restart — from calling ``_upsert_source`` and silently recreating
# it. This marker is the durable "no, stay deleted" record that
# survives both.
_DELETED_BUILTIN_SOURCE_KEY_PREFIX = "deleted_builtin_source:"


async def _is_builtin_source_deleted(session: AsyncSession, name: str) -> bool:
    row = await session.get(AppSetting, f"{_DELETED_BUILTIN_SOURCE_KEY_PREFIX}{name}")
    return row is not None


async def _mark_builtin_source_deleted(session: AsyncSession, name: str) -> None:
    key = f"{_DELETED_BUILTIN_SOURCE_KEY_PREFIX}{name}"
    if await session.get(AppSetting, key) is not None:
        return
    session.add(AppSetting(key=key, value=dt.datetime.now(dt.timezone.utc).isoformat()))


async def _upsert_source(session: AsyncSession, plugin_cls: Any) -> Source | None:
    """Make sure the sources table has a row for this plugin. Idempotent.

    Returns None if the user has deliberately deleted this built-in
    source (see ``delete_source``) — the caller must treat that as
    "skip this ingest tick entirely," not "create it anyway."
    """
    existing = await session.scalar(
        select(Source).where(Source.name == plugin_cls.name)
    )
    if existing is not None:
        return existing
    if await _is_builtin_source_deleted(session, plugin_cls.name):
        return None
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


# Built-ins whose class category changed after installs already had
# their row: name → (old category, new category). ``_upsert_source``
# returns an existing row unchanged, so the new class category alone
# never reaches it. ``_move_builtin_categories`` moves each row once,
# only while it is still in the old category (a category the user
# picked stays), and records the move under this key prefix so a
# later choice of the old category is never undone.
_BUILTIN_CATEGORY_MOVES: dict[str, tuple[str, str]] = {
    "wikipedia_on_this_day": ("news", "on_this_day"),
}
_BUILTIN_CATEGORY_MOVED_KEY_PREFIX = "builtin_category_moved:"


async def _move_builtin_categories() -> None:
    async with SessionLocal() as session:
        for name, (old, new) in _BUILTIN_CATEGORY_MOVES.items():
            key = f"{_BUILTIN_CATEGORY_MOVED_KEY_PREFIX}{name}"
            if await session.get(AppSetting, key) is not None:
                continue
            row = await session.scalar(select(Source).where(Source.name == name))
            if row is not None and row.category == old:
                row.category = new
                logger.info("scheduler: moved built-in %s from %s to %s", name, old, new)
            # Recorded even with no row: a row created later gets the
            # class's new category from ``_upsert_source``.
            await session.execute(
                pg_insert(AppSetting)
                .values(key=key, value=dt.datetime.now(dt.timezone.utc).isoformat())
                .on_conflict_do_nothing(index_elements=["key"])
            )
        await session.commit()


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
    """Build the embed text from a normalized item. Empty → zero vector.

    The summary lives in ``norm["meta"]`` — ``validate_required``
    buckets every non-(title/url/published_at/meta) top-level key into
    the meta dict, including ``summary``. The previous
    ``norm.get("summary")`` read a top-level key the normalizer never
    produced, so the summary half of the designed
    ``title + " — " + summary`` embed text was always empty and every
    embedding was title-only — a weaker signal for the personal
    scorer, the preference vector, and Framing Watch clustering. RSS
    summaries are near-plain text with occasional HTML wrappers (the
    same shape ``routes.entries._clean_summary`` handles), so strip
    tags and collapse whitespace before embedding to keep markup
    noise out of the vector.
    """
    title = (norm.get("title") or "").strip()
    meta = norm.get("meta") or {}
    summary = ""
    if isinstance(meta, dict):
        raw_summary = meta.get("summary")
        if raw_summary:
            summary = re.sub(r"<[^>]+>", " ", str(raw_summary))
            summary = re.sub(r"\s+", " ", summary).strip()
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


# Favicon retry gate: at most one attempt per source per hour, measured
# from the feed's last check of any outcome
# (``_last_check_of_current_address``). Catches
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

# Backoff on consecutive failures. Without this, a source on a fast
# cadence (Hacker News: 5 min) that starts failing retries at full
# speed right up until auto-disable — 6 failures land inside 30
# minutes, hammering an already-struggling or paywalled endpoint the
# whole time. Doubling the interval per consecutive failure spreads
# those same 6 attempts out (5 → 10 → 20 → 40 → 80 → 160 min for a
# 5-min base), capped so a slow source (CISA KEV: 6h) doesn't back
# off into "once a week" territory before auto-disable even kicks in.
_BACKOFF_MULTIPLIER = 2
_BACKOFF_MAX_SECONDS = 6 * 3600  # 6h cap regardless of base interval


def _backoff_seconds(base_seconds: float, error_count: int) -> float:
    """The interval a feed's job runs at after ``error_count``
    consecutive failures: its base interval doubled per failure, capped
    at ``_BACKOFF_MAX_SECONDS``. ``queue_check``'s backoff gate uses
    the same number, measured from ``last_attempt_at``."""
    return min(base_seconds * (_BACKOFF_MULTIPLIER ** error_count), _BACKOFF_MAX_SECONDS)


# Every kind ``_classify_error`` can return. A failed or setup-state
# check writes ``last_error`` as ``[<kind>] <exception>``, and each kind
# has one fix in the feed list (the frontend's table keys on the
# bracketed prefix):
#   blocked         HTTP 401, 402, 403
#   not found       HTTP 404
#   upstream error  HTTP 5xx
#   http error      any other HTTP status, 429 included
#   network         connect errors and timeouts (there is no separate
#                   timeout kind)
#   not a feed      the address answered with something that isn't a
#                   feed (``NotAFeedError``)
#   no list         a website feed found no list of articles
#                   (``NoArticleListError``); a setup state
#   reddit off      Reddit is off on this server; a setup state
#   error           anything else
ERROR_KINDS: tuple[str, ...] = (
    "blocked",
    "not found",
    "upstream error",
    "http error",
    "network",
    "not a feed",
    "no list",
    "reddit off",
    "error",
)

# Setup states: the feed is set up in a way that can't bring stories
# (no article list to read, Reddit off on the server), whatever its
# age. They are recorded (``last_error``, ``last_attempt_at``) but are
# not failures: they never count toward ``error_count``, never back the
# job off and never auto-disable the feed, because retrying sooner or
# later changes nothing and stopping the feed would hide the fix.
SETUP_STATE_KINDS = frozenset({"no list", "reddit off"})

# Connect errors, read and write errors, timeouts of every stage, and a
# connection dropped mid-response. ``asyncio.TimeoutError`` covers a
# plugin's own ``wait_for`` budget.
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.TooManyRedirects,
    asyncio.TimeoutError,
)


def _classify_error(exc: Exception) -> str:
    """Short kind (``ERROR_KINDS``) prefixed onto ``last_error``.

    Retry/backoff is keyed on the raw consecutive-failure count, not on
    the kind, except that a setup state (``SETUP_STATE_KINDS``) is not
    a failure at all (see ``_ingest_once``). The kind tells the feed
    list which fix to offer. "blocked" specifically flags the
    401/402/403 family so a paywall/auth-gate (see rfd.py's Tollbit
    note) reads as "this endpoint is refusing us" rather than an
    unexplained error the user has to go dig through logs to
    understand.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 402, 403):
            return "blocked"
        if code == 404:
            return "not found"
        if 500 <= code < 600:
            return "upstream error"
        return "http error"
    if isinstance(exc, _NETWORK_ERRORS):
        return "network"
    if isinstance(exc, NotAFeedError):
        return "not a feed"
    if isinstance(exc, NoArticleListError):
        return "no list"
    if isinstance(exc, RedditDisabledError):
        return "reddit off"
    return "error"


def _ingest_job_id(plugin_cls: Any) -> str | None:
    """The scheduler job id for a class-driven or row-driven plugin.

    Mirrors the id each is registered under in ``start_scheduler`` /
    ``_add_or_replace_dynamic_job``. Returns None for a dynamic
    instance that somehow has no ``source_id`` (shouldn't happen —
    defensive so a reschedule attempt no-ops instead of raising).
    """
    if isinstance(plugin_cls, SourcePlugin):
        if plugin_cls.source_id is None:
            return None
        return _dynamic_job_id(plugin_cls.source_id)
    return f"ingest:{plugin_cls.name}"


def _reschedule_ingest_job(plugin_cls: Any, interval_seconds: float) -> None:
    """Change an ingest job's interval without waiting for a restart.

    Used both to back a job off after a failure and to restore its
    normal cadence once it starts succeeding again. Best-effort: a
    rescheduling hiccup is logged and swallowed rather than allowed
    to turn into (or mask) an ingest failure of its own — the job
    keeps running at whatever interval it already had.
    """
    if _scheduler is None:
        return
    job_id = _ingest_job_id(plugin_cls)
    if job_id is None:
        return
    try:
        _scheduler.reschedule_job(job_id, trigger=IntervalTrigger(seconds=interval_seconds))
    except Exception:
        logger.debug("scheduler: could not reschedule %s", job_id, exc_info=True)


async def _stored_urls(session: AsyncSession, urls: list[str]) -> set[str]:
    """The ones of ``urls`` already stored as entries (by any feed).
    ``_ingest`` runs it on every listing before the ingest rules, and
    hands it to a website feed (``_known_urls_lookup``) so the plugin
    skips stored pages before fetching them."""
    if not urls:
        return set()
    return set((await session.scalars(select(Entry.url).where(Entry.url.in_(urls)))).all())


async def _known_urls_lookup(urls: list[str]) -> set[str]:
    """``_stored_urls`` in its own short session: the ``known_urls``
    lookup ``_ingest`` passes to a plugin with ``takes_ingest_hints``
    (``app.sources.base.KnownUrls``), called during its fetch, when no
    ingest session is open."""
    async with SessionLocal() as session:
        return await _stored_urls(session, urls)


async def _ingest_tunables() -> ingest_rules.IngestTunables:
    """The ``ingest.*`` knobs from runtime settings, parsed and range
    checked (see ``ingest_rules.parse_tunables``). There is no env
    fallback, so an unset key reads as None and gets its default."""
    raw = await runtime_settings.get_many(list(ingest_rules.TUNABLE_KEYS))
    return ingest_rules.parse_tunables(raw)


async def _is_first_fetch(
    session: AsyncSession,
    source: Source,
    now: dt.datetime,
    rules: ingest_rules.IngestTunables,
) -> bool:
    """Whether this poll gets the first-fetch rules.

    True when the feed has never had a successful poll, when it has
    stored nothing since its baseline (``created_at``: when it was
    added, when its first fetch landed, or when its address last
    changed), or when its last success is more than
    ``resume_gap_hours`` ago (a pause, an auto-disable followed by a
    resume, a long failure streak). ``last_fetch_at`` alone is not
    enough: a poll that returns nothing still counts as a success and
    sets it, before the feed has ever brought a story.
    """
    last = source.last_fetch_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    if now - last > dt.timedelta(hours=rules.resume_gap_hours):
        return True
    # One EXISTS, answered from the entries.source_id index.
    has_rows = await session.scalar(
        select(
            exists().where(
                Entry.source_id == source.id,
                Entry.fetched_at >= source.created_at,
            )
        )
    )
    return not has_rows


async def _backlog_cutoff(
    session: AsyncSession,
    source: Source,
    now: dt.datetime,
    rules: ingest_rules.IngestTunables,
) -> dt.datetime | None:
    """For a website feed's fetch (``takes_ingest_hints``): the date
    before which a page is old backlog on this poll, the line
    ``ingest_rules`` draws for dated items on a later poll (the
    baseline's backfill window, or ``max_age_days``, whichever is
    later). None on a first fetch, which keeps the newest items
    whatever their age. ``generic_scrape._backlog_rule`` holds an
    undated page's sitemap ``<lastmod>`` against it."""
    if await _is_first_fetch(session, source, now, rules):
        return None
    cutoff = now - dt.timedelta(days=rules.max_age_days)
    baseline = _as_utc(source.created_at)
    if baseline is not None:
        cutoff = max(cutoff, baseline - dt.timedelta(hours=rules.backfill_hours))
    return cutoff


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    # Timestamps are timezone-aware UTC in practice, but tolerate a
    # naive value coming from a pre-upgrade DB row.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _last_check_of_current_address(source: Source) -> dt.datetime | None:
    """When the feed's current address was last checked, whatever the
    outcome, for the favicon gate.

    ``last_fetch_at`` moves only on a success; ``last_attempt_at`` also
    moves on a failure or a setup state. Measured from the success
    alone, a feed that never succeeds would retry its favicon on every
    tick, and a setup state is never backed off or stopped.

    An address change resets ``last_fetch_at`` and moves the baseline
    (``created_at``) to the time of the edit (``update_source``), and a
    new URL also clears the cached favicon. A ``last_attempt_at`` from
    before the baseline is a check of the old address, so it doesn't
    hold back the new address's favicon. (The baseline also moves when
    a first fetch lands, inside the transaction that stamps
    ``last_attempt_at``, so that stamp is never before it.)"""
    last_success = _as_utc(source.last_fetch_at)
    last_attempt = _as_utc(source.last_attempt_at)
    baseline = _as_utc(source.created_at)
    if last_attempt is None or (baseline is not None and last_attempt < baseline):
        return last_success
    if last_success is None or last_attempt > last_success:
        return last_attempt
    return last_success


def _should_retry_favicon(last_check_at: dt.datetime | None) -> bool:
    last_check_at = _as_utc(last_check_at)
    if last_check_at is None:
        return True
    return (dt.datetime.now(dt.timezone.utc) - last_check_at) >= _FAVICON_RETRY_INTERVAL


# Ingest runs in progress, by job id (``_ingest_job_id``).
# ``_ingest_running`` counts runs until they return.
# ``_ingest_unrecorded`` counts the runs that haven't yet stamped their
# result (``last_attempt_at``, see ``_stamp_attempt``); a run
# stays in ``_ingest_running`` after that while its thumbnail and
# og:image passes finish. ``queue_check`` reads both: APScheduler drops
# a run that comes due while one is still executing
# (``max_instances=1``), so a running job can't be moved up.
# ``_ingest_finished`` counts the runs that have returned, and is
# never counted down: ``queue_check`` reads its rows across awaits, so
# it snapshots this first, and a job whose count moved while it was
# reading has a run that finished after the row was read (its
# ``error_count`` and ``last_fetch_at`` may be stale).
# Process-local (see ARCHITECTURE.md).
_ingest_running: Counter[str] = Counter()
_ingest_unrecorded: Counter[str] = Counter()
_ingest_finished: Counter[str] = Counter()


def _count_down(counter: Counter[str], key: str) -> None:
    counter[key] -= 1
    if counter[key] <= 0:
        del counter[key]


def _stamp_attempt(source: Source, mark_recorded: Callable[[], None]) -> dt.datetime:
    """Record when this run's check finished (``last_attempt_at``).

    The one place the stamp is written. Every path that records an
    outcome (success, failure, setup state) calls it inside the
    transaction that writes that outcome, so the outcome and its time
    land together. Paths that record nothing (deleted, paused, an
    address changed mid-fetch) don't stamp.

    The run is marked recorded first: a ``queue_check`` that still saw
    it as unrecorded (and so reported it queued) took its ``queued_at``
    before this stamp, so its caller sees ``last_attempt_at >=
    queued_at`` and knows the check landed. Returns the stamp."""
    mark_recorded()
    at = dt.datetime.now(dt.timezone.utc)
    source.last_attempt_at = at
    return at


async def _ingest(plugin_cls: Any) -> dict:
    """Fetch from a plugin, write entries to DB, update source bookkeeping.

    Returns a small summary dict the manual /ingest endpoint surfaces.
    Catches all exceptions per-source so one broken source can't take
    the scheduler down.

    Every run, scheduled or manual, is counted in ``_ingest_running``
    and ``_ingest_unrecorded`` for ``queue_check``.
    """
    job_id = _ingest_job_id(plugin_cls)
    if job_id is None:
        return await _ingest_once(plugin_cls, lambda: None)
    _ingest_running[job_id] += 1
    _ingest_unrecorded[job_id] += 1
    recorded = False

    def mark_recorded() -> None:
        nonlocal recorded
        if not recorded:
            recorded = True
            _count_down(_ingest_unrecorded, job_id)

    try:
        return await _ingest_once(plugin_cls, mark_recorded)
    finally:
        mark_recorded()
        _count_down(_ingest_running, job_id)
        _ingest_finished[job_id] += 1


async def _ingest_once(plugin_cls: Any, mark_recorded: Callable[[], None]) -> dict:
    """One ingest run (see ``_ingest``). A run that records an outcome
    (a success, a failure or a setup state) calls ``mark_recorded``
    through ``_stamp_attempt`` just before it stamps
    ``last_attempt_at``, so a success's image passes run after it.
    Every other path (deleted, paused, re-pointed mid-fetch) writes
    nothing and returns, and ``_ingest`` marks it then.

    A paused feed's summary carries ``status: "paused"``."""
    summary = {
        "source": plugin_cls.name,
        "fetched": 0,
        "inserted": 0,
        "duplicates": 0,
        "skipped": 0,
        "error": None,
    }
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
    # Every entry id inserted this tick. Re-fetched after the commit
    # for the post-ingest hook (CVE notifications). The previous code
    # only collected ids that landed in ``thumbnail_jobs`` — i.e.
    # entries with a feed-supplied image — so the notify hook never
    # saw NVD/CISA entries (which ship no images) and high-CVSS CVE
    # alerts never fired. Tracking every insert closes that gap; the
    # notify path itself filters by CVSS threshold and no-ops cheaply
    # for entries without cvss meta.
    inserted_ids: list[int] = []
    # Track entries that need a thumbnail pass. Collected here so we
    # can do the network fetches outside the DB session (each
    # ``fetch_thumbnail`` can take up to 20s with the new retry
    # logic — holding a DB transaction for 50 entries × 20s is a
    # pool starvation waiting to happen). The pass runs after the
    # entries commit and writes back via a single bulk UPDATE.
    thumbnail_jobs: list[tuple[int, str]] = []
    # Entries that landed with NO feed-supplied image — deferred the
    # same way ``thumbnail_jobs`` is, but probing the article's own
    # page for an og:image fallback (see ``assets.fetch_og_image_fallback``)
    # instead of downloading a URL we already have. Runs as its own
    # pass, after the thumbnail pass, so a source with lots of
    # image-less entries doesn't double the concurrency of the first
    # pass.
    og_image_jobs: list[tuple[int, str]] = []
    # Decide whether to fetch the favicon BEFORE opening the long
    # ingest transaction. ``fetch_favicon`` makes two network
    # round-trips (HTML probe + icon download, each up to 10s) —
    # running them inside the session would hold an idle DB
    # transaction for ~20s per source on every retry, starving the
    # pool. We snapshot the upserted source's id/url/state, close
    # the session, run the network work, and re-open the session
    # only to persist the result. Set before the ``try`` because the
    # failure path below reads them too.
    source_id: int | None = None
    source_url: str | None = None
    # What the plugin is about to fetch: the feed's URL, sitemap
    # URL and link pattern. If the row no longer has that address
    # when the batch (or the failure) is recorded, it came from the
    # old address and is discarded (see the commit session and the
    # failure path below). A row-driven plugin carries the address it
    # was built from (``SourcePlugin.fetch_target``): its job keeps
    # that instance until ``update_source`` replaces it after an
    # edit's commit, so the row read below can already hold the new
    # address while this instance still fetches the old one. A
    # class-driven built-in's address can't be edited, so its row is
    # the snapshot.
    fetch_target: tuple[str | None, ...] | None = getattr(plugin, "fetch_target", None)
    try:
        needs_favicon = False
        reddit_off = False
        async with SessionLocal() as session:
            source = await _upsert_source(session, plugin_cls)
            if source is None:
                # User deliberately deleted this built-in source — see
                # ``_upsert_source`` / ``delete_source``. Skip the tick
                # entirely rather than recreating the row.
                logger.debug("ingest: %s was deleted by the user — skipping", plugin_cls.name)
                return summary
            source_id = source.id
            source_url = source.url
            if fetch_target is None:
                fetch_target = (source.url, source.sitemap_url, source.link_pattern)
            # Only subreddit feeds depend on the server's Reddit setting
            # (``is_disabled`` is global). Read here so a Reddit-off feed
            # doesn't ask reddit.com for its favicon either; raised
            # below, after the paused check.
            reddit_off = source.type == "reddit" and reddit_client.is_disabled()
            needs_favicon = (
                source.favicon_url is None
                and not reddit_off
                and _should_retry_favicon(_last_check_of_current_address(source))
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

        rules = await _ingest_tunables()
        takes_hints = getattr(plugin, "takes_ingest_hints", False)
        backlog_before: dt.datetime | None = None
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
                summary["status"] = "paused"
                return summary
            if takes_hints:
                backlog_before = await _backlog_cutoff(
                    session, source, dt.datetime.now(dt.timezone.utc), rules
                )

        # Raised before the fetch (``reddit_off`` is read above), so the
        # failure path records it as the ``[reddit off]`` setup state
        # instead of a silently empty success.
        if reddit_off:
            raise RedditDisabledError(
                "Reddit isn't set up on this server (no proxy, and direct mode is off)"
            )

        # Network fetch with no DB session open: some plugins (podcasts,
        # generic scrape) take minutes, and an idle transaction for that
        # long starves the connection pool.
        if takes_hints:
            # A website feed fetches one page per candidate, so it asks
            # which candidates are already stored before fetching them
            # (one short session per poll), and caps undated pages
            # itself, holding them against this poll's backlog line
            # (``GenericScrapePlugin``).
            raw_items = await plugin.fetch(
                known_urls=_known_urls_lookup,
                max_undated=rules.max_undated_per_poll,
                backlog_before=backlog_before,
            )
        else:
            raw_items = await plugin.fetch()
        summary["fetched"] = len(raw_items)

        async with SessionLocal() as session:
            # Loaded first: it commits when it has to create the
            # profile row, which would end the transaction (and drop the
            # lock) taken below.
            profile = await _load_profile(session)
            # Lock the feed's row for the rest of this transaction
            # (FOR NO KEY UPDATE: the entry inserts' foreign-key locks
            # don't conflict with it). ``update_source`` takes the same
            # lock, so an address change either lands before this
            # point, and the check below discards the batch, or waits
            # until this commit and then resets the first-fetch state
            # after it.
            source = await session.get(
                Source,
                source_id,
                with_for_update={"key_share": True},
                populate_existing=True,
            )
            if source is None:
                return summary
            if (source.url, source.sitemap_url, source.link_pattern) != fetch_target:
                logger.info(
                    "ingest: %s changed address during the fetch — discarding %d item(s)",
                    plugin_cls.name, len(raw_items),
                )
                return summary
            normalized: list[dict] = []
            for raw in raw_items:
                try:
                    normalized.append(plugin.normalize(raw))
                except ValueError as exc:
                    logger.warning("%s: skipping bad item: %s", plugin_cls.name, exc)
            # Most polls return mostly items we already have. Skip them
            # before embedding, which is the expensive step; the INSERT's
            # ON CONFLICT still covers a concurrent writer.
            urls = [n["url"] for n in normalized if n.get("url")]
            known = await _stored_urls(session, urls)
            # The ingest rules (``app.ingest_rules``): a first fetch
            # keeps the newest few plus the last couple of days, later
            # polls skip old and undated backlog, and one poll stores at
            # most ``max_per_poll`` new items.
            now = dt.datetime.now(dt.timezone.utc)
            first_fetch = await _is_first_fetch(session, source, now, rules)
            kept, skipped = ingest_rules.select_items_for_ingest(
                normalized,
                known=known,
                first_fetch=first_fetch,
                baseline_at=source.created_at,
                now=now,
                keep_first=rules.first_fetch_keep,
                backfill_h=rules.backfill_hours,
                max_age_d=rules.max_age_days,
                per_poll=rules.max_per_poll,
                max_undated=rules.max_undated_per_poll,
            )
            summary["skipped"] = sum(skipped.values())
            # Known URLs and repeats within this listing.
            summary["duplicates"] = len(normalized) - len(kept) - summary["skipped"]
            if skipped:
                logger.info(
                    "ingest: %s skipped %d item(s)%s: %s",
                    plugin_cls.name,
                    summary["skipped"],
                    " (first fetch)" if first_fetch else "",
                    ", ".join(f"{reason}={n}" for reason, n in sorted(skipped.items())),
                )
            for norm in kept:
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
                # No Entry row exists yet (the INSERT below creates it),
                # so score from the fields directly.
                composite = composite_scorer.score_partial(
                    published_at=norm["published_at"],
                    raw_score=raw_score,
                    embedding=embedding,
                    entry_meta=meta or None,
                    source=source,
                    profile=profile,
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
                    inserted_ids.append(inserted_id)
                    if remote_image_url:
                        # Defer the network fetch to a single pass
                        # after the commit (see below).
                        thumbnail_jobs.append((inserted_id, remote_image_url))
                    elif norm["url"]:
                        # No image from the feed itself — queue an
                        # og:image fallback probe against the
                        # article's own page. Best-effort: many
                        # entries still won't get one (paywalls, non-
                        # HTML targets, sites with no og:image), but
                        # this covers most ordinary news/blog
                        # articles regardless of source type.
                        og_image_jobs.append((inserted_id, norm["url"]))
                else:
                    summary["duplicates"] += 1
            if first_fetch and summary["inserted"] > 0:
                # The first fetch landed: it becomes the feed's
                # baseline. ``now()`` is this transaction's start time,
                # the same value the rows just inserted got as
                # ``fetched_at``, so ``_is_first_fetch`` sees them from
                # the next poll on. An empty first fetch leaves the
                # baseline alone, so a feed that was blocked for days
                # still gets a first fetch when it finally works.
                source.created_at = func.now()
            was_backed_off = (source.error_count or 0) > 0
            # Marked before the timestamp is taken (``_stamp_attempt``):
            # a ``queue_check`` that still saw this run as unrecorded
            # has a ``queued_at`` at or before this stamp, so its caller
            # sees the check land. On a success the two stamps are the
            # same instant.
            source.last_fetch_at = _stamp_attempt(source, mark_recorded)
            source.last_error = None
            source.error_count = 0
            # How many items the feed listed, before the ingest rules
            # cut them, and how many of them were new stories.
            source.last_item_count = summary["fetched"]
            source.last_new_count = summary["inserted"]
            await session.commit()
            if was_backed_off:
                # Recovering from a failure streak — restore the
                # normal cadence (see the failure branch below for
                # where it got widened). A healthy source that's
                # never failed skips this; nothing to restore.
                _reschedule_ingest_job(plugin_cls, plugin_cls.refresh_interval_seconds)
            # Re-fetch inserted rows for the post-hook (notification
            # path) after the commit so they have stable ids. Covers
            # EVERY insert this tick (``inserted_ids``), not just the
            # thumbnail-eligible ones — see the comment on
            # ``inserted_ids`` above for why the narrower set silently
            # disabled the CVE notify path.
            if inserted_ids:
                rows = (
                    await session.scalars(select(Entry).where(Entry.id.in_(inserted_ids)))
                ).all()
                for row in rows:
                    newly_inserted.append((row, source))
    except Exception as exc:
        category = _classify_error(exc)
        # A setup state isn't a failure (``SETUP_STATE_KINDS``): no
        # traceback (it is logged below, once the row says whether the
        # feed just entered it), and none of the failure bookkeeping.
        setup_state = category in SETUP_STATE_KINDS
        if not setup_state:
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
        detail = f"{exc_type}: {msg}" if msg else exc_type
        summary["error"] = f"[{category}] {detail}"
        try:
            async with SessionLocal() as session:
                if source_id is not None:
                    # The same row lock as the commit session, so an
                    # address change either lands first (and the check
                    # below drops this result) or waits for this commit.
                    source = await session.get(
                        Source,
                        source_id,
                        with_for_update={"key_share": True},
                        populate_existing=True,
                    )
                else:
                    # Failed before the row was read (the upsert itself).
                    source = await session.scalar(
                        select(Source).where(Source.name == plugin_cls.name)
                    )
                if (
                    source is not None
                    and fetch_target is not None
                    and (source.url, source.sitemap_url, source.link_pattern) != fetch_target
                ):
                    # The error came from an address the feed no longer
                    # has. Recording it would count a failure (and back
                    # off the job) against the new address, and its
                    # ``last_attempt_at`` would tell a Check now waiting
                    # on the new address that it had been checked.
                    logger.info(
                        "ingest: %s changed address during the fetch — not recording its error",
                        plugin_cls.name,
                    )
                    source = None
                if source is not None:
                    if setup_state:
                        # One INFO line when the feed enters the state,
                        # DEBUG while it stays: a server with Reddit off
                        # would otherwise log every subreddit every tick.
                        entering = not (source.last_error or "").startswith(f"[{category}]")
                        logger.log(
                            logging.INFO if entering else logging.DEBUG,
                            "ingest: %s can't bring stories [%s]: %s",
                            plugin_cls.name, category, detail,
                        )
                    source.last_error = summary["error"]
                    # A setup state skips all of this: ``error_count``,
                    # the job's interval and ``active`` stay as they were.
                    if not setup_state:
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
                        elif source.active:
                            # Back off instead of retrying at full cadence —
                            # see _BACKOFF_MULTIPLIER's comment. Skipped once
                            # auto-disabled above (a disabled source doesn't
                            # tick at all, so there's nothing to reschedule).
                            _reschedule_ingest_job(
                                plugin_cls,
                                _backoff_seconds(plugin_cls.refresh_interval_seconds, source.error_count),
                            )
                    _stamp_attempt(source, mark_recorded)
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
    #
    # Slice 27: bound concurrency with an asyncio.Semaphore. A source
    # with ~100 entries was firing 100 concurrent HTTP requests with
    # no throttle — same anti-pattern as the old framing-LLM path
    # (slice 16/26 era). 10 concurrent is enough to overlap the ~3s
    # per-fetch latency on a small ingest while keeping a big
    # ingest's open-connection count bounded.
    _FETCH_CONCURRENCY = 10

    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _bounded_fetch(fn, *args):
        """Wrap an asset-fetcher call in the semaphore so the gather
        below can't fire more than ``_FETCH_CONCURRENCY`` at once.
        ``assets.fetch_thumbnail`` and ``fetch_og_image_fallback``
        both return ``Optional[str]`` / ``Optional[tuple[str, str]]``;
        we forward whatever they return so the surrounding bulk
        UPDATE loop sees the same shape.
        """
        async with sem:
            return await fn(*args)

    def _thumbnail_fetch(eid, url):
        return _bounded_fetch(assets.fetch_thumbnail, url, eid)

    def _og_image_fetch(eid, url):
        return _bounded_fetch(assets.fetch_og_image_fallback, url, eid)

    if thumbnail_jobs:
        results = await asyncio.gather(
            *(_thumbnail_fetch(eid, url) for eid, url in thumbnail_jobs),
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

    # og:image fallback pass. Same outside-the-session, gather-then-
    # bulk-UPDATE shape as the thumbnail pass above, run afterward
    # (not merged into the same gather) so entries that already have
    # a perfectly good feed-supplied image never pay for the extra
    # HTML probe. Unlike the thumbnail pass, both ``image_url`` (the
    # discovered og:image URL, for provenance) and ``image_path``
    # (the cached file) get written — these entries had NULL
    # image_url going in.
    if og_image_jobs:
        results = await asyncio.gather(
            *(_og_image_fetch(eid, url) for eid, url in og_image_jobs),
            return_exceptions=True,
        )
        og_result_by_id: dict[int, tuple[str, str]] = {}
        for (eid, _url), result in zip(og_image_jobs, results):
            if isinstance(result, BaseException):
                logger.debug("og:image fallback failed for entry %d", eid, exc_info=result)
                continue
            if result is not None:
                og_result_by_id[eid] = result
        if og_result_by_id:
            try:
                async with SessionLocal() as session:
                    from sqlalchemy import case
                    url_whens = {eid: remote for eid, (remote, _local) in og_result_by_id.items()}
                    path_whens = {eid: local for eid, (_remote, local) in og_result_by_id.items()}
                    ids = list(og_result_by_id.keys())
                    url_expr = case(url_whens, value=Entry.id)
                    path_expr = case(path_whens, value=Entry.id)
                    await session.execute(
                        Entry.__table__.update()
                        .where(Entry.id.in_(ids))
                        .values(image_url=url_expr, image_path=path_expr)
                    )
                    await session.commit()
                    logger.info(
                        "og:image fallback thumbnails cached for %s: %d / %d",
                        plugin_cls.name, len(og_result_by_id), len(og_image_jobs),
                    )
            except Exception:
                logger.exception("og:image fallback bulk update failed for %s", plugin_cls.name)

    # Post-ingest hook: high-CVSS CVE notifications. Only fires when
    # the scheduler actually inserted something (not on every duplicate
    # re-ingest), and only when the notifier is wired up.
    if newly_inserted and _brief_generator is not None:
        await _maybe_notify_cves(newly_inserted)

    return summary


async def _backfill_embeddings(batch_size: int | None = None) -> None:
    """One-shot at startup: embed any existing entries with NULL embedding.

    Runs in batches so we don't OOM the embedder. Logs progress; never
    raises — a failure here is logged and swallowed so the rest of
    the app can start.
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
    # ``dwell`` is the per-card read-time signal, and the only
    # value-scaled type: the recompute reads ``Interaction.value``
    # (dwell seconds) and applies
    #     contribution = PREF_VECTOR_WEIGHT_DWELL * min(value / 10, 1.0)
    # via ``_interaction_row_weight`` below. The knob lives in
    # config (``pref_vector_weight_dwell``, default 0.3, override
    # with PREF_VECTOR_WEIGHT_DWELL) and is read at call
    # time. The cap keeps a tab left open overnight from
    # out-weighing a deliberate deep read; the 10s reference keeps
    # the knob's units interpretable. NOTE: the weight is a
    # MULTIPLIER on the normalized read time, not a divisor -- the
    # comment previously here claimed "0.3 means a 10-second read
    # contributes ~3.0, comparable to a single click", which
    # contradicted the issue #99 formula and is corrected here
    # (see the sweep2 PR's notes-for-review for why the default
    # deliberately stays below click-parity).
    "dwell": settings.pref_vector_weight_dwell,
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


# Reference read time for the dwell value-scaling (issue #99): a
# fully-read article contributes the full knob weight; shorter
# reads scale linearly below it; longer reads cap at it.
_DWELL_REFERENCE_SECONDS = 10.0


def _interaction_row_weight(itype: str, value: float | None) -> float:
    """Per-row contribution weight for the preference-vector recompute.

    Fixed-weight types (view / click / thumb_up / bookmark /
    thumb_down / never / share) read ``_INTERACTION_WEIGHTS``.
    ``dwell`` is value-scaled per issue #99:

        contribution = PREF_VECTOR_WEIGHT_DWELL * min(value / 10, 1.0)

    where the knob (``settings.pref_vector_weight_dwell``, default
    0.3) is the fully-read cap and ``value`` is the dwell time in
    seconds. The knob is read at CALL time so an env override
    actually changes the recompute's behavior (pinned by
    ``test_pref_vector_dwell_value.py``); ``_INTERACTION_WEIGHTS``
    keeps a snapshot entry for the validity mask and for any reader
    that only asks "is this type a signal at all".
    """
    if itype == "dwell":
        knob = settings.pref_vector_weight_dwell
        try:
            seconds = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds < 0.0:
            seconds = 0.0
        return knob * min(seconds / _DWELL_REFERENCE_SECONDS, 1.0)
    return _INTERACTION_WEIGHTS.get(itype, 0.0)


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

    OIDC off (the homelab case): aggregate over the fixed
    ``_AGGREGATION_USER_IDS_ALL`` tuple — see that constant's
    docstring.

    OIDC on: this is a single-operator app, not a multi-tenant one
    (there's exactly one ``UserProfile`` row, no per-user column),
    so "the OIDC user" is whichever real sub(s) actually show up in
    the interactions table — there's no per-request context to read
    from inside a scheduled background job. Scope to those subs plus
    "local-bypass" (the same operator hitting from the LAN CIDR
    bypass is still their own signal); "anonymous" and "default" stay
    excluded — those are soft-auth artifacts from requests that
    couldn't be tied to a real identity, and folding them in here
    would blend an unauthenticated caller's taste into the one
    account that can see it.
    """
    if not settings.oidc_enabled:
        return _AGGREGATION_USER_IDS_ALL
    rows = await session.execute(
        select(Interaction.user_id)
        .where(Interaction.user_id.notin_(_AGGREGATION_USER_IDS_ALL))
        .distinct()
    )
    oidc_subs = tuple(r[0] for r in rows.all())
    return oidc_subs + ("local-bypass",)


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
    2. Sum ``entry.embedding * _interaction_row_weight(type, value)``
       for each interaction. The sum is a 384-dim vector. Dwell rows
       are value-scaled --
       ``PREF_VECTOR_WEIGHT_DWELL * min(seconds / 10, 1.0)`` per
       issue #99 -- every other type uses its fixed
       ``_INTERACTION_WEIGHTS`` entry.
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
                    select(
                        Entry.id,
                        Entry.embedding,
                        Interaction.type,
                        Interaction.value,
                    )
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
            # Aggregate in a single numpy matmul instead of a Python
            # ``for i in range(dim)`` loop. The old code ran
            # ``dim * len(rows)`` Python-level ops — for a heavy
            # reader with 5000 interactions over 30 days at dim=384
            # that's 1.9M Python float adds, dominated by Python
            # interpreter overhead. With numpy the whole aggregation
            # is a single ``sum(w_i * emb_i)`` over a stacked
            # (rows, dim) matrix, which numpy executes in C in
            # ~50ms. The ``agg`` here is the user-taste centroid;
            # the L2-normalise + blend + re-normalise below stays
            # in numpy so the whole hot path is one C-level
            # expression.
            import numpy as np  # local import; numpy is a runtime dep but
                                # keeping it local means the import cost is
                                # paid only when this code path is hit
                                # (which already requires numpy via the
                                # embedder's dependency tree).
            valid_mask = [
                (emb is not None) and (_INTERACTION_WEIGHTS.get(itype, 0.0) != 0.0)
                for _entry_id, emb, itype, _value in rows
            ]
            if not any(valid_mask):
                return
            embs = np.asarray(
                [np.asarray(emb, dtype=np.float32) for emb, keep in zip(
                    (r[1] for r in rows), valid_mask,
                ) if keep and emb is not None],
                dtype=np.float32,
            )
            # Dwell rows are value-scaled via the settings knob
            # (``_interaction_row_weight``); every other type keeps
            # its fixed ``_INTERACTION_WEIGHTS`` entry. Row shape is
            # (id, embedding, type, value) -- value joined the
            # SELECT for the dwell normalization.
            weights = np.asarray(
                [
                    _interaction_row_weight(r[2], r[3])
                    for r, keep in zip(rows, valid_mask)
                    if keep and r[1] is not None
                ],
                dtype=np.float32,
            )
            n_used = int(embs.shape[0])
            if n_used == 0:
                return
            # Weighted sum via einsum: ``np.einsum('i,ij->j', w, m)``
            # is the canonical numpy idiom for ``sum_i w_i * m_ij``
            # and runs in compiled C without building a
            # ``(n_used, dim)`` intermediate. For n_used=5000 at
            # dim=384 the einsum is ~350x faster than the
            # equivalent Python ``for`` loops and ~4x faster than
            # the ``(weights[:, None] * embs).sum(axis=0)``
            # expression because no temporary is allocated.
            agg = np.einsum("i,ij->j", weights, embs).astype(np.float32)
            # L2-normalize the aggregated vector so the cosine math
            # in personal.py is meaningful.
            norm_sq = float(np.dot(agg, agg))
            if norm_sq == 0.0:
                return
            agg = agg * (1.0 / math.sqrt(norm_sq))
            # Load the current vector and blend. NULL →
            # zero vector; the blend collapses to
            # ``blend_new * agg``.
            profile = await _load_profile(session)
            old_list = profile.preference_vector or [0.0] * dim
            if len(old_list) != dim:
                # Embedder dim changed between the old
                # vector and now. Drop the old vector
                # rather than crash on the blend; the
                # next tick (10 min later) re-stabilises.
                logger.warning(
                    "pref vector: dim mismatch (old=%d, new=%d), discarding old",
                    len(old_list), dim,
                )
                old_list = [0.0] * dim
            old = np.asarray(old_list, dtype=np.float32)
            blend = max(0.0, min(1.0, settings.pref_vector_blend_new))
            # Blended = blend * agg + (1 - blend) * old, in numpy
            # so it's a single C-level expression regardless of
            # dim.
            blended = blend * agg + (1.0 - blend) * old
            # Re-normalize the blend too, so the persisted
            # vector is always unit length.
            sq = float(np.dot(blended, blended))
            if sq > 0.0:
                blended = blended * (1.0 / math.sqrt(sq))
            # pgvector's ``vector`` column type accepts a Python
            # list of floats; convert the numpy array back so the
            # SQLAlchemy binding emits the expected shape.
            blended_list = blended.tolist()
            await session.execute(
                UserProfile.__table__.update()
                .where(UserProfile.id == profile.id)
                .values(preference_vector=blended_list)
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
                # Guard against NaN/inf in the scorer's output. A
                # single NaN in a column silently breaks
                # ``composite_score >= N`` filters across the whole
                # feed (NaN comparisons return False) and would
                # poison ranking + recommendation queries that
                # depend on a finite score. The scorers' public
                # contract is to return a finite float; if one
                # doesn't, log a WARNING and skip the row rather
                # than write a corrupt value.
                if not math.isfinite(new_composite) or not math.isfinite(new_personal):
                    logger.warning(
                        "rescore: non-finite score for entry id=%s "
                        "(composite=%r, personal=%r) — skipping",
                        entry.id, new_composite, new_personal,
                    )
                    continue
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
                    len(updates), len(rows),
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


async def _cluster_framing() -> None:
    """Framing Watch: recompute same-story/different-headline clusters
    over the trailing ``framing_window_hours``. See ``app.framing`` for
    the full algorithm — this is just the scheduler wrapper (own
    session, log-and-swallow on failure, matching every other job in
    this module)."""
    try:
        async with SessionLocal() as session:
            result = await framing.cluster_recent_entries(session)
            logger.info(
                "framing: considered=%d clusters=%d tone_calls=%d",
                result["entries_considered"], result["clusters"], result["tone_calls"],
            )
    except Exception:
        logger.exception("framing cluster job failed")


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

    # Short-circuit when the crossref cache is empty: the sweep can
    # only match URLs posted in a configured subreddit in the last
    # 15 min. In direct-Atom mode without active fetches, the cache
    # is empty and every ``search_thread_by_url`` call returns None
    # after 100ms of polite sleep — a 5s/tick of pure wait, every
    # hour, for no work. Bail before the candidate scan so a quiet
    # week doesn't burn 80 scheduler seconds.
    #
    # Direct attribute access (not ``getattr(..., None)``): the
    # cache is defined in ``app.reddit_client`` as a module-level
    # dict; a missing attribute means the module was edited and we
    # want the AttributeError to surface so the operator notices,
    # not a silent "no work" return that masks the real bug.
    if not reddit_client._crossref_cache:
        return

    # Phase 1: open a short-lived session to gather the candidate
    # set, then close it. The sweep's network work is slow (50
    # entries × ~100ms = ~5s) and we don't want the session
    # pinned for the duration — that would block other concurrent
    # requests on the connection pool. The same pattern as the
    # thumbnail and og:image passes below.
    to_check: list[tuple[int, str]] = []
    try:
        async with SessionLocal() as session:
            # Fetch the set of Source rows that are Reddit-typed so
            # we can skip their entries (their own URL IS the
            # Reddit thread) directly in the candidate query below.
            reddit_source_ids = set((
                await session.scalars(
                    select(SourceModel.id).where(SourceModel.type == "reddit")
                )
            ).all())

            # "Not yet stamped" is pushed into SQL via the GIN-indexed
            # ``NOT (meta ? 'reddit_thread_url')`` predicate (see
            # migration 0014, which added the index specifically for
            # this query) instead of an in-Python filter after an
            # unconditional ``LIMIT``. Filtering in Python meant the
            # top-``_CROSSREF_BATCH * 4`` most-recent-by-id rows were
            # fetched regardless of whether they'd already been
            # stamped or were Reddit-sourced — on any install where
            # most recent entries have already been checked (the
            # steady-state case after the first sweep), the LIMIT
            # budget was mostly spent on rows the Python loop would
            # immediately discard, so genuinely-uncheckable-yet
            # entries could scroll out of the window before ever
            # being looked at even once. Filtering in SQL means every
            # row this query returns is a real candidate.
            stmt = (
                select(Entry.id, Entry.url)
                .where(
                    Entry.url.isnot(None),
                    # ``meta`` is nullable; ``NULL ? 'key'`` evaluates
                    # to NULL (excluded by a bare WHERE), not TRUE, so
                    # COALESCE to an empty object first — a NULL meta
                    # has no reddit_thread_url key either.
                    text("NOT (COALESCE(entries.meta, '{}'::jsonb) ? 'reddit_thread_url')"),
                )
                .order_by(Entry.id.desc())
                .limit(_CROSSREF_BATCH * 4)
            )
            if reddit_source_ids:
                stmt = stmt.where(Entry.source_id.notin_(reddit_source_ids))
            candidates = (await session.execute(stmt)).all()
            to_check = [(row.id, row.url) for row in candidates]
    except Exception:
        logger.exception("crossref candidate scan failed")
        return

    if not to_check:
        return
    logger.info("crossref sweep: %d entries to check", len(to_check))

    # Phase 2: outside the session, do the network work. Per-entry
    # 100ms sleep (NOT per-batch) because the in-batch cost is
    # dominated by the per-entry ``search_thread_by_url`` roundtrip
    # — 100ms between *batches* would bunch all 50 requests into a
    # single 0.5s wall, while 100ms *per entry* spreads the same
    # 5 seconds across the tick and gives Hydra's per-connection
    # throughput a breather.
    #
    # We collect (id, thread_url, num_comments) tuples instead of
    # updating the DB inline. The DB session was the long-held
    # resource in the old code; moving the network loop outside
    # the session shrinks the held window from ~5s to a few ms.
    patches: list[tuple[int, str, int]] = []
    for entry_id, entry_url in to_check[:_CROSSREF_BATCH]:
        try:
            match = await reddit_client.search_thread_by_url(entry_url)
        except Exception:
            # Defensive: search_thread_by_url already swallows
            # ProviderError internally, but anything else (e.g. a
            # malformed cache entry) shouldn't take the whole sweep
            # down. The outer try/except would catch this anyway,
            # but logging here gives per-entry visibility.
            logger.debug("crossref search failed for entry %d", entry_id, exc_info=True)
            await asyncio.sleep(_CROSSREF_BATCH_SLEEP)
            continue
        if match is None:
            # Still pause so a Hydra outage doesn't let us spin
            # through 50 entries in 0ms on the failure path — same
            # wall-clock cap as a healthy tick.
            await asyncio.sleep(_CROSSREF_BATCH_SLEEP)
            continue
        thread_url = f"https://www.reddit.com{match['permalink']}"
        num_comments = int(match["num_comments"])
        patches.append((entry_id, thread_url, num_comments))
        await asyncio.sleep(_CROSSREF_BATCH_SLEEP)

    if not patches:
        return

    # Phase 3: short-lived session, bulk UPDATE. One round-trip
    # for the whole batch instead of N. ``jsonb || jsonb`` merge
    # preserves any other meta keys the entry already has; the
    # raw SQL is needed because SQLAlchemy's JSONB column type
    # doesn't auto-merge on update. The same pattern as the
    # thumbnail/og:image bulk UPDATEs above.
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE entries "
                    "SET meta = COALESCE(meta, '{}'::jsonb) || :patch::jsonb "
                    "WHERE id = :id"
                ),
                [
                    {
                        "patch": json.dumps({
                            "reddit_thread_url": thread_url,
                            "reddit_comment_count": num_comments,
                        }),
                        "id": entry_id,
                    }
                    for entry_id, thread_url, num_comments in patches
                ],
            )
            await session.commit()
            logger.info(
                "crossref sweep: stamped %d entries (of %d candidates)",
                len(patches), len(to_check),
            )
    except Exception:
        logger.exception("crossref bulk update failed")

    # If we had more candidates than we processed in this batch,
    # the next hourly tick picks them up. Keeping the per-tick cap
    # bounded so the sweep can't monopolise the scheduler's
    # single thread.


async def _already_notified_urls(session: AsyncSession) -> set[str]:
    """URLs already in the CVE dedup ledger.

    Backed by the ``notification_dedup`` table rather than
    ``Brief.meta.notified_urls`` — the old layout wrote to one Brief
    row and read across all rows, which silently dropped entries past
    the 500-row bucket cap.

    Windowed to the ``notification_dedup_retention_days`` window so
    the read cost stays bounded as the ledger grows over months. The
    prune job (``_prune_notification_dedup``) removes rows older
    than the same window on a daily cadence — so a row that the
    prune has already removed wouldn't be in the set anyway, and
    a row the prune hasn't reached yet (within the window) is
    exactly the set we want to dedup against. When
    ``retention_days=0`` (operator chose to keep the full history),
    the read stays unbounded to match.
    """
    stmt = select(NotificationDedup.key).where(NotificationDedup.kind == "cve_url")
    retention_days = getattr(settings, "notification_dedup_retention_days", 30)
    if retention_days > 0:
        cutoff = func.now() - func.make_interval(0, 0, 0, retention_days)
        stmt = stmt.where(NotificationDedup.last_notified_at >= cutoff)
    return {row[0] for row in (await session.execute(stmt)).all()}


async def _already_alerted_slugs(session: AsyncSession) -> set[str]:
    """Slugs already in the convergence alert dedup ledger.

    Same ledger table, different ``kind`` discriminator. Was a union
    across Brief rows; now a direct PK lookup. Same windowing
    rationale as ``_already_notified_urls`` — bounded to the
    retention window so the read cost doesn't grow with months of
    accumulated slugs.
    """
    stmt = select(NotificationDedup.key).where(NotificationDedup.kind == "convergence_slug")
    retention_days = getattr(settings, "notification_dedup_retention_days", 30)
    if retention_days > 0:
        cutoff = func.now() - func.make_interval(0, 0, 0, retention_days)
        stmt = stmt.where(NotificationDedup.last_notified_at >= cutoff)
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

    # Job defaults apply to every job this scheduler ever holds,
    # including the dynamic per-row ingest jobs added later by
    # ``add_source``/``update_source``. No ``add_job`` call sets its
    # own grace, so without ``misfire_grace_time`` here every job
    # kept APScheduler's 1 s default: a run that started more than a
    # second late (event loop busy serving the API, or a long ingest
    # still finishing) was silently skipped until the next interval.
    # ``coalesce`` folds several missed runs into one; jobs that pass
    # their own ``coalesce`` keep it.
    _scheduler = AsyncIOScheduler(timezone="UTC", job_defaults=_JOB_DEFAULTS)
    # Before the startup ingest ticks below, so the first ingest after
    # an upgrade already scores with the new category.
    try:
        await _move_builtin_categories()
    except Exception:
        logger.exception("scheduler: built-in category move failed — will retry on next start")
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
    # have a backing plugin class — ``rss``, ``reddit``, ``podcast``,
    # and ``youtube_channel`` rows all dispatch through ``_plugin_for``
    # below, which picks the right plugin class per row.
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
    # restart mid-day. A once-a-day job gets a much longer grace than
    # the default: a run held up by a busy loop should still send that
    # day's brief, not skip to tomorrow. (The job store is in memory,
    # so a restart that lands after the hour still waits for the next
    # day; the grace covers only lateness within one process.)
    if settings.brief_schedule_hour >= 0:
        _scheduler.add_job(
            _daily_brief,
            trigger=CronTrigger(hour=settings.brief_schedule_hour, minute=0),
            id="brief:daily",
            name="Daily brief",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_BRIEF_DAILY_MISFIRE_GRACE_SECONDS,
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

    # Framing Watch: same-story/different-headline clustering (see
    # app.framing). Needs entry embeddings the same way the
    # preference-vector job does, so it's gated the same way. Default
    # cadence: 60 min — full rescan of the trailing
    # ``framing_window_hours`` each tick, cheap at personal-dashboard
    # entry volume. Fires once on startup (20s delay) so Framing Watch
    # isn't empty for a full hour after a restart.
    if settings.embedding_enabled and embedder().loaded:
        _scheduler.add_job(
            _cluster_framing,
            trigger=IntervalTrigger(minutes=settings.framing_cluster_interval_minutes),
            id="framing:cluster",
            name="Framing Watch clustering",
            replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=20),
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
    """Run a single plugin once on demand. Used by the /ingest endpoint.

    Accepts both registry-registered plugin class names
    (``bbc_news``, ``hn_top``, etc.) and dynamic ``Source`` row names
    (anything the user has added via Add custom or Track anyway).
    The class-driven path is the fast one — it goes through the
    in-memory registry. The dynamic path opens a DB session and runs
    the plugin instance the row's scheduler job holds, so the manual
    fetch is the scheduled tick's own: the same in-flight address
    (``fetch_target``) and the same per-instance state, such as a
    website feed's ``_extracted_urls``, which a fresh instance would
    lose (and re-extract every page). ``_plugin_for(row)`` builds one
    only when there is no job (a paused feed, or no scheduler).

    Returns a small summary dict; ``error`` is set on the
    unknown-source case.
    """
    plugins = list_sources()
    if plugin_name in plugins:
        return await _ingest(plugins[plugin_name])
    # Not in the registry — check the DB for a dynamic row.
    # ``Source`` is a SQLAlchemy ORM model; import lazily to keep
    # the module-load graph the same as before (the registry-only
    # path is the hot one and shouldn't pay for the dynamic
    # import on every startup).
    from app.db import SessionLocal
    from app.models import Source

    async with SessionLocal() as session:
        row = await session.scalar(select(Source).where(Source.name == plugin_name))
    if row is None:
        return {"source": plugin_name, "error": "unknown source", "fetched": 0, "inserted": 0, "duplicates": 0}
    plugin = None
    if _scheduler is not None:
        job = _scheduler.get_job(_dynamic_job_id(row.id))
        if job is not None and job.args and isinstance(job.args[0], SourcePlugin):
            plugin = job.args[0]
    if plugin is None:
        plugin = _plugin_for(row)
    if plugin is None:
        # The row exists but its ``type`` doesn't map to a dynamic
        # plugin class — should not happen since ``add_source``
        # only allows the five types ``_plugin_for`` handles, but
        # guard against drift.
        return {"source": plugin_name, "error": f"unsupported source type: {row.type!r}", "fetched": 0, "inserted": 0, "duplicates": 0}
    return await _ingest(plugin)


async def backfill_now() -> dict:
    """Run the embedding backfill once on demand (e.g. from a debug endpoint)."""
    await _backfill_embeddings()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Check now (pull to refresh)
# ---------------------------------------------------------------------------
#
# ``queue_check`` moves feed jobs up instead of fetching in the request:
# the fetch runs as the job itself, so APScheduler's ``max_instances=1``
# and ``coalesce`` still keep one run per feed at a time, and the
# backoff, auto-disable and ingest rules all apply as on a scheduled
# tick.

# At most this many feeds per call: the cap ``POST
# /api/sources/check-now`` enforces with a 422 (``CheckNowRequest``).
_CHECK_NOW_MAX_FEEDS = CHECK_NOW_MAX_IDS
# Queued feeds start this many seconds apart, so a tab of 20 feeds
# doesn't fetch (and embed) all in the same second.
_CHECK_NOW_STAGGER_SECONDS = 3
# Added to ``expected_done_s`` for the last fetch to finish and commit.
_CHECK_NOW_SETTLE_SECONDS = 20
# ``refresh.cooldown_seconds``: after a check, a feed isn't queued
# again for this long. Read like the ``ingest.*`` knobs: a missing,
# non-numeric or out-of-range value gives the default.
_REFRESH_COOLDOWN_KEY = "refresh.cooldown_seconds"
_REFRESH_COOLDOWN_DEFAULT = 600
_REFRESH_COOLDOWN_MIN = 60
_REFRESH_COOLDOWN_MAX = 24 * 3600
# ``force`` (an explicit Check now on one feed) shortens the cooldown
# to this, and also skips the backoff gate.
_FORCE_COOLDOWN_SECONDS = 60

# When check-now last queued each feed, by source id, as
# ``time.monotonic()``. Entries older than the cooldown are dropped on
# every call, so it holds at most the feeds queued in the last
# cooldown. Process-local (see ARCHITECTURE.md): a restart forgets it,
# and the ``last_fetch_at`` half of the cooldown still applies.
_last_queued: dict[int, float] = {}

# Reddit direct mode: when (``time.monotonic()``) the subreddits
# check-now has queued so far will have had their calls, at one per
# ``reddit_client.direct_call_interval_seconds()``. A pull soon after
# another waits behind it in the token bucket, and ``expected_done_s``
# says so. Process-local, like ``_last_queued``.
_reddit_calls_done_at: float = 0.0


async def _refresh_cooldown_seconds() -> int:
    raw = await runtime_settings.get_many([_REFRESH_COOLDOWN_KEY])
    return ingest_rules.parse_int_knob(
        raw.get(_REFRESH_COOLDOWN_KEY),
        _REFRESH_COOLDOWN_DEFAULT,
        _REFRESH_COOLDOWN_MIN,
        _REFRESH_COOLDOWN_MAX,
    )


def _source_job_id(name: str, source_id: int, *, builtin: bool) -> str:
    """The ingest job id for a feed's row: ``ingest:<name>`` for a
    built-in (class-driven) feed, ``ingest:dynamic:<id>`` otherwise.
    Mirrors ``_ingest_job_id``, which derives it from the plugin."""
    if builtin:
        return f"ingest:{name}"
    return _dynamic_job_id(source_id)


async def queue_check(ids: Sequence[int], force: bool = False) -> dict:
    """Ask for these feeds to be checked soon (``POST
    /api/sources/check-now``, pull to refresh).

    Each of at most ``_CHECK_NOW_MAX_FEEDS`` feeds, in the order given,
    lands in exactly one list:

    - ``paused``: no row, an inactive row, or no scheduled job.
    - ``queued_ids``: its job's next run moves to ``now + i × 3 s``
      (``i`` counts the feeds queued so far). A job already due sooner
      is never postponed. A run still fetching counts as queued and is
      left alone: APScheduler would drop a run that comes due while it
      executes.
    - ``cooling_down``: checked within the cooldown
      (``refresh.cooldown_seconds``, or ``_FORCE_COOLDOWN_SECONDS``
      with ``force``), either queued here or with a successful fetch
      (``last_fetch_at``) that recent. A run that has recorded its
      result but is still finishing its image passes counts here too.
      So does a feed whose run finished while its row was being read.
      In Reddit direct mode (one call per 75 s across the process), a
      subreddit fetched within its own refresh interval (``force``
      included), and every subreddit after the first one this call
      queues: Reddit's call budget is what is cooling down, so the
      next pull takes the next one.
    - ``backing_off``: without ``force``, a failing feed
      (``error_count > 0``) whose job isn't due yet and whose backoff
      (``_backoff_seconds``) hasn't run out since its last check
      (``last_attempt_at``). Each failure doubles the interval and the
      sixth disables the feed (``_AUTO_DISABLE_THRESHOLD``), so pulling
      every few minutes must not run it early. Measured from
      ``last_attempt_at`` rather than from the job, because a restart
      registers jobs at their base interval and forgets the backoff. A
      row checked before migration 0031 has no ``last_attempt_at``: its
      job's ``next_run_time`` decides.

    ``queued_at`` is the server's clock before any job moved. A queued
    feed is done once its ``last_attempt_at`` is at or after it,
    whatever the check found (a success also moves ``last_fetch_at``).
    ``expected_done_s`` estimates when the last one finishes, Reddit's
    token bucket included.
    """
    global _reddit_calls_done_at

    wanted = list(dict.fromkeys(ids))[:_CHECK_NOW_MAX_FEEDS]
    # Taken before the first await: a run that finishes while the rows
    # below are being read changes its count, and its row may predate
    # its result.
    finished_before = Counter(_ingest_finished)
    configured_cooldown = await _refresh_cooldown_seconds()
    cooldown = _FORCE_COOLDOWN_SECONDS if force else configured_cooldown
    rows: dict[int, Source] = {}
    if wanted:
        async with SessionLocal() as session:
            found = await session.scalars(select(Source).where(Source.id.in_(wanted)))
            rows = {row.id: row for row in found.all()}
    registered = list_sources()
    reddit_direct = reddit_client.is_direct_mode()
    reddit_call_seconds = reddit_client.direct_call_interval_seconds()

    # No await from here on: the cooldown reads and writes below can't
    # interleave with another call's.
    now = dt.datetime.now(dt.timezone.utc)
    mono = time.monotonic()
    for sid, at in list(_last_queued.items()):
        if mono - at >= configured_cooldown:
            del _last_queued[sid]

    queued_ids: list[int] = []
    cooling_down: list[int] = []
    paused: list[int] = []
    backing_off: list[int] = []
    reddit_running = 0
    reddit_moved = 0
    moved = 0
    for sid in wanted:
        row = rows.get(sid)
        job = None
        job_id = None
        if row is not None and row.active and _scheduler is not None:
            job_id = _source_job_id(row.name, row.id, builtin=row.name in registered)
            job = _scheduler.get_job(job_id)
        if job is None or job.next_run_time is None:
            paused.append(sid)
            continue
        is_reddit = reddit_direct and row.type == "reddit"
        if _ingest_unrecorded.get(job_id, 0) > 0:
            queued_ids.append(sid)
            _last_queued[sid] = mono
            if is_reddit:
                reddit_running += 1
            continue
        if _ingest_running.get(job_id, 0) > 0:
            # Its result is stamped (or committing) on the row: it was
            # checked just now.
            cooling_down.append(sid)
            continue
        if _ingest_finished.get(job_id, 0) != finished_before.get(job_id, 0):
            # A run finished while the row was being read, so the row
            # can predate its result: a failure's ``error_count`` (its
            # job already backed off) or a success's ``last_fetch_at``.
            # It was checked just now; leave the job where that run put
            # it.
            cooling_down.append(sid)
            continue
        last_fetch = row.last_fetch_at
        if last_fetch is not None and last_fetch.tzinfo is None:
            last_fetch = last_fetch.replace(tzinfo=dt.timezone.utc)
        since_fetch = (now - last_fetch).total_seconds() if last_fetch is not None else None
        since_queued = mono - _last_queued[sid] if sid in _last_queued else None
        if (since_queued is not None and since_queued < cooldown) or (
            since_fetch is not None and since_fetch < cooldown
        ):
            cooling_down.append(sid)
            continue
        if not force and (row.error_count or 0) > 0 and job.next_run_time > now:
            # The job's backed-off interval comes from the plugin: a
            # built-in's class, or the row a dynamic plugin was built
            # from.
            plugin_cls = registered.get(row.name)
            base = (
                plugin_cls.refresh_interval_seconds
                if plugin_cls is not None
                else row.refresh_interval_seconds
            )
            last_attempt = row.last_attempt_at
            if last_attempt is not None and last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=dt.timezone.utc)
            in_backoff = last_attempt is None or now < last_attempt + dt.timedelta(
                seconds=_backoff_seconds(base, row.error_count)
            )
            if in_backoff:
                backing_off.append(sid)
                continue
        if is_reddit:
            fetched_this_interval = (
                since_fetch is not None and since_fetch < row.refresh_interval_seconds
            )
            if reddit_moved >= 1 or fetched_this_interval:
                cooling_down.append(sid)
                continue
        run_at = now + dt.timedelta(seconds=moved * _CHECK_NOW_STAGGER_SECONDS)
        if job.next_run_time > run_at:
            _scheduler.modify_job(job_id, next_run_time=run_at)
        moved += 1
        if is_reddit:
            reddit_moved += 1
            _reddit_calls_done_at = max(_reddit_calls_done_at, mono) + reddit_call_seconds
        queued_ids.append(sid)
        _last_queued[sid] = mono

    expected_done_s = 0
    if queued_ids:
        # A subreddit queued here waits for the calls queued before it;
        # one still running is counted at one call.
        reddit_s = reddit_running * reddit_call_seconds
        if reddit_moved:
            reddit_s += _reddit_calls_done_at - mono
        others = len(queued_ids) - reddit_running - reddit_moved
        expected_done_s = round(
            reddit_s + others * _CHECK_NOW_STAGGER_SECONDS + _CHECK_NOW_SETTLE_SECONDS
        )
    logger.info(
        "check-now: %d queued, %d cooling down, %d paused, %d backing off (force=%s)",
        len(queued_ids), len(cooling_down), len(paused), len(backing_off), force,
    )
    return {
        "queued": len(queued_ids),
        "cooling_down": cooling_down,
        "paused": paused,
        "backing_off": backing_off,
        "queued_ids": queued_ids,
        "queued_at": now,
        "expected_done_s": expected_done_s,
    }


# ---------------------------------------------------------------------------
# Phase 5: dynamic source rows
# ---------------------------------------------------------------------------
#
# ``Source`` rows whose ``name`` is not in the registered plugin
# registry are "dynamic" — they have no class backing them and need a
# runtime-constructed plugin to be fetched. Covers ``type="rss"``
# (``DynamicRssPlugin``), ``type="reddit"``
# (``DynamicRedditPlugin``), ``type="podcast"``
# (``DynamicPodcastPlugin``), and ``type="youtube_channel"``
# (``DynamicYouTubePlugin``) — podcast and
# YouTube channel feeds are both RSS/Atom-shaped, so both are thin
# wrappers around the same fetch path.
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


def _dynamic_source_offset(name: str, interval_seconds: float) -> float:
    """Deterministic, stable sub-interval offset for a dynamic source.

    Hashes the source ``name`` into the range ``[0, interval_seconds)``
    so jobs that share the same interval don't all fire in the same
    second after a scheduler restart. The offset is reproducible across
    restarts (same name -> same offset) and bounded so the first run still
    happens within one interval window.
    """
    if interval_seconds < 1:
        return 0.0
    # 32 bits of hash is plenty of spread and avoids big-integer math.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16)
    return float(bucket % int(interval_seconds))


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
    if row.type == "podcast":
        return DynamicPodcastPlugin(row)
    if row.type == "youtube_channel":
        return DynamicYouTubePlugin(row)
    if row.type == "generic_scrape":
        return GenericScrapePlugin(row)
    logger.debug(
        "scheduler: no plugin for source %s (id=%d, type=%s) — skipping",
        row.name, row.id, row.type,
    )
    return None


def _add_or_replace_dynamic_job(scheduler: Any, row: Source, *, stagger: bool = True) -> bool:
    """Register (or replace) the scheduler job for a dynamic row.

    Returns True if a job was registered, False if the row's type
    has no plugin yet. ``replace_existing=True`` so PATCH refresh-
    interval changes take effect without first removing the job.

    ``stagger`` controls whether the first run is spread across the
    refresh interval (via ``_dynamic_source_offset``) or fires
    immediately. Staggering only makes sense for the startup walk,
    which re-registers every dynamic row at once and would otherwise
    fire them all in the same second. A user-triggered add/edit/
    reactivate (``add_source``/``update_source``) should ingest
    right away so the user gets immediate feedback, not wait up to a
    full refresh interval.
    """
    plugin = _plugin_for(row)
    if plugin is None:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    offset_seconds = (
        _dynamic_source_offset(row.name, row.refresh_interval_seconds)
        if stagger
        else 0.0
    )
    first_run = now + dt.timedelta(seconds=offset_seconds)
    scheduler.add_job(
        _ingest,
        trigger=IntervalTrigger(
            seconds=row.refresh_interval_seconds,
            start_date=first_run,
        ),
        # ``IntervalTrigger.get_next_fire_time`` rounds its first fire
        # UP to the next whole interval once ``now`` (evaluated a few
        # microseconds after ``start_date`` above was computed) has
        # passed ``start_date`` at all — which it always has by the
        # time ``add_job`` gets around to asking. Left to the trigger,
        # even ``offset_seconds == 0`` would silently turn into "wait
        # one full interval", defeating both the immediate (stagger=
        # False) and the startup-stagger cases. Passing ``next_run_time``
        # explicitly overrides the trigger's first-fire computation
        # (subsequent runs are unaffected — they're computed from the
        # previous fire time, not from ``start_date``).
        next_run_time=first_run,
        args=[plugin],
        id=_dynamic_job_id(row.id),
        name=f"Ingest {row.name} (dynamic)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "scheduler: registered dynamic source id=%d name=%s interval=%ds offset=%.0fs",
        row.id, row.name, row.refresh_interval_seconds, offset_seconds,
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
    # Slice 20: optional discovery hints for ``type='generic_scrape'``
    # sources. Both default to None (use the plugin's default
    # behavior). The route layer validates them before this is
    # called (sitemap_url parsed as an http(s) URL; link_pattern
    # must start with '/' and not contain a protocol).
    sitemap_url: str | None = None,
    link_pattern: str | None = None,
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
        # Slice 20: discovery hints propagated to the row.
        # None means "use plugin defaults" — exactly the same
        # behavior as a pre-slice row that never had these fields.
        sitemap_url=sitemap_url,
        link_pattern=link_pattern,
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
        _add_or_replace_dynamic_job(_scheduler, row, stagger=False)
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
    sitemap_url: str | None = _UNSET,
    link_pattern: str | None = _UNSET,
    custom_headers: dict | None = _UNSET,
) -> Source | None:
    """Apply a partial update to a Source row and reschedule if needed.

    Returns the updated row, or None if no row exists with that id.

    All fields are optional — missing fields are left untouched. The
    route layer enforces name/url constraints (``^[a-z0-9_]+$``,
    URL parse, no built-in collisions, no name collisions with other
    rows); this function assumes the inputs have already passed
    validation.

    ``sitemap_url`` participates in the same change-detection /
    reschedule dance as ``url``: a change to the sitemap URL means
    the next ingest should re-fetch the sitemap entirely, so the
    dynamic job gets replaced with one that knows about the new
    URL. Sentinel value (``_UNSET``) carries the "field missing
    from the request body, don't touch" semantic; ``None`` means
    "explicitly clear it back to discovery-by-homepage".

    Scheduler effects:
      - ``active`` False → remove the dynamic job. A class-driven
        source (``name in list_sources()``) is left alone; the
        class-driven job continues independently of the row.
      - ``active`` True, ``refresh`` change, ``name`` change,
        ``url`` change, or ``sitemap_url`` change → re-add the
        dynamic job with the new trigger (``replace_existing=True``
        handles idempotency). ``replace_existing=True`` rebinds the
        job's args — for dynamic rows this means the next tick uses
        a freshly-constructed ``DynamicRssPlugin`` / ``GenericScrapePlugin``
        instance built from the updated row.

    URL changes clear the cached favicon: ``favicon_url`` and
    ``favicon_path`` are NULLed on the row, and the on-disk file
    at ``assets/favicons/<id>.*`` is unlinked via
    ``assets.delete_favicon``. The next ingest re-downloads the new
    favicon into the same path via ``os.replace`` (atomic
    overwrite). The file is keyed by ``source.id`` (stable across
    URL changes) so no rename is needed.
    """
    from app import assets as assets_mod

    # Same row lock ``_ingest`` takes in its commit session, so an
    # address change and an in-flight poll of the old address never
    # interleave (see the first-fetch reset below). ``populate_existing``
    # re-reads the committed row: the PATCH route loaded it earlier in
    # this session, and the change checks below must not compare
    # against that older read.
    row = await session.get(
        Source,
        source_id,
        with_for_update={"key_share": True},
        populate_existing=True,
    )
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
    # ``sitemap_url`` uses the sentinel pattern: missing from the
    # body = no-op; ``None`` (explicit null) = clear; any string = set.
    # Comparing against the row's current value avoids a pointless
    # scheduler rebuild on an idempotent PATCH.
    sitemap_url_changed = (
        sitemap_url is not _UNSET
        and sitemap_url != row.sitemap_url
    )
    # ``link_pattern`` participates in the same sentinel dance as
    # ``sitemap_url``: missing = no-op, explicit ``None`` = clear
    # (turn off the page_links fallback), string = set. Note the
    # plugin treats both ``link_pattern=None`` and ``link_pattern=""``
    # as "don't try page_links" via falsy-check on the field, so
    # an empty string in the body also clears.
    link_pattern_changed = (
        link_pattern is not _UNSET
        and link_pattern != row.link_pattern
    )
    # All eight fields participate in the early-return guard.
    if not (
        refresh_changed
        or active_changed
        or category_changed
        or name_changed
        or url_changed
        or headers_changed
        or sitemap_url_changed
        or link_pattern_changed
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
    if sitemap_url is not _UNSET:
        # ``None`` = explicit clear back to homepage discovery.
        # Any string = set the direct sitemap URL the plugin
        # should parse with ``app.feed_autodiscovery.discover_sitemap_urls``
        # next poll.
        row.sitemap_url = sitemap_url
    if link_pattern is not _UNSET:
        # Same semantics as ``sitemap_url``: ``None`` clears the
        # page_links fallback, any string sets it. The plugin
        # treats both ``None`` and ``""`` as "don't try
        # page_links" so an empty-string body also counts as a
        # clear here.
        row.link_pattern = link_pattern
    if url_changed or sitemap_url_changed or link_pattern_changed:
        # Each of the three re-points the feed, so it starts again
        # from a first fetch: newest few plus the last couple of days,
        # not the new address's whole back catalogue (see
        # ``_is_first_fetch``). ``clock_timestamp()``, not ``now()``:
        # this transaction may have started before an in-flight poll
        # of the old address committed (the lock above waits for it),
        # and the new baseline has to come after that poll's rows.
        row.last_fetch_at = None
        row.created_at = func.clock_timestamp()
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

    # A category-only edit needs no job rebuild: ingest reads the
    # category from the row each run, while the job holds a plugin
    # built from the row's name, URL and headers. Rebuilding would
    # also fetch the feed right away, which the category picker's
    # autosave would trigger on every pick.
    if category_changed and not (
        refresh_changed
        or active_changed
        or name_changed
        or url_changed
        or headers_changed
        or sitemap_url_changed
        or link_pattern_changed
    ):
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
    # up the new name/url. Not staggered: a user re-enabling or
    # editing a source expects the next ingest to run right away,
    # not wait up to a full refresh interval.
    _add_or_replace_dynamic_job(_scheduler, row, stagger=False)
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
    # Captured before the delete/commit below — ``row`` is expired
    # (SQLAlchemy's default expire_on_commit) once the transaction
    # commits, and it's been deleted besides, so touching row.name
    # afterward would try to re-SELECT a row that's no longer there.
    source_name = row.name
    # Built-in sources (BBC, HN, Wikipedia On This Day, etc.) ARE
    # deletable. The row goes away, the scheduler job is removed, and
    # (unlike before) the deletion is recorded in ``app_settings`` via
    # ``_mark_builtin_source_deleted`` — the plugin class stays
    # registered in memory for the rest of the process's life (that
    # part can't be undone at runtime), but ``_upsert_source`` checks
    # this marker before recreating the row, so neither the next
    # scheduled tick nor a backend restart resurrects it.
    is_builtin = source_name in list_sources()
    # Delete child rows first. ``Entry.interactions`` also has an
    # ON DELETE CASCADE FK, so dropping entries drops their
    # interactions transparently — no need to fan out further.
    await session.execute(
        delete(Entry).where(Entry.source_id == source_id)
    )
    await session.delete(row)
    if is_builtin:
        await _mark_builtin_source_deleted(session, source_name)
    await session.commit()
    if _scheduler is not None:
        # Built-ins and dynamic rows are registered under different
        # job id schemes (see ``_ingest_job_id``) — ``_dynamic_job_id``
        # alone silently no-ops for a built-in, which is what let the
        # scheduler job keep firing (and recreating the row) after a
        # "successful" delete.
        job_id = _source_job_id(source_name, source_id, builtin=is_builtin)
        try:
            _scheduler.remove_job(job_id)
            logger.info("scheduler: removed job %s for %s (id=%d)", job_id, source_name, source_id)
        except Exception:
            pass
    return True