"""Tests for Reddit thread comment fetching (app.reddit_client's
comment-specific additions) and LLM summarization
(app.reddit_comment_summary), plus the
/entries/{id}/reddit_comment_summary route's cache/availability
composition.

Same convention as test_article_summary.py / test_podcast_transcript.py:
the real fetch+LLM-summarize happy path needs a network fetch and a
configured provider, neither of which a unit test should depend on
(and this module's route-level tests specifically avoid ever calling
the real ``fetch_thread_comments`` — the test app's ``app_client``
fixture runs without the app lifespan, so
``reddit_client.init_client()`` never fires and there's no shared
client to safely short-circuit an accidental real request). Covered
directly instead: the pure comment parser against fixture Atom XML,
the non-blocking rate-limit token bucket, and the provider-fallback
contract.
"""

from __future__ import annotations

import pytest

import app.reddit_client as reddit_client
from app.reddit_client import RedditRateLimited, _parse_comment_entries, fetch_thread_comments
from app.reddit_comment_summary import summarize_comments
from factories import make_entry, make_source

# --- app.reddit_client._parse_comment_entries ----------------------------

_COMMENT_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Comments on: A test thread</title>
  <entry>
    <author><name>/u/original_poster</name></author>
    <content type="html">&lt;p&gt;This is the submission text itself.&lt;/p&gt;</content>
  </entry>
  <entry>
    <author><name>/u/commenter_one</name></author>
    <content type="html">&lt;p&gt;This is a real, substantive comment reply.&lt;/p&gt;</content>
  </entry>
  <entry>
    <author><name>/u/commenter_two</name></author>
    <summary>Fallback via summary tag instead of content.</summary>
  </entry>
  <entry>
    <author><name>/u/commenter_three</name></author>
    <content type="html"></content>
  </entry>
</feed>
"""


def test_parse_comment_entries_extracts_author_and_text():
    result = _parse_comment_entries(_COMMENT_FEED_XML)
    assert result[0] == {"author": "/u/original_poster", "text": "This is the submission text itself."}
    assert result[1] == {"author": "/u/commenter_one", "text": "This is a real, substantive comment reply."}


def test_parse_comment_entries_falls_back_to_summary_tag():
    result = _parse_comment_entries(_COMMENT_FEED_XML)
    assert {"author": "/u/commenter_two", "text": "Fallback via summary tag instead of content."} in result


def test_parse_comment_entries_skips_empty_content():
    result = _parse_comment_entries(_COMMENT_FEED_XML)
    authors = [c["author"] for c in result]
    assert "/u/commenter_three" not in authors


def test_parse_comment_entries_malformed_xml_raises_value_error():
    with pytest.raises(ValueError):
        _parse_comment_entries("not xml at all <<<")


def test_parse_comment_entries_empty_feed_returns_empty_list():
    empty_feed = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    assert _parse_comment_entries(empty_feed) == []


# --- app.reddit_client rate-limit token bucket ----------------------------


@pytest.fixture
def isolated_bucket():
    """Save/restore the module-level token-bucket globals so draining
    them in a test doesn't leak into other tests (or other tests'
    state doesn't leak into this one) — the bucket is process-wide
    state, not per-test."""
    saved_tokens = reddit_client._bucket_tokens
    saved_last = reddit_client._bucket_last
    saved_direct = reddit_client._direct_client
    saved_proxy = reddit_client._proxy_client
    yield
    reddit_client._bucket_tokens = saved_tokens
    reddit_client._bucket_last = saved_last
    reddit_client._direct_client = saved_direct
    reddit_client._proxy_client = saved_proxy


@pytest.mark.asyncio
async def test_try_take_token_succeeds_when_bucket_full(isolated_bucket):
    reddit_client._bucket_tokens = 1.0
    import time
    reddit_client._bucket_last = time.monotonic()
    assert await reddit_client._try_take_token() is True


@pytest.mark.asyncio
async def test_try_take_token_fails_without_blocking_when_bucket_empty(isolated_bucket):
    import time
    reddit_client._bucket_tokens = 0.0
    reddit_client._bucket_last = time.monotonic()
    start = time.monotonic()
    result = await reddit_client._try_take_token()
    elapsed = time.monotonic() - start
    assert result is False
    # Non-blocking: should return near-instantly, nowhere close to
    # the ~75s refill interval.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_fetch_thread_comments_raises_rate_limited_when_bucket_empty(isolated_bucket):
    import time
    # Simulate an active direct-mode client (no proxy) with an empty
    # bucket — the exact state that should raise rather than fall
    # through to a real network attempt.
    reddit_client._direct_client = object()
    reddit_client._proxy_client = None
    reddit_client._bucket_tokens = 0.0
    reddit_client._bucket_last = time.monotonic()

    with pytest.raises(RedditRateLimited):
        await fetch_thread_comments("https://www.reddit.com/r/test/comments/abc123/a_thread/")


# --- app.reddit_comment_summary.summarize_comments ------------------------


@pytest.mark.asyncio
async def test_summarize_comments_empty_list_returns_none():
    result = await summarize_comments("A thread title", [])
    assert result is None


@pytest.mark.asyncio
async def test_summarize_comments_unreachable_provider_returns_none():
    # Test env has no cloud LLM API keys set, so the provider chain
    # falls through to local Ollama (always attempted, per the
    # router's unconditional fallback) — unreachable in this sandbox.
    comments = [{"author": "someone", "text": "an opinion about the thread"}]
    result = await summarize_comments("A thread title", comments)
    assert result is None


# --- POST /api/entries/{id}/reddit_comment_summary: cache/availability ----


@pytest.mark.asyncio
async def test_reddit_comment_summary_route_no_thread_url_returns_unavailable(app_client, db_session):
    source = await make_source(db_session, "some_source")
    entry = await make_entry(db_session, source, "An entry with no Reddit thread")

    resp = await app_client.post(f"/api/entries/{entry.id}/reddit_comment_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["summary"] is None
    assert body["cached"] is False
    assert body["rate_limited"] is False


@pytest.mark.asyncio
async def test_reddit_comment_summary_route_returns_cached_without_refetching(app_client, db_session):
    source = await make_source(db_session, "some_source2")
    entry = await make_entry(db_session, source, "An entry with a cached comment summary")
    entry.reddit_comment_summary = "A previously generated discussion summary."
    await db_session.commit()

    resp = await app_client.post(f"/api/entries/{entry.id}/reddit_comment_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is True
    assert body["available"] is True
    assert body["summary"] == "A previously generated discussion summary."


@pytest.mark.asyncio
async def test_reddit_comment_summary_route_404_for_missing_entry(app_client):
    resp = await app_client.post("/api/entries/999999/reddit_comment_summary")
    assert resp.status_code == 404
