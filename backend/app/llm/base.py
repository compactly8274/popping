"""Provider ABC for LLM backends.

A Provider wraps one remote LLM endpoint behind a single ``complete``
method. The router (``app.llm.__init__``) picks a provider per task;
callers don't have to know which one is configured.

Phase 2 doesn't use this — phase 4's Brief generator will. The plumbing
is here so phase 4 doesn't have to touch the dependency graph.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """One LLM backend. Stateless w.r.t. the caller — each call is a
    single ``complete`` and returns the generated text."""

    name: str

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        stop: list[str] | None = None,
        think: bool = True,
    ) -> str:
        """Generate a completion for ``prompt``. Implementations should
        return the assistant's text (no chat preamble).

        ``stop`` is an optional list of strings; if the model produces
        any of them, generation halts. Useful for capping the response
        to a known structure (e.g. a brief that should never contain
        markdown headers or numbered analysis).

        ``think`` toggles extended-thinking / chain-of-thought mode on
        providers that support it (currently only ``ollama`` and
        ``ollama_cloud`` — see ``app.llm.tags._THINKING_MODELS``).
        Default ``True`` preserves existing behavior for callers like
        the Brief generator, which wants the model's full reasoning
        budget. Callers with a small ``max_tokens`` budget and a short,
        direct-answer prompt (article/podcast/Reddit-comment
        summarizers) should pass ``think=False`` — a thinking model
        given a tight token cap can burn its entire budget on the CoT
        preamble and never reach the actual answer, at which point the
        provider's "response empty, fall back to the ``thinking``
        field" behavior surfaces the raw (and often truncated)
        reasoning trace as if it were the answer. Providers that don't
        support a thinking mode (Anthropic, OpenAI, Groq) accept and
        ignore this parameter."""
        raise NotImplementedError


class ProviderError(RuntimeError):
    """Raised when a provider can't fulfill a request. Callers decide
    whether to retry, fall through to another provider, or surface
    the failure."""
