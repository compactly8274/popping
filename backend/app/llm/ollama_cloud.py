"""Ollama Cloud provider.

Same HTTP shape as the local ``app.llm.ollama`` provider (``POST
/api/generate``), just with a different base URL and a Bearer token.
The Cloud API is OpenAI-compatible for ``/api/chat`` but uses the
native Ollama schema for ``/api/generate``, so we can reuse the
request/response parsing logic byte-for-byte.

Base URL is fixed at ``https://ollama.com`` per Ollama's docs — the
API key selects the account. We don't read this from settings so a
typo can't point it at a private host.

Auth: ``Authorization: Bearer $OLLAMA_CLOUD_API_KEY``. Key is opaque
(the same way ``ANTHROPIC_API_KEY`` is opaque) — never logged.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.llm.base import Provider, ProviderError
from app.llm.tags import _THINKING_MODELS

logger = logging.getLogger("popping.llm.ollama_cloud")


class OllamaCloudProvider(Provider):
    name = "ollama_cloud"
    _BASE_URL = "https://ollama.com"

    def __init__(self, model: str, api_key: str) -> None:
        self._base = self._BASE_URL
        self._model = model
        self._api_key = api_key

    async def complete(self, prompt: str, *, max_tokens: int = 512) -> str:
        url = f"{self._base}/api/generate"
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama cloud transport error: {exc}") from exc
        if resp.status_code != 200:
            # Don't echo the body back into logs — Ollama's auth-failure
            # body can be verbose. The status code is enough to act on.
            raise ProviderError(
                f"ollama cloud returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        text = data.get("response", "")
        thinking = data.get("thinking", "")
        # Only substitute the thinking field for models we know put
        # their answer there. Otherwise an empty ``response`` is a real
        # error (context overflow, model misbehavior, malformed prompt)
        # and substituting the CoT blob would dump raw reasoning into
        # the user's brief. See ``_THINKING_MODELS`` in tags.py.
        if not text and thinking and self._model in _THINKING_MODELS:
            # Thinking-style model: the final answer is empty and the
            # chain-of-thought / actual content is in ``thinking``. We
            # use the thinking text as the completion rather than
            # failing — it's still useful output, even if it's not a
            # "clean" answer. The user can re-prompt or switch models.
            logger.info(
                "ollama cloud: response empty, falling back to thinking for model=%s thinking_len=%d",
                self._model, len(thinking),
            )
            text = thinking
        if not text:
            # Log the response shape (not the body — it's potentially
            # large). Helps diagnose model-not-found / context-overflow
            # cases that surface as 200 + empty response instead of 4xx.
            logger.warning(
                "ollama cloud: empty response for model=%s keys=%s done=%s done_reason=%s eval_count=%s response_len=%d thinking_len=%d",
                self._model,
                sorted(data.keys()),
                data.get("done"),
                data.get("done_reason"),
                data.get("eval_count"),
                len(data.get("response", "") or ""),
                len(data.get("thinking", "") or ""),
            )
            raise ProviderError(f"ollama cloud returned empty response (model={self._model})")
        return text