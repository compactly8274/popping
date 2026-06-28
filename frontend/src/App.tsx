// App: 3-4 column desktop grid, single-column mobile with swipe.
// Top bar has the hamburger (opens Drawer) and a refresh button.
// When OIDC is enabled and the user isn't logged in, the dashboard
// content is replaced with a LoginPage.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Brief, type CurrentUser, type Entry, type Health, type Source } from './api'
import { BriefCard } from './components/BriefCard'
import { Card } from './components/Card'
import { Column } from './components/Column'
import { Drawer } from './components/Drawer'
import { ForYou } from './components/ForYou'
import { Hamburger } from './components/Hamburger'
import { LoginPage } from './components/LoginPage'
import { UserBadge } from './components/UserBadge'

const REFRESH_INTERVAL_MS = 60_000

export function App() {
  const [entries, setEntries] = useState<Entry[]>([])
  const [forYou, setForYou] = useState<Entry[]>([])
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
  // When set, the dashboard shows only entries from this source. Set
  // by tapping a source in the Drawer; cleared by tapping the same
  // source again or the "clear filter" pill.
  const [sourceFilter, setSourceFilter] = useState<string | null>(null)
  // Latest terse Brief for the dashboard card. Null until either the
  // scheduler has run today or the user manually generates one.
  const [brief, setBrief] = useState<Brief | null>(null)
  // Tracks in-flight brief generation requests from the header
  // button so a second tap doesn't fire a parallel LLM roundtrip.
  // The BriefCard and Drawer manage their own generating state for
  // their own buttons; this is just for the header button which
  // lives up here in App.
  const [generatingBrief, setGeneratingBrief] = useState(false)
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
      // forYou may 401 when OIDC is on and the user isn't logged in —
      // we already routed them to LoginPage in that case, so this only
      // runs for signed-in users. Tolerate 401 anyway for safety.
      const [e, s, h, fy] = await Promise.all([
        api.entries({ limit: 200, source: sourceFilter ?? undefined }),
        api.sources(),
        api.health(),
        api.forYou({ limit: 25 }).catch(() => [] as Entry[]),
      ])
      setEntries(e)
      setSources(s)
      setHealth(h)
      setForYou(fy)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [sourceFilter])

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
      <header className="flex items-center gap-2 sm:gap-3 px-4 py-1.5 sm:py-3 border-b border-slate-800 bg-slate-950">
        <Hamburger onClick={() => setDrawerOpen(true)} />
        {/* Title: text-base on mobile saves a couple px of line-height
            vs text-lg without losing legibility. sm: bumps back to
            the desktop look. */}
        <h1 className="text-base sm:text-lg font-bold">Popping</h1>
        {/* The "X entries · Y sources" chip is duplicative with the
            Drawer sources list and squashes the action buttons on
            narrow phones. Hide below sm; the Drawer covers the
            curious case where someone wants the counts. */}
        <span className="ml-auto hidden sm:inline text-xs text-slate-400">
          {health
            ? `${health.entries} entries · ${health.sources} sources · ${health.status}`
            : 'connecting…'}
        </span>
        {/* min-h-[36px] on mobile / [44px] on sm+: keeps the row
            slim on phones while staying above the iOS 44px guidance
            on tablets/desktops. The icon-equivalent text labels
            mean a thumb can still land the button accurately. */}
        <button
          onClick={refresh}
          className="min-h-[36px] sm:min-h-[44px] rounded px-3 py-1 text-sm bg-slate-800 active:bg-slate-700 text-slate-200 [@media(hover:hover)]:hover:bg-slate-700"
        >
          Refresh
        </button>
        {/* Header-level Brief button: hides on mobile because the
            BriefCard itself surfaces a Generate / Regenerate button.
            Two CTAs on the same action is noise, and on a 320px
            viewport there's no room for both. */}
        <button
          onClick={async () => {
            if (generatingBrief) return
            setGeneratingBrief(true)
            try {
              const next = await api.briefGenerate('terse')
              setBrief(next)
            } catch (err) {
              setError((err as Error).message)
            } finally {
              setGeneratingBrief(false)
            }
          }}
          disabled={generatingBrief}
          className="hidden sm:inline-flex min-h-[44px] rounded px-3 py-1 text-sm bg-blue-800 active:bg-blue-900 disabled:opacity-50 text-blue-100 [@media(hover:hover)]:hover:bg-blue-700"
          title="Generate today's brief now"
        >
          {generatingBrief ? '…' : 'Brief'}
        </button>
        {user && <UserBadge user={user} onSignedOut={() => setUser(null)} />}
      </header>

      {error && (
        <div className="px-4 py-2 bg-red-900/40 border-b border-red-800 text-sm text-red-200">
          {error}
        </div>
      )}

      {sourceFilter && (
        <div className="px-4 py-2 border-b border-slate-800 bg-slate-900 flex items-center gap-2 text-sm">
          <span className="text-slate-400">Filtering by source:</span>
          <span className="rounded bg-slate-800 px-2 py-0.5 text-slate-200">{sourceFilter}</span>
          <button
            onClick={() => setSourceFilter(null)}
            className="ml-auto min-h-[36px] sm:min-h-[44px] rounded px-3 py-1 text-slate-400 active:bg-slate-800 [@media(hover:hover)]:hover:text-slate-100 [@media(hover:hover)]:hover:bg-slate-800"
            aria-label="clear source filter"
          >
            clear ✕
          </button>
        </div>
      )}

      <ForYou entries={forYou} sourcesById={sourcesById} />

      <BriefCard brief={brief} onBriefChange={setBrief} />

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

      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        categories={categories}
        sourceFilter={sourceFilter}
        onSourceSelect={setSourceFilter}
      />
    </div>
  )
}