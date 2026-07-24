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

    # Net thumbs score across every entry from this source:
    # sum(thumb_up) - sum(thumb_down). NOT an ORM attribute — there's
    # no column or relationship on ``Source`` for this, it's a SQL
    # aggregate the route computes separately (see
    # ``routes.sources._net_vote_scores``) and assigns onto this
    # field after ``model_validate`` runs. Defaults to 0 (never
    # voted on) rather than None so the frontend can render it
    # directly without a null check.
    net_vote_score: int = 0

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


class SourceTestRequest(BaseModel):
    """Body for ``POST /api/sources/test``. Same shape as
    ``SourceCreate`` but ``name`` is optional and the endpoint
    never persists — it just dispatches the fetch to the same
    plugin the live source would use and returns the result.

    The frontend uses this for the Add-Custom "Test" button: the
    user types a URL, hits Test, and gets a friendly
    "Looks good (42 items)" or "Site blocks automated access"
    before committing the row. Without this the user would have
    to add, refresh, look at the column, and delete on failure —
    3 clicks and 30+ seconds for the common case.
    """
    name: Optional[str] = None
    type: str = "rss"
    category: str = "news"
    url: str
    custom_headers: Optional[dict] = None


class SourceTestResult(BaseModel):
    """Body of ``POST /api/sources/test``.

    ``ok`` is the only field the UI branches on. ``status_code``
    is the upstream HTTP status (or None for parse / network
    failures). ``item_count`` is the number of items the plugin
    successfully extracted. ``sample_titles`` is a small list of
    the first 3 titles — useful so the user can tell the
    response was the right feed (e.g. a Hacker News mirror
    looks identical to the real one in the URL field).

    ``error_kind`` is a short enum string the frontend uses to
    pick a friendly message. ``error`` is the underlying
    technical message (httpx exception, parser complaint, etc.)
    — included in the response so power users can debug without
    re-running the test against curl.

    ``error_kind`` enum:
      - ``not_found``        upstream returned 404 / 410
      - ``forbidden``        upstream returned 401 / 403
      - ``timeout``          request took longer than the budget
      - ``parse_error``      response was received but didn't look
                             like a feed
      - ``name_conflict``    ``name`` was provided and collides
                             with an existing source or built-in
      - ``invalid_url``      URL failed the http/https / subreddit
                             validator
      - ``unsupported_type`` ``type`` is not in the accepted set
      - ``network_error``    connection refused, DNS, TLS, etc.
      - ``unknown``          catch-all

    ``resolved_url`` is set when the submitted URL was an Apple
    Podcasts show page (``podcasts.apple.com/.../id<N>``) that got
    resolved to its actual RSS feed via ``app.apple_podcasts`` —
    null for every other URL, including one that was already a
    direct feed URL. The frontend shows this so the user sees what
    actually got fetched/would be added, rather than the resolution
    happening invisibly.
    """
    ok: bool
    status_code: Optional[int] = None
    item_count: Optional[int] = None
    sample_titles: list[str] = []
    error_kind: Optional[str] = None
    error: Optional[str] = None
    resolved_url: Optional[str] = None


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
    # "editorial" (hand-picked) or "llm" (found by app.feed_discovery).
    # Lets the Recommended tab label discovered rows distinctly.
    source: str = "editorial"


class FeedDiscoverRequest(BaseModel):
    """Body for ``POST /api/feed-recommendations/discover``.

    ``category`` is optional — omit it to let the backend infer one
    from the user's top-scoring category (``app.feed_recommendations.
    top_category_for_user``), falling back to a fixed default when
    there's no engagement history yet (cold start).
    """
    category: Optional[str] = None


class FeedDiscoverResult(BaseModel):
    """Body of ``POST /api/feed-recommendations/discover``.

    ``category`` is the category actually searched (echoes the
    request's ``category``, or reports the inferred one). ``added``
    is how many new candidates were validated and persisted —
    frequently 0, which is a normal outcome, not an error. ``note``
    is None when ``added`` is 0 for the unremarkable reason ("the LLM
    ran and genuinely had nothing new to suggest") and a short,
    specific reason otherwise — no provider configured, a provider's
    actual error, or "N suggestions, none passed validation". Without
    this, every ``added=0`` looks identical to the user, and a real
    config gap (no API key, wrong model, unreachable local Ollama)
    reads as a silently broken button instead of something they can
    fix in Settings.
    """
    category: str
    added: int
    note: Optional[str] = None


class EntrySummaryOut(BaseModel):
    """Body of ``POST /api/entries/{id}/summary``.

    ``summary`` is the cleaned text (HTML stripped, length-capped)
    or ``None`` when the feed shipped nothing usable. ``cached`` is
    True when the row already had ``cached_summary`` populated and
    the route returned it without re-extracting — useful for
    debugging but not load-bearing for the UI."""
    summary: Optional[str] = None
    cached: bool = False


class EntryPodcastSummaryOut(BaseModel):
    """Body of ``POST /api/entries/{id}/podcast_summary``.

    ``available`` is False when the entry has no
    ``meta.transcript_url`` at all — the podcast doesn't publish a
    Podcasting-2.0 transcript, so there's nothing to summarize. This
    is distinct from ``summary=""``, which means a transcript existed
    but the fetch or the LLM call failed (or no LLM provider is
    configured); that failure is still cached (see
    ``Entry.podcast_transcript_summary``) so a retry isn't
    attempted on every tap, but ``available`` staying True tells the
    frontend the feature is applicable here, just unsuccessful this
    time.
    """
    summary: Optional[str] = None
    cached: bool = False
    available: bool = True


class EntryRedditCommentSummaryOut(BaseModel):
    """Body of ``POST /api/entries/{id}/reddit_comment_summary``.

    ``available`` is False when the entry has no
    ``meta.reddit_thread_url`` at all — there's no Reddit discussion
    to summarize. ``rate_limited`` is True for the one failure mode
    that's worth telling the user apart from "no summary" — Reddit's
    direct-mode fetch allows only ~1 request/75s (see
    ``app.reddit_client``), and an on-demand tap can lose that race
    against the scheduled ingest jobs. It's never cached (a retry a
    minute later can succeed), unlike every other ``summary=""``
    case, which does persist so a tap doesn't re-attempt a fetch or
    LLM call that already failed for a real reason.
    """
    summary: Optional[str] = None
    cached: bool = False
    available: bool = True
    rate_limited: bool = False


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
    # Joined source name. Populated by endpoints
    # that JOIN sources for display (e.g. the
    # by-ids endpoint used by the Settings
    # overlay's Hidden and Starred tabs). The
    # list endpoint deliberately omits it —
    # the dashboard already has a source name
    # map (sourcesById) so the JOIN would be
    # redundant bytes on the wire for the
    # common 200-row list poll.
    source_name: Optional[str] = None

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
    # Podcast episode audio, same pull-out-of-meta pattern as the
    # reddit fields above. Both null for every non-podcast entry;
    # the card only renders the "Listen" affordance when audio_url
    # is non-null.
    audio_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    # Podcasting 2.0 transcript URL, when the feed publishes one.
    # Drives the "Summarize episode" affordance — see
    # POST /entries/{id}/podcast_summary.
    transcript_url: Optional[str] = None
    # Framing Watch cluster membership (app.framing). Non-null only
    # for entries the hourly clustering job has grouped with 2+
    # other outlets' coverage of the same story. A real column, not
    # pulled from meta — the card only renders the "Related
    # coverage" affordance when this is non-null, same on/off
    # pattern as reddit_thread_url / transcript_url above. The
    # sibling articles themselves are fetched on demand via
    # GET /entries/{id}/related, not shipped in the list payload.
    story_cluster_id: Optional[int] = None

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
    boot with no env seeds has everything blank.

    The ``*_api_key_set`` fields are booleans, never the key value —
    this endpoint must never echo a secret back to the browser. True
    means a key is currently active for that backend, from EITHER a
    Settings-UI override or the env var; the caller can't tell which
    from this alone (matching how the model/provider fields already
    don't distinguish DB-override from env-default)."""
    llm_provider: Optional[str] = None       # runtime_settings value of "llm.provider"
    llm_model_brief: Optional[str] = None   # runtime_settings value of "llm.model_brief"
    llm_model_scoring: Optional[str] = None  # runtime_settings value of "llm.model_scoring"
    anthropic_api_key_set: bool = False
    openai_api_key_set: bool = False
    groq_api_key_set: bool = False
    ollama_cloud_api_key_set: bool = False


class LLMSettingsUpdate(BaseModel):
    """Body for ``PUT /api/settings/llm``. Each field is optional —
    missing fields are left untouched (PUT but partial update).

    The ``*_api_key`` fields follow the same null/empty/value
    convention every other field here does: omit (null) to leave
    alone, ``""`` to clear the override and fall back to the env var,
    any other string to set/replace it. Never returned by GET — write-
    only from the client's perspective, same as a password field."""
    provider: Optional[str] = None           # ollama_cloud / ollama / anthropic / openai / groq / None
    model_brief: Optional[str] = None
    model_scoring: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    ollama_cloud_api_key: Optional[str] = None


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


class InteractionOut(BaseModel):
    """Single engagement event with joined entry + source metadata.

    Returned by ``GET /api/interactions/recent`` for the Drawer's
    History view. The History view needs the entry title + URL
    + source name + the interaction's type (view / never /
    bookmark) + value (signed: +1 for positive, -1 for negative)
    + created_at (when the user did the thing).

    The frontend renders a colored chip for the type:
      view     = "Read"   (green / accent)
      never    = "Hidden" (red)
      bookmark = "Saved"  (yellow / star)
      click    = "Opened" (gray)
      dwell    = ""       (not surfaced in History)
    plus the entry title (clickable, opens the URL) and a
    source badge.

    The value field is signed so the frontend can show the
    polarity (e.g. "Read" entries with a positive value get
    a green dot, "Hidden" entries with a negative value get
    a red dot). We also accept a string type for the
    interaction — a future model could record richer types
    (e.g. "share", "dwell") without breaking the schema.
    """

    id: int
    type: str
    value: float
    created_at: dt.datetime
    entry_id: int
    entry_title: str
    entry_url: str
    entry_published_at: Optional[dt.datetime] = None
    source_id: int
    source_name: str

    class Config:
        from_attributes = True


class InteractionListOut(BaseModel):
    """Response shape for ``GET /api/interactions/recent``.

    Returns a list of ``InteractionOut`` plus the total count
    of interactions matching the filter (so the frontend can
    show "Showing 50 of 1,234" without an extra round-trip).

    ``has_more`` is true when the returned page was the full
    LIMIT, hinting to the UI that more exist. The frontend
    ignores it for now (the History view fits 50 on screen)
    but it's a free signal for the future.
    """

    items: list[InteractionOut]
    total: int
    has_more: bool


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



# ---------------------------------------------------------------------------
# User preferences.
#
# Read state, last-viewed timestamps, and column sort/filter prefs are
# all per-user opaque JSONB values keyed by ``(user_id, key)``. The
# frontend uses namespaced keys (``read_entries:<source_id>`` etc.)
# but the server treats the key as opaque -- new preference types
# land without a schema change.
# ---------------------------------------------------------------------------


class UserPreferenceOut(BaseModel):
    """One row from ``user_preferences`` -- a single (key, value) pair.

    Returned both as part of the bulk ``GET /api/preferences`` response
    and as the body of ``GET /api/preferences/{key}``.

    ``updated_at`` is the server's record of when the row was last
    written. The frontend doesn't currently use it (PUT is unconditional
    upsert; the client owns the "did this change" logic) but it's
    surfaced for future diff-based sync and for debugging "when did
    my read state last change?".
    """

    key: str
    # The value column is JSONB; pass through whatever the caller
    # stored. ``value: Any`` in pydantic is a deliberate ``Any`` --
    # we'd rather validate per-preference-type at the route layer
    # (where the key is known) than enforce one shape at the schema
    # layer and reject all the others.
    value: Optional[object] = None
    updated_at: dt.datetime


class UserPreferenceListOut(BaseModel):
    """Body of ``GET /api/preferences``. Returns every (key, value) row
    the caller has, in arbitrary order. The frontend rebuilds its
    in-memory state from this on first load (one round-trip, all keys).
    """

    items: list[UserPreferenceOut]


class UserPreferenceIn(BaseModel):
    """Body of ``PUT /api/preferences/{key}``. The key is in the URL
    (not the body) so the frontend can hit a single endpoint per
    key and the route can validate "this URL key matches what the
    caller is writing".

    ``value`` is opaque; the route passes it through to the
    JSONB column without coercion. The frontend's TS types are the
    source of truth for what shape each key holds.
    """

    value: Optional[object] = None


class FramingArticleOut(BaseModel):
    """One outlet's version of a Framing Watch story. ``framing_tone``
    is null until ``app.framing``'s batched classifier tags it (which
    only happens for clustered entries, and only once per entry)."""

    entry_id: int
    title: str
    url: str
    source_name: str
    favicon_path: Optional[str] = None
    published_at: Optional[dt.datetime] = None
    framing_tone: Optional[str] = None


class FramingClusterOut(BaseModel):
    """Body of one row of ``GET /api/framing-clusters``. ``wire_source``
    is a best-effort AP/Reuters/AFP label (null when nothing matched —
    the cluster still stands on embedding similarity alone).
    ``articles`` has 2+ entries by construction (see
    ``app.framing.cluster_recent_entries`` — clusters below 2 members
    get deleted), ordered oldest-published first."""

    cluster_id: int
    wire_source: Optional[str] = None
    first_seen_at: Optional[dt.datetime] = None
    articles: list[FramingArticleOut]
