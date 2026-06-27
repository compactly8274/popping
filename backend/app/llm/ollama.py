"""Ollama provider. Local HTTP API at ``${base}/api/generate``.

Works with any Ollama-served model. Default model name lives in
Settings (``ollama_model_*``). Single-shot generate, not chat — we
send a prompt and want the completion.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.llm.base import Provider, ProviderError

logger = logging.getLogger("popping.llm.ollama")


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str) -> None:
        self._base = settings.ollama_base_url.rstrip("/")
        self._model = model

    async def complete(self, prompt: str, *, max_tokens: int = 512) -> str:
        url = f"{self._base}/api/generate"
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama transport error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"ollama returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        text = data.get("response", "")
        if not text:
            raise ProviderError("ollama returned empty response")
        return text
