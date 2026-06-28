"""LLM provider abstraction.

Phase 2 ships the plumbing only — no call site uses it yet. Phase 4's
Brief generator imports ``Router.provider_for(task)`` and calls
``.complete(prompt)``.

Selection order for ``provider_for(task)``:

    1. If ``llm.provider`` is set in runtime_settings (via the UI picker),
       pin to that provider — bypass the env chain entirely.
    2. Otherwise walk the env chain:
       - Anthropic    (if ``ANTHROPIC_API_KEY`` is set)
       - OpenAI       (if ``OPENAI_API_KEY`` is set)
       - Groq         (if ``GROQ_API_KEY`` is set)
       - Ollama Cloud (if ``OLLAMA_CLOUD_API_KEY`` is set)
       - Ollama local (always available if the host runs Ollama)

If none are configured, ``provider_for`` returns ``None``. Callers
must handle absence — the Brief generator logs and skips; nothing
crashes.

The ``provider_for`` method is sync because BriefGenerator and its
route handlers are sync on the hot path. Model selection reads through
``runtime_settings.snapshot_sync()`` which returns the in-process
cache (warm) or env values (cold); no DB I/O on this path.
"""

from __future__ import annotations

import logging

from app import runtime_settings
from app.config import settings
from app.llm.base import Provider, ProviderError
from app.llm.groq import GroqProvider
from app.llm.ollama import OllamaProvider
from app.llm.ollama_cloud import OllamaCloudProvider
from app.llm.openai import OpenAIProvider

# Anthropic is imported lazily inside ``_construct`` so the ``anthropic``
# package isn't required at install time. Other providers live at module
# scope because they're tiny (httpx only) and don't add much weight.
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
    def _env_model_for(task: str, backend: str) -> str | None:
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
            # Cloud model defaults to the local Ollama model when unset —
            # the same tags (llama3.1, gpt-oss:120b, …) are typically
            # available on both. Explicit overrides for billing / quality.
            cloud_default = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
            explicit = settings.ollama_cloud_model_brief if task == "brief" else settings.ollama_cloud_model_scoring
            v = explicit or cloud_default
            return v or None
        if backend == "ollama":
            v = settings.ollama_model_brief if task == "brief" else settings.ollama_model_scoring
            return v or None
        return None

    @staticmethod
    def _model_for(task: str, backend: str) -> str | None:
        """Resolve the model name for ``backend`` + ``task``.

        Read precedence:
            1. ``llm.model_<task>`` in runtime_settings (UI override).
            2. ``settings.*`` field via ``_env_model_for`` (env default).
            3. ``None`` (caller decides — usually means "use whatever
               the Provider's __init__ saw", but Provider requires an
               explicit model so the None propagates up and the caller
               should treat it as "no provider configured").

        Sync: relies on ``runtime_settings.snapshot_sync()`` for the
        first lookup. See runtime_settings docstring.
        """
        runtime_key = f"llm.model_{task}"
        runtime_val = runtime_settings.snapshot_sync().get(runtime_key)
        if runtime_val:
            return runtime_val
        return Router._env_model_for(task, backend)

    @staticmethod
    def _construct(backend: str, model: str) -> Provider | None:
        """Build a Provider instance for ``backend`` + ``model``.

        Returns None when the chosen backend has no usable auth (e.g.
        user pinned "anthropic" but ANTHROPIC_API_KEY is unset) — we
        don't 500, we just report "not configured" and let the caller
        surface that to the UI.
        """
        if backend == "anthropic":
            if not settings.anthropic_api_key:
                return None
            from app.llm.anthropic import AnthropicProvider

            return AnthropicProvider(model, settings.anthropic_api_key)
        if backend == "openai":
            if not settings.openai_api_key:
                return None
            return OpenAIProvider(model, settings.openai_api_key)
        if backend == "groq":
            if not settings.groq_api_key:
                return None
            return GroqProvider(model, settings.groq_api_key)
        if backend == "ollama_cloud":
            if not settings.ollama_cloud_api_key:
                return None
            return OllamaCloudProvider(model, settings.ollama_cloud_api_key)
        if backend == "ollama":
            return OllamaProvider(model)
        return None

    def provider_for(self, task: str) -> Provider | None:
        """Pick the provider AND its model name together.

        Earlier versions picked the model from a single chain regardless
        of which provider actually won — so when no API keys were set
        and Ollama was the fallback, we'd ask Ollama for a model named
        "claude-sonnet-4-6" (or similar), which doesn't exist locally
        and returned 404 from /api/generate.

        Now: first try the user-pinned provider (runtime_settings),
        then fall through to the env chain. Model name is resolved for
        the chosen backend only.
        """
        snap = runtime_settings.snapshot_sync()

        # ---- Path 1: user-pinned provider (UI override) ----------------
        # The UI picker lets the user pin a specific provider regardless
        # of which env keys are set. _construct returns None if the
        # chosen backend has no usable auth — we surface that as "not
        # configured" rather than 500-ing.
        pinned = snap.get("llm.provider")
        if pinned:
            model = self._model_for(task, pinned)
            if not model:
                return None
            return self._construct(pinned, model)

        # ---- Path 2: env-driven chain -----------------------------------
        # First API key set wins. ``_construct`` short-circuits to None
        # when the chosen backend has no auth, so the chain naturally
        # falls through to the next candidate.
        for backend in ("anthropic", "openai", "groq", "ollama_cloud"):
            model = self._model_for(task, backend)
            if not model:
                continue
            provider = self._construct(backend, model)
            if provider is not None:
                return provider
        # Local Ollama is the unconditional fallback — no API key needed.
        model = self._model_for(task, "ollama")
        if not model:
            return None
        return OllamaProvider(model)


router = Router()


__all__ = ["Provider", "ProviderError", "Router", "router"]