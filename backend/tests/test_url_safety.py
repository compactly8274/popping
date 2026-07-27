"""Regression test for the "summaries aren't loading, across the
board" bug: ``ssrf_event_hook`` was a plain (sync) ``def``, but
``httpx.AsyncClient`` awaits every callable registered under
``event_hooks={"request": [...]}``. Calling a sync function still
"works" (Python doesn't type-check that), but it returns None, and
httpx's ``await hook(request)`` then raises ``TypeError: object
NoneType can't be used in 'await' expression`` on the very first
request through any client that registers the hook — which is every
article/podcast summary fetch (``app.article_extract``,
``app.podcast_transcript``, ``app.podcast_asr``, ``app.assets``).

Exercises the hook exactly the way httpx does: registered on a real
``AsyncClient`` via ``event_hooks``, driven through a ``MockTransport``
so no real network is needed.
"""

from __future__ import annotations

import httpx
import pytest

from app.url_safety import ssrf_event_hook

pytestmark = pytest.mark.no_db


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


@pytest.mark.asyncio
async def test_ssrf_event_hook_is_a_valid_async_request_hook():
    """A client with the hook registered must be able to complete a
    request to an allowed host without the hook itself raising
    TypeError. This is the exact regression: before the fix, this
    call raised ``TypeError: object NoneType can't be used in
    'await' expression`` regardless of the target host."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(
        transport=transport,
        event_hooks={"request": [ssrf_event_hook]},
    ) as client:
        resp = await client.get("https://example.com/")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ssrf_event_hook_blocks_denied_host():
    """The hook's actual job: abort a request to a denied address
    before it's sent. Confirms the fix didn't just silence the
    TypeError by making the hook a no-op."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(
        transport=transport,
        event_hooks={"request": [ssrf_event_hook]},
    ) as client:
        with pytest.raises(httpx.RequestError):
            await client.get("http://127.0.0.1:9999/admin")
