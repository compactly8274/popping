"""Tests for GET /api/entries/{id}/related — the per-card "other
outlets' coverage of this story" panel, which re-surfaces Framing
Watch's existing clustering (app.framing) scoped to a single entry
rather than the standalone section's full cluster listing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models import StoryCluster
from factories import make_entry, make_source


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@pytest.mark.asyncio
async def test_related_route_404_for_missing_entry(app_client):
    resp = await app_client.get("/api/entries/999999/related")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_related_route_returns_null_when_not_clustered(app_client, db_session):
    source = await make_source(db_session, "solo_source")
    entry = await make_entry(db_session, source, "A story nobody else covered")

    resp = await app_client.get(f"/api/entries/{entry.id}/related")
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_related_route_returns_other_outlets_excluding_self(app_client, db_session):
    source_a = await make_source(db_session, "outlet_a", category="news")
    source_b = await make_source(db_session, "outlet_b", category="news")
    source_c = await make_source(db_session, "outlet_c", category="news")
    cluster = StoryCluster(wire_source="AP", first_seen_at=_now())
    db_session.add(cluster)
    await db_session.commit()
    await db_session.refresh(cluster)

    entry_a = await make_entry(db_session, source_a, "Storm makes landfall", published_at=_now())
    entry_b = await make_entry(db_session, source_b, "Hurricane strikes region", published_at=_now())
    entry_c = await make_entry(db_session, source_c, "Coastal areas evacuated", published_at=_now())
    entry_a.story_cluster_id = cluster.id
    entry_b.story_cluster_id = cluster.id
    entry_c.story_cluster_id = cluster.id
    entry_b.framing_tone = "alarmist"
    db_session.add_all([entry_a, entry_b, entry_c])
    await db_session.commit()

    resp = await app_client.get(f"/api/entries/{entry_a.id}/related")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cluster_id"] == cluster.id
    assert body["wire_source"] == "AP"

    # Excludes entry_a itself — only the OTHER outlets' coverage.
    titles = {a["title"] for a in body["articles"]}
    assert titles == {"Hurricane strikes region", "Coastal areas evacuated"}
    entry_ids = {a["entry_id"] for a in body["articles"]}
    assert entry_a.id not in entry_ids

    tone_by_title = {a["title"]: a["framing_tone"] for a in body["articles"]}
    assert tone_by_title["Hurricane strikes region"] == "alarmist"
    assert tone_by_title["Coastal areas evacuated"] is None


@pytest.mark.asyncio
async def test_related_route_returns_null_when_no_surviving_siblings(app_client, db_session):
    # A cluster row exists and this entry points at it, but no OTHER
    # entry shares the cluster (e.g. the siblings were since purged) —
    # nothing left to show, same as "not clustered" from the
    # frontend's point of view.
    source = await make_source(db_session, "lonely_source")
    cluster = StoryCluster(wire_source=None, first_seen_at=_now())
    db_session.add(cluster)
    await db_session.commit()
    await db_session.refresh(cluster)

    entry = await make_entry(db_session, source, "Only member left")
    entry.story_cluster_id = cluster.id
    await db_session.commit()

    resp = await app_client.get(f"/api/entries/{entry.id}/related")
    assert resp.status_code == 200
    assert resp.json() is None
