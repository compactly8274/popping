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
}

export interface Health {
  status: string
  sources: number
  entries: number
  db: string
  redis: string
  last_fetch: string | null
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
  auth_method?: 'oidc' | 'local' | 'loopback'
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
  /** Personal top-N feed. Requires auth when OIDC is enabled; loopback
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