"""``POST /api/sources/test`` for ``type="reddit"``.

The route used to import ``fetch_subreddit`` from ``app.sources.reddit``,
which has none, so every subreddit Test failed with an ImportError
reported as ``unknown``. It now calls ``reddit_client.fetch_subreddit``
with the subreddit name, the same client the scheduled ingest uses,
and maps its outcomes:

- client disabled -> ``unsupported_type``
- ``RedditFetchThrottled`` (429 / 5xx) -> ``network_error``
- empty listing (also private, banned and 403 subs, which
  ``fetch_subreddit`` turns into ``[]``) -> ``not_found``
- a listing -> ``ok`` with the count and sample titles
"""

from __future__ import annotations

import pytest

from app import reddit_client
from app.routes import sources as sources_routes
from app.schemas import SourceTestRequest

pytestmark = pytest.mark.no_db


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    # The canonical URL is always www.reddit.com; skip the real DNS
    # lookup the SSRF guard would do (this container has no DNS).
    monkeypatch.setattr(sources_routes, "check_url_safe", lambda url: (True, None))


async def _run(url: str = "r/Python"):
    # No ``name`` in the body, so the route never touches the session.
    return await sources_routes._test_source_impl(
        SourceTestRequest(type="reddit", url=url), session=None
    )


def _fake_fetch(result=None, exc: Exception | None = None, calls: list | None = None):
    async def _fetch(subreddit, listing="hot", limit=50):
        if calls is not None:
            calls.append((subreddit, listing, limit))
        if exc is not None:
            raise exc
        return result

    return _fetch


async def test_disabled_client_is_unsupported(monkeypatch):
    calls: list = []
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: True)
    monkeypatch.setattr(reddit_client, "fetch_subreddit", _fake_fetch([], calls=calls))
    result = await _run()
    assert result.ok is False
    assert result.error_kind == "unsupported_type"
    assert calls == []


async def test_throttled_is_a_network_error(monkeypatch):
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: False)
    monkeypatch.setattr(
        reddit_client,
        "fetch_subreddit",
        _fake_fetch(exc=reddit_client.RedditFetchThrottled(429)),
    )
    result = await _run()
    assert result.ok is False
    assert result.error_kind == "network_error"
    assert result.status_code == 429


async def test_empty_listing_is_not_found(monkeypatch):
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: False)
    monkeypatch.setattr(reddit_client, "fetch_subreddit", _fake_fetch([]))
    result = await _run()
    assert result.ok is False
    assert result.error_kind == "not_found"


async def test_listing_is_ok_and_passes_the_subreddit_name(monkeypatch):
    calls: list = []
    posts = [{"title": f"Post {i}", "url": f"https://example.com/{i}"} for i in range(5)]
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: False)
    monkeypatch.setattr(reddit_client, "fetch_subreddit", _fake_fetch(posts, calls=calls))
    result = await _run("https://www.reddit.com/r/Python/hot")
    assert result.ok is True
    assert result.item_count == 5
    assert result.sample_titles == ["Post 0", "Post 1", "Post 2"]
    # The subreddit name, not the canonical URL, and a small page.
    assert calls == [("python", "hot", 25)]
