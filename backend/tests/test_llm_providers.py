"""Regression test for the "I get literal summaries now" bug: a
thinking-capable model (glm-5.2:cloud, deepseek-r1, gpt-oss — see
``app.llm.tags._THINKING_MODELS``) given a small ``max_tokens`` budget
can spend the whole budget on its chain-of-thought preamble and never
reach the actual answer. When that happens, ``response`` comes back
empty and the provider falls back to using the (often truncated) raw
``thinking`` text as the completion — which is exactly what leaked
into the UI as a "summary": a restatement of the task instructions
instead of an actual summary.

Fix: ``Provider.complete()`` gained a ``think`` parameter. The short,
tightly-token-capped, direct-answer callers (article/podcast/Reddit-
comment summarizers, framing's tone classifier) now pass
``think=False`` so Ollama/Ollama Cloud skip the CoT preamble entirely.
These tests cover the two provider implementations that actually act
on it (the wire payload must include ``"think": false`` when disabled,
and must NOT include the key at all — preserving the exact prior wire
shape — when the default is used, so the Brief generator's behavior
is untouched) and the call-site plumbing in each summarizer module.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm.ollama import OllamaProvider
from app.llm.ollama_cloud import OllamaCloudProvider


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Records the last request's JSON body; always returns a fixed
    Ollama-shaped response so ``complete()`` runs to completion."""

    def __init__(self, response_body: dict):
        self.last_request: httpx.Request | None = None
        self._response_body = response_body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(200, json=self._response_body)


@pytest.fixture
def patch_httpx_client(monkeypatch):
    """Monkeypatch ``httpx.AsyncClient`` so every provider's internal
    ``httpx.AsyncClient(timeout=...)`` construction is routed through
    a capturing transport instead of a real socket. Returns the
    transport so the test can inspect the request that was sent."""
    transport = _CapturingTransport({"response": "a real summary", "thinking": ""})

    real_client = httpx.AsyncClient

    class _PatchedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)
    return transport


def _sent_payload(transport: _CapturingTransport) -> dict:
    assert transport.last_request is not None
    return json.loads(transport.last_request.content)


@pytest.mark.asyncio
async def test_ollama_cloud_omits_think_key_by_default(patch_httpx_client):
    provider = OllamaCloudProvider(model="glm-5.2:cloud", api_key="test-key")
    result = await provider.complete("summarize this", max_tokens=200)

    assert result == "a real summary"
    payload = _sent_payload(patch_httpx_client)
    assert "think" not in payload


@pytest.mark.asyncio
async def test_ollama_cloud_sets_think_false_when_disabled(patch_httpx_client):
    provider = OllamaCloudProvider(model="glm-5.2:cloud", api_key="test-key")
    await provider.complete("summarize this", max_tokens=200, think=False)

    payload = _sent_payload(patch_httpx_client)
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_ollama_local_sets_think_false_when_disabled(patch_httpx_client):
    provider = OllamaProvider(model="gpt-oss:20b")
    await provider.complete("summarize this", max_tokens=200, think=False)

    payload = _sent_payload(patch_httpx_client)
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_ollama_local_omits_think_key_by_default(patch_httpx_client):
    provider = OllamaProvider(model="gpt-oss:20b")
    await provider.complete("summarize this", max_tokens=200)

    payload = _sent_payload(patch_httpx_client)
    assert "think" not in payload


@pytest.mark.asyncio
async def test_ollama_cloud_omits_think_key_for_non_thinking_model_even_when_disabled(
    patch_httpx_client,
):
    """A model not in _THINKING_MODELS has no concept of "thinking" —
    the key must not be sent at all, even when the caller asks for
    think=False, so a model/backend that errors on unrecognized
    fields is never at risk."""
    provider = OllamaCloudProvider(model="llama3.1:8b", api_key="test-key")
    await provider.complete("summarize this", max_tokens=200, think=False)

    payload = _sent_payload(patch_httpx_client)
    assert "think" not in payload


# --- call-site plumbing: the summarizers must request think=False ----------


class _RecordingProvider:
    """Minimal stand-in for a Provider — records the kwargs each
    caller passes to ``complete()`` without touching the network."""

    name = "recording"

    def __init__(self, response: str = "a real summary"):
        self.calls: list[dict] = []
        self._response = response

    async def complete(self, prompt, *, max_tokens=512, stop=None, think=True):
        self.calls.append({"max_tokens": max_tokens, "stop": stop, "think": think})
        return self._response


@pytest.mark.asyncio
async def test_article_summary_requests_think_false(monkeypatch):
    from app import article_summary

    fake = _RecordingProvider()
    monkeypatch.setattr(article_summary.router, "providers_for", lambda task: [fake])

    result = await article_summary.summarize_article("Headline", "Article body text.")

    assert result == "a real summary"
    assert fake.calls[-1]["think"] is False


@pytest.mark.asyncio
async def test_podcast_transcript_requests_think_false(monkeypatch):
    from app import podcast_transcript

    fake = _RecordingProvider()
    monkeypatch.setattr(podcast_transcript.router, "providers_for", lambda task: [fake])

    result = await podcast_transcript.summarize_transcript("Episode", "Transcript text.")

    assert result == "a real summary"
    assert fake.calls[-1]["think"] is False


@pytest.mark.asyncio
async def test_reddit_comment_summary_requests_think_false(monkeypatch):
    from app import reddit_comment_summary

    fake = _RecordingProvider()
    monkeypatch.setattr(reddit_comment_summary.router, "providers_for", lambda task: [fake])

    result = await reddit_comment_summary.summarize_comments(
        "Thread title", [{"author": "alice", "text": "great point"}],
    )

    assert result == "a real summary"
    assert fake.calls[-1]["think"] is False
