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
        # Pick the provider AND its model name together. Earlier
        # versions picked the model from a single chain regardless of
        # which provider actually wins — so when no API keys were set
        # and Ollama was the fallback, we'd ask Ollama for a model
        # named "claude-sonnet-4-6" (or similar), which doesn't exist
        # locally and returned 404 from /api/generate.
        if settings.anthropic_api_key:
            from app.llm.anthropic import AnthropicProvider

            model = settings.claude_model_brief if task == "brief" else settings.claude_model_scoring
            if not model:
                return None
            return AnthropicProvider(model, settings.anthropic_api_key)
        if settings.openai_api_key:
            model = settings.openai_model_brief if task == "brief" else settings.openai_model_scoring
            if not model:
                return None
            return OpenAIProvider(model, settings.openai_api_key)
        if settings.groq_api_key:
            model = settings.groq_model_brief if task == "brief" else settings.groq_model_scoring
            if not model:
                return None
            return GroqProvider(model, settings.groq_api_key)
        # No cloud key — fall through to Ollama. Use the Ollama model
        # name explicitly; the cloud-model chain doesn't apply here.
        model = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
        if not model:
            return None
        return OllamaProvider(model)


router = Router()


__all__ = ["Provider", "ProviderError", "Router", "router"]
