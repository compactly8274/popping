"""Anthropic provider. POST to ``/v1/messages``.

Uses ``anthropic-version: 2023-06-01`` and a single-user message.
``max_tokens`` is required by the API, so we always set it.

``stop`` sequences are passed as ``stop_sequences`` (Anthropic
caps this at 4; we cap defensively to avoid an API error).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.llm.base import Provider, ProviderError

logger = logging.getLogger("popping.llm.anthropic")

# Anthropic's ``stop_sequences`` parameter caps at 4 entries.
_MAX_STOP_SEQUENCES = 4


class AnthropicProvider(Provider):
    name = "anthropic"

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
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if stop:
            payload["stop_sequences"] = stop[:_MAX_STOP_SEQUENCES]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic transport error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"anthropic returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            raise ProviderError("anthropic returned no text content")
        return text
