"""Tests for article full-text extraction (app.article_extract) and
LLM summarization (app.article_summary), plus the /entries/{id}/summary
route's LLM-then-extract-fallback composition.

Same convention as test_podcast_transcript.py: the real
fetch+LLM-summarize happy path needs a network fetch and a configured
provider, neither of which a unit test should depend on. Covered
directly instead: the pure extraction function against fixture HTML,
the provider-fallback contract with no provider configured (the test
env's actual state), and the route's caching + fallback-composition
logic.
"""

from __future__ import annotations

import pytest

from app.article_extract import extract_text
from app.article_summary import summarize_article
from factories import make_entry, make_source

# --- app.article_extract.extract_text ---------------------------------


_ARTICLE_HTML = """
<html>
<head><title>Test Article</title></head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<article>
<h1>A Long Enough Headline For The Test</h1>
<p>This is the first paragraph of a real article, with enough
substantive content that a readability-style extractor should
recognize it as the main body text rather than boilerplate chrome.</p>
<p>This is a second paragraph continuing the story with more detail,
context, and enough total length to clear the extraction module's
minimum-usable-characters threshold comfortably.</p>
</article>
<footer>Copyright 2026. Privacy policy. Terms of service.</footer>
</body>
</html>
"""


def test_extract_text_pulls_article_body_not_nav_or_footer():
    text = extract_text(_ARTICLE_HTML)
    assert text is not None
    assert "first paragraph" in text
    assert "second paragraph" in text
    assert "Privacy policy" not in text
    assert "Home" not in text


def test_extract_text_returns_none_for_junk_html():
    assert extract_text("<html><body><nav>Home About</nav></body></html>") is None


def test_extract_text_returns_none_for_empty_string():
    assert extract_text("") is None


def test_extract_text_returns_none_below_minimum_usable_length():
    # A real <article> tag, but too short to trust as a genuine
    # extraction rather than a stray fragment.
    assert extract_text("<html><body><article><p>Too short.</p></article></body></html>") is None


# --- app.article_summary.summarize_article ------------------------------


@pytest.mark.asyncio
async def test_summarize_article_unreachable_provider_returns_none():
    # Test env has no cloud LLM API keys set, so the provider chain
    # falls through to local Ollama (always attempted, per the
    # router's unconditional fallback — see test_feed_discovery.py's
    # equivalent note) — unreachable in this sandbox, so every
    # provider fails and this exercises the "all providers failed"
    # path without crashing.
    result = await summarize_article("A headline", "Some article body text.")
    assert result is None


# --- POST /api/entries/{id}/summary: LLM-then-extract-fallback composition ---


@pytest.mark.asyncio
async def test_summary_route_falls_back_to_feed_blurb_when_article_fetch_fails(app_client, db_session):
    # Local Ollama is always attempted regardless of configuration
    # (see test_summarize_article_unreachable_provider_returns_none),
    # so this can't rely on "no provider at all" to reach the
    # fallback path deterministically across environments (a CI
    # runner with real internet access would actually fetch a
    # reachable article URL). Point the entry at a loopback address
    # instead — ``check_url_safe`` rejects it unconditionally, so the
    # article-fetch attempt short-circuits with no real network I/O
    # in any environment, and the route falls through to the feed's
    # own blurb exactly as it always did before the LLM path existed.
    source = await make_source(db_session, "some_feed")
    entry = await make_entry(db_session, source, "A headline", url="http://127.0.0.1:1/unreachable")
    entry.meta = {"summary": "<p>The feed's own short blurb.</p>"}
    await db_session.commit()

    resp = await app_client.post(f"/api/entries/{entry.id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == "The feed's own short blurb."
    assert body["cached"] is False


@pytest.mark.asyncio
async def test_summary_route_returns_cached_without_recomputing(app_client, db_session):
    source = await make_source(db_session, "some_feed2")
    entry = await make_entry(db_session, source, "A headline")
    entry.cached_summary = "A previously generated summary."
    await db_session.commit()

    resp = await app_client.post(f"/api/entries/{entry.id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is True
    assert body["summary"] == "A previously generated summary."


@pytest.mark.asyncio
async def test_summary_route_no_blurb_caches_empty_string(app_client, db_session):
    source = await make_source(db_session, "some_feed3")
    # Loopback URL — see the fallback test above for why: guarantees
    # the article-fetch attempt short-circuits with no real network
    # I/O regardless of environment.
    entry = await make_entry(db_session, source, "A headline", url="http://127.0.0.1:1/unreachable")
    # No meta.summary, no body_text — nothing for either path to work with.

    resp = await app_client.post(f"/api/entries/{entry.id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == ""
    assert body["cached"] is False

    # Second call hits the cache (cached_summary == "") rather than
    # re-running the fallback chain.
    resp2 = await app_client.post(f"/api/entries/{entry.id}/summary")
    assert resp2.json() == {"summary": "", "cached": True}


@pytest.mark.asyncio
async def test_summary_route_404_for_missing_entry(app_client):
    resp = await app_client.post("/api/entries/999999/summary")
    assert resp.status_code == 404
