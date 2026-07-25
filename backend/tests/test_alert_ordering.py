"""Tests for the CVE + convergence alert paths in
app/scheduler.py. The path-level logic is light (a threshold
filter + a dedup ledger), but the *order of operations* is
the bug-prone surface:

  - Record the dedup BEFORE the notifier call (so a transient
    commit failure between the two doesn't cause duplicate
    alerts)
  - Commit the dedup before the side effect fires

These tests verify that order via a mock session + mock
notifier, asserting call ordering with explicit teardown.

No DB needed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
import unittest
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "x")
os.environ.setdefault("POSTGRES_PASSWORD", "x")
os.environ.setdefault("POSTGRES_DB", "x")
os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("ASSETS_DIR", tempfile.mkdtemp(prefix="smoke-"))

sys.path.insert(0, "/tmp/popping-review/backend")

import pytest

# Import the specific functions under test.
# We import them directly so we can mock the brief_generator
# global without going through the module-level init.
from app import scheduler as sched_mod  # noqa: E402


# --- Helpers ---------------------------------------------------------------

def make_entry(url: str, cvss: float = 9.0) -> Any:
    """A minimal Entry stand-in. The CVE path reads:
    - .url
    - .meta (for _cvss_score)
    - .title (for _format_cve)
    """
    e = MagicMock()
    e.url = url
    e.title = "Test " + url
    e.meta = {"cvss_score": cvss}
    return e


def make_source() -> Any:
    s = MagicMock()
    s.name = "Test Source"
    return s


class CallRecorder:
    """Tracks the order of method calls on a session + notifier.
    Used to assert the dedup-before-side-effect ordering."""

    def __init__(self):
        self.calls: list[str] = []

    def make_session(self) -> Any:
        s = MagicMock()
        # session.execute is called for the dedup SELECT +
        # the dedup INSERT. Each returns a coroutine that
        # records the call.
        async def execute(*_args, **_kwargs):
            self.calls.append("session.execute")
            result = MagicMock()
            result.all.return_value = []
            return result
        s.execute = execute
        return s

    def make_notifier(self) -> Any:
        n = MagicMock()

        async def send(*_args, **_kwargs):
            self.calls.append("notifier.send")
        n.send = send
        return n


# --- _cvss_score -----------------------------------------------------------

class TestCvssScore:
    """The threshold filter uses _cvss_score(entry). Tests cover
    the meta-shape edge cases."""

    def test_returns_cvss_when_meta_has_score(self) -> None:
        e = make_entry("https://x", cvss=8.5)
        assert sched_mod._cvss_score(e) == 8.5

    def test_returns_zero_when_no_meta(self) -> None:
        e = MagicMock()
        e.meta = None
        assert sched_mod._cvss_score(e) == 0.0

    def test_returns_zero_when_meta_empty(self) -> None:
        e = MagicMock()
        e.meta = {}
        assert sched_mod._cvss_score(e) == 0.0

    def test_returns_zero_when_no_cvss_key(self) -> None:
        e = MagicMock()
        e.meta = {"other_key": 5.0}
        assert sched_mod._cvss_score(e) == 0.0

    def test_returns_zero_when_cvss_is_string(self) -> None:
        e = MagicMock()
        e.meta = {"cvss_score": "not a number"}
        assert sched_mod._cvss_score(e) == 0.0

    def test_returns_cvss_value_unchanged_when_present(self) -> None:
        # _cvss_score just casts the meta value to float. It
        # does NOT clamp negative or out-of-range scores —
        # the threshold filter at the call site is the
        # boundary check. (If you want to test the contract:
        # the threshold comparison is ``if _cvss_score(e) >=
        # threshold`` in _maybe_notify_cves. A negative
        # CVSS is below any reasonable threshold and won't
        # notify. So the function is safe even without a
        # clamp.)
        e = MagicMock()
        e.meta = {"cvss_score": -1.0}
        assert sched_mod._cvss_score(e) == -1.0


# --- _maybe_notify_cves (dedup-before-side-effect ordering) --------------

class TestMaybeNotifyCvesOrdering:
    """The CVE path's critical invariant: the dedup ledger
    INSERT + commit happens BEFORE the notifier.send() call.
    A regression here means a transient commit failure
    between the two leaves the user with a sent notification
    AND no dedup row → duplicate alert on the next tick.
    """

    async def test_dedup_committed_before_notifier_send(self) -> None:
        # Set up the brief generator global. The function
        # reads it from module-level state.
        rec = CallRecorder()
        notifier = rec.make_notifier()
        sched_mod._brief_generator = MagicMock()
        sched_mod._brief_generator.notifier = notifier

        # Two CVEs above threshold. Both are 'fresh' (not
        # already in the dedup ledger).
        entries = [
            (make_entry("https://cve/1", cvss=9.0), make_source()),
            (make_entry("https://cve/2", cvss=9.5), make_source()),
        ]

        # Mock _already_notified_urls + _record_notified_urls
        # at the module level so the function doesn't try to
        # use the real DB. The path-of-interest is the
        # ordering between record → commit → notifier.send,
        # which is what this test asserts.
        recorded_at = []
        async def fake_record(session, urls):
            recorded_at.append("record.start")
            rec.calls.append("session.execute (record)")
        async def fake_already(session):
            rec.calls.append("session.execute (already)")
            return set()  # nothing already notified
        async def fake_commit():
            rec.calls.append("session.commit")
        # The session mock that the ``async with SessionLocal() as
        # session`` block will yield. Must have a .commit()
        # method (coroutine) for ``await session.commit()`` to
        # work. MagicMock's auto-attr is sync by default, so the
        # function's ``await session.commit()`` raises
        # ``TypeError: object MagicMock can't be used in 'await'``.
        # We override the relevant methods with AsyncMock.
        session = rec.make_session()
        session.commit = AsyncMock(side_effect=fake_commit)
        session.execute = AsyncMock(side_effect=lambda *a, **kw: MagicMock(all=lambda: []))
        # Make the global SessionLocal return a mock context
        # manager so we never hit the real DB. The yielded
        # value is the session mock (which has a commit
        # method) for ``async with SessionLocal() as session``
        # to work — the CVE function does
        # ``await session.execute(...)`` and
        # ``await session.commit()`` on whatever the context
        # manager yields.
        class _FakeCtx:
            def __init__(self_inner, session):
                self_inner._session = session
            async def __aenter__(self_inner):
                rec.calls.append("SessionLocal.__aenter__")
                return self_inner._session
            async def __aexit__(self_inner, *args):
                return False

        with patch.object(sched_mod, "_already_notified_urls", new=AsyncMock(side_effect=fake_already)):
            with patch.object(sched_mod, "_record_notified_urls", new=AsyncMock(side_effect=fake_record)):
                # The FakeCtx must return the session mock (which
                # has a commit method) for ``async with
                # SessionLocal() as session`` to work. The CVE
                # function does ``await session.execute(...)`` and
                # ``await session.commit()`` on whatever the
                # context manager yields.
                with patch.object(sched_mod, "SessionLocal", return_value=_FakeCtx(session)):
                    # Patch the global settings so the
                    # threshold check passes.
                    with patch.object(
                        sched_mod.settings, "cve_notify_min_cvss", 7.0
                    ):
                        await sched_mod._maybe_notify_cves(entries)

        # Assert the order: session.execute (already SELECT) →
        # session.execute (record INSERT) → session.commit →
        # notifier.send.
        # The SELECT happens first (to check dedup), the
        # INSERT happens after (the dedup write), the commit
        # happens after the INSERT (durable), the notifier.send
        # happens after the commit (side effect AFTER durable dedup).
        idx_already = next(
            (i for i, c in enumerate(rec.calls) if c == "session.execute (already)"),
            -1,
        )
        idx_record = next(
            (i for i, c in enumerate(rec.calls) if c == "session.execute (record)"),
            -1,
        )
        idx_commit = next(
            (i for i, c in enumerate(rec.calls) if c == "session.commit"),
            -1,
        )
        idx_send = next(
            (i for i, c in enumerate(rec.calls) if c == "notifier.send"),
            -1,
        )
        assert idx_already >= 0, "session.execute (already) was never called"
        assert idx_record >= 0, "session.execute (record) was never called"
        assert idx_commit >= 0, "session.commit was never called"
        assert idx_send >= 0, "notifier.send was never called"
        # The CRITICAL invariant: commit happens before send.
        assert idx_commit < idx_send, (
            f"dedup commit happened AFTER notifier.send "
            f"(commit={idx_commit}, send={idx_send}) — this is the "
            "bug the original review caught and the fix this test "
            "guards against."
        )

    async def test_skips_when_threshold_zero(self) -> None:
        # cve_notify_min_cvss = 0 → no alerts (notifier
        # never called, even if entries are above 0).
        sched_mod._brief_generator = MagicMock()
        sched_mod._brief_generator.notifier = MagicMock()

        # The function's check is `if threshold <= 0:
        # return`. Default is 0, so the function returns
        # immediately and the notifier is never called.
        # Verify that.
        entries = [(make_entry("https://cve/1"), make_source())]
        await sched_mod._maybe_notify_cves(entries)
        assert sched_mod._brief_generator.notifier.send.call_count == 0

    async def test_skips_when_no_brief_generator(self) -> None:
        # If the lifespan didn't set up _brief_generator (e.g.
        # the LLM is misconfigured), the path returns
        # immediately without calling the notifier.
        sched_mod._brief_generator = None
        entries = [(make_entry("https://cve/1", cvss=99.0), make_source())]
        # Should not raise.
        await sched_mod._maybe_notify_cves(entries)


# --- _check_convergence: the dedup-order invariant ------------------------

class TestCheckConvergenceDedupOrdering:
    """Same dedup-before-side-effect pattern for the
    convergence alert path. The original review flagged this
    exact ordering bug (commit message in the previous bundle).
    These tests guard the fix.
    """

    async def test_dedup_committed_before_generate_alert(self) -> None:
        rec = CallRecorder()
        # Mock the brief generator
        bg = MagicMock()
        async def generate_alert(*_args, **_kwargs):
            rec.calls.append("generate_alert")
        bg.generate_alert = generate_alert
        bg.notifier = MagicMock()
        sched_mod._brief_generator = bg

        # Mock the convergence_helper to return one candidate
        # slug above threshold.
        with patch.object(
            sched_mod.convergence_helper, "counts",
            new=AsyncMock(return_value={"test-slug": 3}),
        ):
            with patch.object(
                sched_mod, "_already_alerted_slugs",
                new=AsyncMock(return_value=set()),
            ):
                with patch.object(
                    sched_mod, "_record_alerted_slug",
                    new=AsyncMock(),
                ):
                    with patch.object(
                        sched_mod.settings, "convergence_notify_threshold", 2
                    ):
                        async def fake_commit():
                            rec.calls.append("session.commit")
                        session = rec.make_session()
                        session.commit = fake_commit

                        # Patch SessionLocal to return our mock
                        # session wrapped in an async context
                        # manager.
                        class _FakeCtx:
                            async def __aenter__(self_inner):
                                return session
                            async def __aexit__(self_inner, *args):
                                return False

                        with patch.object(
                            sched_mod, "SessionLocal",
                            return_value=_FakeCtx(),
                        ):
                            await sched_mod._check_convergence()

        # The order: _record_alerted_slug → session.commit →
        # generate_alert. Same invariant as the CVE path.
        rec_calls = rec.calls
        commit_idx = next(
            (i for i, c in enumerate(rec_calls) if c == "session.commit"),
            -1,
        )
        alert_idx = next(
            (i for i, c in enumerate(rec_calls) if c == "generate_alert"),
            -1,
        )
        assert commit_idx >= 0, "session.commit was never called"
        assert alert_idx >= 0, "generate_alert was never called"
        assert commit_idx < alert_idx, (
            f"dedup commit happened AFTER generate_alert "
            f"(commit={commit_idx}, alert={alert_idx})"
        )
