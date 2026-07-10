"""Tests for podcast transcript extraction (app.sources.rss) and
plain-text conversion + route caching (app.podcast_transcript,
routes/entries.py's /podcast_summary endpoint).

The fetch + LLM-summarize happy path isn't covered here (it needs a
real network fetch and a real LLM provider — not something a unit
test should depend on); the conversion functions and route caching
logic are covered directly instead, which is where a regression is
actually likely to land.
"""

from __future__ import annotations

import pytest

from app.podcast_transcript import (
    _plain_text_from_captions,
    _plain_text_from_json,
    _to_plain_text,
)
from app.sources.rss import _extract_podcast_transcripts
from factories import make_entry, make_source


# --- app.sources.rss._extract_podcast_transcripts -------------------------

_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>Test Podcast</title>
    <item>
      <title>Episode 1</title>
      <link>https://example.com/ep1</link>
      <guid>ep1-guid</guid>
      <podcast:transcript url="https://example.com/ep1.srt" type="application/srt" />
      <podcast:transcript url="https://example.com/ep1.json" type="application/json" />
    </item>
    <item>
      <title>Episode 2</title>
      <link>https://example.com/ep2</link>
      <guid>ep2-guid</guid>
    </item>
  </channel>
</rss>
"""


def test_extract_podcast_transcripts_prefers_json_over_srt():
    result = _extract_podcast_transcripts(_FEED_XML)
    assert result["https://example.com/ep1"] == ("https://example.com/ep1.json", "application/json")
    # Same entry, keyed by guid too.
    assert result["ep1-guid"] == ("https://example.com/ep1.json", "application/json")


def test_extract_podcast_transcripts_no_tag_for_item_without_one():
    result = _extract_podcast_transcripts(_FEED_XML)
    assert "https://example.com/ep2" not in result
    assert "ep2-guid" not in result


def test_extract_podcast_transcripts_non_podcast_feed_returns_empty():
    plain_rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel><item><title>x</title><link>https://example.com/x</link></item></channel></rss>
    """
    assert _extract_podcast_transcripts(plain_rss) == {}


def test_extract_podcast_transcripts_malformed_xml_returns_empty():
    assert _extract_podcast_transcripts("not xml at all <<<") == {}


# --- app.podcast_transcript conversion helpers -----------------------------


def test_plain_text_from_json_segments():
    body = '{"segments": [{"speaker": "Alice", "body": "Hello there."}, {"speaker": "Bob", "body": "Hi Alice."}]}'
    assert _plain_text_from_json(body) == "Alice: Hello there.\nBob: Hi Alice."


def test_plain_text_from_json_flat_text_fallback():
    body = '{"text": "A single blob of transcript text."}'
    assert _plain_text_from_json(body) == "A single blob of transcript text."


def test_plain_text_from_json_unparseable_returns_raw():
    assert _plain_text_from_json("not json") == "not json"


def test_plain_text_from_captions_strips_timing_and_sequence():
    vtt = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "Welcome to the show.\n\n"
        "2\n"
        "00:00:04.500 --> 00:00:07.000\n"
        "Today we talk about testing.\n"
    )
    assert _plain_text_from_captions(vtt) == "Welcome to the show. Today we talk about testing."


def test_plain_text_from_captions_srt_comma_timing():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Hello.\n"
    )
    assert _plain_text_from_captions(srt) == "Hello."


def test_to_plain_text_dispatches_by_content_type():
    assert _to_plain_text('{"text": "hi"}', "application/json") == "hi"
    assert _to_plain_text("<p>hi <b>there</b></p>", "text/html") == "hi there"
    assert _to_plain_text("plain body", "text/plain") == "plain body"
    assert _to_plain_text("unrecognized body", "") == "unrecognized body"


# --- route caching behavior --------------------------------------------


@pytest.mark.asyncio
async def test_podcast_summary_route_no_transcript_returns_unavailable(app_client, db_session):
    source = await make_source(db_session, "some_podcast", category="podcast", type="podcast")
    entry = await make_entry(db_session, source, "Episode with no transcript")

    resp = await app_client.post(f"/api/entries/{entry.id}/podcast_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["summary"] is None
    assert body["cached"] is False


@pytest.mark.asyncio
async def test_podcast_summary_route_returns_cached_without_refetching(app_client, db_session):
    source = await make_source(db_session, "some_podcast2", category="podcast", type="podcast")
    entry = await make_entry(db_session, source, "Episode with a cached summary")
    entry.podcast_transcript_summary = "A previously generated summary."
    await db_session.commit()

    resp = await app_client.post(f"/api/entries/{entry.id}/podcast_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is True
    assert body["available"] is True
    assert body["summary"] == "A previously generated summary."


@pytest.mark.asyncio
async def test_podcast_summary_route_404_for_missing_entry(app_client):
    resp = await app_client.post("/api/entries/999999/podcast_summary")
    assert resp.status_code == 404
