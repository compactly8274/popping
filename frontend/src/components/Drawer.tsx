// Slide-in drawer. Lists the category columns and the registered
// sources. Tapping a source filters the dashboard to that source's
// entries and closes the drawer. Also surfaces the notifications
// backend status (Apprise / Pushover / none) — useful confirmation
// that the user's env vars are wired up.

import { useEffect, useState } from 'react'
import { api, type NotificationStatus, type Source } from '../api'

type Props = {
  open: boolean
  onClose: () => void
  categories: string[]
  sourceFilter: string | null
  onSourceSelect: (name: string | null) => void
}

export function Drawer({ open, onClose, categories, sourceFilter, onSourceSelect }: Props) {
  const [sources, setSources] = useState<Source[]>([])
  const [notif, setNotif] = useState<NotificationStatus | null>(null)
  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    api.sources().then(setSources).catch(() => setSources([]))
    api
      .notificationStatus()
      .then(setNotif)
      .catch(() => setNotif({ configured: false, backend: null, scheme: null }))
  }, [open])

  return (
    <>
      {/* backdrop */}
      <div
        onClick={onClose}
        className={`fixed inset-0 bg-black/40 z-30 transition-opacity ${open ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
      />
      <aside
        className={`fixed top-0 left-0 z-40 h-full w-72 bg-slate-900 border-r border-slate-800 shadow-xl transform transition-transform ${open ? 'translate-x-0' : '-translate-x-full'}`}
      >
        <div className="flex items-center justify-between p-4 border-b border-slate-800">
          <h2 className="text-lg font-semibold">Popping</h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:text-slate-100 hover:bg-slate-800"
            aria-label="close drawer"
          >
            ✕
          </button>
        </div>
        <nav className="p-4 space-y-4 overflow-y-auto">
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
              Notifications
            </h3>
            <div className="rounded border border-slate-800 bg-slate-950 px-3 py-2 text-xs">
              {notif === null ? (
                <span className="text-slate-500">checking…</span>
              ) : notif.configured ? (
                <span className="text-emerald-400">
                  ✓ configured ({notif.backend} · {notif.scheme})
                </span>
              ) : (
                <span className="text-amber-400">
                  not configured — set APPRISE_URL or PUSHOVER_* in .env
                </span>
              )}
            </div>
            <button
              onClick={async () => {
                setGenError(null)
                setGenerating(true)
                try {
                  await api.briefGenerate('terse')
                  onClose()
                } catch (err) {
                  setGenError((err as Error).message)
                } finally {
                  setGenerating(false)
                }
              }}
              disabled={generating}
              className="mt-2 w-full rounded bg-blue-800 hover:bg-blue-700 disabled:opacity-50 text-blue-100 px-3 py-1.5 text-xs"
            >
              {generating ? 'Generating brief…' : 'Generate brief now'}
            </button>
            {genError && (
              <p className="mt-1 text-[10px] text-red-300 break-words">{genError}</p>
            )}
          </div>
          <div className="pt-4 border-t border-slate-800">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">Categories</h3>
            <ul className="space-y-1">
              {categories.map((c) => (
                <li key={c} className="rounded px-2 py-1 text-sm text-slate-200 hover:bg-slate-800">{c}</li>
              ))}
            </ul>
          </div>
          <div className="pt-4 border-t border-slate-800">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">Sources</h3>
            {sources.length === 0 ? (
              <p className="text-xs text-slate-500 italic">loading…</p>
            ) : (
              <ul className="space-y-1">
                {sources.map((s) => {
                  const active = s.name === sourceFilter
                  return (
                    <li key={s.id}>
                      <button
                        onClick={() => {
                          onSourceSelect(active ? null : s.name)
                          onClose()
                        }}
                        className={`w-full text-left rounded px-2 py-1 text-sm flex items-center justify-between gap-2 transition ${
                          active
                            ? 'bg-slate-700 text-white'
                            : 'text-slate-200 hover:bg-slate-800'
                        }`}
                      >
                        <span className="flex items-center gap-2 min-w-0">
                          {s.favicon_path && (
                            <img
                              src={`/assets/${s.favicon_path}`}
                              alt=""
                              width={16}
                              height={16}
                              loading="lazy"
                              className="shrink-0 w-4 h-4 rounded-sm bg-slate-800"
                              onError={(e) => {
                                ;(e.currentTarget as HTMLImageElement).style.display = 'none'
                              }}
                            />
                          )}
                          <span className="truncate">{s.name}</span>
                        </span>
                        <span className="text-xs text-slate-500 shrink-0">{s.category}</span>
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        </nav>
      </aside>
    </>
  )
}