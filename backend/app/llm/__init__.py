"""LLM provider abstraction.

Phase 2 ships the plumbing only — no call site uses it yet. Phase 4's
Brief generator will import ``Router.provider_for(task)`` and call
``.complete(prompt)``.

Selection order for ``provider_for(task)``:

    1. Anthropic    (if ``ANTHROPIC_API_KEY`` is set)
    2. OpenAI       (if ``OPENAI_API_KEY`` is set)
    3. Groq         (if ``GROQ_API_KEY`` is set)
    4. Ollama Cloud (if ``OLLAMA_CLOUD_API_KEY`` is set)
    5. Ollama local (always available if the host runs Ollama)

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
from app.llm.ollama_cloud import OllamaCloudProvider
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

    def status(self, task: str = "brief") -> dict:
        """Human-readable state for the Drawer chip. Doesn't leak secrets.

        Cheap: no DB / network — just inspects settings + constructs the
        chosen provider (which only stores fields at __init__)."""
        provider = self.provider_for(task)
        if provider is None:
            return {"configured": False, "backend": None, "model": None}
        # ``provider.name`` is the class attribute (anthropic / openai /
        # groq / ollama_cloud / ollama). The model isn't exposed on the
        # provider's public API, so we look it up from settings here.
        model = self._model_for(task, provider.name)
        return {"configured": True, "backend": provider.name, "model": model}

    @staticmethod
    def _model_for(task: str, backend: str) -> str | None:
        """Which settings field is the model name for this backend + task.

        Kept in sync with ``provider_for`` — when adding a new provider
        add a branch here too. Returns None if the configured value is
        empty (caller decides what to display).
        """
        if backend == "anthropic":
            v = settings.claude_model_brief if task == "brief" else settings.claude_model_scoring
            return v or None
        if backend == "openai":
            v = settings.openai_model_brief if task == "brief" else settings.openai_model_scoring
            return v or None
        if backend == "groq":
            v = settings.groq_model_brief if task == "brief" else settings.groq_model_scoring
            return v or None
        if backend == "ollama_cloud":
            cloud_default = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
            explicit = settings.ollama_cloud_model_brief if task == "brief" else settings.ollama_cloud_model_scoring
            v = explicit or cloud_default
            return v or None
        if backend == "ollama":
            v = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
            return v or None
        return None

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
        if settings.ollama_cloud_api_key:
            # Cloud model defaults to the local Ollama model when unset —
            # the same tags (llama3.1, gpt-oss:120b, …) are typically
            # available on both. Explicit overrides for billing / quality.
            cloud_default = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
            model = (
                settings.ollama_cloud_model_brief if task == "brief" else settings.ollama_cloud_model_scoring
            ) or cloud_default
            if not model:
                return None
            return OllamaCloudProvider(model, settings.ollama_cloud_api_key)
        # No cloud key — fall through to local Ollama. Use the Ollama
        # model name explicitly; the cloud-model chain doesn't apply here.
        model = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
        if not model:
            return None
        return OllamaProvider(model)


router = Router()


__all__ = ["Provider", "ProviderError", "Router", "router"]
