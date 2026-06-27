// App: 3-4 column desktop grid, single-column mobile with swipe.
// Top bar has the hamburger (opens Drawer) and a refresh button.
// When OIDC is enabled and the user isn't logged in, the dashboard
// content is replaced with a LoginPage.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type CurrentUser, type Entry, type Health, type Source } from './api'
import { Card } from './components/Card'
import { Column } from './components/Column'
import { Drawer } from './components/Drawer'
import { Hamburger } from './components/Hamburger'
import { LoginPage } from './components/LoginPage'
import { UserBadge } from './components/UserBadge'

const REFRESH_INTERVAL_MS = 60_000

export function App() {
  const [entries, setEntries] = useState<Entry[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [mobileCol, setMobileCol] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [user, setUser] = useState<CurrentUser | null>(null)
  // True once we've finished probing /auth/me. Until then we don't render
  // the dashboard — avoids flashing the login page for already-logged-in
  // users on a hard refresh.
  const [authProbed, setAuthProbed] = useState(false)
  const [oidcDisabled, setOidcDisabled] = useState(false)
  const touchStartX = useRef<number | null>(null)

  const sourcesById = useMemo(
    () => new Map(sources.map((s) => [s.id, s.name])),
    [sources],
  )

  const byCategory = useMemo(() => {
    const grouped = new Map<string, Entry[]>()
    for (const e of entries) {
      const src = sources.find((s) => s.id === e.source_id)
      const cat = src?.category ?? 'other'
      const arr = grouped.get(cat) ?? []
      arr.push(e)
      grouped.set(cat, arr)
    }
    return grouped
  }, [entries, sources])

  const categories = useMemo(() => Array.from(byCategory.keys()).sort(), [byCategory])

  const refresh = useCallback(async () => {
    try {
      const [e, s, h] = await Promise.all([api.entries({ limit: 200 }), api.sources(), api.health()])
      setEntries(e)
      setSources(s)
      setHealth(h)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [])

  // Probe auth state once on mount. If /auth/me 404s, OIDC is disabled —
  // we stay in single-user mode (no login screen). If 200 or 401, OIDC
  // is enabled; show the dashboard or LoginPage accordingly.
  useEffect(() => {
    let cancelled = false
    api.me()
      .then((u) => {
        if (cancelled) return
        setUser(u)
        setOidcDisabled(false)
        setAuthProbed(true)
      })
      .catch(() => {
        if (cancelled) return
        // 404 → no auth surface at all (OIDC off). Any other error →
        // safest default is "no login", since that matches the
        // single-user behavior the user has been running.
        setOidcDisabled(true)
        setAuthProbed(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // Don't start polling until auth has been resolved; the LoginPage
    // doesn't need it, and we don't want to flood the API while the
    // user is being redirected through the IdP.
    if (!authProbed) return
    refresh()
    const id = setInterval(refresh, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refresh, authProbed])

  const onTouchStart = (e: React.TouchEvent) => { touchStartX.current = e.touches[0].clientX }
  const onTouchEnd = (e: React.TouchEvent) => {
    if (touchStartX.current == null) return
    const delta = e.changedTouches[0].clientX - touchStartX.current
    touchStartX.current = null
    if (Math.abs(delta) < 60) return
    if (delta < 0) setMobileCol((i) => Math.min(i + 1, Math.max(categories.length - 1, 0)))
    else setMobileCol((i) => Math.max(i - 1, 0))
  }

  // --- Render gates -----------------------------------------------------

  // 1. Auth probe in flight — render nothing (avoids login-page flash).
  if (!authProbed) {
    return <div className="h-full" />
  }

  // 2. OIDC enabled and not logged in — show LoginPage.
  if (!oidcDisabled && user === null) {
    return <LoginPage returnTo="/" onSignedIn={setUser} />
  }

  // 3. Default — the dashboard.
  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center gap-3 px-4 py-3 border-b border-slate-800 bg-slate-950">
        <Hamburger onClick={() => setDrawerOpen(true)} />
        <h1 className="text-lg font-bold">Popping</h1>
        <span className="ml-auto text-xs text-slate-400">
          {health
            ? `${health.entries} entries · ${health.sources} sources · ${health.status}`
            : 'connecting…'}
        </span>
        <button
          onClick={refresh}
          className="rounded px-3 py-1 text-sm bg-slate-800 hover:bg-slate-700 text-slate-200"
        >
          Refresh
        </button>
        {user && <UserBadge user={user} onSignedOut={() => setUser(null)} />}
      </header>

      {error && (
        <div className="px-4 py-2 bg-red-900/40 border-b border-red-800 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* Desktop: grid */}
      <main className="hidden md:grid md:grid-cols-3 lg:grid-cols-4 gap-4 p-4 flex-1 overflow-hidden">
        {categories.map((cat) => (
          <Column key={cat} name={cat} entries={byCategory.get(cat) ?? []} sourcesById={sourcesById} />
        ))}
        {categories.length === 0 && (
          <div className="col-span-full flex items-center justify-center text-slate-500">
            no entries yet — the scheduler will fetch the first batch shortly, or hit Refresh
          </div>
        )}
      </main>

      {/* Mobile: one column + swipe */}
      <main
        className="md:hidden flex-1 overflow-hidden p-3"
        onTouchStart={onTouchStart}
        onTouchEnd={onTouchEnd}
      >
        {categories.length === 0 ? (
          <div className="flex items-center justify-center h-full text-slate-500 text-sm">
            no entries yet
          </div>
        ) : (
          <>
            <Column
              name={categories[mobileCol] ?? ''}
              entries={byCategory.get(categories[mobileCol] ?? '') ?? []}
              sourcesById={sourcesById}
            />
            {categories.length > 1 && (
              <div className="flex justify-center gap-1 mt-2">
                {categories.map((c, i) => (
                  <span
                    key={c}
                    className={`h-1.5 w-1.5 rounded-full ${i === mobileCol ? 'bg-slate-300' : 'bg-slate-700'}`}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </main>

      <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} categories={categories} />
    </div>
  )
}