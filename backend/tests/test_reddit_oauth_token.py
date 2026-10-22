"""The OAuth token cache must work once credentials are configured (it
used to raise UnboundLocalError on the first call)."""

from __future__ import annotations

import httpx
import pytest

from app import reddit_client


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_token_is_fetched_once_and_cached(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        reddit_client.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(reddit_client.settings, "reddit_client_id", "id")
    monkeypatch.setattr(reddit_client.settings, "reddit_client_secret", "secret")
    monkeypatch.setattr(reddit_client, "_oauth_token", None)
    monkeypatch.setattr(reddit_client, "_oauth_expires_at", 0.0)

    assert await reddit_client._fresh_oauth_token() == "tok"
    assert await reddit_client._fresh_oauth_token() == "tok"
    assert len(calls) == 1


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_token_requested_from_documented_endpoint(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"access_token": "t", "expires_in": 60})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        reddit_client.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(reddit_client.settings, "reddit_client_id", "id")
    monkeypatch.setattr(reddit_client.settings, "reddit_client_secret", "secret")
    monkeypatch.setattr(reddit_client, "_oauth_token", None)
    monkeypatch.setattr(reddit_client, "_oauth_expires_at", 0.0)

    await reddit_client._fresh_oauth_token()
    assert seen == ["https://www.reddit.com/api/v1/access_token"]
