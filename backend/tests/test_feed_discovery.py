"""Tests for app.feed_discovery: the LLM-based expansion of the
recommendation pool (app.feed_recommendations).

The full "LLM suggests a URL, we fetch-validate it, persist it" happy
path needs a real LLM provider and a real feed to fetch — not
something a unit test should depend on (same call the podcast-
transcript summarizer's tests made — see test_podcast_transcript.py's
module docstring). Covered directly instead: the deterministic
sanitization helpers, the recency-count query the auto-trigger
cooldown relies on, and the route's category validation / inference,
which don't need a live provider to exercise.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

import app.feed_discovery as feed_discovery
from app.feed_discovery import (
    _slugify,
    _strip_code_fence,
    _validate_feed_url,
    discover_candidates,
    recent_llm_candidate_count,
)
from app.llm import router as llm_router
from app.models import FeedRecommendationCandidate


# --- _slugify ----------------------------------------------------------------


def test_slugify_lowercases_and_replaces_punctuation():
    assert _slugify("The Verge Tech News!") == "the_verge_tech_news"


def test_slugify_strips_leading_trailing_underscores():
    assert _slugify("  --hello world--  ") == "hello_world"


def test_slugify_empty_input_returns_empty():
    assert _slugify("   ") == ""


def test_slugify_truncates_to_120_chars():
    assert len(_slugify("a" * 200)) == 120


# --- _strip_code_fence ---------------------------------------------------


def test_strip_code_fence_removes_json_fence():
    text = '```json\n[{"name": "x"}]\n```'
    assert _strip_code_fence(text) == '[{"name": "x"}]'


def test_strip_code_fence_removes_bare_fence():
    text = '```\n[1, 2, 3]\n```'
    assert _strip_code_fence(text) == "[1, 2, 3]"


def test_strip_code_fence_passthrough_when_no_fence():
    assert _strip_code_fence('[{"name": "x"}]') == '[{"name": "x"}]'


# --- recent_llm_candidate_count ----------------------------------------------


async def test_recent_llm_candidate_count_only_counts_llm_source(db_session):
    db_session.add_all([
        FeedRecommendationCandidate(
            name="editorial_one", category="tech", url="https://example.com/e1",
            blurb="b", source="editorial",
        ),
        FeedRecommendationCandidate(
            name="llm_one", category="tech", url="https://example.com/l1",
            blurb="b", source="llm",
        ),
    ])
    await db_session.commit()

    assert await recent_llm_candidate_count(db_session, "tech") == 1


async def test_recent_llm_candidate_count_scoped_to_category(db_session):
    db_session.add_all([
        FeedRecommendationCandidate(
            name="llm_tech", category="tech", url="https://example.com/lt",
            blurb="b", source="llm",
        ),
        FeedRecommendationCandidate(
            name="llm_news", category="news", url="https://example.com/ln",
            blurb="b", source="llm",
        ),
    ])
    await db_session.commit()

    assert await recent_llm_candidate_count(db_session, "tech") == 1
    assert await recent_llm_candidate_count(db_session, "news") == 1
    assert await recent_llm_candidate_count(db_session, "science") == 0


async def test_recent_llm_candidate_count_excludes_old_rows(db_session):
    old = FeedRecommendationCandidate(
        name="llm_old", category="tech", url="https://example.com/lo",
        blurb="b", source="llm",
    )
    db_session.add(old)
    await db_session.commit()
    await db_session.refresh(old)
    # Backdate past the 7-day lookback window the function uses.
    old.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    db_session.add(old)
    await db_session.commit()

    assert await recent_llm_candidate_count(db_session, "tech") == 0


# --- discover_candidates: no provider configured -----------------------------


async def test_discover_candidates_returns_empty_without_crashing(db_session):
    # Test env has no cloud LLM API keys set, so this exercises the
    # real "every provider failed / unusable" path rather than a mock
    # (local Ollama is still attempted per the router's unconditional
    # fallback, but isn't reachable in this sandbox).
    created, note = await discover_candidates(
        db_session, category="tech", context="test context"
    )
    assert created == []
    assert note


# --- timeout ceilings (found in a repo-wide audit: the manual --------------
# --- "find more feeds" endpoint is synchronous and user-waiting, but ------
# --- had no ceiling on either the LLM call or the per-candidate fetch, ----
# --- so a slow-but-not-dead provider or feed host could hold the ----------
# --- request open for minutes) ----------------------------------------------


class _SlowProvider:
    """A fake LLM provider whose complete() never returns in time —
    stands in for a provider that's slow rather than actually down
    (a real down provider fails fast with a connection error, which
    is a different, already-handled path)."""

    name = "slow_test_provider"

    async def complete(self, prompt, max_tokens=800, think=None):
        await asyncio.sleep(10)
        return "should never get here"


async def test_ask_llm_for_suggestions_times_out_slow_provider(monkeypatch):
    monkeypatch.setattr(feed_discovery, "_LLM_CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(llm_router, "providers_for", lambda task: [_SlowProvider()])

    start = time.monotonic()
    suggestions, note = await feed_discovery._ask_llm_for_suggestions(
        "tech", "context", set(), 5
    )
    elapsed = time.monotonic() - start

    assert suggestions == []
    assert note is not None and "timed out" in note
    # Bounded by the timeout, not by the provider's 10s sleep.
    assert elapsed < 2.0


async def test_validate_feed_url_times_out_slow_fetch(monkeypatch):
    monkeypatch.setattr(feed_discovery, "_VALIDATION_FETCH_TIMEOUT_S", 0.05)
    # Skip the real check_url_safe (which does a live DNS lookup) so
    # this test's only variable is the fetch timeout, not network
    # conditions in whatever sandbox it runs in.
    monkeypatch.setattr(feed_discovery, "check_url_safe", lambda url: (True, ""))

    async def _slow_fetch_rss(url, headers=None):
        await asyncio.sleep(10)
        return [{"title": "should never get here"}]

    monkeypatch.setattr(feed_discovery, "fetch_rss", _slow_fetch_rss)

    start = time.monotonic()
    result = await _validate_feed_url("https://example.com/feed.xml")
    elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 2.0


# --- POST /api/feed-recommendations/discover ---------------------------------


async def test_discover_route_rejects_empty_category(app_client):
    resp = await app_client.post(
        "/api/feed-recommendations/discover", json={"category": "   "}
    )
    assert resp.status_code == 422


async def test_discover_route_uses_named_category(app_client):
    resp = await app_client.post(
        "/api/feed-recommendations/discover", json={"category": "science"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "science"
    assert isinstance(body["added"], int)


async def test_discover_route_reports_a_note_when_added_is_zero(app_client):
    # Test env has no cloud LLM API keys set, so the env chain falls
    # through to local Ollama (always attempted, per the router's
    # design) — which isn't reachable in this sandbox, so every call
    # fails with a transport error. ``added`` is 0 here for a
    # specific, actionable reason, not "the LLM ran and found nothing
    # new" — the response needs to say so or "find more feeds" looks
    # silently broken rather than unconfigured/unreachable.
    resp = await app_client.post(
        "/api/feed-recommendations/discover", json={"category": "tech"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] == 0
    assert body["note"]


async def test_discover_route_falls_back_to_default_category_cold_start(app_client):
    # No category given, no interaction history in this test's DB
    # state — falls back to the fixed cold-start default.
    resp = await app_client.post("/api/feed-recommendations/discover", json={})
    assert resp.status_code == 200
    assert resp.json()["category"] == "tech"


# --- POST /api/sources: auto-discovery trigger doesn't break Add -------------


async def test_create_source_still_succeeds_with_auto_discovery_wired_up(app_client):
    resp = await app_client.post(
        "/api/sources",
        json={
            "name": "auto_discovery_probe",
            "type": "rss",
            "category": "tech",
            "url": "https://example.com/auto_discovery_probe.xml",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "auto_discovery_probe"
