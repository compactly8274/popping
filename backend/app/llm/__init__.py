"""LLM provider abstraction.

Phase 2 ships the plumbing only — no call site uses it yet. Phase 4's
Brief generator will import ``Router.provider_for(task)`` and call
``.complete(prompt)``.

Selection order for ``provider_for(task)``:

    1. Anthropic (if ``ANTHROPIC_API_KEY`` is set)
    2. OpenAI    (if ``OPENAI_API_KEY`` is set)
    3. Groq      (if ``GROQ_API_KEY`` is set)
    4. Ollama    (always available locally if the host runs Ollama)

If none are configured, ``provider_for`` returns ``None``. Callers
must handle absence — the Brief generator logs and skips; nothing
crashes.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.llm.base import Provider, ProviderError
from app.llm.groq import GroqProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai import OpenAIProvider

# Anthropic provider is imported lazily inside the factory so the
# ``anthropic`` package isn't required at install time.
logger = logging.getLogger("popping.llm")


class Router:
    """Picks a provider for a given task ('scoring' or 'brief').

    The selection is order-dependent (see module docstring). Each task
    has its own configured model name (``*_model_scoring`` /
    ``*_model_brief``).
    """

    def provider_for(self, task: str) -> Provider | None:
        model = self._model_for(task)
        if not model:
            return None
        if settings.anthropic_api_key:
            from app.llm.anthropic import AnthropicProvider

            return AnthropicProvider(model, settings.anthropic_api_key)
        if settings.openai_api_key:
            return OpenAIProvider(model, settings.openai_api_key)
        if settings.groq_api_key:
            return GroqProvider(model, settings.groq_api_key)
        return OllamaProvider(model)

    @staticmethod
    def _model_for(task: str) -> str | None:
        if task == "scoring":
            return (
                settings.claude_model_scoring
                or settings.openai_model_scoring
                or settings.groq_model_scoring
                or settings.ollama_model_scoring
            )
        if task == "brief":
            return (
                settings.claude_model_brief
                or settings.openai_model_brief
                or settings.groq_model_brief
                or settings.ollama_model_brief
            )
        return None


router = Router()


__all__ = ["Provider", "ProviderError", "Router", "router"]
