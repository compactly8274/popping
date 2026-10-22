"""``GET /api/entries/by-ids`` returns the same card fields as
``/api/entries`` plus ``source_name`` (PR4b), so Saved renders a story
loaded by id the same way a tab renders it."""

from __future__ import annotations

import datetime as dt

from app.models import Entry, StoryCluster
from factories import make_entry, make_source


async def _rich_entry(db_session) -> Entry:
    feed = await make_source(db_session, "rich_feed", category="podcasts")
    cluster = StoryCluster()
    db_session.add(cluster)
    await db_session.commit()
    await db_session.refresh(cluster)
    entry = await make_entry(db_session, feed, "Episode 12", composite_score=12.5)
    entry.image_url = "https://example.com/art.jpg"
    entry.image_path = "thumbnails/1.jpg"
    entry.cached_summary = "A short summary."
    entry.story_cluster_id = cluster.id
    entry.meta = {
        "summary": "the long feed blurb the card never needs",
        "reddit_thread_url": "https://www.reddit.com/r/x/comments/abc/",
        "reddit_comment_count": 42,
        "audio_url": "https://example.com/ep12.mp3",
        "duration_seconds": 3600,
        "transcript_url": "https://example.com/ep12.vtt",
    }
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)
    return entry


async def test_a_seeded_row_returns_the_card_fields(app_client, db_session):
    entry = await _rich_entry(db_session)

    resp = await app_client.get("/api/entries/by-ids", params={"ids": str(entry.id)})
    assert resp.status_code == 200, resp.text
    [row] = resp.json()
    assert row["source_name"] == "rich_feed"
    assert row["image_url"] == "https://example.com/art.jpg"
    assert row["image_path"] == "thumbnails/1.jpg"
    assert row["cached_summary"] == "A short summary."
    assert row["reddit_thread_url"] == "https://www.reddit.com/r/x/comments/abc/"
    assert row["reddit_comment_count"] == 42
    assert row["audio_url"] == "https://example.com/ep12.mp3"
    assert row["duration_seconds"] == 3600
    assert row["transcript_url"] == "https://example.com/ep12.vtt"
    assert row["story_cluster_id"] == entry.story_cluster_id
    # The full meta blob is gone.
    assert "meta" not in row


async def test_same_projection_as_the_list(app_client, db_session):
    entry = await _rich_entry(db_session)

    [listed] = (await app_client.get("/api/entries")).json()
    [by_id] = (await app_client.get("/api/entries/by-ids", params={"ids": str(entry.id)})).json()
    assert by_id.pop("source_name") == "rich_feed"
    assert by_id == listed


async def test_input_order_and_bad_ids(app_client, db_session):
    feed = await make_source(db_session, "order_feed")
    now = dt.datetime.now(dt.timezone.utc)
    a = await make_entry(db_session, feed, "a", published_at=now)
    b = await make_entry(db_session, feed, "b", published_at=now)

    # Not numbers, out of the BIGINT range, or no such row: dropped,
    # never a 4xx or 5xx for the whole call.
    ids = f"{b.id},abc,-1,0,{2**63},99999999999999999999,{a.id},424242"
    resp = await app_client.get("/api/entries/by-ids", params={"ids": ids})
    assert resp.status_code == 200, resp.text
    assert [r["id"] for r in resp.json()] == [b.id, a.id]


async def test_empty_and_all_bad_ids(app_client):
    for ids in ("", " , ", "x,y"):
        resp = await app_client.get("/api/entries/by-ids", params={"ids": ids})
        assert resp.status_code == 200, resp.text
        assert resp.json() == []
