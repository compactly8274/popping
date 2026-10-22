"""Scheduled jobs are not skipped when they start a little late.

APScheduler 3's default ``misfire_grace_time`` is 1 s, and no
``add_job`` call in ``app.scheduler`` sets its own. So any run that
started more than a second after its fire time (event loop busy with
an API request or a page parse, or a long ingest still finishing) was
dropped until the next interval. ``start_scheduler`` now builds the
scheduler with job defaults (120 s grace, ``coalesce``), and
``brief:daily`` gets an hour so a run held up by a busy loop still
sends the day's brief.

The cause of the lateness, ``trafilatura.bare_extraction`` running on
the event loop inside ``generic_scrape._extract_one``, is guarded by
the last test here.

These tests build the real scheduler through ``start_scheduler`` and
read the jobs as built. APScheduler only fills job defaults when a job
reaches its job store, which happens at ``start()``; the scheduler is
started paused so no job (startup ingest included) ever runs.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import scheduler
from app.config import settings
from app.sources import generic_scrape

pytestmark = pytest.mark.no_db


class _PausedScheduler(AsyncIOScheduler):
    """``start()`` always starts paused: jobs land in the job store
    (with defaults applied) but nothing is ever dispatched."""

    def start(self, paused: bool = False) -> None:  # noqa: ARG002 - always paused
        super().start(paused=True)


def _dynamic_row(source_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=source_id,
        name=name,
        type="rss",
        category="news",
        url="https://example.com/feed.xml",
        refresh_interval_seconds=900,
        custom_headers=None,
    )


@pytest.fixture
async def built_scheduler(monkeypatch):
    """The scheduler ``start_scheduler`` builds, with every optional
    job enabled and one dynamic row registered by the startup walk.
    Shut down in teardown whatever the test did."""
    monkeypatch.setattr(scheduler, "AsyncIOScheduler", _PausedScheduler)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_brief_generator", None)
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(settings, "brief_schedule_hour", 8)
    monkeypatch.setattr(scheduler, "embedder", lambda: SimpleNamespace(loaded=True))

    async def _fake_walk(sched, plugins):
        # Stands in for the DB walk: registers a dynamic row the way
        # the real one does, before ``start()``.
        scheduler._add_or_replace_dynamic_job(sched, _dynamic_row(1, "walked_feed"))

    monkeypatch.setattr(scheduler, "_register_dynamic_source_jobs", _fake_walk)

    async def _no_category_move():
        # The startup category move writes to the DB; these tests don't
        # use one.
        return None

    monkeypatch.setattr(scheduler, "_move_builtin_categories", _no_category_move)

    built = await scheduler.start_scheduler()
    try:
        yield built
    finally:
        built.shutdown(wait=False)
        scheduler._scheduler = None
        scheduler._brief_generator = None


async def test_every_built_job_gets_the_grace_and_coalesce(built_scheduler):
    jobs = {job.id: job for job in built_scheduler.get_jobs()}
    # Every kind of job the plan names is present, so the loop below
    # really covers them.
    for expected in (
        "ingest:hn_top",
        scheduler._dynamic_job_id(1),
        "pref:recompute",
        "score:rescore_recent",
        "framing:cluster",
        "brief:daily",
    ):
        assert expected in jobs, f"{expected} missing from {sorted(jobs)}"

    for job_id, job in jobs.items():
        assert job.coalesce is True, job_id
        if job_id == "brief:daily":
            continue
        assert job.misfire_grace_time == 120, (
            f"{job_id} has misfire_grace_time={job.misfire_grace_time!r}; "
            "APScheduler's 1 s default skips any run that starts late"
        )


async def test_brief_daily_gets_an_hour_of_grace(built_scheduler):
    job = built_scheduler.get_job("brief:daily")
    assert job is not None
    assert job.misfire_grace_time == 3600


async def test_a_dynamic_job_added_after_start_gets_the_defaults(built_scheduler):
    # ``add_source`` / ``update_source`` add jobs to the running
    # scheduler; the defaults must reach those too.
    scheduler._add_or_replace_dynamic_job(
        built_scheduler, _dynamic_row(2, "added_later"), stagger=False
    )
    job = built_scheduler.get_job(scheduler._dynamic_job_id(2))
    assert job is not None
    assert job.misfire_grace_time == 120
    assert job.coalesce is True


async def test_page_extraction_does_not_block_the_event_loop(monkeypatch):
    """``_extract_one`` parses in a thread, so the loop keeps serving
    other work (API requests, scheduler wakeups) during a parse."""

    async def _fake_fetch_html_checked(url, custom_headers=None, **kwargs):
        return "<html><body>page</body></html>", url

    def _slow_parse(*args, **kwargs):
        time.sleep(0.3)  # a CPU-bound parse, as far as the loop can tell
        return None

    monkeypatch.setattr(generic_scrape, "fetch_html_checked", _fake_fetch_html_checked)
    import trafilatura

    monkeypatch.setattr(trafilatura, "bare_extraction", _slow_parse)

    ticks = 0
    done = asyncio.Event()

    async def _ticker():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        result = await generic_scrape._extract_one("https://example.com/a")
    finally:
        done.set()
        await ticker
    assert result is None
    # Run on the loop, the parse holds it for the whole 0.3 s and the
    # ticker gets at most a tick or two in.
    assert ticks >= 5, f"event loop was blocked during extraction (ticks={ticks})"
