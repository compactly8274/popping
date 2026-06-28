"""Pydantic request/response shapes."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel


class SourceOut(BaseModel):
    id: int
    name: str
    type: str
    category: str
    url: str
    refresh_interval_seconds: int
    last_fetch_at: Optional[dt.datetime]
    last_error: Optional[str]
    error_count: int
    active: bool
    # Remote URL of the source's favicon. NULL until first ingest
    # downloads it (typically origin's /favicon.ico).
    favicon_url: Optional[str] = None
    # Local path under /assets, e.g. "favicons/3.png".
    favicon_path: Optional[str] = None

    class Config:
        from_attributes = True


class EntryOut(BaseModel):
    id: int
    source_id: int
    title: str
    url: str
    published_at: Optional[dt.datetime]
    composite_score: float
    personal_score: float
    raw_score: float
    meta: Optional[dict]
    # Remote URL of the entry's thumbnail (parsed from the feed).
    image_url: Optional[str] = None
    # Local path under /assets, e.g. "thumbnails/1234.jpg".
    image_path: Optional[str] = None

    class Config:
        from_attributes = True


class IngestResult(BaseModel):
    source: str
    fetched: int
    inserted: int
    duplicates: int
    error: Optional[str] = None


class BriefOut(BaseModel):
    id: int
    generated_at: dt.datetime
    tone: str
    content: str
    delivered_at: Optional[dt.datetime] = None
    meta: Optional[dict] = None

    class Config:
        from_attributes = True


class NotificationStatus(BaseModel):
    """What the Drawer chip reads. Doesn't leak secrets."""
    configured: bool
    backend: Optional[str] = None
    scheme: Optional[str] = None


class LLMStatus(BaseModel):
    """Same shape as NotificationStatus, for the LLM. Used by the
    Drawer chip + the Brief endpoint to surface provider / model."""
    configured: bool
    backend: Optional[str] = None    # anthropic / openai / groq / ollama_cloud / ollama
    model: Optional[str] = None


class LLMTag(BaseModel):
    """One model entry in the Ollama-style ``/api/tags`` response.
    Slimmed-down subset of Ollama's full record — we don't expose
    ``digest`` or ``modified_at`` to the picker (they're long and the
    user doesn't act on them)."""
    name: str
    size: Optional[int] = None
    family: Optional[str] = None
    parameter_size: Optional[str] = None
    quantization_level: Optional[str] = None


class LLMTagsResponse(BaseModel):
    """Response from ``GET /api/llm/tags``. ``stale`` is set when the
    live fetch failed and we served a previously-cached value — the
    picker shows a banner so the user knows the list may be outdated."""
    models: list[LLMTag]
    cached_at: Optional[dt.datetime] = None
    ttl_seconds: int
    stale: Optional[bool] = None


class SettingsOut(BaseModel):
    """What ``GET /api/settings`` returns. All fields nullable — first
    boot with no env seeds has everything blank."""
    llm_provider: Optional[str] = None       # runtime_settings value of "llm.provider"
    llm_model_brief: Optional[str] = None   # runtime_settings value of "llm.model_brief"
    llm_model_scoring: Optional[str] = None  # runtime_settings value of "llm.model_scoring"


class LLMSettingsUpdate(BaseModel):
    """Body for ``PUT /api/settings/llm``. Each field is optional —
    missing fields are left untouched (PUT but partial update)."""
    provider: Optional[str] = None           # ollama_cloud / ollama / anthropic / openai / groq / None
    model_brief: Optional[str] = None
    model_scoring: Optional[str] = None


class HealthOut(BaseModel):
    status: str
    sources: int
    entries: int
    db: str
    redis: str
    last_fetch: Optional[dt.datetime] = None