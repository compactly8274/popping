// Slide-in drawer. Lists the category columns and the registered
// sources. Tapping a source filters the dashboard to that source's
// entries and closes the drawer.

import { useEffect, useState } from 'react'
import { api, type Source } from '../api'

type Props = {
  open: boolean
  onClose: () => void
  categories: string[]
  sourceFilter: string | null
  onSourceSelect: (name: string | null) => void
}

export function Drawer({ open, onClose, categories, sourceFilter, onSourceSelect }: Props) {
  const [sources, setSources] = useState<Source[]>([])

  useEffect(() => {
    if (!open) return
    api.sources().then(setSources).catch(() => setSources([]))
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
                        className={`w-full text-left rounded px-2 py-1 text-sm flex items-center justify-between transition ${
                          active
                            ? 'bg-slate-700 text-white'
                            : 'text-slate-200 hover:bg-slate-800'
                        }`}
                      >
                        <span>{s.name}</span>
                        <span className="text-xs text-slate-500">{s.category}</span>
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