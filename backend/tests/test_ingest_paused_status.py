"""A paused feed's ingest says so: ``status: "paused"``.

Before, a manual fetch of a paused feed returned the same summary as a
check that found nothing new (all zeros, no error). ``IngestResult``
drops keys it doesn't declare, so the field has to exist on the model
for ``POST /api/ingest/{name}`` to carry it.
"""

from __future__ import annotations

from app import scheduler
from app.schemas import IngestResult
from factories import make_source
from ingest_harness import item, make_plugin, source_row


async def test_a_paused_feed_says_paused_and_fetches_nothing(db_session, offline_ingest):
    plugin = make_plugin("ps_paused")
    plugin.items = [item(0, hours_ago=1)]
    await make_source(db_session, plugin.name, url=plugin.url, active=False)

    summary = await scheduler._ingest(plugin)

    assert summary["status"] == "paused"
    assert (summary["fetched"], summary["inserted"], summary["error"]) == (0, 0, None)
    assert offline_ingest == []
    row = await source_row(db_session, plugin.name)
    assert row.last_fetch_at is None


async def test_an_active_feed_has_no_status(db_session, offline_ingest):
    plugin = make_plugin("ps_active")
    plugin.items = [item(0, hours_ago=1)]

    summary = await scheduler._ingest(plugin)

    assert "status" not in summary
    assert IngestResult(**summary).status is None


def test_ingest_result_keeps_the_status():
    result = IngestResult(source="x", fetched=0, inserted=0, duplicates=0, status="paused")
    assert result.status == "paused"
    assert result.model_dump()["status"] == "paused"


async def test_the_ingest_route_says_paused(db_session, app_client, offline_ingest):
    await make_source(db_session, "ps_route_paused", url="https://paused.test/feed", active=False)

    response = await app_client.post("/api/ingest/ps_route_paused")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "paused"
    assert body["fetched"] == 0
    assert body["error"] is None


async def test_the_ingest_route_has_no_status_for_an_active_feed(db_session, app_client, offline_ingest, monkeypatch):
    from app.sources.dynamic_rss import DynamicRssPlugin

    async def _empty(self):
        return []

    monkeypatch.setattr(DynamicRssPlugin, "fetch", _empty)
    await make_source(db_session, "ps_route_active", url="https://active.test/feed")

    response = await app_client.post("/api/ingest/ps_route_active")

    assert response.status_code == 200
    assert response.json()["status"] is None
