"""Pydantic request/response shapes."""

from __future__ import annotations

import datetime as dt
import enum
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
    # Per-source HTTP header overrides merged on top of the defaults
    # at fetch time. NULL = use defaults. Most-common use is
    # ``{"User-Agent": "<browser UA>"}`` for CDNs that block our
    # default ``Popping/0.2`` UA. Validated at the route layer.
    custom_headers: Optional[dict] = None

    # Computed property on the Source ORM model — see
    # ``app.models.Source.auto_disabled`` for the derivation. Not a
    # Pydantic ``@computed_field`` because we want it to read off the
    # ORM object via ``from_attributes=True`` rather than be
    # recomputed by the schema layer (avoids importing
    # ``app.scheduler`` into ``app.schemas``).
    auto_disabled: bool = False

    class Config:
        from_attributes = True


class SourceCreate(BaseModel):
    """Body for ``POST /api/sources``. All fields required except
    ``refresh_interval_seconds`` (defaults to 1h) and
    ``custom_headers`` (defaults to None — use the source-plugin defaults).

    The route layer validates ``name`` against ``^[a-z0-9_]{1,120}$``
    and ``url`` against an http/https URL parse — clients get a 422
    with a clear field-level error before any DB write.
    ``custom_headers`` is rejected if it contains Cookie / Authorization
    or is shaped like a non-object — see ``_validate_custom_headers``
    in ``routes.sources``. This is the escape hatch for CDNs that
    block our default ``Popping/0.2`` User-Agent (CBC, etc.).
    """
    name: str
    type: str = "rss"
    category: str
    url: str
    refresh_interval_seconds: int = 3600
    # Per-source HTTP header overrides merged on top of the defaults
    # at fetch time. NULL = use defaults. Most-common use is
    # ``{"User-Agent": "<browser UA>"}`` for CDNs that block our
    # default UA. Validated at the route layer — without this
    # field, the route's ``body.custom_headers`` lookup raises
    # ``AttributeError`` and the request 500s before reaching any
    # client-visible error message.
    custom_headers: Optional[dict] = None


class SourceUpdate(BaseModel):
    """Body for ``PATCH /api/sources/{id}``. All fields optional —
    missing fields are left untouched.

    ``name`` and ``url`` are now editable for dynamic sources. The
    route layer validates ``name`` against ``^[a-z0-9_]{1,120}$``
    (same regex as POST), rejects built-in sources, and surfaces a
    409 on a name collision. URL changes clear the cached favicon
    and let the next ingest re-download.

    Built-in sources still reject PATCH on name/url: the row's
    name is the registry key, and a built-in's URL is bound to its
    plugin class. The route layer enforces this with the same
    `row.name in registered_plugin_names()` check DELETE uses.
    """
    refresh_interval_seconds: Optional[int] = None
    active: Optional[bool] = None
    category: Optional[str] = None
    name: Optional[str] = None
    url: Optional[str] = None
    # Set to ``{}`` (or empty dict) to clear an existing override.
    # ``None`` leaves the column untouched (the PATCH endpoint
    # treats missing fields as no-ops).
    custom_headers: Optional[dict] = None


class FeedRecommendation(BaseModel):
    """One row of ``GET /api/feed-recommendations``. The frontend
    renders the ``name`` + ``blurb`` and shows the ``url`` only on
    demand (e.g. long-press / copy-link).

    ``default_headers`` is an optional map of HTTP header overrides
    pre-applied when the user taps Add on the recommendation. Today
    only CBC needs it — the CBC CDN hangs requests with our default
    ``Popping/0.2`` User-Agent, so the recommendation ships a
    browser-shaped UA that the frontend passes through to
    ``POST /api/sources`` as ``custom_headers``. Other entries
    leave it null and the user adds the source with the default
    UA; the route layer accepts both.

    ``type`` is the ``Source.type`` to pass to ``POST /api/sources``
    when the user taps Add. Defaults to ``"rss"`` for forward
    compat with the editor rows that predate the Reddit rollout;
    Reddit recommendations set this to ``"reddit"`` so the Add
    handler picks the right source-shape validator on the backend.
    """
    name: str
    category: str
    url: str
    blurb: str
    type: str = "rss"
    default_headers: Optional[dict] = None


class EntrySummaryOut(BaseModel):
    """Body of ``POST /api/entries/{id}/summary``.

    ``summary`` is the cleaned text (HTML stripped, length-capped)
    or ``None`` when the feed shipped nothing usable. ``cached`` is
    True when the row already had ``cached_summary`` populated and
    the route returned it without re-extracting — useful for
    debugging but not load-bearing for the UI."""
    summary: Optional[str] = None
    cached: bool = False


class EntryOut(BaseModel):
    id: int
    source_id: int
    title: str
    url: str
    published_at: Optional[dt.datetime]
    # When the row landed in our DB. Distinct from ``published_at``
    # (when the source article was published) — Wikipedia OTD entries
    # carry a very old ``published_at`` but a fresh ``fetched_at``.
    # The frontend uses this to compute "new since last visit" /
    # read/unread state. Optional for forward-compat with rows written
    # before the column existed; in practice every row has a value.
    fetched_at: Optional[dt.datetime] = None
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


class EntryListOut(BaseModel):
    """Slim subset of ``EntryOut`` for the list endpoints.

    The list payload is the dominant bytes-on-wire shape — the
    dashboard polls ``/api/entries`` every 60s and renders up to
    200 rows. Including the full ``meta`` JSONB blob (~500B/row)
    and the ``embedding`` vector (not modelled here, but in the row)
    was ~100 KB of unused JSON per poll.

    ``meta`` is fetched on demand by ``POST /api/entries/{id}/summary``
    when the user expands a card. ``embedding`` is server-internal
    and never returned by any list endpoint.

    The full ``EntryOut`` is still used for endpoints that need the
    meta — the per-card summary endpoint, etc.

    ``reddit_thread_url`` + ``reddit_comment_count`` are pulled out of
    ``Entry.meta`` at projection time (``routes/entries.py``) so the
    card footer can read them as top-level fields. Cheaper than
    shipping the whole ``meta`` blob — these two keys are tiny
    (~80 bytes) and the rest of ``meta`` (engagement, source-natural
    keys, etc.) is unused by the list render.
    """
    id: int
    source_id: int
    title: str
    url: str
    published_at: Optional[dt.datetime]
    fetched_at: Optional[dt.datetime] = None
    composite_score: float
    personal_score: float
    raw_score: float
    # Cached summary text — small, denormalised, and the only meta
    # fragment the dashboard ever needs at list time. Saves a round-
    # trip to /api/entries/{id}/summary on cards that have been
    # expanded before.
    cached_summary: Optional[str] = None
    image_url: Optional[str] = None
    image_path: Optional[str] = None
    # Reddit cross-reference footer. Both null = no cross-ref found
    # or feature disabled. The card renders a "Discussed on Reddit"
    # link when ``reddit_thread_url`` is non-null. ``null`` (not
    # missing) so the frontend's TypeScript discriminated-union
    # narrowing on ``Entry.reddit_thread_url !== null`` works without
    # optional-chaining on every render.
    reddit_thread_url: Optional[str] = None
    reddit_comment_count: Optional[int] = None

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
    user doesn't act on them).

    ``recommended`` and ``recommended_note`` are populated by the tags
    endpoint based on a curated list of well-known Ollama Cloud models
    (``app.llm.tags._RECOMMENDED``). Both default to safe values so
    older cached payloads without the keys still parse cleanly."""
    name: str
    size: Optional[int] = None
    family: Optional[str] = None
    parameter_size: Optional[str] = None
    quantization_level: Optional[str] = None
    recommended: bool = False
    recommended_note: Optional[str] = None


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


class InteractionType(str, enum.Enum):
    """The set of engagement events a client can POST. ``view`` /
    ``click`` are the common ones; ``thumb_up`` / ``thumb_down``
    record explicit preference; ``never`` is a "I never want to see
    this again" signal (used by the recommendation co-occurrence
    ranker to subtract category scores); ``dwell`` measures
    how long the user spent reading an entry; ``bookmark`` /
    ``share`` are high-value positive signals.

    The string values match the canonical names the recommendations
    SQL aggregates (``thumb_down``, ``never`` subtract 1 from the
    per-category count). Adding a new enum value here requires
    updating the ranker in ``app.feed_recommendations`` if the
    event should count toward preferences.
    """
    view = "view"
    click = "click"
    dwell = "dwell"
    thumb_up = "thumb_up"
    thumb_down = "thumb_down"
    bookmark = "bookmark"
    share = "share"
    never = "never"


class InteractionIn(BaseModel):
    """Body for ``POST /api/interactions``. Records one event.

    ``value`` defaults to 1.0 and accepts negative floats so
    ``thumb_down`` / ``never`` can record a -1.0 contribution to
    the recommendation ranker. ``entry_id`` is validated against
    the entries table inside the route handler — pydantic only
    enforces the type, not the referential integrity (the FK is
    on the column; a missing entry would surface as a 422 from
    SQLAlchemy, which we convert to a 404).
    """
    entry_id: int
    type: InteractionType
    value: float = 1.0


class InteractionBatchIn(BaseModel):
    """Body for ``POST /api/interactions/batch``. Up to 50 events at
    once. The frontend batches view events (one per visible card on
    a dashboard render) into a single request, flushing on
    ``requestIdleCallback`` or ``visibilitychange: hidden`` via
    ``navigator.sendBeacon``. Click events go through the single-
    shot endpoint because they're sparse and want immediate
    feedback.
    """
    events: list[InteractionIn]