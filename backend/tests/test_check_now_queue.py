"""Check now (pull to refresh): ``scheduler.queue_check`` and
``POST /api/sources/check-now``.

``queue_check`` never fetches in the request. It moves each feed's
ingest job up (``now + i × 3 s``), so the fetch runs as the job, under
the same ``max_instances=1``, backoff and auto-disable rules as a
scheduled tick. What it must never do:

- push a failing feed past its backoff (each failure doubles the
  interval and the sixth disables the feed), unless ``force``;
- postpone a job that is already due;
- touch a job that is running (APScheduler would drop the run);
- spend more than one Reddit call per pull in direct mode.

The scheduler here is a real ``AsyncIOScheduler`` started paused: jobs
sit in the job store and nothing is dispatched. Where a test needs a
job to run, it awaits ``_ingest`` itself, the way the scheduler would.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import update

from app import runtime_settings, scheduler
from app.config import settings
from app.db import SessionLocal
from app.models import Source
from app.schemas import CHECK_NOW_MAX_IDS
from app.sources.dynamic_reddit import DynamicRedditPlugin
from app.sources.dynamic_rss import DynamicRssPlugin
from factories import make_source
from ingest_harness import source_row, utcnow


@pytest.fixture
async def sched(monkeypatch):
    """A paused scheduler installed as the app's, with empty check-now
    and run-tracking state."""
    s = AsyncIOScheduler(timezone="UTC", job_defaults=scheduler._JOB_DEFAULTS)
    s.start(paused=True)
    monkeypatch.setattr(scheduler, "_scheduler", s)
    monkeypatch.setattr(scheduler, "_last_queued", {})
    monkeypatch.setattr(scheduler, "_reddit_calls_done_at", 0.0)
    monkeypatch.setattr(scheduler, "_ingest_running", Counter())
    monkeypatch.setattr(scheduler, "_ingest_unrecorded", Counter())
    monkeypatch.setattr(scheduler, "_ingest_finished", Counter())
    # Proxy mode unless a test says otherwise, so only the Reddit
    # tests get the direct-mode rules.
    monkeypatch.setattr(settings, "reddit_hydra_url", "http://reddit-proxy.test")
    monkeypatch.setattr(settings, "reddit_direct_disabled", False)
    try:
        yield s
    finally:
        s.shutdown(wait=False)


@pytest.fixture
def direct_reddit(monkeypatch):
    monkeypatch.setattr(settings, "reddit_hydra_url", "")
    monkeypatch.setattr(settings, "reddit_direct_disabled", False)


@pytest.fixture
def no_favicon(monkeypatch):
    async def _none(*args, **kwargs):
        return None, None

    monkeypatch.setattr(scheduler.assets, "fetch_favicon", _none)


async def add_feed(
    s,
    name: str,
    *,
    type: str = "rss",
    url: str | None = None,
    due_in_s: float | None = 3600,
    error_count: int = 0,
    last_fetch_ago_s: float | None = None,
    refresh_interval_seconds: int = 3600,
    active: bool = True,
    with_job: bool = True,
) -> Source:
    """A feed row and its dynamic job, next due ``due_in_s`` from now
    (negative: already due)."""
    async with SessionLocal() as session:
        row = await make_source(
            session,
            name,
            type=type,
            url=url,
            active=active,
            error_count=error_count,
            refresh_interval_seconds=refresh_interval_seconds,
        )
        if last_fetch_ago_s is not None:
            row.last_fetch_at = utcnow() - dt.timedelta(seconds=last_fetch_ago_s)
            await session.commit()
            await session.refresh(row)
    if with_job:
        scheduler._add_or_replace_dynamic_job(s, row, stagger=False)
        if due_in_s is not None:
            s.modify_job(job_id(row), next_run_time=utcnow() + dt.timedelta(seconds=due_in_s))
    return row


def job_id(row: Source) -> str:
    return scheduler._dynamic_job_id(row.id)


def next_run(s, row: Source) -> dt.datetime:
    return s.get_job(job_id(row)).next_run_time


async def run_due_jobs(s) -> int:
    """Run every job that is due, as the scheduler would. Returns how
    many ran."""
    ran = 0
    for job in s.get_jobs():
        if job.next_run_time is not None and job.next_run_time <= utcnow():
            await job.func(*job.args)
            ran += 1
    return ran


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


async def test_queues_each_feed_3_seconds_after_the_last(sched):
    feeds = [await add_feed(sched, f"cn_stagger_{n}") for n in range(3)]

    result = await scheduler.queue_check([f.id for f in feeds])

    assert result["queued_ids"] == [f.id for f in feeds]
    assert result["queued"] == 3
    assert result["cooling_down"] == result["paused"] == result["backing_off"] == []
    at = result["queued_at"]
    for i, feed in enumerate(feeds):
        offset = (next_run(sched, feed) - at).total_seconds()
        assert offset == pytest.approx(i * 3, abs=0.01)
    assert result["expected_done_s"] == 3 * 3 + 20


async def test_a_job_already_due_is_never_postponed(sched):
    later = await add_feed(sched, "cn_due_later")
    due = await add_feed(sched, "cn_due_now", due_in_s=-5)
    soon = await add_feed(sched, "cn_due_soon", due_in_s=1)
    due_before = next_run(sched, due)
    soon_before = next_run(sched, soon)

    result = await scheduler.queue_check([later.id, due.id, soon.id])

    # ``due`` and ``soon`` take slots 1 and 2 (+3 s, +6 s); both were
    # due sooner than that, so neither moved.
    assert result["queued_ids"] == [later.id, due.id, soon.id]
    assert next_run(sched, due) == due_before
    assert next_run(sched, soon) == soon_before
    assert (next_run(sched, later) - result["queued_at"]).total_seconds() == pytest.approx(0, abs=0.01)


async def test_repeated_and_extra_ids_are_taken_once_and_capped(sched):
    feeds = [await add_feed(sched, f"cn_cap_{n}") for n in range(22)]
    ids = [f.id for f in feeds]

    result = await scheduler.queue_check([ids[0], ids[0], *ids[1:]])

    assert result["queued_ids"] == ids[:20]
    assert next_run(sched, feeds[20]) - utcnow() > dt.timedelta(minutes=50)


# ---------------------------------------------------------------------------
# Backoff gate
# ---------------------------------------------------------------------------


async def test_a_backing_off_feed_is_left_alone_and_six_pulls_never_disable_it(
    sched, monkeypatch, no_favicon
):
    async def _failing_fetch(self):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _failing_fetch)
    # One failure so far: its job was rescheduled to twice its interval.
    feed = await add_feed(sched, "cn_failing", error_count=1, due_in_s=2 * 3600)
    before = next_run(sched, feed)

    for _pull in range(6):
        result = await scheduler.queue_check([feed.id])
        assert result["backing_off"] == [feed.id]
        assert result["queued_ids"] == [] and result["queued"] == 0
        assert result["expected_done_s"] == 0
        assert next_run(sched, feed) == before
        assert await run_due_jobs(sched) == 0
        # Ten minutes pass between pulls: the cooldown is over.
        scheduler._last_queued.clear()

    async with SessionLocal() as session:
        row = await source_row(session, feed.name)
    assert row.active is True
    assert row.error_count == 1


async def test_force_still_queues_a_backing_off_feed(sched, monkeypatch, no_favicon):
    async def _failing_fetch(self):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _failing_fetch)
    feed = await add_feed(sched, "cn_forced", error_count=1, due_in_s=2 * 3600)

    result = await scheduler.queue_check([feed.id], force=True)

    assert result["queued_ids"] == [feed.id]
    assert result["backing_off"] == []
    assert next_run(sched, feed) <= utcnow()
    # It runs as its own job, so the failure is recorded and backed off
    # as on any scheduled tick.
    assert await run_due_jobs(sched) == 1
    async with SessionLocal() as session:
        row = await source_row(session, feed.name)
    assert row.error_count == 2
    # A failure is stamped too, so the caller sees this check land.
    assert row.last_attempt_at >= result["queued_at"]
    # error_count 2: four times the 1 h interval.
    assert next_run(sched, feed) - utcnow() > dt.timedelta(hours=3, minutes=55)


async def test_a_failing_feed_whose_job_is_due_is_queued(sched):
    feed = await add_feed(sched, "cn_failing_due", error_count=2, due_in_s=-1)

    result = await scheduler.queue_check([feed.id])

    assert result["queued_ids"] == [feed.id]


async def set_last_attempt(row: Source, ago_s: float | None) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(Source)
            .where(Source.id == row.id)
            .values(last_attempt_at=None if ago_s is None else utcnow() - dt.timedelta(seconds=ago_s))
        )
        await session.commit()


async def test_the_gate_measures_the_backoff_from_the_last_attempt(sched):
    # One failure half an hour ago on a 1 h feed: backed off for 2 h.
    feed = await add_feed(sched, "cn_gate_inside", error_count=1, due_in_s=90 * 60)
    await set_last_attempt(feed, 30 * 60)

    result = await scheduler.queue_check([feed.id])

    assert result["backing_off"] == [feed.id]
    assert next_run(sched, feed) - utcnow() > dt.timedelta(minutes=85)


async def test_a_backoff_that_ran_out_before_a_restart_is_queued(sched):
    """A restart registers every job at its base interval (staggered),
    so the job alone can't say whether the backoff is over. Three hours
    after a failure that backed the feed off for 2 h, the job sits 50
    minutes out: the feed is due, and the pull moves it up."""
    feed = await add_feed(sched, "cn_gate_restart", error_count=1, due_in_s=50 * 60)
    await set_last_attempt(feed, 3 * 3600)

    result = await scheduler.queue_check([feed.id])

    assert result["queued_ids"] == [feed.id]
    assert result["backing_off"] == []
    assert next_run(sched, feed) <= utcnow()


async def test_a_row_from_before_0031_falls_back_to_the_job(sched):
    # No ``last_attempt_at`` yet: the job's next run decides.
    feed = await add_feed(sched, "cn_gate_legacy", error_count=1, due_in_s=50 * 60)
    await set_last_attempt(feed, None)

    result = await scheduler.queue_check([feed.id])

    assert result["backing_off"] == [feed.id]


async def test_the_gate_uses_a_built_ins_class_interval(sched, monkeypatch):
    """A built-in's job runs at its class interval (here 5 min, backed
    off to 10 after one failure), whatever the row's
    ``refresh_interval_seconds`` says (here 1 h, which would back off
    to 2 h)."""
    from ingest_harness import make_plugin

    plugin = make_plugin("cn_gate_builtin")
    plugin.refresh_interval_seconds = 300
    monkeypatch.setattr(scheduler, "list_sources", lambda: {"cn_gate_builtin": plugin})
    async with SessionLocal() as session:
        row = await make_source(session, "cn_gate_builtin", error_count=1, refresh_interval_seconds=3600)
    sched.add_job(
        scheduler._ingest,
        args=[plugin],
        id="ingest:cn_gate_builtin",
        trigger="interval",
        seconds=300,
        next_run_time=utcnow() + dt.timedelta(minutes=4),
    )
    await set_last_attempt(row, 15 * 60)

    result = await scheduler.queue_check([row.id])

    assert result["queued_ids"] == [row.id]

    # Five minutes after the failure it is still inside its 10 minutes.
    scheduler._last_queued.clear()
    sched.modify_job("ingest:cn_gate_builtin", next_run_time=utcnow() + dt.timedelta(minutes=4))
    await set_last_attempt(row, 5 * 60)
    assert (await scheduler.queue_check([row.id]))["backing_off"] == [row.id]


# ---------------------------------------------------------------------------
# Running jobs
# ---------------------------------------------------------------------------


async def test_a_running_job_counts_as_queued_and_is_not_modified(
    sched, monkeypatch, offline_ingest
):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_fetch(self):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _slow_fetch)
    feed = await add_feed(sched, "cn_running")
    plugin = sched.get_job(job_id(feed)).args[0]
    run = asyncio.create_task(scheduler._ingest(plugin))
    try:
        await asyncio.wait_for(started.wait(), 5)
        before = next_run(sched, feed)

        result = await scheduler.queue_check([feed.id])

        assert result["queued_ids"] == [feed.id]
        assert result["queued"] == 1
        assert next_run(sched, feed) == before
        assert result["expected_done_s"] == 3 + 20
    finally:
        release.set()
        await asyncio.wait_for(run, 5)
    assert scheduler._ingest_running == Counter()
    assert scheduler._ingest_unrecorded == Counter()
    # Its check landed after ``queued_at``, which is how the caller
    # tells it is done.
    async with SessionLocal() as session:
        row = await source_row(session, feed.name)
    assert row.last_attempt_at >= result["queued_at"]
    assert row.last_fetch_at == row.last_attempt_at


async def test_a_run_that_recorded_its_result_counts_as_checked(
    sched, monkeypatch, offline_ingest
):
    """A run past its bookkeeping commit is still running (its image
    passes), so it can't be moved up, and its result is already on the
    row: it was checked just now."""
    in_image_pass = asyncio.Event()
    release = asyncio.Event()

    async def _one_item(self):
        return [{"title": "One", "url": "https://cn-recorded.test/1", "published_at": utcnow()}]

    async def _slow_og_image(*args, **kwargs):
        in_image_pass.set()
        await release.wait()
        return None

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _one_item)
    monkeypatch.setattr(scheduler.assets, "fetch_og_image_fallback", _slow_og_image)
    # Even with no cooldown left to cover it, the running job is not
    # touched.
    monkeypatch.setattr(scheduler, "_FORCE_COOLDOWN_SECONDS", 0)
    feed = await add_feed(sched, "cn_recorded")
    plugin = sched.get_job(job_id(feed)).args[0]
    run = asyncio.create_task(scheduler._ingest(plugin))
    try:
        await asyncio.wait_for(in_image_pass.wait(), 5)
        before = next_run(sched, feed)

        result = await scheduler.queue_check([feed.id], force=True)

        assert result["cooling_down"] == [feed.id]
        assert result["queued_ids"] == []
        assert next_run(sched, feed) == before
    finally:
        release.set()
        await asyncio.wait_for(run, 5)


async def test_a_check_during_the_success_commit_still_sees_it_land(
    sched, monkeypatch, offline_ingest
):
    """A run is marked recorded before it stamps ``last_fetch_at``. A
    check that lands while the success commit is in flight must not be
    told the feed is queued with a ``queued_at`` later than that
    stamp: its caller would wait for a check that already happened."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.ext.asyncio import AsyncSession

    async def _empty(self):
        return []

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _empty)
    # No cooldown, so only the run tracking decides.
    monkeypatch.setattr(scheduler, "_FORCE_COOLDOWN_SECONDS", 0)
    feed = await add_feed(sched, "cn_commit_race")
    plugin = sched.get_job(job_id(feed)).args[0]
    during: list[dict] = []
    real_commit = AsyncSession.commit

    async def _commit(self):
        stamping = any(
            isinstance(obj, Source)
            and obj.id == feed.id
            and sa_inspect(obj).attrs.last_fetch_at.history.has_changes()
            for obj in self.dirty
        )
        if stamping and not during:
            during.append(await scheduler.queue_check([feed.id], force=True))
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    await scheduler._ingest(plugin)

    assert len(during) == 1
    result = during[0]
    async with SessionLocal() as session:
        row = await source_row(session, feed.name)
    if feed.id in result["queued_ids"]:
        assert row.last_fetch_at >= result["queued_at"]
        assert row.last_attempt_at == row.last_fetch_at
    else:
        assert result["cooling_down"] == [feed.id]


async def test_a_check_during_the_failure_commit_still_sees_it_land(
    sched, monkeypatch, no_favicon
):
    """The same for a failure: it is marked recorded before it stamps
    ``last_attempt_at``, so a check that lands while the failure commit
    is in flight either sees the stamp at or after its ``queued_at`` or
    reports the feed as just checked."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.ext.asyncio import AsyncSession

    async def _failing_fetch(self):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _failing_fetch)
    monkeypatch.setattr(scheduler, "_FORCE_COOLDOWN_SECONDS", 0)
    feed = await add_feed(sched, "cn_fail_commit_race")
    plugin = sched.get_job(job_id(feed)).args[0]
    during: list[dict] = []
    real_commit = AsyncSession.commit

    async def _commit(self):
        stamping = any(
            isinstance(obj, Source)
            and obj.id == feed.id
            and sa_inspect(obj).attrs.last_attempt_at.history.has_changes()
            for obj in self.dirty
        )
        if stamping and not during:
            during.append(await scheduler.queue_check([feed.id], force=True))
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    summary = await scheduler._ingest(plugin)

    assert summary["error"].startswith("[network]")
    assert len(during) == 1
    result = during[0]
    async with SessionLocal() as session:
        row = await source_row(session, feed.name)
    assert row.error_count == 1
    if feed.id in result["queued_ids"]:
        assert row.last_attempt_at >= result["queued_at"]
    else:
        assert result["cooling_down"] == [feed.id]


async def test_a_failed_run_is_cleared_from_the_running_counts(sched, monkeypatch, no_favicon):
    async def _failing_fetch(self):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _failing_fetch)
    feed = await add_feed(sched, "cn_fail_clears")
    plugin = sched.get_job(job_id(feed)).args[0]

    summary = await scheduler._ingest(plugin)

    assert summary["error"]
    assert scheduler._ingest_running == Counter()
    assert scheduler._ingest_unrecorded == Counter()


def finish_run_during_the_row_read(monkeypatch, plugin) -> dict:
    """Make ``queue_check``'s row read return a snapshot taken before a
    whole run of ``plugin``: the run starts and finishes after the
    SELECT and before ``queue_check`` gets its rows back. Returns a dict
    that holds the job's ``next_run_time`` as that run left it."""
    from sqlalchemy.ext.asyncio import AsyncSession

    real_scalars = AsyncSession.scalars
    seen: dict = {}

    async def _scalars(self, statement, *args, **kwargs):
        result = await real_scalars(self, statement, *args, **kwargs)
        if not seen:
            seen["fired"] = True
            await scheduler._ingest(plugin)
            seen["next_run"] = scheduler._scheduler.get_job(
                scheduler._ingest_job_id(plugin)
            ).next_run_time
        return result

    monkeypatch.setattr(AsyncSession, "scalars", _scalars)
    return seen


async def test_a_run_that_fails_during_the_row_read_keeps_its_backoff(
    sched, monkeypatch, no_favicon
):
    """The row read sees ``error_count`` 0, but by the time the rows
    come back the run has failed and backed its job off. The stale row
    must not let the pull move the job up past that backoff."""
    async def _failing_fetch(self):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _failing_fetch)
    feed = await add_feed(sched, "cn_fail_in_read", last_fetch_ago_s=3600)
    plugin = sched.get_job(job_id(feed)).args[0]
    seen = finish_run_during_the_row_read(monkeypatch, plugin)

    result = await scheduler.queue_check([feed.id])

    assert seen["fired"]
    assert result["queued_ids"] == []
    assert result["cooling_down"] == [feed.id]
    # Left where the failure put it: twice the 1 h interval.
    assert next_run(sched, feed) == seen["next_run"]
    assert next_run(sched, feed) - utcnow() > dt.timedelta(hours=1, minutes=55)
    assert await run_due_jobs(sched) == 0
    async with SessionLocal() as session:
        row = await source_row(session, feed.name)
    assert row.error_count == 1
    assert row.active is True


async def test_a_run_that_succeeds_during_the_row_read_is_not_fetched_again(
    sched, monkeypatch, offline_ingest
):
    async def _empty(self):
        return []

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _empty)
    feed = await add_feed(sched, "cn_ok_in_read", last_fetch_ago_s=3600)
    plugin = sched.get_job(job_id(feed)).args[0]
    seen = finish_run_during_the_row_read(monkeypatch, plugin)

    result = await scheduler.queue_check([feed.id])

    assert seen["fired"]
    assert result["queued_ids"] == []
    assert result["cooling_down"] == [feed.id]
    assert next_run(sched, feed) == seen["next_run"]


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


async def test_a_feed_queued_within_the_cooldown_is_cooling_down(sched):
    feed = await add_feed(sched, "cn_cooldown")
    first = await scheduler.queue_check([feed.id])
    assert first["queued_ids"] == [feed.id]
    # The job ran in the meantime and is next due in an hour again.
    sched.modify_job(job_id(feed), next_run_time=utcnow() + dt.timedelta(hours=1))
    moved = next_run(sched, feed)

    second = await scheduler.queue_check([feed.id])

    assert second["cooling_down"] == [feed.id]
    assert second["queued_ids"] == []
    assert next_run(sched, feed) == moved

    # Ten minutes later it queues again.
    scheduler._last_queued[feed.id] -= 601
    third = await scheduler.queue_check([feed.id])
    assert third["queued_ids"] == [feed.id]


async def test_a_feed_fetched_within_the_cooldown_is_cooling_down(sched):
    fresh = await add_feed(sched, "cn_fresh", last_fetch_ago_s=120)
    stale = await add_feed(sched, "cn_stale", last_fetch_ago_s=700)

    result = await scheduler.queue_check([fresh.id, stale.id])

    assert result["cooling_down"] == [fresh.id]
    assert result["queued_ids"] == [stale.id]


async def test_force_shortens_the_cooldown_to_a_minute(sched):
    feed = await add_feed(sched, "cn_force_cooldown")
    await scheduler.queue_check([feed.id])

    scheduler._last_queued[feed.id] -= 30
    assert (await scheduler.queue_check([feed.id], force=True))["cooling_down"] == [feed.id]
    scheduler._last_queued[feed.id] -= 31
    assert (await scheduler.queue_check([feed.id], force=True))["queued_ids"] == [feed.id]


async def test_the_cooldown_follows_refresh_cooldown_seconds(sched):
    feed = await add_feed(sched, "cn_knob")
    try:
        await runtime_settings.set("refresh.cooldown_seconds", "120")
        await scheduler.queue_check([feed.id])
        scheduler._last_queued[feed.id] -= 100
        assert (await scheduler.queue_check([feed.id]))["cooling_down"] == [feed.id]
        scheduler._last_queued[feed.id] -= 21
        assert (await scheduler.queue_check([feed.id]))["queued_ids"] == [feed.id]
    finally:
        runtime_settings.invalidate_all()


@pytest.mark.parametrize("raw", ["10", "999999", "soon", ""])
async def test_a_bad_cooldown_value_uses_the_default(sched, raw):
    try:
        if raw:
            await runtime_settings.set("refresh.cooldown_seconds", raw)
        assert await scheduler._refresh_cooldown_seconds() == 600
    finally:
        runtime_settings.invalidate_all()


async def test_old_cooldown_entries_are_dropped(sched):
    feed = await add_feed(sched, "cn_prune")
    scheduler._last_queued[999_999] = scheduler.time.monotonic() - 700

    await scheduler.queue_check([feed.id])

    assert set(scheduler._last_queued) == {feed.id}


# ---------------------------------------------------------------------------
# Paused
# ---------------------------------------------------------------------------


async def test_inactive_unknown_and_jobless_feeds_are_paused(sched):
    inactive = await add_feed(sched, "cn_inactive", active=False)
    jobless = await add_feed(sched, "cn_jobless", with_job=False)
    paused_job = await add_feed(sched, "cn_paused_job")
    sched.pause_job(job_id(paused_job))
    healthy = await add_feed(sched, "cn_healthy")

    result = await scheduler.queue_check([inactive.id, 424242, jobless.id, paused_job.id, healthy.id])

    assert result["paused"] == [inactive.id, 424242, jobless.id, paused_job.id]
    assert result["queued_ids"] == [healthy.id]
    # The paused job stays paused.
    assert sched.get_job(job_id(paused_job)).next_run_time is None


async def test_every_feed_is_paused_without_a_scheduler(sched, monkeypatch):
    feed = await add_feed(sched, "cn_no_scheduler")
    monkeypatch.setattr(scheduler, "_scheduler", None)

    result = await scheduler.queue_check([feed.id])

    assert result["paused"] == [feed.id]
    assert result["expected_done_s"] == 0


async def test_a_built_in_feed_uses_its_class_job(sched, monkeypatch):
    from ingest_harness import make_plugin

    plugin = make_plugin("cn_builtin")
    monkeypatch.setattr(scheduler, "list_sources", lambda: {"cn_builtin": plugin})
    async with SessionLocal() as session:
        row = await make_source(session, "cn_builtin")
    sched.add_job(
        scheduler._ingest,
        args=[plugin],
        id="ingest:cn_builtin",
        trigger="interval",
        seconds=3600,
        next_run_time=utcnow() + dt.timedelta(hours=1),
    )

    result = await scheduler.queue_check([row.id])

    assert result["queued_ids"] == [row.id]
    assert sched.get_job("ingest:cn_builtin").next_run_time <= utcnow()


# ---------------------------------------------------------------------------
# Reddit in direct mode
# ---------------------------------------------------------------------------


async def test_direct_mode_queues_one_reddit_feed_per_call(sched, direct_reddit):
    subs = [
        await add_feed(sched, f"cn_sub_{n}", type="reddit", url=f"r/sub{n}", refresh_interval_seconds=900)
        for n in range(3)
    ]
    blog = await add_feed(sched, "cn_blog")

    result = await scheduler.queue_check([s.id for s in subs] + [blog.id])

    assert result["queued_ids"] == [subs[0].id, blog.id]
    assert result["cooling_down"] == [subs[1].id, subs[2].id]
    for sub in subs[1:]:
        assert next_run(sched, sub) - utcnow() > dt.timedelta(minutes=50)
    # One Reddit call (75 s in direct mode) plus one other feed.
    assert result["expected_done_s"] == 75 + 3 + 20

    # The next pull takes the next subreddit, whose call waits behind
    # the first one in the token bucket.
    again = await scheduler.queue_check([s.id for s in subs])
    assert again["queued_ids"] == [subs[1].id]
    assert again["expected_done_s"] == pytest.approx(2 * 75 + 20, abs=1)


async def test_direct_mode_a_subreddit_fetched_within_its_interval_is_cooling_down(
    sched, direct_reddit
):
    # 12 minutes ago: past the 10-minute cooldown, inside its 15-minute
    # interval.
    recent = await add_feed(
        sched, "cn_sub_recent", type="reddit", url="r/recent",
        refresh_interval_seconds=900, last_fetch_ago_s=720,
    )
    older = await add_feed(
        sched, "cn_sub_older", type="reddit", url="r/older",
        refresh_interval_seconds=900, last_fetch_ago_s=1000,
    )

    result = await scheduler.queue_check([recent.id, older.id])

    assert result["cooling_down"] == [recent.id]
    assert result["queued_ids"] == [older.id]


async def test_direct_mode_force_keeps_the_subreddit_interval_rule(sched, direct_reddit):
    """``force`` shortens the cooldown to 60 s and skips the backoff
    gate, but a subreddit fetched within its own interval stays
    ``cooling_down``: a run after a 429 counts as fetched, and moving
    it up would spend its one-cycle skip early and bring the next real
    reddit.com call forward."""
    recent = await add_feed(
        sched, "cn_sub_forced", type="reddit", url="r/forced",
        refresh_interval_seconds=900, last_fetch_ago_s=120,
    )
    before = next_run(sched, recent)

    result = await scheduler.queue_check([recent.id], force=True)

    assert result["cooling_down"] == [recent.id]
    assert result["queued_ids"] == []
    assert next_run(sched, recent) == before


async def test_proxy_mode_queues_every_reddit_feed(sched):
    subs = [
        await add_feed(sched, f"cn_proxy_sub_{n}", type="reddit", url=f"r/p{n}", refresh_interval_seconds=900)
        for n in range(3)
    ]

    result = await scheduler.queue_check([s.id for s in subs])

    assert result["queued_ids"] == [s.id for s in subs]
    assert result["expected_done_s"] == 3 * 3 + 20


async def test_a_running_reddit_job_does_not_use_up_the_call(sched, direct_reddit):
    running = await add_feed(sched, "cn_sub_running", type="reddit", url="r/running")
    waiting = await add_feed(sched, "cn_sub_waiting", type="reddit", url="r/waiting")
    scheduler._ingest_running[job_id(running)] += 1
    scheduler._ingest_unrecorded[job_id(running)] += 1

    result = await scheduler.queue_check([running.id, waiting.id])

    assert result["queued_ids"] == [running.id, waiting.id]
    assert result["expected_done_s"] == 2 * 75 + 20


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def test_route_reports_every_kind(sched, app_client):
    healthy = await add_feed(sched, "cn_route_healthy")
    failing = await add_feed(sched, "cn_route_failing", error_count=3, due_in_s=4 * 3600)
    inactive = await add_feed(sched, "cn_route_inactive", active=False)
    fresh = await add_feed(sched, "cn_route_fresh", last_fetch_ago_s=60)

    before = utcnow()
    resp = await app_client.post(
        "/api/sources/check-now",
        json={"source_ids": [healthy.id, failing.id, inactive.id, fresh.id], "force": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "queued", "cooling_down", "paused", "backing_off", "queued_ids", "queued_at", "expected_done_s",
    }
    assert body["queued"] == 1
    assert body["queued_ids"] == [healthy.id]
    assert body["backing_off"] == [failing.id]
    assert body["paused"] == [inactive.id]
    assert body["cooling_down"] == [fresh.id]
    queued_at = dt.datetime.fromisoformat(body["queued_at"])
    assert queued_at.tzinfo is not None
    assert before <= queued_at <= utcnow()
    assert body["expected_done_s"] == 3 + 20


async def test_route_force_defaults_to_false(sched, app_client):
    failing = await add_feed(sched, "cn_route_default", error_count=1, due_in_s=3600)

    resp = await app_client.post("/api/sources/check-now", json={"source_ids": [failing.id]})

    assert resp.status_code == 200
    assert resp.json()["backing_off"] == [failing.id]


async def test_route_passes_force(sched, app_client):
    failing = await add_feed(sched, "cn_route_force", error_count=1, due_in_s=3600)

    resp = await app_client.post(
        "/api/sources/check-now", json={"source_ids": [failing.id], "force": True}
    )

    assert resp.status_code == 200
    assert resp.json()["queued_ids"] == [failing.id]


@pytest.mark.parametrize(
    "body",
    [
        {"source_ids": list(range(1, 22))},
        {"source_ids": []},
        {},
        {"source_ids": ["5"]},
        {"source_ids": [1.0]},
        {"source_ids": [True]},
        {"source_ids": [0]},
        {"source_ids": [2**31]},
        {"source_ids": [1], "force": "yes"},
        {"source_ids": [1], "force": 1},
        {"source_ids": [1], "extra": 1},
        [1, 2],
    ],
)
async def test_route_refuses_bad_bodies(sched, app_client, body):
    resp = await app_client.post("/api/sources/check-now", json=body)
    assert resp.status_code == 422


async def test_route_accepts_20_ids(sched, app_client):
    resp = await app_client.post("/api/sources/check-now", json={"source_ids": list(range(1, 21))})
    assert resp.status_code == 200
    assert resp.json()["paused"] == list(range(1, 21))


async def test_route_has_no_get(sched, app_client):
    resp = await app_client.get("/api/sources/check-now")
    # Falls through to ``GET /sources/{source_id}``, which refuses a
    # non-integer id; nothing is queued by a GET.
    assert resp.status_code == 422


@pytest.mark.no_db
def test_the_route_and_the_scheduler_share_the_cap():
    assert CHECK_NOW_MAX_IDS == scheduler._CHECK_NOW_MAX_FEEDS == 20


async def test_reddit_rows_build_the_reddit_plugin(sched):
    # Guards the fixture: ``add_feed`` with type="reddit" makes a real
    # Reddit job, so the direct-mode tests exercise Reddit rows.
    sub = await add_feed(sched, "cn_sub_kind", type="reddit", url="r/kind")
    assert isinstance(sched.get_job(job_id(sub)).args[0], DynamicRedditPlugin)


async def test_queue_check_reads_rows_as_committed(sched):
    """The route reads the rows in its own session, so a pause that
    committed a moment ago is honoured."""
    feed = await add_feed(sched, "cn_committed")
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.id == feed.id).values(active=False))
        await session.commit()

    result = await scheduler.queue_check([feed.id])

    assert result["paused"] == [feed.id]
