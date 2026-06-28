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
  composite_score: number
  personal_score: number
  raw_score: number
  meta: Record<string, unknown> | null
  // Thumbnail (if the source ships one). Path is relative to /assets.
  image_url: string | null
  image_path: string | null
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
  entries: (opts?: { category?: string; source?: string; limit?: number }) => {
    const params = new URLSearchParams()
    if (opts?.category) params.set('category', opts.category)
    if (opts?.source) params.set('source', opts.source)
    if (opts?.limit) params.set('limit', String(opts.limit))
    const q = params.toString()
    return jsonFetch<Entry[]>(`/api/entries${q ? `?${q}` : ''}`)
  },
  sources: () => jsonFetch<Source[]>('/api/sources'),
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

  // ---- The Brief (phase 4) ----
  briefLatest: (opts?: { tone?: string; limit?: number }) => {
    const params = new URLSearchParams()
    if (opts?.tone) params.set('tone', opts.tone)
    if (opts?.limit) params.set('limit', String(opts.limit))
    const q = params.toString()
    return jsonFetch<Brief[]>(`/api/brief/latest${q ? `?${q}` : ''}`)
  },
  briefGenerate: (tone: string = 'terse') =>
    jsonFetch<Brief>(
      `/api/brief/generate${tone ? `?tone=${encodeURIComponent(tone)}` : ''}`,
      { method: 'POST' },
    ),
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
  /** Fetch the model list for ``provider`` (Ollama-shaped only). */
  llmTags: (provider: string = 'ollama_cloud', refresh: boolean = false) => {
    const params = new URLSearchParams()
    params.set('provider', provider)
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