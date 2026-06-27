// Typed fetch wrappers for the Popping API.
// In dev, Vite proxies /api -> backend; in production the frontend is
// served from the same origin and the proxy is unnecessary.

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
  const resp = await fetch(url, init)
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText} for ${url}`)
  }
  return resp.json() as Promise<T>
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
  ingest: (sourceName: string) =>
    jsonFetch<{ source: string; fetched: number; inserted: number; duplicates: number; error: string | null }>(
      `/api/ingest/${encodeURIComponent(sourceName)}`,
      { method: 'POST' },
    ),
}