"""Unit tests for the ``notification_dedup`` read-window filter.

``scheduler._already_notified_urls`` and ``_already_alerted_slugs``
both used to issue an unbounded
``SELECT key FROM notification_dedup WHERE kind = '...'``. The
``_prune_notification_dedup`` job removes rows older than
``notification_dedup_retention_days`` daily — so anything older
than the window wouldn't be in the set anyway. Windowing the read
matches the prune and bounds the read cost as the ledger grows
over months of CVE ingest.

These tests don't touch Postgres — they assert on the SQL emitted
through a recording AsyncSession stub. The tests are no_db so the
conftest skip path keeps them runnable on hosts without a
reachable DB.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.scheduler import _already_notified_urls, _already_alerted_slugs


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class RecordingSession:
    """AsyncSession stub that records the SQL passed to ``execute`` and
    returns the next pre-loaded ``FakeResult`` from ``self.queue``."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self.queue:
            return FakeResult([])
        return self.queue.pop(0)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_cve_dedup_read_windowed_when_retention_positive(monkeypatch):
    """With ``retention_days=30`` (the default), the SQL must include
    a ``last_notified_at >= ...`` filter so the read cost is
    bounded by the retention window.
    """
    monkeypatch.setattr(settings, "notification_dedup_retention_days", 30, raising=True)
    session = RecordingSession([FakeResult([])])

    result = await _already_notified_urls(session)

    assert result == set()
    assert len(session.statements) == 1
    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "last_notified_at" in compiled.lower(), compiled
    # The interval must reference 30 (the retention window).
    assert "30" in compiled, compiled
    # And the kind filter must still be there — windowing shouldn't
    # remove the original WHERE clause.
    assert "kind = 'cve_url'" in compiled or '"cve_url"' in compiled, compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_cve_dedup_read_unbounded_when_retention_zero(monkeypatch):
    """``retention_days=0`` is the operator opt-out for unbounded
    history. The read must match — no windowing filter — otherwise
    an operator who set ``retention_days=0`` to keep the full
    history would silently get a windowed read that doesn't
    match.
    """
    monkeypatch.setattr(settings, "notification_dedup_retention_days", 0, raising=True)
    session = RecordingSession([FakeResult([])])

    await _already_notified_urls(session)

    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    # The windowing predicate must NOT be present.
    assert "last_notified_at" not in compiled.lower(), compiled
    assert "make_interval" not in compiled.lower(), compiled
    # The kind filter must still be there.
    assert "cve_url" in compiled, compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_slug_dedup_read_windowed_when_retention_positive(monkeypatch):
    """Same wire for the convergence-slug dedup read. Locks down
    both reads against a future refactor that drops one but not
    the other.
    """
    monkeypatch.setattr(settings, "notification_dedup_retention_days", 30, raising=True)
    session = RecordingSession([FakeResult([])])

    await _already_alerted_slugs(session)

    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "last_notified_at" in compiled.lower(), compiled
    assert "30" in compiled, compiled
    assert "convergence_slug" in compiled, compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_slug_dedup_read_unbounded_when_retention_zero(monkeypatch):
    """Same escape-hatch contract for the slug read."""
    monkeypatch.setattr(settings, "notification_dedup_retention_days", 0, raising=True)
    session = RecordingSession([FakeResult([])])

    await _already_alerted_slugs(session)

    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "last_notified_at" not in compiled.lower(), compiled
    assert "make_interval" not in compiled.lower(), compiled
    assert "convergence_slug" in compiled, compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_window_value_taken_from_settings(monkeypatch):
    """Different retention values produce different intervals in the
    SQL. Locks the wire between ``config.settings`` and the
    SQL builder — a future change to read the setting from a
    different place is caught here.
    """
    monkeypatch.setattr(settings, "notification_dedup_retention_days", 7, raising=True)
    session = RecordingSession([FakeResult([])])
    await _already_notified_urls(session)
    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    # 7 days, not 30.
    assert "make_interval(0, 0, 0, 7)" in compiled, compiled
    assert "make_interval(0, 0, 0, 30)" not in compiled, compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_returned_set_built_from_rows(monkeypatch):
    """The function should still build the ``set[str]`` return from
    the rows the session returned — the windowing is a SQL
    filter, not a Python filter. Locks the row path so a future
    refactor that moves the filter into Python (e.g. for
    in-memory testing) is caught.
    """
    monkeypatch.setattr(settings, "notification_dedup_retention_days", 30, raising=True)
    rows = [("https://a.example.com/cve-1",), ("https://a.example.com/cve-2",)]
    session = RecordingSession([FakeResult(rows)])

    result = await _already_notified_urls(session)

    assert result == {"https://a.example.com/cve-1", "https://a.example.com/cve-2"}
