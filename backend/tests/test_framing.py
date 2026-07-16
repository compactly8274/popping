"""Tests for Framing Watch (app.framing): same-story/different-headline
clustering by embedding similarity, wire-source detection, and the
batched headline-tone classifier.

The live "LLM classifies tone" happy path isn't covered — needs a
real provider, same tradeoff made throughout this codebase (podcast
summarizer, feed discovery). Covered directly instead: the clustering
algorithm itself (deterministic, no LLM involved), wire-source regex
matching, and the tone response parser/prompt builder.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.framing import (
    _parse_tone_response,
    cluster_recent_entries,
    detect_wire_source,
)
from app.models import Entry, StoryCluster
from factories import make_entry, make_source

# Two vectors with cosine ~0.995 (well above the 0.93 default
# threshold): identical except one component flipped sign.
_VEC_A = [1.0] * 384
_VEC_B = [1.0] * 383 + [-1.0]
# Orthogonal to _VEC_A (cosine 0.0) — clearly below threshold.
_VEC_ORTHOGONAL = [1.0] * 192 + [-1.0] * 192


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- detect_wire_source -------------------------------------------------


def test_detect_wire_source_matches_ap():
    assert detect_wire_source("Storm hits coast (AP)", None) == "AP"


def test_detect_wire_source_matches_reuters_in_summary():
    assert detect_wire_source("Storm hits coast", "Reuters reports flooding downtown") == "Reuters"


def test_detect_wire_source_matches_afp():
    assert detect_wire_source("Election results in", "AFP — votes still being counted") == "AFP"


def test_detect_wire_source_no_match_returns_none():
    assert detect_wire_source("Local team wins championship", "A great day for fans") is None


# --- _parse_tone_response -------------------------------------------------


def test_parse_tone_response_valid():
    assert _parse_tone_response('["neutral", "alarmist"]', 2) == ["neutral", "alarmist"]


def test_parse_tone_response_strips_code_fence():
    assert _parse_tone_response('```json\n["urgent"]\n```', 1) == ["urgent"]


def test_parse_tone_response_wrong_length_rejected():
    assert _parse_tone_response('["neutral"]', 2) is None


def test_parse_tone_response_invalid_label_rejected():
    assert _parse_tone_response('["neutral", "sarcastic"]', 2) is None


def test_parse_tone_response_malformed_json_rejected():
    assert _parse_tone_response("not json at all", 1) is None


# --- cluster_recent_entries -------------------------------------------------


async def test_cluster_forms_for_similar_embeddings_within_window(db_session):
    source_a = await make_source(db_session, "outlet_a", category="news")
    source_b = await make_source(db_session, "outlet_b", category="news")
    now = _now()
    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=now, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=now, embedding=_VEC_B)

    await cluster_recent_entries(db_session)

    await db_session.refresh(entry_a)
    await db_session.refresh(entry_b)
    assert entry_a.story_cluster_id is not None
    assert entry_a.story_cluster_id == entry_b.story_cluster_id


async def test_no_cluster_for_dissimilar_embeddings(db_session):
    source_a = await make_source(db_session, "outlet_c", category="news")
    source_b = await make_source(db_session, "outlet_d", category="news")
    now = _now()
    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=now, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "City council approves budget", published_at=now, embedding=_VEC_ORTHOGONAL)

    await cluster_recent_entries(db_session)

    await db_session.refresh(entry_a)
    await db_session.refresh(entry_b)
    assert entry_a.story_cluster_id is None
    assert entry_b.story_cluster_id is None


async def test_no_cluster_outside_time_window(db_session):
    source_a = await make_source(db_session, "outlet_e", category="news")
    source_b = await make_source(db_session, "outlet_f", category="news")
    now = _now()
    old = now - dt.timedelta(hours=100)  # outside the default 48h window
    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=old, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=now, embedding=_VEC_B)

    await cluster_recent_entries(db_session)

    await db_session.refresh(entry_a)
    await db_session.refresh(entry_b)
    # entry_a wasn't even in the candidate pool (outside window); entry_b
    # had no partner within the window, so no cluster forms for either.
    assert entry_a.story_cluster_id is None
    assert entry_b.story_cluster_id is None


async def test_cluster_id_stable_across_reruns(db_session):
    source_a = await make_source(db_session, "outlet_g", category="news")
    source_b = await make_source(db_session, "outlet_h", category="news")
    now = _now()
    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=now, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=now, embedding=_VEC_B)

    await cluster_recent_entries(db_session)
    await db_session.refresh(entry_a)
    first_cluster_id = entry_a.story_cluster_id

    await cluster_recent_entries(db_session)
    await db_session.refresh(entry_a)
    assert entry_a.story_cluster_id == first_cluster_id


async def test_third_similar_entry_joins_existing_cluster(db_session):
    source_a = await make_source(db_session, "outlet_i", category="news")
    source_b = await make_source(db_session, "outlet_j", category="news")
    source_c = await make_source(db_session, "outlet_k", category="news")
    now = _now()
    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=now, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=now, embedding=_VEC_B)
    await cluster_recent_entries(db_session)
    await db_session.refresh(entry_a)
    cluster_id = entry_a.story_cluster_id

    entry_c = await make_entry(db_session, source_c, "Massive storm batters coastline", published_at=now, embedding=_VEC_A)
    await cluster_recent_entries(db_session)

    await db_session.refresh(entry_c)
    assert entry_c.story_cluster_id == cluster_id


async def test_orphan_cluster_deleted_when_member_drops_out(db_session):
    source_a = await make_source(db_session, "outlet_l", category="news")
    source_b = await make_source(db_session, "outlet_m", category="news")
    now = _now()
    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=now, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=now, embedding=_VEC_B)
    await cluster_recent_entries(db_session)
    await db_session.refresh(entry_a)
    cluster_id = entry_a.story_cluster_id
    assert cluster_id is not None

    # Age entry_b out of the window so the cluster drops to 1 member.
    entry_b.published_at = now - dt.timedelta(hours=100)
    db_session.add(entry_b)
    await db_session.commit()

    await cluster_recent_entries(db_session)

    await db_session.refresh(entry_a)
    assert entry_a.story_cluster_id is None
    remaining = await db_session.get(StoryCluster, cluster_id)
    assert remaining is None


async def test_wire_source_detected_on_cluster(db_session):
    source_a = await make_source(db_session, "outlet_n", category="news")
    source_b = await make_source(db_session, "outlet_o", category="news")
    now = _now()
    await make_entry(db_session, source_a, "Storm makes landfall (AP)", published_at=now, embedding=_VEC_A)
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=now, embedding=_VEC_B)

    await cluster_recent_entries(db_session)

    await db_session.refresh(entry_b)
    cluster = await db_session.get(StoryCluster, entry_b.story_cluster_id)
    assert cluster.wire_source == "AP"


# --- route ------------------------------------------------------------------


async def test_framing_clusters_route_returns_grouped_articles(app_client, db_session):
    source_a = await make_source(db_session, "outlet_p", category="news")
    source_b = await make_source(db_session, "outlet_q", category="news")
    cluster = StoryCluster(wire_source="Reuters", first_seen_at=_now())
    db_session.add(cluster)
    await db_session.commit()
    await db_session.refresh(cluster)

    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=_now())
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=_now())
    entry_a.story_cluster_id = cluster.id
    entry_b.story_cluster_id = cluster.id
    db_session.add_all([entry_a, entry_b])
    await db_session.commit()

    resp = await app_client.get("/api/framing-clusters")

    assert resp.status_code == 200
    body = resp.json()
    match = next(c for c in body if c["cluster_id"] == cluster.id)
    assert match["wire_source"] == "Reuters"
    assert len(match["articles"]) == 2
    titles = {a["title"] for a in match["articles"]}
    assert titles == {"Storm makes landfall", "Hurricane strikes region"}
