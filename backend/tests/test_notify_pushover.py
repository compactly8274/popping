"""PushoverNotifier keeps messages within Pushover's limits."""

from __future__ import annotations

import httpx
import pytest

from app import notify as notify_mod
from app.notify import PushoverNotifier


def _capture(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": 1})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        notify_mod.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def _form(request):
    return dict(httpx.QueryParams(request.content.decode()))


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_long_message_clipped_to_pushover_limit(monkeypatch):
    seen = _capture(monkeypatch)
    await PushoverNotifier("u", "t").send(title="T" * 300, body="x" * 3000)
    form = _form(seen[0])
    assert len(form["title"]) == 250
    assert len(form["message"]) == 1024
    assert form["message"].endswith("…")


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_short_message_unchanged(monkeypatch):
    seen = _capture(monkeypatch)
    await PushoverNotifier("u", "t").send(title="Brief", body="hello")
    form = _form(seen[0])
    assert form["message"] == "hello"
    assert form["title"] == "Brief"
