"""OpenAI-compatible chat-completions provider.

``OpenAIProvider`` talks to ``OPENAI_BASE_URL`` (default
https://api.openai.com/v1), so any service with the same API works too:
OpenRouter, Gemini's OpenAI endpoint, DeepSeek, Mistral, Together, etc.
``GroqProvider`` (app.llm.groq) is the same class with Groq's URL.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.llm.base import Provider, ProviderError

logger = logging.getLogger("popping.llm.openai")


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        self._model = model
        self._api_key = api_key
        self._url = (base_url or self._default_base_url()).rstrip("/") + "/chat/completions"

    def _default_base_url(self) -> str:
        return settings.openai_base_url

    def _payload_overrides(self, max_tokens: int) -> dict[str, Any]:
        """Extra or replacement request fields for a specific service."""
        return {}

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        stop: list[str] | None = None,
        think: bool = True,
    ) -> str:
        # ``think`` is an Ollama-specific knob (see Provider.complete's
        # docstring) — accepted for call-site uniformity, unused here.
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        # OpenAI's own API wants ``max_completion_tokens`` (its newer
        # models reject ``max_tokens``); compatible services still expect
        # ``max_tokens``.
        token_key = (
            "max_completion_tokens"
            if urlparse(self._url).hostname == "api.openai.com"
            else "max_tokens"
        )
        payload: dict[str, Any] = {
            "model": self._model,
            token_key: max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if stop:
            payload["stop"] = stop
        payload.update(self._payload_overrides(max_tokens))
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                resp = await client.post(self._url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} transport error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"{self.name} returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name} returned invalid JSON: {exc}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name} returned no choices")
        text = (choices[0].get("message") or {}).get("content", "")
        if not text:
            raise ProviderError(f"{self.name} returned empty message content")
        return text
