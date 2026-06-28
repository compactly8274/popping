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
from app.llm.tags import _THINKING_MODELS

logger = logging.getLogger("popping.llm.ollama")


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str) -> None:
        self._base = settings.ollama_base_url.rstrip("/")
        self._model = model

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        stop: list[str] | None = None,
    ) -> str:
        url = f"{self._base}/api/generate"
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        if stop:
            # Ollama's native ``stop`` field. Halts generation the
            # moment any of these strings appear in the output.
            payload["stop"] = stop
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
        thinking = data.get("thinking", "")
        # Only substitute the thinking field for models we know put
        # their answer there. See the matching comment in
        # ``ollama_cloud.py`` and ``_THINKING_MODELS`` in tags.py.
        if not text and thinking and self._model in _THINKING_MODELS:
            # Thinking-style model: the final answer is empty and the
            # chain-of-thought / actual content is in ``thinking``. We
            # use the thinking text as the completion rather than
            # failing — it's still useful output, even if it's not a
            # "clean" answer. The user can re-prompt or switch models.
            logger.info(
                "ollama: response empty, falling back to thinking for model=%s thinking_len=%d",
                self._model, len(thinking),
            )
            text = thinking
        if not text:
            # Log the response shape (not the body — it's potentially
            # large). Helps diagnose model-not-found / context-overflow
            # cases that surface as 200 + empty response instead of 4xx.
            logger.warning(
                "ollama: empty response for model=%s keys=%s done=%s done_reason=%s eval_count=%s response_len=%d thinking_len=%d",
                self._model,
                sorted(data.keys()),
                data.get("done"),
                data.get("done_reason"),
                data.get("eval_count"),
                len(data.get("response", "") or ""),
                len(data.get("thinking", "") or ""),
            )
            raise ProviderError("ollama returned empty response")
        return text
