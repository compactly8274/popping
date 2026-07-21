"""Tests for the per-source net vote score (``GET /api/sources``'
``net_vote_score`` field) — sum(thumb_up) - sum(thumb_down) across a
source's entries, powering the FeedManager's "which sources am I
consistently downvoting" badge.
"""

from __future__ import annotations

from app.models import Interaction
from factories import make_entry, make_source


async def _vote(db_session, entry, vote_type: str) -> None:
    db_session.add(Interaction(entry_id=entry.id, type=vote_type))
    await db_session.commit()


async def test_source_with_no_votes_defaults_to_zero(app_client, db_session):
    await make_source(db_session, "quiet_source")

    resp = await app_client.get("/api/sources")
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["name"] == "quiet_source")
    assert row["net_vote_score"] == 0


async def test_net_vote_score_nets_up_and_down(app_client, db_session):
    source = await make_source(db_session, "mixed_source")
    e1 = await make_entry(db_session, source, "Entry one")
    e2 = await make_entry(db_session, source, "Entry two")

    # 2 upvotes, 1 downvote across the source's entries -> net +1.
    await _vote(db_session, e1, "thumb_up")
    await _vote(db_session, e1, "thumb_up")
    await _vote(db_session, e2, "thumb_down")

    resp = await app_client.get("/api/sources")
    row = next(r for r in resp.json() if r["name"] == "mixed_source")
    assert row["net_vote_score"] == 1


async def test_net_vote_score_five_down_five_up_is_zero(app_client, db_session):
    # The exact scenario the user described: downvote 5 times (-5),
    # then upvote the same source 5 times, net should land back at 0.
    source = await make_source(db_session, "recovered_source")
    entry = await make_entry(db_session, source, "Some entry")
    for _ in range(5):
        await _vote(db_session, entry, "thumb_down")
    for _ in range(5):
        await _vote(db_session, entry, "thumb_up")

    resp = await app_client.get("/api/sources")
    row = next(r for r in resp.json() if r["name"] == "recovered_source")
    assert row["net_vote_score"] == 0


async def test_net_vote_score_isolated_per_source(app_client, db_session):
    source_a = await make_source(db_session, "source_a")
    source_b = await make_source(db_session, "source_b")
    entry_a = await make_entry(db_session, source_a, "A entry")
    entry_b = await make_entry(db_session, source_b, "B entry")
    await _vote(db_session, entry_a, "thumb_down")
    await _vote(db_session, entry_b, "thumb_up")

    resp = await app_client.get("/api/sources")
    rows = {r["name"]: r["net_vote_score"] for r in resp.json()}
    assert rows["source_a"] == -1
    assert rows["source_b"] == 1


async def test_non_vote_interaction_types_dont_affect_score(app_client, db_session):
    source = await make_source(db_session, "clicky_source")
    entry = await make_entry(db_session, source, "Clicked entry")
    for vtype in ("view", "click", "dwell", "bookmark", "never"):
        db_session.add(Interaction(entry_id=entry.id, type=vtype))
    await db_session.commit()

    resp = await app_client.get("/api/sources")
    row = next(r for r in resp.json() if r["name"] == "clicky_source")
    assert row["net_vote_score"] == 0
