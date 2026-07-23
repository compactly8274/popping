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
import json

import pytest

from app.framing import (
    _extract_first_json_array,
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


def test_parse_tone_response_extracts_array_from_cot():
    """Thinking models (gpt-oss, deepseek-r1, glm-5.2) wrap the JSON
    answer in chain-of-thought prose. The provider substitutes the
    ``thinking`` field for ``response`` on these models, so the parser
    receives a CoT blob and must pull the array out of it. Without
    the bracket-extractor fallback, every framing tone call to a
    thinking model returns None and the warning fires."""
    cot = (
        "We need to classify the tone of each news headline below as "
        "exactly one of: neutral, urgent, or alarmist. Headlines: 1. "
        "\"Scientists Discover New Species\" 2. \"TERRIFYING: New Virus\". "
        "Let me analyze each: 1. The first is matter-of-fact, so neutral. "
        "2. The second uses fear language, so alarmist. So the answer is "
        '["neutral", "alarmist"].'
    )
    assert _parse_tone_response(cot, 2) == ["neutral", "alarmist"]


def test_parse_tone_response_extracts_short_cot():
    """Even a one-line CoT with a trailing array should parse."""
    assert _parse_tone_response(
        'Let me think... The answer is ["neutral", "alarmist"].',
        2,
    ) == ["neutral", "alarmist"]


def test_parse_tone_response_ignores_extra_arrays():
    """If the model rambles past the first balanced array, the
    parser should take the first one (deterministic) and let the
    length / label checks catch the mismatch."""
    # First array has 2 items (matches expected_len); second is bogus.
    assert _parse_tone_response(
        'Result: ["neutral", "alarmist"]. Bonus: [1, 2, 3].',
        2,
    ) == ["neutral", "alarmist"]


def test_parse_tone_response_handles_escaped_quotes():
    """Bracket-extractor must skip over string contents, including
    escaped quotes that look like a closing-bracket but aren't.

    The string below is the literal text ``["a \\"quoted\\" alarmist", "neutral"]`` —
    i.e. standard JSON encoding of two tone labels, where the first
    string contains a literal double-quote character (not a string
    terminator — the backslash escapes it). Bracket-extractor must
    keep ``in_string=True`` across the escape sequence and find the
    real ``]`` at the end.

    The validate step will then reject ``'a "quoted" alarmist'`` as
    a tone label (not in ``_VALID_TONES``) — that's expected. This
    test only proves the bracket-extractor itself doesn't terminate
    on the backslash-quote and the resulting JSON round-trips.
    """
    extracted = _extract_first_json_array(
        'Output: ["a \\"quoted\\" alarmist", "neutral"]',
    )
    # Round-trip: the extracted substring is valid JSON whose first
    # item is the string ``a "quoted" alarmist`` (with a literal
    # double-quote from the escape).
    assert json.loads(extracted) == ['a "quoted" alarmist', "neutral"]


def test_parse_tone_response_valid_tones_with_escaped_quote():
    """End-to-end: a model response that includes an escaped quote
    INSIDE a valid tone label. The text is ``["alarmist", "neu\\"tral"]``,
    i.e. the second label is intentionally malformed to confirm the
    parser rejects it (escaped quotes are a tokenizer issue, not
    something we silently accept). A response with the escaped
    quote but otherwise valid would be ``["alarmist", "neutral"]`` —
    covered by the CoT tests above. This case confirms the parser
    doesn't crash on backslash-quote input.
    """
    # malformed label -> parser returns None (validate rejects)
    assert _parse_tone_response(
        '["alarmist", "neu\\"tral"]',
        2,
    ) is None


def test_parse_tone_response_truncated_array_returns_none():
    """No closing bracket -> No balanced array -> None."""
    assert _parse_tone_response('["neutral", "alarmist"', 2) is None


def test_parse_tone_response_empty_input_returns_none():
    assert _parse_tone_response("", 2) is None
    assert _parse_tone_response(None, 2) is None  # type: ignore[arg-type]


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
