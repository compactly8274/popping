// Slide-in drawer. Lists the category columns and the registered
// sources. Tapping a source filters the dashboard to that source's
// entries and closes the drawer. Also surfaces the notifications
// backend status (Apprise / Pushover / none) and the LLM provider
// status — useful confirmation that the user's env vars are wired up.

import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type LLMTagsResponse,
  type LLMStatus,
  type NotificationStatus,
  type SettingsOut,
  type Source,
} from '../api'

type Props = {
  open: boolean
  onClose: () => void
  categories: string[]
  sourceFilter: string | null
  onSourceSelect: (name: string | null) => void
}

// Whitelist mirrors backend ``_VALID_PROVIDERS``. Includes a sentinel
// empty value so the user can pick "use env default" (which is what
// happens when no runtime override is set).
const PROVIDER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '— env default —' },
  { value: 'ollama_cloud', label: 'Ollama Cloud' },
  { value: 'ollama', label: 'Ollama (local)' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'groq', label: 'Groq' },
]

export function Drawer({ open, onClose, categories, sourceFilter, onSourceSelect }: Props) {
  const [sources, setSources] = useState<Source[]>([])
  const [notif, setNotif] = useState<NotificationStatus | null>(null)
  const [llm, setLlm] = useState<LLMStatus | null>(null)
  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    api.sources().then(setSources).catch(() => setSources([]))
    api
      .notificationStatus()
      .then(setNotif)
      .catch(() => setNotif({ configured: false, backend: null, scheme: null }))
    api
      .llmStatus()
      .then(setLlm)
      .catch(() => setLlm({ configured: false, backend: null, model: null }))
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
          </div>
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
              LLM
            </h3>
            <LLMSection llm={llm} onChange={setLlm} />
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
              disabled={generating || (llm !== null && !llm.configured)}
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

// ---------------------------------------------------------------------------
// LLM section: chip + inline picker
// ---------------------------------------------------------------------------

function LLMSection({
  llm,
  onChange,
}: {
  llm: LLMStatus | null
  onChange: (next: LLMStatus) => void
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [tags, setTags] = useState<LLMTagsResponse | null>(null)
  const [provider, setProvider] = useState<string>('')
  const [modelBrief, setModelBrief] = useState<string>('')
  const [modelScoring, setModelScoring] = useState<string>('')
  const [freeTextBrief, setFreeTextBrief] = useState<string>('')
  const [useFreeText, setUseFreeText] = useState<boolean>(false)
  const [tagsError, setTagsError] = useState<string | null>(null)
  const [tagsLoading, setTagsLoading] = useState<boolean>(false)
  const [saving, setSaving] = useState<boolean>(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  // Fetch settings + tags when the picker is opened. We re-fetch on
  // open rather than on drawer-open because settings can change mid-
  // drawer-open (e.g. user saves, then opens picker again to verify).
  const openPicker = async () => {
    setPickerOpen(true)
    setSaveError(null)
    setTagsError(null)
    setTagsLoading(true)
    try {
      // Fetch settings first so we can derive which provider's tag
      // list to pull (Ollama-shaped only — Anthropic/OpenAI/Groq don't
      // expose a /api/tags). If the user has pinned a non-Ollama
      // provider, skip the tags fetch entirely and show free-text.
      const s = await api.settings()
      const prov = providerForTagsFetch(s)
      const t = prov
        ? await api.llmTags(prov)
        : null
      setTags(t)
      applySettingsToForm(s, t)
    } catch (err) {
      setTagsError((err as Error).message)
    } finally {
      setTagsLoading(false)
    }
  }

  // The chip's backend may not be one we've exposed in the picker
  // (e.g. user pinned "anthropic"). Only Ollama-shaped providers
  // expose /api/tags today — return null for the others so the picker
  // skips the fetch and shows a free-text input.
  const providerForTagsFetch = (s: SettingsOut | null): string | null => {
    const pinned = s?.llm_provider || ''
    if (pinned === 'ollama_cloud' || pinned === '') return 'ollama_cloud'
    if (pinned === 'ollama') return 'ollama'
    return null
  }

  const applySettingsToForm = (s: SettingsOut, t: LLMTagsResponse | null) => {
    setProvider(s.llm_provider || '')
    setModelBrief(s.llm_model_brief || '')
    setModelScoring(s.llm_model_scoring || '')
    // If the saved model isn't in the tags list (e.g. user typed it
    // freehand last time), keep it as free text so they can edit.
    const tagNames = t?.models?.map((m) => m.name) ?? []
    const isFreeText =
      Boolean(s.llm_model_brief) && tagNames.length > 0 && !tagNames.includes(s.llm_model_brief || '')
    setUseFreeText(isFreeText)
    setFreeTextBrief(isFreeText ? s.llm_model_brief || '' : '')
  }

  const refreshTags = async () => {
    setTagsLoading(true)
    setTagsError(null)
    try {
      const prov = providerForTagsFetch({
        llm_provider: provider || null,
        llm_model_brief: modelBrief || null,
        llm_model_scoring: modelScoring || null,
      })
      if (!prov) {
        // Pinned to a non-Ollama provider; no /api/tags to fetch.
        setTags(null)
        return
      }
      const t = await api.llmTags(prov, true)
      setTags(t)
      // Re-evaluate free-text mode now that we have a fresh list. If
      // the model the user has in the form isn't in the freshly-
      // fetched list, switch to free-text so they can edit it.
      if (!useFreeText && modelBrief && !t.models.some((m) => m.name === modelBrief)) {
        setUseFreeText(true)
        setFreeTextBrief(modelBrief)
      }
    } catch (err) {
      setTagsError((err as Error).message)
    } finally {
      setTagsLoading(false)
    }
  }

  const save = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      // Always send all three fields. Empty string = reset to env
      // (backend deletes the row); any other value = upsert. See the
      // docstring on ``PUT /api/settings/llm``.
      const next = await api.updateLLMSettings({
        provider: provider,
        model_brief: useFreeText ? freeTextBrief : modelBrief,
        model_scoring: modelScoring,
      })
      // ``next`` is the persisted settings row from the response. We
      // don't read it directly because the chip is rebuilt from a
      // fresh ``/api/llm/status`` call — that one is the source of
      // truth for what the backend will actually use on the next Brief.
      void next
      const status = await api.llmStatus()
      onChange(status)
      setPickerOpen(false)
    } catch (err) {
      setSaveError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  // Tag-name list for the dropdown. Empty array triggers free-text mode.
  const tagNames = useMemo(() => tags?.models?.map((m) => m.name) ?? [], [tags])

  return (
    <>
      <div className="rounded border border-slate-800 bg-slate-950 px-3 py-2 text-xs flex items-center justify-between gap-2">
        {llm === null ? (
          <span className="text-slate-500">checking…</span>
        ) : llm.configured ? (
          <span className="text-emerald-400 truncate">
            ✓ {llm.backend} · {llm.model}
          </span>
        ) : (
          <span className="text-amber-400">no LLM provider configured</span>
        )}
        <button
          onClick={openPicker}
          className="shrink-0 rounded px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-800"
          aria-label="edit LLM settings"
        >
          {pickerOpen ? 'close' : 'change'}
        </button>
      </div>

      {pickerOpen && (
        <div className="mt-2 rounded border border-slate-800 bg-slate-950 p-3 space-y-2 text-xs">
          <div>
            <label className="block text-slate-400 mb-1">Provider</label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100"
            >
              {PROVIDER_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-slate-400">Model (brief)</label>
              <button
                onClick={refreshTags}
                disabled={tagsLoading}
                className="text-[10px] text-slate-400 hover:text-slate-100 disabled:opacity-50"
              >
                {tagsLoading ? 'refreshing…' : 'refresh list'}
              </button>
            </div>
            {tagsError ? (
              <p className="text-[10px] text-amber-400 break-words mb-1">
                couldn’t load model list: {tagsError}
              </p>
            ) : null}
            {tags?.stale ? (
              <p className="text-[10px] text-amber-400 mb-1">
                showing cached list (live fetch failed)
              </p>
            ) : null}
            {tagNames.length === 0 || useFreeText ? (
              <input
                type="text"
                value={useFreeText ? freeTextBrief : modelBrief}
                onChange={(e) => {
                  setUseFreeText(true)
                  setFreeTextBrief(e.target.value)
                  setModelBrief(e.target.value)
                }}
                placeholder="model name, e.g. gpt-oss:120b"
                className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100 placeholder:text-slate-600"
              />
            ) : (
              <select
                value={modelBrief}
                onChange={(e) => setModelBrief(e.target.value)}
                className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100"
              >
                <option value="">— pick a model —</option>
                {tagNames.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            )}
            {tagNames.length > 0 && (
              <button
                onClick={() => {
                  if (useFreeText) {
                    // Switching back to dropdown — keep current value if it’s in the list
                    setUseFreeText(false)
                    if (!tagNames.includes(modelBrief)) {
                      setModelBrief('')
                    }
                  } else {
                    setUseFreeText(true)
                    setFreeTextBrief(modelBrief)
                  }
                }}
                className="mt-1 text-[10px] text-slate-400 hover:text-slate-100"
              >
                {useFreeText ? '← back to list' : 'type a name instead'}
              </button>
            )}
          </div>
          <div>
            <label className="block text-slate-400 mb-1">Model (scoring)</label>
            <input
              type="text"
              value={modelScoring}
              onChange={(e) => setModelScoring(e.target.value)}
              placeholder="same as brief if blank"
              className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100 placeholder:text-slate-600"
            />
          </div>
          {saveError && <p className="text-[10px] text-red-300 break-words">{saveError}</p>}
          <div className="flex gap-2 pt-1">
            <button
              onClick={save}
              disabled={saving}
              className="flex-1 rounded bg-blue-800 hover:bg-blue-700 disabled:opacity-50 text-blue-100 px-2 py-1"
            >
              {saving ? 'saving…' : 'save'}
            </button>
            <button
              onClick={() => setPickerOpen(false)}
              disabled={saving}
              className="flex-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-200 px-2 py-1"
            >
              cancel
            </button>
          </div>
          <p className="text-[10px] text-slate-500 leading-snug">
            Changes apply immediately — no restart needed. An empty
            value resets to the env default.
          </p>
        </div>
      )}
    </>
  )
}