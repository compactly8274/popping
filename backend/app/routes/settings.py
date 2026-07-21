"""Runtime settings endpoints.

  GET  /api/settings            — current runtime overrides (all fields nullable)
  PUT  /api/settings/llm        — write provider / model_brief / model_scoring
  GET  /api/llm/tags?provider=  — Ollama-style model list, 1h TTL cache

These back the inline model picker in the Drawer. The LLM status chip
already lives in ``routes.brief`` (``GET /api/llm/status``) — keeping
the tags listing here because it's a settings-page concept, not a
runtime-status concept.

Auth: gated by ``require_user`` only when OIDC is on — same pattern
as ``routes.ingest`` and ``routes.brief``. In single-user / loopback
deploys the picker is open; that's fine because the runtime-settings
table is global and there's no per-user concept to leak between.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from app import runtime_settings
from app.auth.deps import require_user
from app.config import settings
from app.llm import tags as llm_tags
from app.schemas import (
    LLMSettingsUpdate,
    LLMTagsResponse,
    SettingsOut,
)

logger = logging.getLogger("popping.routes.settings")


_route_deps = [Depends(require_user)] if settings.oidc_enabled else []

router = APIRouter(tags=["settings"], dependencies=_route_deps)


# Whitelist of providers the UI can pick from. Matches the keys in
# ``Router.provider_for`` and the values returned by ``Provider.name``.
# ``None`` AND ``""`` both mean "reset to env-driven chain" — the UI
# uses ``""`` (the "— env default —" sentinel) and an omitted field
# deserializes to ``None``. Both clear the runtime override.
_VALID_PROVIDERS: frozenset[str | None] = frozenset(
    {"anthropic", "openai", "groq", "ollama_cloud", "ollama", None, ""}
)


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    """Return the current runtime overrides. Each field is the value in
    ``app_settings`` if present and non-empty, else None — the frontend
    shows ``None`` as "using env default".

    All knobs are read in one DB round-trip (cached separately in the
    runtime_settings module) so a stale-cache Drawer open doesn't
    serialise N SELECTs. API keys are collapsed to a bool (set/unset)
    before leaving this function — see ``SettingsOut``'s docstring."""
    values = await runtime_settings.get_many(
        [
            "llm.provider",
            "llm.model_brief",
            "llm.model_scoring",
            "llm.anthropic_api_key",
            "llm.openai_api_key",
            "llm.groq_api_key",
            "llm.ollama_cloud_api_key",
        ]
    )
    return SettingsOut(
        llm_provider=values["llm.provider"],
        llm_model_brief=values["llm.model_brief"],
        llm_model_scoring=values["llm.model_scoring"],
        anthropic_api_key_set=bool(values["llm.anthropic_api_key"]),
        openai_api_key_set=bool(values["llm.openai_api_key"]),
        groq_api_key_set=bool(values["llm.groq_api_key"]),
        ollama_cloud_api_key_set=bool(values["llm.ollama_cloud_api_key"]),
    )


@router.put("/settings/llm", response_model=SettingsOut)
async def update_llm_settings(payload: LLMSettingsUpdate) -> SettingsOut:
    """Write one or more LLM knobs. Semantics per field:

    - ``null`` (omitted): leave the DB row alone.
    - ``""`` (empty string): delete the DB row → fall back to env.
    - any other string: upsert the row with that value.

    The frontend always sends every field so the user can both *set*
    and *reset* values; missing = null = "don't touch" is the
    convention for non-UI callers (curl, scripts) that want to update
    just one knob. The four ``*_api_key`` fields use the exact same
    convention — an empty string reverts that provider to its env
    var, same as clearing the provider/model fields does.

    Invalidating the tags cache on save so the picker doesn't show a
    stale list after the user has already committed to a model name
    that's no longer in the dropdown.
    """
    if payload.provider not in _VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid provider {payload.provider!r}; "
                f"expected one of: anthropic, openai, groq, ollama_cloud, ollama, "
                f"or null to leave alone / \"\" to reset to env"
            ),
        )

    try:
        await _apply_field("llm.provider", payload.provider)
        await _apply_field("llm.model_brief", payload.model_brief)
        await _apply_field("llm.model_scoring", payload.model_scoring)
        await _apply_field("llm.anthropic_api_key", payload.anthropic_api_key)
        await _apply_field("llm.openai_api_key", payload.openai_api_key)
        await _apply_field("llm.groq_api_key", payload.groq_api_key)
        await _apply_field("llm.ollama_cloud_api_key", payload.ollama_cloud_api_key)
    except SQLAlchemyError as exc:
        logger.exception("settings: failed to persist")
        raise HTTPException(status_code=500, detail="failed to persist settings") from exc

    # Invalidate the tags cache so the next picker refresh reflects the
    # current provider. Cheaper than storing a provider→base_url
    # mapping; covers all cases since the Drawer reloads on save.
    llm_tags.invalidate_all()

    # Never log key VALUES — only which ones this call touched, so an
    # operator can audit "a key was changed" from the logs without the
    # secret itself ever landing there.
    touched_keys = [
        name
        for name, value in (
            ("anthropic", payload.anthropic_api_key),
            ("openai", payload.openai_api_key),
            ("groq", payload.groq_api_key),
            ("ollama_cloud", payload.ollama_cloud_api_key),
        )
        if value is not None
    ]
    logger.info(
        "settings: LLM updated provider=%s model_brief=%s model_scoring=%s api_keys_touched=%s",
        payload.provider, payload.model_brief, payload.model_scoring, touched_keys,
    )

    return await get_settings()


async def _apply_field(runtime_key: str, value: Optional[str]) -> None:
    """Apply a single field update with the "null/empty/non-empty"
    semantics described on ``update_llm_settings``.

    Model names are stripped of surrounding whitespace before being
    stored — common when users paste a tag like " llama3.1:8b ".
    """
    if value is None:
        return
    if value == "":
        await runtime_settings.delete(runtime_key)
        return
    await runtime_settings.set(runtime_key, value.strip())


# ---------------------------------------------------------------------------
# Tags listing
# ---------------------------------------------------------------------------


@router.get("/llm/tags", response_model=LLMTagsResponse)
async def llm_tags_endpoint(
    provider: str = Query(
        default="ollama_cloud",
        description=(
            "Which provider's model list to fetch. Only Ollama-shaped "
            "providers expose /api/tags today (ollama_cloud, ollama)."
        ),
    ),
    task: str = Query(
        default="brief",
        description=(
            "Which picker the tags are being fetched for — ``brief`` or "
            "``scoring``. Selects the curated recommendation list used "
            "to annotate the response."
        ),
    ),
    refresh: bool = Query(
        default=False,
        description="Bypass the TTL cache and force a fresh fetch.",
    ),
) -> LLMTagsResponse:
    """Return the user's available models for ``provider``, annotated
    for ``task``. Used by both the brief and scoring dropdowns in the
    Drawer."""
    try:
        result = await llm_tags.fetch_tags(provider, force_refresh=refresh, task=task)
    except llm_tags.TagsError as exc:
        logger.warning("tags: fetch failed for %s/%s: %s", provider, task, exc)
        raise HTTPException(
            status_code=503,
            detail=f"failed to fetch tags for {provider}: {exc}",
        )
    return LLMTagsResponse(**result)