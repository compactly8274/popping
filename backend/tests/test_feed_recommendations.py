"""Tests for the DB-backed feed recommendation pool
(app.feed_recommendations) and its route
(GET /api/feed-recommendations).

Covers: pool filtering (active-only, already-added exclusion),
category co-occurrence ranking, embedding-similarity ranking (via a
seeded UserProfile.preference_vector and pre-set candidate
embeddings, since the real sentence-transformers model isn't loaded
in the test environment), and the route's end-to-end response shape.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.feed_recommendations import recommendations_for, recommendations_for_user
from app.models import FeedRecommendationCandidate, Interaction, UserProfile
from factories import make_entry, make_source


async def _make_candidate(
    session,
    name: str,
    *,
    category: str = "tech",
    active: bool = True,
    source: str = "editorial",
    embedding: list[float] | None = None,
) -> FeedRecommendationCandidate:
    row = FeedRecommendationCandidate(
        name=name,
        category=category,
        url=f"https://example.com/{name}.xml",
        blurb=f"{name} blurb",
        active=active,
        source=source,
        embedding=embedding,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# --- recommendations_for ----------------------------------------------------


async def test_recommendations_for_excludes_already_added(db_session):
    await _make_candidate(db_session, "cand_a")
    await _make_candidate(db_session, "cand_b")

    result = await recommendations_for(db_session, ["cand_a"])

    names = [r["name"] for r in result]
    assert "cand_a" not in names
    assert "cand_b" in names


async def test_recommendations_for_excludes_inactive_rows(db_session):
    await _make_candidate(db_session, "cand_active")
    await _make_candidate(db_session, "cand_inactive", active=False)

    result = await recommendations_for(db_session, [])

    names = [r["name"] for r in result]
    assert "cand_active" in names
    assert "cand_inactive" not in names


async def test_recommendations_for_defaults_null_type_to_rss(db_session):
    await _make_candidate(db_session, "cand_rss")

    result = await recommendations_for(db_session, [])

    assert result[0]["type"] == "rss"


async def test_recommendations_for_preserves_reddit_type(db_session):
    row = await _make_candidate(db_session, "cand_reddit")
    row.type = "reddit"
    db_session.add(row)
    await db_session.commit()

    result = await recommendations_for(db_session, [])

    assert result[0]["type"] == "reddit"


# --- recommendations_for_user: category co-occurrence -----------------------


async def test_recommendations_for_user_ranks_by_category_engagement(db_session):
    await _make_candidate(db_session, "tech_cand", category="tech")
    await _make_candidate(db_session, "news_cand", category="news")

    source = await make_source(db_session, "liked_source", category="tech")
    entry = await make_entry(db_session, source, "An entry")
    db_session.add(
        Interaction(entry_id=entry.id, user_id="anonymous", type="click", value=1.0)
    )
    await db_session.commit()

    result = await recommendations_for_user(db_session, [], ("anonymous",))

    names = [r["name"] for r in result]
    assert names.index("tech_cand") < names.index("news_cand")


async def test_recommendations_for_user_falls_back_to_pool_order_with_no_signal(db_session):
    await _make_candidate(db_session, "first_cand")
    await _make_candidate(db_session, "second_cand")

    result = await recommendations_for_user(db_session, [], ("anonymous",))

    assert [r["name"] for r in result] == ["first_cand", "second_cand"]


async def test_recommendations_for_user_strips_internal_id_key(db_session):
    await _make_candidate(db_session, "cand_x")

    result = await recommendations_for_user(db_session, [], ("anonymous",))

    assert "_id" not in result[0]


# --- recommendations_for_user: vector similarity -----------------------------


async def test_recommendations_for_user_ranks_by_embedding_similarity(db_session):
    # Two candidates, pre-embedded (no live model needed): one aligned
    # with the user's preference vector, one opposed.
    aligned = [1.0] + [0.0] * 383
    opposed = [-1.0] + [0.0] * 383
    await _make_candidate(db_session, "aligned_cand", embedding=aligned)
    await _make_candidate(db_session, "opposed_cand", embedding=opposed)

    db_session.add(UserProfile(id=1, preference_vector=aligned))
    await db_session.commit()

    result = await recommendations_for_user(db_session, [], ("anonymous",))

    names = [r["name"] for r in result]
    assert names.index("aligned_cand") < names.index("opposed_cand")


# --- route ------------------------------------------------------------------


async def test_feed_recommendations_route_returns_pool(app_client, db_session):
    await _make_candidate(db_session, "route_cand", category="tech")

    resp = await app_client.get("/api/feed-recommendations")

    assert resp.status_code == 200
    body = resp.json()
    names = [r["name"] for r in body]
    assert "route_cand" in names
    row = next(r for r in body if r["name"] == "route_cand")
    assert row["source"] == "editorial"
    assert row["type"] == "rss"


async def test_feed_recommendations_route_excludes_existing_sources(app_client, db_session):
    await _make_candidate(db_session, "already_added")
    await make_source(db_session, "already_added", category="tech")

    resp = await app_client.get("/api/feed-recommendations")

    names = [r["name"] for r in resp.json()]
    assert "already_added" not in names
