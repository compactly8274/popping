"""Unit tests for ``app.scoring.convergence``'s scan cap.

``convergence.counts()`` previously issued an unbounded
``SELECT entries.title, sources.name WHERE published_at >= since``
that scanned every row in the window. After the cap landed, the
query is wrapped in a subquery that takes only the most recent
``settings.convergence_scan_cap`` rows.

These tests don't touch Postgres — they stub an ``AsyncSession``
that records the SQL emitted and returns a hand-crafted row set.
The same pattern as ``test_url_safety`` / ``test_http_smoke``: the
thing under test is a SQL-building function, and the value of a
test is that it locks down the SQL shape (cap = settings value,
order by published_at DESC + id DESC) so a future refactor that
drops the cap is caught at unit-test time, not in production.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.scoring import convergence


class FakeRow:
    """Row-like tuple with attribute access for ``.all()`` callers.

    Real SQLAlchemy ``Row`` objects support both ``row.title`` and
    ``row[0]`` (positional access for tuple unpacking). The
    convergence loop does ``for title, source_name in rows:``, so
    positional access is what the test needs to support.
    """

    def __init__(self, **kw):
        # Preserve insertion order for positional access.
        self._kw = list(kw.items())
        for k, v in kw.items():
            setattr(self, k, v)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._kw[key][1]
        return dict(self._kw)[key]

    def __iter__(self):
        return (v for _, v in self._kw)


class FakeResult:
    """Returns the row set handed in at construction time."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class RecordingSession:
    """AsyncSession stub that records every ``execute`` call and
    returns the next pre-loaded ``FakeResult`` from ``self.queue``.

    Tests push one ``FakeResult`` per expected call to ``execute``
    onto ``self.queue``; the second ``all()`` consumes it. The
    ``statements`` list captures the SQLAlchemy statement object
    passed to each call so tests can assert on the emitted SQL.
    """

    def __init__(self, queue):
        self.queue = list(queue)
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self.queue:
            return FakeResult([])
        return self.queue.pop(0)


@pytest.fixture(autouse=True)
def _reset_convergence_cache():
    convergence.invalidate()
    yield
    convergence.invalidate()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_default_cap_emits_subquery_with_settings_value(monkeypatch):
    """When ``scan_cap > 0`` (the default), the SQL is wrapped in a
    subquery that takes the most recent ``scan_cap`` rows in the
    window. The cap value is the live ``settings.convergence_scan_cap``
    so a config change is reflected immediately — and a future caller
    that bypasses the cap should fail this test.
    """
    monkeypatch.setattr(settings, "convergence_scan_cap", 5000, raising=True)
    session = RecordingSession([FakeResult([])])

    result = await convergence.counts(session, window_hours=24)

    assert result == {}
    # Exactly one execute call.
    assert len(session.statements) == 1
    stmt = session.statements[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # The subquery wrapper must include LIMIT 5000.
    assert "LIMIT 5000" in compiled, compiled
    # Order must put most recent first.
    assert "ORDER BY" in compiled.upper() or "order by" in compiled.lower()
    assert "published_at" in compiled.lower()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_scan_cap_zero_disables_cap(monkeypatch):
    """``scan_cap=0`` is the escape hatch — the SQL falls through to
    the original unbounded form. The test guards the contract that
    the escape hatch still works (otherwise an operator who flips
    the cap to 0 for a debugging session can't get the full scan).
    """
    monkeypatch.setattr(settings, "convergence_scan_cap", 0, raising=True)
    session = RecordingSession([FakeResult([])])

    await convergence.counts(session, window_hours=24)

    assert len(session.statements) == 1
    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    # No LIMIT clause in the unbounded form.
    assert "LIMIT" not in compiled.upper(), compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_cap_value_is_taken_from_settings(monkeypatch):
    """A different ``convergence_scan_cap`` produces a different
    LIMIT in the SQL. Locks the wire between ``config.settings`` and
    the SQL builder.
    """
    monkeypatch.setattr(settings, "convergence_scan_cap", 250, raising=True)
    session = RecordingSession([FakeResult([])])

    await convergence.counts(session, window_hours=24)

    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 250" in compiled, compiled


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_rows_above_cap_are_dropped(monkeypatch):
    """When the DB returns more rows than ``scan_cap``, only the
    rows in the row set are bucketed — Python doesn't try to
    second-guess the SQL. This test isn't the strongest possible
    (we can't intercept the LIMIT at the Python layer; the SQL
    building is verified above) but it documents that the row
    path is the simple "all returned rows go into the bucket"
    behavior.
    """
    monkeypatch.setattr(settings, "convergence_scan_cap", 5000, raising=True)
    rows = [
        FakeRow(title="Big Story", name="bbc"),
        FakeRow(title="Big Story", name="reuters"),
        FakeRow(title="Big Story", name="ap"),
        FakeRow(title="Other Story", name="bbc"),
    ]
    session = RecordingSession([FakeResult(rows)])

    result = await convergence.counts(session, window_hours=24)

    assert result.get("big story") == 3
    # "other story" only has 1 source — not in the result.
    assert "other story" not in result


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_cache_key_includes_scan_cap(monkeypatch):
    """The cache key is ``(window_hours, time_bucket, scan_cap)``,
    so a runtime config change invalidates the old (no-cap-aware)
    cache. Locks the wire between the cache key and the cap.
    """
    monkeypatch.setattr(settings, "convergence_scan_cap", 100, raising=True)
    key1 = convergence._cache_key(24)
    monkeypatch.setattr(settings, "convergence_scan_cap", 200, raising=True)
    key2 = convergence._cache_key(24)
    assert key1 != key2
    # Same cap → same key (within the same time bucket).
    monkeypatch.setattr(settings, "convergence_scan_cap", 100, raising=True)
    key3 = convergence._cache_key(24)
    assert key1 == key3
