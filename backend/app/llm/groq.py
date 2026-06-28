"""Groq provider. OpenAI-compatible at ``/openai/v1/chat/completions``.

Groq uses the same chat-completions schema as OpenAI; the only
difference is the base URL and the Authorization scheme.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.llm.base import Provider, ProviderError

logger = logging.getLogger("popping.llm.groq")


class GroqProvider(Provider):
    name = "groq"

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._api_key = api_key

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        stop: list[str] | None = None,
    ) -> str:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if stop:
            payload["stop"] = stop
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"groq transport error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"groq returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("groq returned no choices")
        text = (choices[0].get("message") or {}).get("content", "")
        if not text:
            raise ProviderError("groq returned empty message content")
        return text
