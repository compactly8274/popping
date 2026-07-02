// Typed fetch wrappers for the Popping API.
// In dev, Vite proxies /api -> backend; in production the frontend is
// served from the same origin and the proxy is unnecessary.
//
// All requests send credentials: 'include' so the OIDC session cookie
// (when enabled) rides along. Endpoints that don't need it ignore the
// cookie; endpoints that gate on it (POST /api/ingest, /api/interactions
// in phase 2) require it to be present.

export interface Entry {
  id: number
  source_id: number
  title: string
  url: string
  published_at: string | null
  // ISO timestamp of when the row landed in our DB (server `fetched_at`).
  // Distinct from `published_at` (source article publish time). The
  // frontend uses this for "new since last refresh" + read/unread.
  fetched_at: string | null
  composite_score: number
  personal_score: number
  raw_score: number
  meta: Record<string, unknown> | null
  // Thumbnail (if the source ships one). Path is relative to /assets.
  image_url: string | null
  image_path: string | null
  // Reddit cross-reference footer: populated by the background sweep
  // (``app.scheduler._crossref_sweep``) when Hydra confirms the
  // entry's URL has a matching discussion thread on Reddit. Both
  // null = no cross-ref found (or feature disabled). The card
  // renders "💬 Discussed on Reddit · N comments" linking to the
  // thread when `reddit_thread_url` is non-null.
  reddit_thread_url: string | null
  reddit_comment_count: number | null
}

export interface Source {
  id: number
  name: string
  type: string
  category: string
  url: string
  refresh_interval_seconds: number
  last_fetch_at: string | null
  last_error: string | null
  error_count: number
  active: boolean
  // Favicon URL (typically origin's /favicon.ico), populated on first ingest.
  favicon_url: string | null
  // Local path under /assets, e.g. "favicons/3.ico".
  favicon_path: string | null
  // Per-source HTTP header overrides. NULL = use defaults.
  // Most common use is ``{"User-Agent": "<browser UA>"}`` for CDNs
  // that block our default ``Popping/0.2`` UA.
  custom_headers: Record<string, string> | null
  // Backend-computed: true when ``active=false`` AND error_count has
  // reached the scheduler's auto-disable threshold. Lets the UI
  // distinguish an auto-disabled source (needs investigation before
  // re-enabling) from a manually-paused one (routine user choice).
  // See ``app.models.Source.auto_disabled``.
  auto_disabled: boolean
}

/** Result of ``POST /api/sources/test``. ``ok`` is the only field the
 * UI branches on; the rest are diagnostic. ``error_kind`` is a short
 * enum the frontend maps to a friendly message — see
 * ``friendlyTestError`` in the AddCustomTab code for the lookup. */
export interface SourceTestResult {
  ok: boolean
  status_code: number | null
  item_count: number | null
  sample_titles: string[]
  error_kind:
    | 'not_found'
    | 'forbidden'
    | 'timeout'
    | 'parse_error'
    | 'name_conflict'
    | 'invalid_url'
    | 'unsupported_type'
    | 'network_error'
    | 'unknown'
    | null
  error: string | null
}

export interface Health {
  status: string
  sources: number
  entries: number
  db: string
  redis: string
  last_fetch: string | null
}

export interface Brief {
  id: number
  generated_at: string
  tone: string
  content: string
  delivered_at: string | null
  meta: Record<string, unknown> | null
}

export interface NotificationStatus {
  configured: boolean
  backend: string | null
  scheme: string | null
}

export interface LLMStatus {
  configured: boolean
  backend: string | null
  model: string | null
}

// /api/settings — runtime-overridable settings the user has saved
// (from the Drawer picker). All fields nullable: first boot or
// "use env default" leaves the value unset.
export interface SettingsOut {
  llm_provider: string | null
  llm_model_brief: string | null
  llm_model_scoring: string | null
}

// PUT /api/settings/llm — all fields optional; missing = unchanged.
export interface LLMSettingsUpdate {
  provider?: string | null
  model_brief?: string | null
  model_scoring?: string | null
}

// One row from /api/llm/tags — Ollama-style model record. Slim subset
// of what Ollama returns; details (digest, modified_at) trimmed.
// ``recommended`` is server-set based on a curated list of well-known
// Ollama Cloud tags (``backend/app/llm/tags.py``). ``recommended_note``
// carries an optional short suffix (e.g. ``"thinking"``) for tags whose
// output goes through the thinking-field fallback.
export interface LLMTag {
  name: string
  size: number | null
  family: string | null
  parameter_size: string | null
  quantization_level: string | null
  recommended?: boolean
  recommended_note?: string | null
}

export interface LLMTagsResponse {
  models: LLMTag[]
  cached_at: string | null
  ttl_seconds: number
  stale?: boolean
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, { credentials: 'include', ...init })
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText} for ${url}`)
  }
  return resp.json() as Promise<T>
}

export interface CurrentUser {
  sub: string
  email: string
  name: string
  auth_method?: 'oidc' | 'local' | 'bypass'
}

export const api = {
  health: () => jsonFetch<Health>('/api/health'),
  entries: (
    opts?: {
      category?: string
      // Multi-source: pass an array; the backend turns this into
      // ``?source=a&source=b`` via ``Query(default=None, ...)``. An
      // empty array means "no filter" (we don't even send the param).
      source?: string[] | string
      // Case-insensitive substring match on title and meta.summary.
      // Backend caps results at 50 regardless of `limit`.
      q?: string
      limit?: number
    },
  ) => {
    const params = new URLSearchParams()
    if (opts?.category) params.set('category', opts.category)
    if (opts?.source) {
      // Accept either an array or a single string so callers don't
      // have to construct an array for the common single-source case.
      const sources = Array.isArray(opts.source) ? opts.source : [opts.source]
      for (const s of sources) {
        if (s) params.append('source', s)
      }
    }
    if (opts?.q) params.set('q', opts.q)
    if (opts?.limit) params.set('limit', String(opts.limit))
    const q = params.toString()
    return jsonFetch<Entry[]>(`/api/entries${q ? `?${q}` : ''}`)
  },
  sources: () => jsonFetch<Source[]>('/api/sources'),

  // ---- Phase 5: feed onboarding + recommendations ----
  /** Create a dynamic source row. Backend validates name regex +
   * url parse + refresh clamp + accepts type='rss' only. The
   * scheduler registers an ingest job for the new row immediately. */
  createSource: (body: {
    name: string
    type?: string
    category: string
    url: string
    refresh_interval_seconds?: number
    custom_headers?: Record<string, string> | null
  }) =>
    jsonFetch<Source>('/api/sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** Probe a URL with the same plugin Add would use, no DB write.
   *
   * Returns ``ok=true`` with a small item count + sample titles on
   * success, or ``ok=false`` with an ``error_kind`` enum the UI maps
   * to a friendly message. The full ``error`` string is included so
   * power users can see the underlying exception without
   * re-running with curl. */
  testSource: (body: {
    name?: string
    type?: 'rss' | 'reddit'
    category?: string
    url: string
    custom_headers?: Record<string, string> | null
  }) =>
    jsonFetch<SourceTestResult>('/api/sources/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** Partial update of a source. All fields optional.
   *
   * ``name`` and ``url`` are now editable for dynamic rows. The
   * backend rejects PATCH on built-ins (BBC, HN, etc.) with a 400
   * and returns 409 on a name collision. URL changes clear the
   * cached favicon; the next ingest re-downloads. */
  updateSource: (
    id: number,
    body: {
      refresh_interval_seconds?: number
      active?: boolean
      category?: string
      name?: string
      url?: string
      // Per-source HTTP header overrides (e.g. ``{User-Agent: ...}``).
      // Send ``{}`` (empty object) to clear an existing override;
      // omit the field to leave it untouched. Cookie/Authorization
      // /Host are rejected by the backend.
      custom_headers?: Record<string, string> | null
    },
  ) =>
    jsonFetch<Source>(`/api/sources/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** Drop a dynamic source row + its scheduler job. 400 if the row
   * is a built-in (BBC, HN, etc.). */
  deleteSource: (id: number) =>
    fetch(`/api/sources/${id}`, {
      method: 'DELETE',
      credentials: 'include',
    }).then((resp) => {
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for /api/sources/${id}`)
    }),
  /** Curated list of feeds the user might want to add, minus any
   * they already have. Phase 5 is static; Phase 8 re-ranks by
   * co-engagement. */
  feedRecommendations: () => jsonFetch<Array<{
    name: string
    category: string
    url: string
    blurb: string
    // ``Source.type`` to send on Add (``"rss"`` for the RSS rows,
    // ``"reddit"`` for per-subreddit rows). Backend defaults to
    // ``"rss"`` when the recommendation omits the field, so older
    // cached payloads without the key still parse cleanly. The
    // Recommended tab forwards this verbatim to
    // ``POST /api/sources`` so the route layer picks the right
    // URL validator and plugin dispatch.
    type?: string
    // Optional HTTP header overrides applied on Add. Set on
    // entries whose CDN blocks our default UA (CBC). Read by the
    // frontend and forwarded as ``custom_headers`` to POST
    // /api/sources. See backend/app/feed_recommendations.py
    // for the curated list and ``schemas.FeedRecommendation`` for
    // the wire shape.
    default_headers?: Record<string, string> | null
  }>>('/api/feed-recommendations'),

  /** Personal top-N feed. Requires auth when OIDC is enabled; local
   * bypass users always pass through. */
  forYou: (opts?: { limit?: number; category?: string }) => {
    const params = new URLSearchParams()
    if (opts?.limit) params.set('limit', String(opts.limit))
    if (opts?.category) params.set('category', opts.category)
    const q = params.toString()
    return jsonFetch<Entry[]>(`/api/foryou${q ? `?${q}` : ''}`)
  },
  ingest: (sourceName: string) =>
    jsonFetch<{ source: string; fetched: number; inserted: number; duplicates: number; error: string | null }>(
      `/api/ingest/${encodeURIComponent(sourceName)}`,
      { method: 'POST' },
    ),
  /** Per-card summary. Returns the feed's own ``meta.summary``
   * (HTML-stripped, length-capped) on first call, or the cached
   * column on subsequent calls. ``summary`` is ``null`` when the
   * source shipped no usable text; the UI surfaces "no summary
   * available" in that case. ``cached`` is true after the first
   * call so the frontend can show a "summary ready" badge later
   * if we want — for now it's informational. */
  entrySummary: (entryId: number) =>
    jsonFetch<{ summary: string | null; cached: boolean }>(
      `/api/entries/${entryId}/summary`,
      { method: 'POST' },
    ),

  // ---- Engagement events (Phase 8) ----
  /** Record one engagement event immediately. The endpoint requires
   * auth when OIDC is on; for click events the synchronous response
   * is preferable so we know the server saw it. Use the batch
   * endpoint for view events. */
  recordInteraction: (event: {
    entry_id: number
    type:
      | 'view'
      | 'click'
      | 'dwell'
      | 'thumb_up'
      | 'thumb_down'
      | 'bookmark'
      | 'share'
      | 'never'
    value?: number
  }) =>
    jsonFetch<{ id: number }>('/api/interactions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
    }),
  /** Record a batch of engagement events. The frontend schedules
   * view events into a buffer and flushes on idle / pagehide —
   * see ``frontend/src/lib/interactions.ts``. */
  recordInteractionBatch: (events: Array<{
    entry_id: number
    type:
      | 'view'
      | 'click'
      | 'dwell'
      | 'thumb_up'
      | 'thumb_down'
      | 'bookmark'
      | 'share'
      | 'never'
    value?: number
  }>) =>
    jsonFetch<{ inserted: number }>('/api/interactions/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events }),
    }),

  /** Fetch the user's recent engagement events for the History
   * view in the Drawer. Returns the events with joined entry
   * + source metadata so the frontend can render the
   * "what I read" / "what I hid" / "what I starred" list
   * in one round-trip.
   *
   * ``types`` filters the event types to include; empty /
   * undefined returns all types (excluding ``dwell`` which
   * is a per-card read-time signal, not a user-meaningful
   * event worth surfacing in the History list).
   *
   * ``limit`` defaults to 50, max 200. ``offset`` for
   * pagination.
   *
   * The History view in the Drawer fetches this on every
   * Drawer open. The query is cheap (a single index seek
   * on ``(user_id, created_at DESC)`` plus a 3-way join)
   * and the result is small (50 rows); no caching needed
   * for the MVP. */
  listRecentInteractions: (opts?: {
    types?: string[]
    limit?: number
    offset?: number
    groupBy?: 'none' | 'entry'
  }) => {
    const params = new URLSearchParams()
    if (opts?.types && opts.types.length > 0) {
      params.set('types', opts.types.join(','))
    }
    if (opts?.limit != null) params.set('limit', String(opts.limit))
    if (opts?.offset != null) params.set('offset', String(opts.offset))
    if (opts?.groupBy) params.set('group_by', opts.groupBy)
    const qs = params.toString()
    return jsonFetch<{
      items: Array<{
        id: number
        type: string
        value: number
        created_at: string
        entry_id: number
        entry_title: string
        entry_url: string
        entry_published_at: string | null
        source_id: number
        source_name: string
      }>
      total: number
      has_more: boolean
    }>(`/api/interactions/recent${qs ? '?' + qs : ''}`)
  },

  // ---- The Brief (phase 4) ----
  briefLatest: (opts?: { tone?: string; limit?: number }) => {
    const params = new URLSearchParams()
    if (opts?.tone) params.set('tone', opts.tone)
    if (opts?.limit) params.set('limit', String(opts.limit))
    const q = params.toString()
    return jsonFetch<Brief[]>(`/api/brief/latest${q ? `?${q}` : ''}`)
  },
  // Kick off a brief generation. Returns 202 + a job id; the card
  // then polls ``briefJobStatus`` until the job reaches a terminal
  // state. The previous synchronous version held the connection
  // open across the LLM roundtrip (3-10 s on Ollama); the
  // 202+ack path keeps the click→ack feedback fast.
  briefGenerate: (tone: string = 'terse') =>
    jsonFetch<{ job_id: string; tone: string }>(
      `/api/brief/generate${tone ? `?tone=${encodeURIComponent(tone)}` : ''}`,
      { method: 'POST' },
    ),
  // Poll a generation job. ``status`` is
  // ``running`` | ``completed`` | ``failed``. Once completed, the
  // card swaps the brief in and stops polling. A 404 here means
  // the job aged out of the in-memory ledger (process restart,
  // ledger cap exceeded) — also treated as terminal.
  briefJobStatus: (jobId: string) =>
    jsonFetch<{
      id: string
      tone: string
      status: 'running' | 'completed' | 'failed'
      brief: Brief | null
      error: string | null
    }>(`/api/brief/jobs/${encodeURIComponent(jobId)}`),
  notificationStatus: () => jsonFetch<NotificationStatus>('/api/notifications/status'),
  llmStatus: () => jsonFetch<LLMStatus>('/api/llm/status'),

  // ---- Runtime settings (LLM picker) ----
  /** Current runtime overrides for LLM knobs. All fields nullable. */
  settings: () => jsonFetch<SettingsOut>('/api/settings'),
  /** Persist one or more LLM knobs. Empty string = reset to env. */
  updateLLMSettings: (update: LLMSettingsUpdate) =>
    jsonFetch<SettingsOut>('/api/settings/llm', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(update),
    }),
  /** Fetch the model list for ``provider`` (Ollama-shaped only).
   * ``task`` selects which curated recommendation list annotates the
   * response — ``"brief"`` for the brief prose generator, ``"scoring"``
   * for per-entry relevance scoring. The picker calls this twice
   * (once per task) so each dropdown shows task-appropriate
   * ``★`` markers. */
  llmTags: (
    provider: string = 'ollama_cloud',
    refresh: boolean = false,
    task: 'brief' | 'scoring' = 'brief',
  ) => {
    const params = new URLSearchParams()
    params.set('provider', provider)
    params.set('task', task)
    if (refresh) params.set('refresh', 'true')
    return jsonFetch<LLMTagsResponse>(`/api/llm/tags?${params.toString()}`)
  },

  // ---- Auth (only meaningful when OIDC is enabled on the backend) ----
  /** Probe the current user. Returns the user, null (logged out), or
   * throws a 404-shaped Error if OIDC isn't enabled on the backend. */
  me: async (): Promise<CurrentUser | null> => {
    const resp = await fetch('/auth/me', { credentials: 'include' })
    if (resp.status === 401) return null
    if (resp.status === 404) throw new Error('OIDC not enabled')
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for /auth/me`)
    return resp.json() as Promise<CurrentUser>
  },
  /** Wipe the session cookie. Backend returns 204; we always succeed. */
  logout: () => fetch('/auth/logout', { method: 'POST', credentials: 'include' }),
  /** Build the OIDC login URL with a return path. Caller navigates. */
  loginUrl: (returnTo: string = '/') => `/auth/login?return_to=${encodeURIComponent(returnTo)}`,

  /** Whether the local fallback user is configured. Used to render the
   * password form in LoginPage. */
  localAuthAvailable: () => jsonFetch<{ enabled: boolean }>('/auth/local/availability'),
  /** POST /auth/local — exchanges username/password for a session cookie. */
  loginLocal: (username: string, password: string) =>
    jsonFetch<CurrentUser>('/auth/local', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
}

