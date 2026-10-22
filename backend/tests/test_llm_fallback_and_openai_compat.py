"""complete_with_fallback and the OpenAI-compatible providers."""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm import ProviderError, complete_with_fallback
from app.llm import router as llm_router
from app.llm import openai as openai_mod
from app.llm.groq import GroqProvider
from app.llm.openai import OpenAIProvider


class _Fake:
    def __init__(self, name, result=None, exc=None):
        self.name = name
        self._result = result
        self._exc = exc
        self.calls = 0

    async def complete(self, prompt, **kwargs):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._result


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_fallback_skips_failing_and_unparsable_providers(monkeypatch):
    broken = _Fake("a", exc=ProviderError("boom"))
    garbage = _Fake("b", result="not json")
    good = _Fake("c", result="[1, 2]")
    monkeypatch.setattr(llm_router, "providers_for", lambda task: [broken, garbage, good])

    def parse(text):
        try:
            return json.loads(text)
        except ValueError:
            return None

    result, note = await complete_with_fallback("brief", "p", log_name="t", parse=parse)
    assert result == [1, 2] and note is None
    assert (broken.calls, garbage.calls, good.calls) == (1, 1, 1)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_fallback_reports_last_failure(monkeypatch):
    monkeypatch.setattr(llm_router, "providers_for", lambda task: [_Fake("x", result="  ")])
    assert await complete_with_fallback("brief", "p", log_name="t") == (
        None,
        "x: returned unusable output",
    )
    monkeypatch.setattr(llm_router, "providers_for", lambda task: [])
    assert await complete_with_fallback("brief", "p", log_name="t") == (
        None,
        "no LLM provider configured",
    )


def _capture_requests(monkeypatch, response):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return response

    real = httpx.AsyncClient
    monkeypatch.setattr(
        openai_mod.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


_OK = httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_openai_uses_max_completion_tokens(monkeypatch):
    seen = _capture_requests(monkeypatch, _OK)
    assert await OpenAIProvider("m", "k").complete("p", max_tokens=50) == "hi"
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert json.loads(seen[0].content)["max_completion_tokens"] == 50


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_compatible_base_url_uses_max_tokens(monkeypatch):
    seen = _capture_requests(monkeypatch, _OK)
    provider = OpenAIProvider("m", "k", base_url="https://openrouter.ai/api/v1/")
    await provider.complete("p", max_tokens=50)
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert json.loads(seen[0].content)["max_tokens"] == 50


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_groq_url_and_error_naming(monkeypatch):
    seen = _capture_requests(monkeypatch, httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError, match="^groq returned invalid JSON"):
        await GroqProvider("m", "k").complete("p")
    assert str(seen[0].url) == "https://api.groq.com/openai/v1/chat/completions"


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_groq_gpt_oss_gets_reasoning_headroom(monkeypatch):
    seen = _capture_requests(monkeypatch, _OK)
    await GroqProvider("openai/gpt-oss-120b", "k").complete("p", max_tokens=600)
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 600 + 1024
    assert body["reasoning_effort"] == "low"
    assert body["include_reasoning"] is False


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_groq_other_models_unchanged(monkeypatch):
    seen = _capture_requests(monkeypatch, _OK)
    await GroqProvider("qwen/qwen3-32b", "k").complete("p", max_tokens=600)
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 600
    assert "reasoning_effort" not in body
