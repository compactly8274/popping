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

import datetime as dt

import pytest

from app.feed_discovery import (
    _slugify,
    _strip_code_fence,
    discover_candidates,
    recent_llm_candidate_count,
)
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
    # Test env has no LLM API keys set, so this exercises the real
    # "every provider failed / unusable" path rather than a mock.
    created = await discover_candidates(
        db_session, category="tech", context="test context"
    )
    assert created == []


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
