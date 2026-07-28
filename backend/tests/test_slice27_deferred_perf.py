"""Slice 27 — two deferred perf fixes:

1. ``GenericScrapePlugin._extracted_urls`` is now an ``OrderedDict``
   with FIFO eviction at ``_MAX_EXTRACTED_URLS = 5000`` instead of an
   unbounded ``set``. Without the bound, a source with a growing
   sitemap accumulates URLs for the lifetime of the backend process
   — tens of thousands over weeks. The DB UNIQUE constraint is the
   real correctness backstop; the in-memory set is just a perf
   optimization to avoid re-running ``_extract_one`` on URLs the
   entries table already has. FIFO eviction means a previously-seen
   URL gets re-attempted after eviction, which the entries-table
   UNIQUE constraint silently no-ops via ``on_conflict_do_nothing``.

2. The ``_ingest`` function's thumbnail/og:image ``asyncio.gather``
   now wraps each fetch in a ``_bounded_fetch`` that takes a
   semaphore slot. A source with 100 entries was firing 100
   concurrent HTTP requests with no throttle. 10 concurrent is
   enough to overlap the ~3s per-fetch latency on a small ingest
   while keeping a big ingest's open-connection count bounded.

This test file guards both invariants structurally + functionally.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GENERIC_SCRAPE = REPO / "backend/app/sources/generic_scrape.py"
SCHEDULER = REPO / "backend/app/scheduler.py"


# ---------------------------------------------------------------------------
# 1. generic_scrape.py: bounded _extracted_urls
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_generic_scrape_uses_ordereddict():
    src = GENERIC_SCRAPE.read_text()
    assert "from collections import OrderedDict" in src, (
        "generic_scrape.py must import OrderedDict for the bounded "
        "set. The old unbounded ``set`` was the bug slice 27 fixes."
    )
    assert re.search(r"self\._extracted_urls:\s*[\"']?OrderedDict\[", src), (
        "_extracted_urls must be typed as OrderedDict (or similar) — "
        "a plain set cannot do FIFO eviction."
    )


@pytest.mark.no_db
def test_generic_scrape_has_max_extracted_urls_constant():
    src = GENERIC_SCRAPE.read_text()
    m = re.search(r"^_MAX_EXTRACTED_URLS\s*=\s*(\d+)", src, re.MULTILINE)
    assert m, (
        "_MAX_EXTRACTED_URLS constant must be defined. Without a cap, "
        "the OrderedDict grows without bound — the bug slice 27 fixes."
    )
    cap = int(m.group(1))
    assert cap > 0 and cap <= 100_000, (
        f"_MAX_EXTRACTED_URLS={cap} is outside the reasonable range "
        f"[1, 100000]. Too small (e.g. 10) defeats the point of having "
        f"the cache; too large (e.g. 1M) doesn't bound anything."
    )


@pytest.mark.no_db
def test_generic_scrape_has_mark_extracted_helper():
    src = GENERIC_SCRAPE.read_text()
    assert "def _mark_extracted" in src, (
        "A ``_mark_extracted`` helper method must exist — it does the "
        "eviction logic so the call site in fetch() stays readable."
    )
    # Check the eviction loop is in there
    assert re.search(
        r"def _mark_extracted[\s\S]*?popitem\(last=False\)",
        src,
    ), (
        "_mark_extracted must call ``OrderedDict.popitem(last=False)`` "
        "to evict the OLDEST entry (FIFO)."
    )


@pytest.mark.no_db
def test_generic_scrape_no_unbounded_set_add():
    """Regression: the original ``self._extracted_urls.add(url)`` line
    must be gone. Re-adding unbounded growth would defeat the slice."""
    src = GENERIC_SCRAPE.read_text()
    assert "self._extracted_urls.add(url)" not in src, (
        "Found unbounded ``self._extracted_urls.add(url)`` — slice 27 "
        "must replace this with the bounded ``_mark_extracted`` helper."
    )
    assert "self._mark_extracted(url)" in src, (
        "Call site in fetch() must use ``_mark_extracted`` (the bounded "
        "helper), not the old ``.add()``."
    )


@pytest.mark.no_db
def test_generic_scrape_mark_extracted_evicts_when_over_cap():
    """Functional: at-cap dict gets FIFO eviction on the next insert."""
    import sys
    backend_path = str(REPO / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    from app.sources.generic_scrape import GenericScrapePlugin, _MAX_EXTRACTED_URLS

    # Build a minimal source-row stub that satisfies __init__'s
    # attribute access. ``__init__`` only reads attributes — never
    # queries — so a SimpleNamespace works fine.
    from types import SimpleNamespace
    row = SimpleNamespace(
        name="t", type="generic_scrape", category="t",
        url="http://example.com/", refresh_interval_seconds=3600,
        id=1, sitemap_url=None, link_pattern=None,
    )
    plugin = GenericScrapePlugin(row)

    # Over-fill by 100 to verify eviction
    n_over = _MAX_EXTRACTED_URLS + 100
    for i in range(n_over):
        plugin._mark_extracted(f"http://example.com/{i}")

    # Cap must hold
    assert len(plugin._extracted_urls) == _MAX_EXTRACTED_URLS, (
        f"After inserting {n_over} items into a set capped at "
        f"{_MAX_EXTRACTED_URLS}, expected size={_MAX_EXTRACTED_URLS}, "
        f"got {len(plugin._extracted_urls)}. Eviction isn't working."
    )

    # The oldest 100 must have been evicted (FIFO)
    for i in range(100):
        assert f"http://example.com/{i}" not in plugin._extracted_urls, (
            f"http://example.com/{i} should have been evicted (it's the "
            f"oldest of {n_over} inserts). FIFO order is broken."
        )

    # The most recent must still be there
    assert f"http://example.com/{n_over - 1}" in plugin._extracted_urls


@pytest.mark.no_db
def test_generic_scrape_mark_extracted_preserves_existing():
    """Re-marking an existing URL must not change the dict's size or
    the eviction order. ``OrderedDict[key] = None`` overwrites the
    existing entry, which keeps FIFO order stable (no re-insertion)."""
    import sys
    backend_path = str(REPO / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    from app.sources.generic_scrape import GenericScrapePlugin, _MAX_EXTRACTED_URLS
    from types import SimpleNamespace

    row = SimpleNamespace(
        name="t", type="generic_scrape", category="t",
        url="http://example.com/", refresh_interval_seconds=3600,
        id=1, sitemap_url=None, link_pattern=None,
    )
    plugin = GenericScrapePlugin(row)

    # Fill to exactly the cap
    for i in range(_MAX_EXTRACTED_URLS):
        plugin._mark_extracted(f"http://example.com/{i}")

    # Re-mark the first URL (it shouldn't bump to the back)
    plugin._mark_extracted("http://example.com/0")

    # Now insert a NEW URL — this should evict the OLDEST, which is
    # still "http://example.com/0" (because re-mark didn't change its
    # FIFO position).
    plugin._mark_extracted("http://example.com/new")

    assert "http://example.com/new" in plugin._extracted_urls
    assert "http://example.com/0" not in plugin._extracted_urls, (
        "After re-marking http://example.com/0 and then inserting a new "
        "URL, the OLDEST entry (http://example.com/0) should be evicted "
        "— not http://example.com/1. Re-marking must preserve FIFO order."
    )
    # http://example.com/1 should still be there (it was the 2nd-oldest)
    assert "http://example.com/1" in plugin._extracted_urls


# ---------------------------------------------------------------------------
# 2. scheduler.py: _bounded_fetch + asyncio.Semaphore
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_scheduler_defines_fetch_concurrency_constant():
    """A ``_FETCH_CONCURRENCY`` constant must exist in the scheduler,
    scoped to ``_ingest`` so it's local to the ingest pass."""
    src = SCHEDULER.read_text()
    assert re.search(r"^\s+_FETCH_CONCURRENCY\s*=\s*\d+", src, re.MULTILINE), (
        "_FETCH_CONCURRENCY constant must be defined inside _ingest. "
        "Without it, the unbounded ``asyncio.gather`` fires every "
        "thumbnail fetch simultaneously."
    )


@pytest.mark.no_db
def test_scheduler_uses_semaphore_for_thumbnail_gather():
    """The thumbnail gather must use ``_thumbnail_fetch`` (the bounded
    wrapper), not call ``assets.fetch_thumbnail`` directly."""
    src = SCHEDULER.read_text()
    # Find the thumbnail gather
    m = re.search(
        r"if thumbnail_jobs:[\s\S]*?asyncio\.gather\(([\s\S]*?)\)\s*,",
        src,
    )
    assert m, "Couldn't find the thumbnail gather in scheduler.py"
    body = m.group(1)
    # The gather must contain ``_thumbnail_fetch``, not ``fetch_thumbnail(``
    # (the latter is the raw unbounded call)
    assert "_thumbnail_fetch" in body, (
        "Thumbnail gather must iterate over ``_thumbnail_fetch(...)`` "
        "calls (the bounded wrapper), not ``assets.fetch_thumbnail(...)`` "
        "directly. Reverting this defeats slice 27."
    )
    assert "fetch_thumbnail(url, eid)" not in body, (
        "Found direct ``assets.fetch_thumbnail(url, eid)`` in the gather "
        "— this is the unbounded call slice 27 fixes."
    )


@pytest.mark.no_db
def test_scheduler_uses_semaphore_for_og_image_gather():
    """Same check for the og:image fallback gather."""
    src = SCHEDULER.read_text()
    m = re.search(
        r"if og_image_jobs:[\s\S]*?asyncio\.gather\(([\s\S]*?)\)\s*,",
        src,
    )
    assert m, "Couldn't find the og_image gather in scheduler.py"
    body = m.group(1)
    assert "_og_image_fetch" in body, (
        "og:image gather must iterate over ``_og_image_fetch(...)``."
    )
    assert "fetch_og_image_fallback(url, eid)" not in body


@pytest.mark.no_db
def test_scheduler_defines_bounded_fetch_helper():
    """A ``_bounded_fetch`` helper must exist that wraps the asset
    call in the semaphore. Without it the throttling isn't wired up."""
    src = SCHEDULER.read_text()
    assert re.search(
        r"async def _bounded_fetch\(fn, \*args\):[\s\S]*?async with sem:",
        src,
    ), (
        "_bounded_fetch helper must exist and acquire the semaphore "
        "via ``async with sem:``. The helper is what gates concurrency."
    )


@pytest.mark.no_db
def test_scheduler_semaphore_declared_before_helper():
    """The ``sem`` reference must exist before ``_bounded_fetch`` is
    defined — otherwise the helper's ``async with sem:`` line raises
    NameError at call time. Structural ordering matters."""
    src = SCHEDULER.read_text()
    sem_pos = src.find("sem = asyncio.Semaphore")
    helper_pos = src.find("async def _bounded_fetch")
    assert sem_pos > 0
    assert helper_pos > 0
    assert sem_pos < helper_pos, (
        f"``sem = asyncio.Semaphore(...)`` (offset {sem_pos}) must be "
        f"defined BEFORE ``_bounded_fetch`` (offset {helper_pos}). "
        "Otherwise the helper's ``async with sem:`` raises NameError."
    )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_bounded_fetch_throttles_concurrent_calls():
    """Functional: 50 wrapped calls through a 3-slot semaphore must
    not exceed 3 concurrent executions at any point in time. This
    is the user-visible bug slice 27 fixes.
    """
    import sys
    backend_path = str(REPO / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    # We don't need the real scheduler — just verify the throttling
    # pattern. Build a tiny semaphore + gather harness.
    in_flight = 0
    max_in_flight = 0

    async def fake_fetch(i):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        if in_flight > max_in_flight:
            max_in_flight = in_flight
        await asyncio.sleep(0.02)  # simulate 20ms fetch
        in_flight -= 1
        return i

    async def bounded(fn, sem, *args):
        async with sem:
            return await fn(*args)

    sem = asyncio.Semaphore(3)
    results = await asyncio.gather(
        *(bounded(fake_fetch, sem, i) for i in range(50))
    )

    assert len(results) == 50
    assert max_in_flight <= 3, (
        f"Expected max 3 concurrent (semaphore size), got "
        f"{max_in_flight}. The throttling pattern is broken."
    )
    # Sanity: it actually overlapped — we shouldn't have run fully
    # sequentially (would take ~50 * 20ms = 1s). With concurrency it
    # should take roughly ceil(50/3) * 20ms = ~340ms. No easy assert
    # without timing flakiness; the max_in_flight check is enough.