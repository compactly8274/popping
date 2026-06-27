// Slide-in drawer. Lists the category columns. Settings (source
// management, followed categories, source weights) is still a future
// phase — for now it lists categories and a brief source registry.

import { useEffect, useState } from 'react'
import { api, type Source } from '../api'

type Props = {
  open: boolean
  onClose: () => void
  categories: string[]
}

export function Drawer({ open, onClose, categories }: Props) {
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
                {sources.map((s) => (
                  <li key={s.id} className="rounded px-2 py-1 text-sm text-slate-200 hover:bg-slate-800 flex items-center justify-between">
                    <span>{s.name}</span>
                    <span className="text-xs text-slate-500">{s.category}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </nav>
      </aside>
    </>
  )
}