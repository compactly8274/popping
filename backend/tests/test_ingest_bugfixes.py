"""Regression tests for the ingest-pipeline bugfixes on this branch.

Five fixes, each guarded here:

1. ``validate_required`` meta flattening (app/sources/base.py) —
   plugins that ship their own nested ``meta`` dict (HN, NVD, CISA
   KEV, GitHub releases, Wikipedia, dynamic Reddit) had it bucketed
   as ``meta["meta"]``, so every top-level meta consumer (engagement
   scoring, the CVE notify hook, the reddit_thread_url projections)
   read nothing. Behavioral tests feed plugin-shaped raw dicts and
   assert the flattened result.

2. ``_escape_like`` (app/routes/entries.py) — the replacement
   template was over-escaped (six backslashes), so any search
   containing ``%`` / ``_`` / ``\`` matched garbage. Behavioral tests
   pin the exact escaped output.

3. Session sliding TTL (app/auth/session.py) — ``decode()`` extended
   ``expires_at`` by the row's REMAINING time, a no-op, so active
   users were logged out exactly 8h after login. Behavioral test
   creates a near-expiry session and asserts the expiry actually
   moves to the full window.

4. CVE notify on every insert (app/scheduler.py) — structural guard:
   ``_ingest`` must collect every inserted id and re-fetch the full
   set for the post-hook, not just the thumbnail-eligible subset
   (NVD/CISA entries ship no images, so the narrower set silently
   disabled the alert path).

5. Embedding summary source (app/scheduler.py) — structural guard:
   ``_embed_text`` must read the summary from ``norm["meta"]``
   (where ``validate_required`` buckets it), not the never-produced
   top-level key.

The behavioral tests are ``no_db`` (pure functions, no Postgres);
the sliding-TTL test uses the DB-backed fixtures (a real SessionRow).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# --- helpers ---------------------------------------------------------------


def _read(rel: str) -> str:
    return (REPO / rel).read_text()


# ===========================================================================
# 1. validate_required meta flattening
# ===========================================================================


@pytest.mark.no_db
def test_flatten_hn_shaped_item():
    """HN items carry a nested ``meta`` dict with the engagement keys
    ``scoring.engagement`` reads at the TOP level. The old passthrough
    nested them as ``meta["meta"]``, so engagement_score never reached
    the composite scorer (25% of its weight was dead for HN)."""
    from app.sources.base import validate_required

    raw = {
        "title": "Show HN: thing",
        "url": "https://news.ycombinator.com/item?id=1",
        "published_at": dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc),
        "summary": "",
        "meta": {
            "hn_id": 1,
            "score": 250,
            "comments": 40,
            "engagement_score": 250,
            "engagement_comments": 40,
        },
    }
    norm = validate_required("hn_top", raw)
    meta = norm["meta"]
    # Top-level, no nested "meta" key.
    assert "meta" not in meta, "nested meta['meta'] bucketing is the bug"
    assert meta["engagement_score"] == 250
    assert meta["engagement_comments"] == 40
    assert meta["hn_id"] == 1


@pytest.mark.no_db
def test_flatten_nvd_shaped_item_restores_cvss_read_path():
    """NVD items carry ``meta.cvss_score`` — the key
    ``scheduler._cvss_score`` reads to decide whether to fire the
    high-CVSS notification. Nested as ``meta["meta"]`` the notify
    hook scored 0.0 for every CVE and never alerted."""
    from app.sources.base import validate_required

    raw = {
        "title": "CVE-2026-1234: bad thing",
        "url": "https://nvd.nist.gov/vuln/detail/CVE-2026-1234",
        "published_at": dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
        "summary": "A flaw exists.",
        "meta": {
            "cve_id": "CVE-2026-1234",
            "cvss_score": 9.8,
            "cvss_severity": "CRITICAL",
        },
    }
    norm = validate_required("nvd_recent", raw)
    meta = norm["meta"]
    assert "meta" not in meta
    assert meta["cvss_score"] == 9.8

    # The notify hook's own reader sees it now.
    class _E:
        pass

    e = _E()
    e.meta = meta
    from app.scheduler import _cvss_score
    assert _cvss_score(e) == 9.8


@pytest.mark.no_db
def test_flatten_plugin_meta_wins_on_collision():
    """A plugin that explicitly sets ``meta.summary`` should not have
    it clobbered by the passthrough bucket — dict.update semantics:
    the nested meta keys win."""
    from app.sources.base import validate_required

    raw = {
        "title": "T",
        "url": "https://example.com/x",
        "published_at": None,
        "summary": "passthrough summary",
        "meta": {"summary": "plugin summary", "k": "v"},
    }
    norm = validate_required("s", raw)
    assert norm["meta"]["summary"] == "plugin summary"
    assert norm["meta"]["k"] == "v"


@pytest.mark.no_db
def test_flatten_noop_for_flat_plugins():
    """Plugins that DON'T ship a nested meta (BBC / RFD /
    generic_scrape shapes — summary and image_url at the top level)
    keep the exact pre-fix behavior: everything unknown lands in
    meta via the passthrough."""
    from app.sources.base import validate_required

    raw = {
        "title": "Some headline",
        "url": "https://example.com/a",
        "published_at": dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc),
        "summary": "A short blurb",
        "image_url": "https://example.com/img.jpg",
    }
    norm = validate_required("bbc_news", raw)
    meta = norm["meta"]
    assert meta["summary"] == "A short blurb"
    assert meta["image_url"] == "https://example.com/img.jpg"
    assert "meta" not in meta


@pytest.mark.no_db
def test_flatten_preserves_non_dict_meta_defensively():
    """A non-dict ``meta`` value (no plugin ships one; defensive) must
    stay in the passthrough rather than being silently dropped."""
    from app.sources.base import validate_required

    raw = {"title": "T", "url": "https://example.com/b", "meta": "oops"}
    norm = validate_required("s", raw)
    assert norm["meta"]["meta"] == "oops"


@pytest.mark.no_db
def test_flatten_title_validation_and_unescape_unchanged():
    """The rest of the contract (missing title/url raises, HTML entity
    unescaping) is unchanged by the flatten fix."""
    from app.sources.base import validate_required

    with pytest.raises(ValueError):
        validate_required("s", {"url": "https://x"})
    norm = validate_required("s", {"title": "Q&amp;A", "url": "https://x"})
    assert norm["title"] == "Q&A"


# ===========================================================================
# 2. _escape_like correctness
# ===========================================================================


@pytest.mark.no_db
def test_escape_like_percent_is_literal():
    """Searching for "100%" must build a pattern matching the literal
    "100%", not the old ``100\\1`` garbage. This is the exact escape
    the over-escaped replacement template broke."""
    from app.routes.entries import _escape_like

    assert _escape_like("100%") == "100\\%"
    # End-to-end wiring: the route wraps the escaped term in %...%.
    pattern = f"%{_escape_like('100%')}%"
    assert pattern == "%100\\%%"


@pytest.mark.no_db
def test_escape_like_underscore_and_backslash_are_literal():
    from app.routes.entries import _escape_like

    assert _escape_like("under_score") == "under\\_score"
    assert _escape_like("back\\slash") == "back\\\\slash"


@pytest.mark.no_db
def test_escape_like_plain_term_unchanged():
    """No metacharacters → no escaping; the common case is a no-op."""
    from app.routes.entries import _escape_like

    assert _escape_like("plain") == "plain"
    assert _escape_like("") == ""


# ===========================================================================
# 3. Session sliding TTL
# ===========================================================================


@pytest.mark.no_db
def test_sliding_ttl_returns_full_configured_window():
    """``_sliding_ttl_seconds`` must return the FULL configured window
    (the extension amount), not the row's remaining time — extending
    by the remaining time is the no-op the old ``_row_ttl`` produced."""
    from app.auth import session as session_mod
    from app.config import settings

    assert session_mod._sliding_ttl_seconds() == settings.session_ttl_seconds


@pytest.mark.no_db
def test_sliding_ttl_falls_back_on_bad_config(monkeypatch):
    """A non-positive TTL (misconfiguration) falls back to 8h rather
    than creating a session that instantly re-expires."""
    from app.auth import session as session_mod
    from app.config import settings

    monkeypatch.setattr(settings, "session_ttl_seconds", 0, raising=False)
    assert session_mod._sliding_ttl_seconds() == 28800


async def test_decode_extends_near_expiry_session(db_session):
    """Behavioral: a session 100s from expiry, on decode, must have
    its ``expires_at`` pushed to the FULL configured window from now.
    Before the fix the UPDATE was ``now + (expires_at - now)`` — the
    value never moved and an active user was logged out exactly 8h
    after login."""
    from app.auth import session as session_mod
    from app.auth.settings import OIDCConfig
    from app.config import settings
    from app.models import Session as SessionRow

    cfg = OIDCConfig(
        issuer="https://idp.example.com",
        client_id="popping",
        scopes="openid",
        public_url="https://popping.example.com",
        session_secret="x" * 32,
        session_ttl_seconds=settings.session_ttl_seconds,
        cookie_name="popping_session",
    )
    sid = await session_mod.create(
        db_session, cfg, sub="user-1", email="u@x", name="U", auth_method="oidc"
    )

    # Simulate a nearly-expired session: pull the expiry back to
    # now + 100s. The sliding refresh must push it out to the full
    # window from now, not leave it ~100s away.
    now = dt.datetime.now(dt.timezone.utc)
    near_expiry = now + dt.timedelta(seconds=100)
    row = await db_session.get(SessionRow, sid)
    row.expires_at = near_expiry
    await db_session.commit()

    payload = await session_mod.decode(db_session, sid)
    assert payload["sub"] == "user-1"

    # The sliding UPDATE runs through Core (session.execute), which
    # does NOT synchronize the ORM identity map — refresh explicitly
    # so the attribute read reflects the row the UPDATE touched.
    refreshed = await db_session.get(SessionRow, sid)
    await db_session.refresh(refreshed)

    expected_full = now + dt.timedelta(seconds=settings.session_ttl_seconds)
    # The expiry must be within a minute of now+full_ttl — NOT still
    # ~100s out (the no-op behavior). 60s of slack covers the test's
    # own runtime between the `now` capture and the decode call; the
    # old code fails this by ~8 hours.
    assert abs((refreshed.expires_at - expected_full).total_seconds()) < 60, (
        f"expires_at={refreshed.expires_at} did not slide to now+{settings.session_ttl_seconds}s "
        f"(expected ~{expected_full}); the sliding TTL is still a no-op."
    )
    assert refreshed.last_used_at >= now


# ===========================================================================
# 4. CVE notify: every insert reaches the post-hook (structural guard)
# ===========================================================================


@pytest.mark.no_db
def test_ingest_collects_every_inserted_id():
    """``_ingest`` must track EVERY inserted id (``inserted_ids``) and
    re-fetch that full set for the post-ingest hook. The previous
    code re-fetched only ``thumbnail_jobs`` ids — entries with a
    feed-supplied image — so NVD/CISA entries (no images) never
    reached ``_maybe_notify_cves`` and high-CVSS alerts never fired."""
    src = _read("backend/app/scheduler.py")
    assert re.search(r"^\s+inserted_ids:\s*list\[int\]\s*=\s*\[\]", src, re.MULTILINE), (
        "_ingest must collect inserted ids in an `inserted_ids` list. "
        "Without it the post-hook only sees thumbnail-eligible entries "
        "and the CVE notification path is dead for image-less sources."
    )
    assert re.search(
        r"inserted_ids\.append\(inserted_id\)", src,
    ), "each successful insert must append to inserted_ids."
    # The post-commit re-fetch must use inserted_ids, not the
    # thumbnail subset.
    m = re.search(
        r"# Re-fetch inserted rows for the post-hook[\s\S]{0,600}?"
        r"Entry\.id\.in_\(inserted_ids\)",
        src,
    )
    assert m, (
        "The post-commit re-fetch must select on Entry.id.in_(inserted_ids) "
        "(the full insert set), not the thumbnail_jobs subset — NVD/CISA "
        "entries have no feed image and were silently excluded from the "
        "CVE notification hook."
    )


# ===========================================================================
# 5. _embed_text reads the summary from norm["meta"] (structural guard)
# ===========================================================================


@pytest.mark.no_db
def test_embed_text_reads_summary_from_meta():
    """``_embed_text`` must read the summary from ``norm["meta"]`` —
    ``validate_required`` buckets every unknown top-level key into
    meta, including ``summary``, so a top-level read always saw an
    empty string and every embedding was title-only.

    The docstring is stripped before the "no top-level read"
    assertion — the fix's own docstring quotes the old buggy
    expression, which is exactly the prose a naive whole-body
    assertion would trip over.
    """
    src = _read("backend/app/scheduler.py")
    m = re.search(r"async def _embed_text\([\s\S]*?(?=\n# Favicon retry gate)", src)
    assert m, "Could not extract _embed_text body"
    body = m.group(0)
    # Strip the docstring so prose ABOUT the old bug doesn't trip the
    # "no top-level read" check.
    body_no_doc = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    assert 'meta.get("summary")' in body_no_doc, (
        "_embed_text must read the summary from the meta dict "
        "(norm['meta']['summary']) — validate_required buckets the "
        "plugin's summary there, and the top-level norm['summary'] key "
        "was never produced."
    )
    # And it must NOT read the dead top-level key.
    assert 'norm.get("summary")' not in body_no_doc, (
        "_embed_text still reads norm.get('summary') — a top-level key "
        "validate_required never produces. Read the summary from "
        "norm['meta'] instead."
    )


@pytest.mark.no_db
async def test_embed_text_builds_title_plus_summary():
    """Behavioral: _embed_text with a summary in meta must embed
    'title — summary', and with none, the title alone. Uses a stub
    embedder so no model is needed (tests run with EMBEDDING_ENABLED=false)."""
    from unittest.mock import patch

    from app import scheduler

    calls: list[str] = []

    class _Embedder:
        dim = 8

        async def embed(self, text):
            calls.append(text)
            return [0.1] * 8

    with patch.object(scheduler, "embedder", return_value=_Embedder()):
        norm = {
            "title": "Headline",
            "url": "https://x",
            "meta": {"summary": "<p>Body text</p>"},
        }
        vec = await scheduler._embed_text(norm)
        assert vec == [0.1] * 8
        # HTML tags stripped, whitespace collapsed.
        assert calls == ["Headline — Body text"], calls

        calls.clear()
        norm2 = {"title": "Only title", "url": "https://x", "meta": {}}
        vec2 = await scheduler._embed_text(norm2)
        assert calls == ["Only title"], calls

        calls.clear()
        norm3 = {"title": "", "url": "https://x", "meta": {}}
        vec3 = await scheduler._embed_text(norm3)
        assert vec3 == [0.0] * 8  # empty → zero vector, no embed call
        assert calls == []