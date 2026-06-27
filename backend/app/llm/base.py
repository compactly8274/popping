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
    async def complete(self, prompt: str, *, max_tokens: int = 512) -> str:
        """Generate a completion for ``prompt``. Implementations should
        return the assistant's text (no chat preamble)."""
        raise NotImplementedError


class ProviderError(RuntimeError):
    """Raised when a provider can't fulfill a request. Callers decide
    whether to retry, fall through to another provider, or surface
    the failure."""
