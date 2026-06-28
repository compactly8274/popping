"""Fetch the list of models available on an Ollama-style provider.

The Ollama ``/api/tags`` endpoint returns the user's account-level model
list — what they can actually invoke. We use it to populate the runtime
model picker in the Drawer (instead of making the user guess tag names
in a freeform text field).

Supports two providers today:

    - ``ollama_cloud`` → ``https://ollama.com/api/tags`` with Bearer auth
    - ``ollama`` (local) → ``settings.ollama_base_url/api/tags`` (no auth)

Other providers (Anthropic / OpenAI / Groq) don't have a generic
``/api/tags``-shape listing and aren't surfaced here — the picker only
shows Ollama Cloud. Env-only knobs stay env-only.

Caching:
    Module-level dict keyed by ``(provider, base_url)``. Entries expire
    after ``settings.llm_tags_cache_ttl_seconds`` (default 1 h). On
    Ollama HTTP error we return the cached value if any (stale > nothing
    — the picker still works after a transient outage); with no cache
    we raise and the route returns a 503.

    The cache is invalidated when the user saves a new model — see
    ``routes.settings`` — so the picker refreshes immediately on next
    open rather than waiting out the TTL.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.llm.base import ProviderError

logger = logging.getLogger("popping.llm.tags")


class TagsError(RuntimeError):
    """Raised when a tags fetch fails AND there's nothing cached. Routes
    surface this as a 503 (Ollama unreachable, no fallback to show)."""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# Cache key is (provider, base_url, task) — task is "brief" or
# "scoring" and lets the brief picker and scoring picker cache
# independent lists (different recommended annotations).
_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


def invalidate(provider: str, base_url: str) -> None:
    """Drop a single cache entry. Called by the settings route on save
    so the picker reflects the user's fresh choice. Drops both tasks
    for the (provider, base_url) since the upstream list itself hasn't
    changed — only the annotation overlay — so a TTL hit would still
    be valid; we wipe for simplicity."""
    for task in ("brief", "scoring"):
        _cache.pop((provider, base_url, task), None)


def invalidate_all() -> None:
    """Wipe the whole cache. Useful from tests / after an env change."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

# Per-task curated lists of well-known Ollama Cloud tags. A model in the
# list for ``task`` gets ``recommended: True`` on its API entry; the
# frontend sorts it to the top of that task's dropdown with a ``★``
# marker.
#
# Why a hardcoded list:
#   - We can't query Ollama Cloud for "good" models — the API only
#     knows about tags on the user's account.
#   - A curated list is deterministic and auditable; new entries go in
#     a code review with a real ``/api/tags`` response to back them up.
#
# Why per-task (not a single shared set):
#   - Brief is prose summarisation; scoring is per-entry relevance
#     judgment. Different work → different recommended model sizes.
#   - Decoupling the two means the next time a new model lands we can
#     promote it to "good for scoring" without it having to be the best
#     choice for brief prose.
#
# Add an entry when:
#   - It's a model you can confirm shows up in at least three users'
#     /api/tags responses (or in Ollama Cloud's public catalog page).
#   - It's actually good at the task, not just technically usable.
#
# Don't add model versions you're not sure exist on Cloud today —
# the annotation only shows up on matching names, so an entry that
# never appears in any user's response is harmless but misleading.
_RECOMMENDED_FOR: dict[str, frozenset[str]] = {
    # Brief / prose generation — larger models, good at writing.
    "brief": frozenset(
        {
            "gpt-oss:120b",
            "gpt-oss:20b",
            "qwen3-coder:480b",
            "deepseek-v3.1:671b",
            "qwen3:480b",
            "llama3.1:70b",
            "mistral-large:latest",
            "glm-5.2:cloud",
            "deepseek-r1:671b",
        }
    ),
    # Scoring — small/fast models, good at short structured judgments.
    # Scoring runs per-entry on every ingest cycle, so latency matters.
    "scoring": frozenset(
        {
            "gpt-oss:20b",
            "qwen3:30b",
            "llama3.1:8b",
            "mistral-small:latest",
            "gemma3:27b",
            "deepseek-r1:14b",
        }
    ),
}

# Per-name display suffix shown in the dropdown after the model name.
# Currently used to flag thinking-style models whose output lives in
# ``thinking`` rather than ``response`` (see ``OllamaCloudProvider``).
# Task-agnostic — if a name is in either curated list and is a thinking
# model, the suffix shows up regardless of which picker the user opened.
# Empty/absent entries are treated as no suffix.
_RECOMMENDED_NOTES: dict[str, str] = {
    "glm-5.2:cloud": "thinking",
    "deepseek-r1:671b": "thinking",
    "deepseek-r1:14b": "thinking",
}

# Set of model names whose chain-of-thought lives in the ``thinking``
# response field rather than ``response``. Both Ollama providers use
# this to gate the thinking-field fallback: we ONLY substitute
# ``thinking`` for ``response`` when the model is on this list.
#
# Why gate it: a generic fallback (any model with empty ``response``
# and non-empty ``thinking`` gets the CoT blob) caused a real bug —
# non-reasoning models that hit context overflow or finish with an
# empty completion would have their raw reasoning dumped into the
# Brief as the "answer." That's worse than failing loudly, because
# the resulting text *looks* like output and the user has no reason
# to suspect it's the model's chain-of-thought. Restricted to the
# documented thinking-style models here, the fallback only fires for
# users who deliberately picked one of these.
_THINKING_MODELS: frozenset[str] = frozenset(
    name for name, note in _RECOMMENDED_NOTES.items() if note == "thinking"
)


def _annotate_recommended(
    models: list[dict[str, Any]], task: str
) -> list[dict[str, Any]]:
    """Stamp ``recommended`` / ``recommended_note`` on each model dict.

    In-place so we don't double the memory hit on a long tag list.
    Recommended-first sort happens here too — the dropdown reads in
    display order, so a stable, curated ordering belongs in the API
    rather than the React render.

    ``task`` picks which curated set to use. Unknown tasks fall back to
    no recommendations (still annotated with ``recommended_note`` if any
    apply). This makes the function safe to call from new code paths
    without forcing every task to declare a list up front."""
    rec_set = _RECOMMENDED_FOR.get(task, frozenset())
    for m in models:
        name = m.get("name") or ""
        is_recommended = name in rec_set
        m["recommended"] = is_recommended
        m["recommended_note"] = _RECOMMENDED_NOTES.get(name)
    # Stable sort: recommended first, then alphabetical. Python's sort
    # is stable so the in-provider alpha order from each fetcher is
    # preserved within each bucket.
    models.sort(key=lambda m: (not m.get("recommended", False), m.get("name") or ""))
    return models


def _cache_get(
    provider: str, base_url: str, task: str
) -> tuple[float, list[dict[str, Any]]] | None:
    hit = _cache.get((provider, base_url, task))
    if hit is None:
        return None
    fetched_at, payload = hit
    if (time.time() - fetched_at) > settings.llm_tags_cache_ttl_seconds:
        _cache.pop((provider, base_url, task), None)
        return None
    return hit


def _cache_set(
    provider: str, base_url: str, task: str, payload: list[dict[str, Any]]
) -> None:
    _cache[(provider, base_url, task)] = (time.time(), payload)


# ---------------------------------------------------------------------------
# Per-provider fetch
# ---------------------------------------------------------------------------


async def _fetch_ollama_cloud(task: str) -> list[dict[str, Any]]:
    """Pull the user's account-level model list from ollama.com.

    Requires ``OLLAMA_CLOUD_API_KEY``. Same auth header as the generate
    endpoint. Response shape (Ollama native schema)::

        {"models": [{"name": "...", "size": N, "modified_at": "...",
                     "details": {"family": "...", "parameter_size": "...",
                                 "quantization_level": "..."}}]}

    We surface ``name``, ``size``, and the four ``details`` fields —
    enough for the picker dropdown + a hover tooltip.

    ``task`` is passed through to the annotator so the cached payload
    carries the right per-task ``recommended`` flags.
    """
    if not settings.ollama_cloud_api_key:
        raise TagsError("ollama_cloud: OLLAMA_CLOUD_API_KEY not set")
    url = "https://ollama.com/api/tags"
    headers = {"Authorization": f"Bearer {settings.ollama_cloud_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise TagsError(f"ollama_cloud transport error: {exc}") from exc
    if resp.status_code != 200:
        # Don't echo the body — same reasoning as ``OllamaCloudProvider``.
        raise TagsError(f"ollama_cloud returned {resp.status_code}")
    data = resp.json()
    raw_models = data.get("models") or []
    out: list[dict[str, Any]] = []
    for m in raw_models:
        details = m.get("details") or {}
        out.append(
            {
                "name": m.get("name"),
                "size": m.get("size"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization_level": details.get("quantization_level"),
            }
        )
    # Annotate (curated-list flag + per-name suffix) and sort
    # recommended-first, then alphabetical. Done before caching so the
    # cached payload already carries the annotations.
    return _annotate_recommended(out, task)


async def _fetch_ollama_local(task: str) -> list[dict[str, Any]]:
    """Pull the local Ollama instance's model list. No auth — same
    response shape as the cloud variant. ``task`` flows through to the
    annotator."""
    base = settings.ollama_base_url.rstrip("/")
    url = f"{base}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise TagsError(f"ollama transport error: {exc}") from exc
    if resp.status_code != 200:
        raise TagsError(f"ollama returned {resp.status_code}")
    data = resp.json()
    raw_models = data.get("models") or []
    out: list[dict[str, Any]] = []
    for m in raw_models:
        details = m.get("details") or {}
        out.append(
            {
                "name": m.get("name"),
                "size": m.get("size"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization_level": details.get("quantization_level"),
            }
        )
    return _annotate_recommended(out, task)


_FETCHERS = {
    "ollama_cloud": (_fetch_ollama_cloud, "https://ollama.com"),
    "ollama": (_fetch_ollama_local, None),  # base_url comes from settings at call time
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_tags(
    provider: str,
    *,
    force_refresh: bool = False,
    task: str = "brief",
) -> dict[str, Any]:
    """Fetch the model list for ``provider`` annotated for ``task``.

    ``task`` selects which curated recommendation set the annotator
    uses — ``"brief"`` (default) or ``"scoring"``. The annotation is
    baked into the cached payload so a TTL hit returns the right
    ``recommended`` flags for whichever task the route asked for.

    Returns ``{"models": [...], "cached_at": <iso>, "ttl_seconds": <int>}``.
    On Ollama HTTP error, returns the stale cached value if any — the
    picker can still render. Raises ``TagsError`` only when there's no
    cache to fall back on.

    ``force_refresh=True`` bypasses the TTL but still populates the
    cache with the fresh payload. Used by the picker's "refresh" button.
    """
    if provider not in _FETCHERS:
        raise TagsError(f"provider {provider!r} doesn't expose a model list")

    fetcher, cloud_base = _FETCHERS[provider]
    base_url = cloud_base if cloud_base else settings.ollama_base_url.rstrip("/")
    cache_key = (provider, base_url, task)

    if not force_refresh:
        hit = _cache_get(provider, base_url, task)
        if hit is not None:
            fetched_at_ts, payload = hit
            return {
                "models": payload,
                "cached_at": dt.datetime.fromtimestamp(
                    fetched_at_ts, tz=dt.timezone.utc
                ),
                "ttl_seconds": settings.llm_tags_cache_ttl_seconds,
            }

    try:
        payload = await fetcher(task)
    except TagsError as exc:
        # Try to serve stale rather than fail outright.
        stale = _cache.get(cache_key)
        if stale is not None:
            logger.warning(
                "tags: live fetch failed for %s/%s (%s) — returning stale cache",
                provider, task, exc,
            )
            fetched_at_ts, payload = stale
            return {
                "models": payload,
                "cached_at": dt.datetime.fromtimestamp(
                    fetched_at_ts, tz=dt.timezone.utc
                ),
                "ttl_seconds": settings.llm_tags_cache_ttl_seconds,
                "stale": True,
            }
        raise

    _cache_set(provider, base_url, task, payload)
    return {
        "models": payload,
        "cached_at": dt.datetime.now(tz=dt.timezone.utc),
        "ttl_seconds": settings.llm_tags_cache_ttl_seconds,
    }