"""Groq provider: OpenAI-compatible chat completions at Groq's URL."""

from __future__ import annotations

from typing import Any

from app.llm.openai import OpenAIProvider

# gpt-oss models reason before answering, and Groq counts those hidden
# reasoning tokens against max_tokens. With the callers' budgets (600
# for a Brief, 120 for scoring) the reasoning used them all up and the
# reply came back 200 with empty content. Low effort keeps the
# reasoning short; the headroom covers it on top of the caller's budget.
_REASONING_HEADROOM = 1024


class GroqProvider(OpenAIProvider):
    name = "groq"

    def _default_base_url(self) -> str:
        return "https://api.groq.com/openai/v1"

    def _payload_overrides(self, max_tokens: int) -> dict[str, Any]:
        if "gpt-oss" not in self._model:
            return {}
        return {
            "max_tokens": max_tokens + _REASONING_HEADROOM,
            "reasoning_effort": "low",
            "include_reasoning": False,
        }
