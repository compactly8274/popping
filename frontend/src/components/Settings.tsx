// Settings overlay.
//
// Full-viewport sheet that slides up from the bottom on mobile and
// in from the right on desktop, with a tab strip across the top and
// a Done button. The four tabs:
//
//   Feeds          — moved from Drawer. The full FeedManager
//                    (My feeds / Recommended / Add custom + the
//                    new "Test" button).
//   LLM            — moved from Drawer. Provider + model pickers,
//                    brief tone pills, "Generate brief now".
//   Notifications  — moved from Drawer. Backend status + retry.
//   Reset          — moved from Drawer. Clear local state.
//
// Tab navigation: the URL holds the open tab via
// ``?view=settings&tab=feeds|llm|notifications|reset`` so the back
// button and refresh both behave correctly. App.tsx reads the param
// and passes the active tab + setter down. Default is ``feeds``.
//
// Why a separate overlay (not a route + nested layout): the rest of
// the app is single-page; adding a router just for this would balloon
// the bundle. The URL param trick gives us "back closes the
// overlay" for free, and the overlay is dismissible by clearing the
// param (Esc + tapping Done both do that).

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  type LLMTagsResponse,
  type LLMStatus,
  type NotificationStatus,
  type SettingsOut,
  type Source,
} from '../api'
import { FeedManager } from './FeedManager'
import { toast } from './Toast'
import { usePreferences, type HistoryGroupByValue } from '../lib/preferences'

export type SettingsTab =
  | 'feeds'
  | 'llm'
  | 'notifications'
  | 'history'
  | 'hidden'
  | 'starred'
  | 'reset'

type Props = {
  open: boolean
  // The active tab. App owns this (driven by the URL param) so the
  // overlay can be re-rendered with the right tab on deep links.
  tab: SettingsTab
  // Open the source list (passed to FeedManager so the "Add custom"
  // path can list existing names client-side).
  sources: Source[]
  onRefreshSources: () => Promise<void>
  onError: (msg: string) => void
  onClose: () => void
  // Lifted state from App. The Settings overlay owns the canonical
  // brief-tone state — Drawer's tone picker is gone, so this is
  // the only place the tone can change.
  briefTone: 'terse' | 'narrative' | 'alert'
  onBriefToneChange: (next: 'terse' | 'narrative' | 'alert') => void
  // Lifted brief-generation trigger. Same trigger the header
  // "Brief" button uses — the Settings button is just another
  // surface for the same action.
  triggerGenerate: (
    tone: 'terse' | 'narrative' | 'alert',
    onError?: (msg: string) => void,
  ) => Promise<void>
  generating: boolean
  // Bubbles the old→new source name mapping from FeedManager's
  // inline edit, so App can remap the active filter chip in the
  // same render cycle.
  onSourceRenamed?: (oldName: string, newName: string) => void
  // Reset hooks. Wipes local namespaced keys; App owns the
  // reload so every component's state mirrors reset in lockstep.
  onResetLocalState: () => void
  // List of currently-hidden entry ids. Shown on the Hidden tab
  // so the user can review and restore.
  hiddenEntries: number[]
  // Restore a single hidden entry (or all of them with
  // ``onRestoreAllHidden``).
  onRestoreHidden: (entryId: number) => void
  onRestoreAllHidden: () => void
  // List of currently-starred (saved) entry ids. Shown on the
  // Saved tab so the user can review and bulk-unsave.
  starredEntries: number[]
  onUnstarAll: () => void
  // Per-row actions for the list view.
  onUnstarEntry: (entryId: number) => void
}

const TAB_META: Record<SettingsTab, { label: string; icon: string }> = {
  feeds: { label: 'Feeds', icon: 'feeds' },
  llm: { label: 'LLM', icon: 'llm' },
  notifications: { label: 'Notifications', icon: 'bell' },
  history: { label: 'History', icon: 'history' },
  hidden: { label: 'Hidden', icon: 'eye-off' },
  starred: { label: 'Saved', icon: 'star' },
  reset: { label: 'Reset', icon: 'reset' },
}

export function Settings({
  open,
  tab,
  sources,
  onRefreshSources,
  onError,
  onClose,
  briefTone,
  onBriefToneChange,
  triggerGenerate,
  generating,
  onSourceRenamed,
  onResetLocalState,
  hiddenEntries,
  onRestoreHidden,
  onRestoreAllHidden,
  starredEntries,
  onUnstarAll,
  onUnstarEntry,
}: Props) {
  // Live data fetched when the overlay opens. The Feeds tab reuses
  // the ``sources`` prop (App owns it), but the LLM and
  // Notifications tabs need their own status fetches. The fetches
  // are local because the Settings overlay is the canonical place
  // for them now.
  const [notif, setNotif] = useState<NotificationStatus | null>(null)
  const [notifError, setNotifError] = useState<string | null>(null)
  const [llm, setLlm] = useState<LLMStatus | null>(null)
  const [llmError, setLlmError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    api
      .notificationStatus()
      .then((s) => alive && setNotif(s))
      .catch((e) => alive && setNotifError((e as Error).message))
    api
      .llmStatus()
      .then((s) => alive && setLlm(s))
      .catch((e) => alive && setLlmError((e as Error).message))
    return () => {
      alive = false
    }
  }, [open])

  // Esc closes. Mirrors the Drawer's pattern; App's keyboard handler
  // also listens for Esc on the search input but this listener
  // captures first when the overlay is open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="settings"
      className="fixed inset-0 z-50 bg-black/70 supports-[backdrop-filter]:backdrop-blur-sm animate-fade-in"
      onClick={(e) => {
        // Backdrop tap closes. The inner card has stopPropagation so
        // taps inside it don't close.
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        // Right-side panel on desktop, full-viewport sheet on mobile.
        // The desktop panel is wider than the Drawer (480px) because
        // the LLM tab needs room for the provider/model pickers and
        // the Feeds tab's add-custom form has a wide textarea.
        className="absolute inset-x-0 bottom-0 top-auto h-[92vh] rounded-t-ios-lg bg-bg-app shadow-2xl flex flex-col
                   md:inset-y-0 md:right-0 md:left-auto md:top-0 md:bottom-auto md:h-full md:w-[480px] md:rounded-none"
      >
        {/* Header — same shape as Drawer but with the tab strip
            underneath the title row instead of inside the body. The
            "Done" button is on the left, title on the right
            (matches the Drawer's right-anchored panel). */}
        <div className="flex items-center justify-between px-4 pt-3 pb-3 md:pt-5 md:pb-4 border-b border-hairline shrink-0">
          <button
            onClick={onClose}
            aria-label="close settings"
            className="min-h-[32px] min-w-[32px] flex items-center justify-center rounded-ios text-accent active:bg-bg-elevated"
          >
            <span className="text-ios-body font-normal">Done</span>
          </button>
          <h2 className="text-2xl md:text-ios-large-title font-bold text-label-primary tracking-tight">
            {tab === 'feeds' ? 'Settings' : `Settings → ${TAB_META[tab].label}`}
          </h2>
        </div>
        {/* Tab strip. iOS segmented control style — same 4-cell
            grid the Drawer's tone picker uses, but extended to
            cover all settings tabs. Active tab uses bg-bg-elevated
            + text-accent; inactive is bg-bg-surface + text-label-
            primary. ``role="tablist"`` so screen readers
            announce it as a tab group. */}
        <div className="flex gap-1 mx-4 mt-3 rounded-ios overflow-hidden border border-hairline" role="tablist">
          {(['feeds', 'llm', 'notifications', 'history', 'hidden', 'starred', 'reset'] as SettingsTab[]).map((t) => {
            const active = t === tab
            return (
              <button
                key={t}
                role="tab"
                aria-selected={active}
                onClick={() => {
                  // Tab changes are URL-driven. App owns the URL
                  // updater; we just bubble the new tab.
                  const u = new URL(window.location.href)
                  u.searchParams.set('tab', t)
                  window.history.replaceState(null, '', u.toString())
                  // Force a re-render via a custom event so App
                  // re-reads the URL. Alternative: lift a callback
                  // to App and call it. The custom event is
                  // simpler and keeps the API minimal.
                  window.dispatchEvent(new Event('popstate'))
                }}
                className={`flex-1 min-h-[36px] flex items-center justify-center gap-1 text-ios-caption transition ${
                  active
                    ? 'bg-bg-elevated text-accent'
                    : 'bg-bg-surface text-label-primary active:bg-bg-elevated'
                }`}
              >
                <TabIcon name={TAB_META[t].icon} active={active} />
                <span className="hidden sm:inline">{TAB_META[t].label}</span>
              </button>
            )
          })}
        </div>
        {/* Body. ``min-h-0`` lets the section actually scroll when
            the content overflows the viewport. Same pattern as the
            Drawer. */}
        <div className="flex-1 min-h-0 overflow-y-auto bg-bg-app pb-8">
          {tab === 'feeds' && (
            <div className="pt-4">
              <FeedManager
                sources={sources}
                onRefresh={onRefreshSources}
                onError={onError}
                onSourceRenamed={onSourceRenamed}
              />
            </div>
          )}
          {tab === 'llm' && (
            <div className="pt-4 px-4 space-y-4">
              <LLMSection
                llm={llm}
                llmError={llmError}
                onChange={setLlm}
                onRetry={() => {
                  setLlmError(null)
                  api
                    .llmStatus()
                    .then((s) => setLlm(s))
                    .catch((e) => setLlmError((e as Error).message))
                }}
              />
              <div>
                <h3 className="text-ios-caption uppercase tracking-wide text-label-tertiary mb-2">
                  Brief tone
                </h3>
                <div className="grid grid-cols-3 gap-0 rounded-ios overflow-hidden border border-hairline">
                  {(
                    [
                      { value: 'terse' as const, label: 'terse' },
                      { value: 'narrative' as const, label: 'narrative' },
                      { value: 'alert' as const, label: 'alert' },
                    ]
                  ).map((t) => {
                    const active = t.value === briefTone
                    return (
                      <button
                        key={t.value}
                        type="button"
                        onClick={() => onBriefToneChange(t.value)}
                        className={`min-h-[44px] text-ios-body font-normal transition ${
                          active
                            ? 'bg-bg-elevated text-accent'
                            : 'bg-bg-surface text-label-primary active:bg-bg-elevated'
                        }`}
                        aria-pressed={active}
                      >
                        {t.label}
                      </button>
                    )
                  })}
                </div>
              </div>
              <button
                onClick={() => {
                  void triggerGenerate(briefTone, (msg) => toast(msg, 'error'))
                }}
                disabled={generating || (llm !== null && !llm.configured)}
                className="w-full min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white text-ios-body font-medium"
              >
                {generating ? 'Generating brief…' : 'Generate brief now'}
              </button>
            </div>
          )}
          {tab === 'notifications' && (
            <div className="pt-4 px-4 space-y-3">
              {notifError ? (
                <div className="rounded-ios bg-red-500/15 border border-red-500/40 p-3">
                  <div className="text-ios-body text-red-200">Couldn't check status</div>
                  <div className="text-ios-caption text-red-300/80">
                    tap retry — {notifError}
                  </div>
                  <button
                    onClick={() => {
                      setNotifError(null)
                      api
                        .notificationStatus()
                        .then((s) => setNotif(s))
                        .catch((e) => setNotifError((e as Error).message))
                    }}
                    className="mt-2 text-ios-caption text-accent active:opacity-60"
                  >
                    Retry
                  </button>
                </div>
              ) : notif === null ? (
                <div className="text-ios-body text-label-secondary">checking…</div>
              ) : notif.configured ? (
                <div className="rounded-ios bg-emerald-500/10 border border-emerald-500/30 p-3">
                  <div className="text-ios-body text-emerald-300">Configured</div>
                  <div className="text-ios-caption text-emerald-300/80">
                    {notif.backend} · {notif.scheme}
                  </div>
                </div>
              ) : (
                <div className="rounded-ios bg-amber-500/10 border border-amber-500/30 p-3">
                  <div className="text-ios-body text-amber-300">Not configured</div>
                  <div className="text-ios-caption text-amber-300/80">
                    set APPRISE_URL or PUSHOVER_* in .env, then restart the backend
                  </div>
                </div>
              )}
              <p className="text-ios-caption text-label-secondary">
                Notifications are sent for high-priority brief sections (alerts).
                Set the URL in <code className="text-ios-caption">.env</code> on the host.
              </p>
            </div>
          )}
          {tab === 'hidden' && (
            <HiddenTabContent
              ids={hiddenEntries}
              onRestore={onRestoreHidden}
              onRestoreAll={onRestoreAllHidden}
            />
          )}

          {tab === 'starred' && (
            <StarredTabContent
              ids={starredEntries}
              onUnstar={onUnstarEntry}
              onClearAll={onUnstarAll}
            />
          )}

          {tab === 'history' && <HistoryTabContent onError={onError} />}

          {tab === 'reset' && (
            <div className="pt-4 px-4 space-y-3">
              <div className="rounded-ios bg-bg-surface border border-hairline p-3">
                <div className="text-ios-body text-label-primary">
                  Clear local state
                </div>
                <p className="text-ios-caption text-label-secondary mt-1">
                  Wipes read marks, column preferences, last-viewed timestamps, and
                  the last-selected mobile column. Server data (entries, sources,
                  briefs) is untouched — a refresh re-fetches everything.
                </p>
                <button
                  onClick={() => onResetLocalState()}
                  className="mt-3 w-full min-h-[44px] rounded-ios bg-red-500/15 border border-red-500/40 text-red-300 active:opacity-60"
                >
                  Clear local state
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// Tab icons. Inline SVGs to avoid a dep on an icon library. Each
// icon is 16x16 in a 20x20 viewBox so they sit visually centered in
// the 36px tab buttons.
function TabIcon({ name, active }: { name: string; active: boolean }) {
  const stroke = active ? 'currentColor' : 'currentColor'
  const sw = 1.5
  switch (name) {
    case 'feeds':
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 5h14M3 10h14M3 15h10" />
        </svg>
      )
    case 'llm':
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="5" y="6" width="10" height="8" rx="2" />
          <line x1="8" y1="3" x2="12" y2="3" />
          <line x1="8" y1="17" x2="12" y2="17" />
          <line x1="3" y1="10" x2="5" y2="10" />
          <line x1="15" y1="10" x2="17" y2="10" />
        </svg>
      )
    case 'bell':
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M10 3a5 5 0 0 0-5 5v3l-2 3h14l-2-3V8a5 5 0 0 0-5-5z" />
          <path d="M8 17a2 2 0 0 0 4 0" />
        </svg>
      )
    case 'reset':
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 10a7 7 0 1 1 2.05 4.95" />
          <polyline points="3 5 3 10 8 10" />
        </svg>
      )
    case 'history':
      // Lucide-style "history" tab icon. A clock face with
      // a counter-clockwise arrow around it \u2014 the standard
      // "what did I do" / "review" affordance. 1.5px
      // stroke, 16x16, matches the rest of the tab strip.
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 10a7 7 0 1 0 2-4.7" />
          <polyline points="3 3 3 6 6 6" />
          <polyline points="10 5 10 10 13 12" />
        </svg>
      )
    case 'eye-off':
      // Lucide-style "eye-off" tab icon. Open eye shape with a
      // diagonal strike through it — the standard "hidden" affordance.
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M2 10s3-6 8-6 8 6 8 6-3 6-8 6-8-6-8-6z" />
          <line x1="3" y1="3" x2="17" y2="17" />
        </svg>
      )
    case 'star':
      // Lucide-style star tab icon. Same outline pattern as the
      // rest of the tab strip (1.5px stroke). 5-pointed, the
      // standard "saved" affordance.
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <polygon points="10 2 12.5 7.5 18 8 14 12 15 18 10 15 5 18 6 12 2 8 7.5 7.5" />
        </svg>
      )
    default:

  }
}

// LLM section. Moved from Drawer.tsx, used inside the Settings
// overlay. The LLM config (provider + model) is the most complex
// piece of the Settings so it lives in its own function. The
// brief-tone picker and Generate button are now siblings, not
// nested.
function LLMSection({
  llm,
  llmError,
  onChange,
  onRetry,
}: {
  llm: LLMStatus | null
  llmError: string | null
  onChange: (next: LLMStatus) => void
  onRetry: () => void
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [tags, setTags] = useState<LLMTagsResponse | null>(null)
  const [scoringTags, setScoringTags] = useState<LLMTagsResponse | null>(null)
  const [provider, setProvider] = useState<string>('')
  const [modelBrief, setModelBrief] = useState<string>('')
  const [modelScoring, setModelScoring] = useState<string>('')
  const [freeTextBrief, setFreeTextBrief] = useState<string>('')
  const [useFreeText, setUseFreeText] = useState<boolean>(false)
  const [tagsError, setTagsError] = useState<string | null>(null)
  const [tagsLoading, setTagsLoading] = useState<boolean>(false)
  const [saving, setSaving] = useState<boolean>(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const openPicker = async () => {
    setPickerOpen(true)
    setSaveError(null)
    setTagsError(null)
    setTagsLoading(true)
    try {
      const s = await api.settings()
      const prov = providerForTagsFetch(s)
      if (prov) {
        const [tb, ts] = await Promise.all([
          api.llmTags(prov, false, 'brief'),
          api.llmTags(prov, false, 'scoring'),
        ])
        setTags(tb)
        setScoringTags(ts)
        applySettingsToForm(s, tb, ts)
      } else {
        setTags(null)
        setScoringTags(null)
        applySettingsToForm(s, null, null)
      }
    } catch (err) {
      setTagsError((err as Error).message)
    } finally {
      setTagsLoading(false)
    }
  }

  const providerForTagsFetch = (s: SettingsOut | null): string | null => {
    const pinned = s?.llm_provider || ''
    if (pinned === 'ollama_cloud' || pinned === '') return 'ollama_cloud'
    if (pinned === 'ollama') return 'ollama'
    return null
  }

  const applySettingsToForm = (
    s: SettingsOut,
    t: LLMTagsResponse | null,
    ts: LLMTagsResponse | null,
  ) => {
    setProvider(s.llm_provider || '')
    setModelBrief(s.llm_model_brief || '')
    setModelScoring(s.llm_model_scoring || '')
    const tagNames = t?.models?.map((m) => m.name) ?? []
    const isFreeText =
      Boolean(s.llm_model_brief) && tagNames.length > 0 && !tagNames.includes(s.llm_model_brief || '')
    setUseFreeText(isFreeText)
    setFreeTextBrief(isFreeText ? s.llm_model_brief || '' : '')
    void ts
  }

  const save = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      await api.updateLLMSettings({
        provider: provider,
        model_brief: useFreeText ? freeTextBrief : modelBrief,
        model_scoring: modelScoring,
      })
      const status = await api.llmStatus()
      onChange(status)
      setPickerOpen(false)
    } catch (err) {
      setSaveError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const tagOptions = useMemo(() => tags?.models ?? [], [tags])
  const tagNames = useMemo(() => tagOptions.map((m) => m.name), [tagOptions])
  const hasRecommendations = useMemo(
    () => tagOptions.some((m) => m.recommended),
    [tagOptions],
  )
  const scoringOptions = useMemo(() => scoringTags?.models ?? [], [scoringTags])
  const hasScoringRecommendations = useMemo(
    () => scoringOptions.some((m) => m.recommended),
    [scoringOptions],
  )

  return (
    <div className="rounded-ios bg-bg-surface border border-hairline p-3">
      <div className="flex items-center gap-2">
        {llmError ? (
          <button
            onClick={onRetry}
            className="flex-1 min-w-0 text-left text-red-400 active:bg-bg-elevated rounded-ios px-2 py-1"
          >
            <div className="text-ios-body truncate">Couldn't check</div>
            <div className="text-ios-caption text-label-secondary truncate">
              tap to retry
            </div>
          </button>
        ) : llm === null ? (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-label-primary truncate">LLM</div>
            <div className="text-ios-caption text-label-secondary truncate">checking…</div>
          </div>
        ) : llm.configured ? (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-label-primary truncate">
              {llm.backend}
            </div>
            <div className="text-ios-caption text-label-secondary truncate">
              {llm.model}
            </div>
          </div>
        ) : (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-amber-400 truncate">Not configured</div>
            <div className="text-ios-caption text-label-secondary truncate">
              set provider in env or pick one below
            </div>
          </div>
        )}
        <button
          onClick={pickerOpen ? () => setPickerOpen(false) : openPicker}
          className="shrink-0 text-ios-body text-accent active:opacity-60 px-2"
        >
          {pickerOpen ? 'Cancel' : 'Edit'}
        </button>
      </div>
      {pickerOpen && (
        <div className="mt-3 space-y-3">
          <div>
            <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
              Provider
            </label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
            >
              <option value="">— env default —</option>
              <option value="ollama_cloud">Ollama Cloud</option>
              <option value="ollama">Ollama (local)</option>
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="groq">Groq</option>
            </select>
          </div>
          <div>
            <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
              Brief model
            </label>
            {useFreeText ? (
              <input
                type="text"
                value={freeTextBrief}
                onChange={(e) => setFreeTextBrief(e.target.value)}
                placeholder="model name"
                className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
              />
            ) : (
              <select
                value={modelBrief}
                onChange={(e) => setModelBrief(e.target.value)}
                className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
              >
                <option value="">— env default —</option>
                {tagOptions.map((m) => (
                  <option key={m.name} value={m.name}>
                    {m.recommended ? '★ ' : ''}
                    {m.name}
                    {m.recommended_note ? ` (${m.recommended_note})` : ''}
                  </option>
                ))}
              </select>
            )}
            {tagNames.length > 0 && (
              <button
                type="button"
                onClick={() => setUseFreeText((v) => !v)}
                className="mt-1 text-ios-caption text-accent active:opacity-60"
              >
                {useFreeText ? 'pick from list' : 'type a name instead'}
              </button>
            )}
          </div>
          <div>
            <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
              Scoring model
            </label>
            <select
              value={modelScoring}
              onChange={(e) => setModelScoring(e.target.value)}
              className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
            >
              <option value="">— env default —</option>
              {scoringOptions.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.recommended ? '★ ' : ''}
                  {m.name}
                  {m.recommended_note ? ` (${m.recommended_note})` : ''}
                </option>
              ))}
            </select>
          </div>
          {tagsLoading && (
            <div className="text-ios-caption text-label-secondary">loading models…</div>
          )}
          {tagsError && (
            <div className="text-ios-caption text-red-400">{tagsError}</div>
          )}
          {saveError && (
            <div className="text-ios-caption text-red-400">{saveError}</div>
          )}
          <button
            onClick={save}
            disabled={saving}
            className="w-full min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white"
          >
            {saving ? 'saving…' : 'save'}
          </button>
        </div>
      )}
    </div>
  )
}





// History tab content. Fetches the user's recent engagement
// events and renders them as a chronological list grouped by
// polarity. Each row is clickable and opens the entry URL in
// a new tab.
//
// Fetches on mount; refreshes when the user navigates back to
// the tab (the ``key`` prop on the parent could enforce a
// remount, but for the MVP we just refetch when the user
// navigates to the tab via the URL param change).
function HistoryTabContent({ onError }: { onError: (msg: string) => void }) {
  type Item = {
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
  }

  // Group-by toggle. ``entry`` = one row per article (most
  // recent interaction wins); ``none`` = one row per
  // interaction. Backed by the backend's ``group_by`` query
  // param. The total count follows the same dedup so the
  // "Showing N of M" count is consistent.
  //
  // Server-backed via the preferences provider. The
  // provider's ``historyGroupBy`` is the source of truth
  // across all of the user's devices; a phone and a
  // laptop both see the same grouping choice.
  const { state: prefsState, setHistoryGroupBy } = usePreferences()
  const groupBy: HistoryGroupByValue = prefsState.historyGroupBy
  const setGroupBy = setHistoryGroupBy

  // The full list of items accumulated from paginated
  // fetches. Appends in place when the user hits "Show
  // more"; resets when ``groupBy`` changes.
  const [items, setItems] = useState<Item[] | null>(null)
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Page size. Matches the backend default; the backend
  // caps at 200. The frontend doesn't ask for more than
  // 200 in a single fetch.
  const PAGE_SIZE = 50

  // Fetch a page of history. Resets the list when
  // ``append`` is false (used on initial load + groupBy
  // change); appends when ``append`` is true (used for
  // "Show more"). The alive-ref guards against a slow
  // fetch racing a tab change / unmount.
  const fetchPage = (append: boolean) => {
    if (append) {
      setLoadingMore(true)
    } else {
      setLoading(true)
    }
    let alive = true
    setError(null)
    api
      .listRecentInteractions({
        limit: PAGE_SIZE,
        offset: append ? (items?.length ?? 0) : 0,
        groupBy,
      })
      .then((res) => {
        if (!alive) return
        setItems((prev) =>
          append ? [...(prev ?? []), ...res.items] : res.items,
        )
        setTotal(res.total)
        setHasMore(res.has_more)
        setLoading(false)
        setLoadingMore(false)
      })
      .catch((err: Error) => {
        if (!alive) return
        setError(err.message)
        setLoading(false)
        setLoadingMore(false)
        onError(err.message)
      })
    return () => {
      alive = false
    }
  }

  // Refetch on mount + when ``groupBy`` changes. The
  // dep array includes ``groupBy`` so the user sees
  // the new grouping without a tab change.
  useEffect(() => {
    const cleanup = fetchPage(false)
    return cleanup
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupBy, onError])

  // Infinite scroll. An invisible sentinel div at the
  // bottom of the list; when it scrolls into view
  // (IntersectionObserver fires ``isIntersecting=true``)
  // and there's more to load, we fetch the next page.
  //
  // Why IntersectionObserver over a scroll handler:
  //  1. No throttle / debounce needed \u2014 the browser
  //     calls us at the right time.
  //  2. No layout reads on the main thread \u2014 IO is
  //     off-thread.
  //  3. The cleanup function disconnects cleanly.
  //
  // The ``sentinelRef`` is on a div at the bottom of
  // the list. ``rootMargin: 200px`` means "fire when
  // the sentinel is within 200px of the viewport" \u2014
  // the next page starts loading before the user
  // reaches the bottom, so the scroll feels instant.
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return
    const el = sentinelRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting && hasMore && !loading && !loadingMore && !error) {
            // Trigger the next page. ``fetchPage(true)``
            // appends in place. We don't await \u2014 the
            // observer can fire again while we're loading,
            // the ``loadingMore`` guard prevents a double
            // fetch.
            void fetchPage(true)
          }
        }
      },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasMore, loading, loadingMore, error, items?.length])

  if (loading) {
    return (
      <div className="pt-4 px-4 text-ios-body text-label-secondary">
        loading history\u2026
      </div>
    )
  }
  if (error) {
    return (
      <div className="pt-4 px-4 text-ios-body text-red-400">
        failed to load history: {error}
      </div>
    )
  }
  if (!items || items.length === 0) {
    return (
      <div className="pt-4 px-4 space-y-3">
        <Controls groupBy={groupBy} onGroupByChange={setGroupBy} />
        <div className="rounded-ios bg-bg-surface border border-hairline p-3">
          <div className="text-ios-body text-label-primary">No history yet</div>
          <div className="text-ios-caption text-label-secondary mt-0.5">
            Mark a card as read (\u2713) or hide it (\u25ef with strike) and it will
            show up here. The History view is grouped by polarity \u2014
            reads are positive, hides are negative \u2014 so you can
            review what you engaged with.
          </div>
        </div>
      </div>
    )
  }

  // Group items by type into Read / Hidden / Saved buckets.
  // The "View" type fires on every card mark-read so the
  // Read group is the dense one. The "never" type fires on
  // eye-button hide so the Hidden group is smaller. The
  // "bookmark" type fires on star so the Saved group is
  // the small one.
  type Group = { label: string; color: string; items: Item[] }
  const read: Item[] = items.filter((i) => i.type === 'view')
  const hidden: Item[] = items.filter((i) => i.type === 'never')
  const saved: Item[] = items.filter((i) => i.type === 'bookmark')
  const groups: Group[] = [
    { label: 'Read', color: 'text-green-500', items: read },
    { label: 'Hidden', color: 'text-red-400', items: hidden },
    { label: 'Saved', color: 'text-amber-400', items: saved },
  ].filter((g) => g.items.length > 0)

  return (
    <div className="pt-4 px-4 space-y-3">
      <Controls groupBy={groupBy} onGroupByChange={setGroupBy} />
      <div className="text-ios-caption text-label-secondary">
        Showing {items.length} of {total}
      </div>
      {groups.map((g) => (
        <div key={g.label} className="space-y-2">
          <div
            className={`text-ios-caption uppercase tracking-wide ${g.color} font-semibold`}
          >
            {g.label} ({g.items.length})
          </div>
          {groupByDate(g.items).map((bucket) => (
            <div key={bucket.label} className="space-y-1">
              <div className="text-ios-caption text-label-tertiary pl-1">
                {bucket.label}
              </div>
              {bucket.items.map((i) => (
                <a
                  key={i.id}
                  href={i.entry_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block rounded-ios bg-bg-surface border border-hairline p-2.5
                             hover:bg-bg-elevated active:opacity-60 transition-colors"
                >
                  <div className="flex items-start gap-2">
                    <span className={`shrink-0 mt-0.5 text-ios-caption ${g.color}`} aria-hidden="true">
                      {g.label === 'Read' ? '\u2713' : g.label === 'Hidden' ? '\u25ef with strike' : '\u2606'}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-ios-body text-label-primary line-clamp-2">
                        {i.entry_title}
                      </div>
                      <div className="text-ios-caption text-label-secondary mt-0.5 flex items-center gap-2">
                        <span>{i.source_name}</span>
                        <span aria-hidden="true">\u00b7</span>
                        <span title={i.created_at}>{timeAgo(i.created_at)}</span>
                      </div>
                    </div>
                  </div>
                </a>
              ))}
            </div>
          ))}
        </div>
      ))}
      {/* Infinite-scroll sentinel + manual fallback. The
          sentinel is an invisible div that the
          IntersectionObserver watches. When it scrolls
          into view, the next page is fetched. The
          loading hint is a small text element (not a
          button) so the user can't accidentally tap
          it \u2014 they get the next page by scrolling
          alone. If IO is unsupported (very old
          browsers), the loading hint is still visible
          and the user can wait. We deliberately don't
          include a clickable "Show more" button here
          \u2014 the infinite-scroll observer is the
          primary path. A small, visually subtle hint
          ("loading\u2026" or "X more remaining") is
          enough to communicate the state. */}
      {hasMore && (
        <div
          ref={sentinelRef}
          className="text-ios-caption text-label-secondary text-center py-3"
          aria-live="polite"
        >
          {loadingMore
            ? 'loading more\u2026'
            : `${total - items.length} more \u2014 scroll to load`}
        </div>
      )}
    </div>
  )
}

// Controls strip: a small two-pill toggle for the group-by
// mode. iOS-style: pill-shaped, the active option is filled
// with the accent color. ``min-h-[28px]`` keeps the touch
// target large enough.
function Controls({
  groupBy,
  onGroupByChange,
}: {
  groupBy: 'none' | 'entry'
  onGroupByChange: (g: 'none' | 'entry') => void
}) {
  return (
    <div className="flex gap-1" role="group" aria-label="group history by">
      <button
        onClick={() => onGroupByChange('entry')}
        aria-pressed={groupBy === 'entry'}
        className={`flex-1 rounded-full px-3 py-1.5 min-h-[28px] text-ios-caption
                    ${groupBy === 'entry' ? 'bg-accent text-white' : 'bg-bg-surface text-label-primary border border-hairline'}`}
      >
        By entry
      </button>
      <button
        onClick={() => onGroupByChange('none')}
        aria-pressed={groupBy === 'none'}
        className={`flex-1 rounded-full px-3 py-1.5 min-h-[28px] text-ios-caption
                    ${groupBy === 'none' ? 'bg-accent text-white' : 'bg-bg-surface text-label-primary border border-hairline'}`}
      >
        All events
      </button>
    </div>
  )
}

// Bucket items into Today / Yesterday / This week / Earlier
// by their ``created_at``. Sorts each bucket by recency
// (most recent first). The labels are short and human-
// readable; the bucket boundaries are computed in the
// user's local timezone (Date.now() and new Date() both
// use the local TZ) so the labels match what the user
// sees on the clock.
type DateBucket = { label: string; items: HistoryItem[] }
type HistoryItem = {
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
}
function groupByDate(items: HistoryItem[]): DateBucket[] {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000
  // Start of the current week: treat Monday as the
  // boundary. The user is reviewing "this week" so the
  // standard Monday-boundary is fine; some users prefer
  // Sunday \u2014 easy to swap here.
  const startOfWeek = (() => {
    const d = new Date(now)
    const day = d.getDay() // 0 = Sunday, 1 = Monday
    const diff = (day === 0 ? 6 : day - 1) // distance from Monday
    d.setDate(d.getDate() - diff)
    d.setHours(0, 0, 0, 0)
    return d.getTime()
  })()
  const buckets: Record<string, HistoryItem[]> = {
    Today: [],
    Yesterday: [],
    'This week': [],
    Earlier: [],
  }
  for (const it of items) {
    const ms = new Date(it.created_at).getTime()
    if (ms >= startOfToday) buckets.Today.push(it)
    else if (ms >= startOfYesterday) buckets.Yesterday.push(it)
    else if (ms >= startOfWeek) buckets['This week'].push(it)
    else buckets.Earlier.push(it)
  }
  return (
    ['Today', 'Yesterday', 'This week', 'Earlier'] as const
  )
    .filter((label) => buckets[label].length > 0)
    .map((label) => ({ label, items: buckets[label] }))
}


// ``timeAgo`` for the History rows. Duplicates the helper
// already used by BriefCard and Card \u2014 not worth the
// module-level indirection for a 10-line function. Returns
// a relative time string like "5m ago" or "2d ago".
function timeAgo(iso: string): string {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d ago`
  const mo = Math.floor(d / 30)
  if (mo < 12) return `${mo}mo ago`
  const y = Math.floor(mo / 12)
  return `${y}y ago`
}





// =============================================================================
// Hidden + Starred list components
// =============================================================================
//
// Both tabs show a list of entries (looked up by id from
// the backend) with a per-row action button. The
// shared shape keeps the two tabs visually consistent:
//   - Header card with count + bulk action
//   - List of rows below, each with title, source
//     name, time-ago timestamp, and a per-row action
//   - Empty state with hint text
//
// The fetch is keyed by the join of ids so a stable
// id set (re-rendering) doesn't re-fetch. Different
// id sets (tab switch with new hides / stars) trigger
// a new fetch. The fetch is also called on the
// overlay open (via the parent's effect that mounts
// these components).

interface EntryIdListContentProps {
  // Action label shown on the per-row button.
  actionLabel: string
  // Header card subtitle. Shown when the list is
  // empty.
  emptyHint: string
  // Header card subtitle. Shown when the list has
  // entries.
  populatedHint: string
  // Bulk action label (the button in the header).
  bulkLabel: string
  onBulkAction: () => void
  // Per-row action.
  onRowAction: (entryId: number) => void
  // Singular / plural noun for the entry count.
  countNoun: string
  // Loaded entries from the backend.
  loaded: Array<{
    id: number
    title: string
    url: string
    source_name: string
    published_at: string | null
  }>
  loading: boolean
  loadError: string | null
}

function EntryIdListContent({
  actionLabel,
  emptyHint,
  populatedHint,
  bulkLabel,
  onBulkAction,
  onRowAction,
  countNoun,
  loaded,
  loading,
  loadError,
}: EntryIdListContentProps) {
  return (
    <div className="pt-4 px-4 space-y-3">
      <div className="rounded-ios bg-bg-surface border border-hairline p-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-ios-body text-label-primary">
              {loaded.length === 0 && !loading
                ? `No ${countNoun}`
                : `${loaded.length} ${countNoun}`}
            </div>
            <div className="text-ios-caption text-label-secondary mt-0.5">
              {loaded.length === 0 && !loading ? emptyHint : populatedHint}
            </div>
          </div>
          {loaded.length > 0 && (
            <button
              onClick={onBulkAction}
              className="text-ios-body text-accent active:opacity-60 shrink-0"
              aria-label={bulkLabel}
            >
              {bulkLabel}
            </button>
          )}
        </div>
      </div>
      {loadError && (
        <div className="rounded-ios bg-red-500/10 border border-red-500/30 p-3 text-ios-caption text-red-400">
          Couldn’t load the list: {loadError}
        </div>
      )}
      {loading && loaded.length === 0 && (
        <div className="rounded-ios bg-bg-surface border border-hairline p-3 text-ios-caption text-label-secondary">
          Loading…
        </div>
      )}
      {loaded.length > 0 && (
        <div className="rounded-ios bg-bg-surface border border-hairline divide-y divide-hairline">
          {loaded.map((e) => (
            <div
              key={e.id}
              className="flex items-start gap-3 p-3"
            >
              <div className="flex-1 min-w-0">
                <a
                  href={e.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block text-ios-body text-label-primary active:opacity-60 truncate"
                  title={e.title}
                >
                  {e.title}
                </a>
                <div className="text-ios-caption text-label-tertiary mt-0.5 truncate">
                  {e.source_name}
                  {e.published_at && (
                    <> · {timeAgo(e.published_at)}</>
                  )}
                </div>
              </div>
              <button
                onClick={() => onRowAction(e.id)}
                className="text-ios-caption text-accent active:opacity-60 shrink-0 self-center"
                aria-label={`${actionLabel}: ${e.title}`}
              >
                {actionLabel}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Thin wrapper for the Hidden tab. Fetches the
// hidden entries by id, then delegates the
// rendering to EntryIdListContent.
function HiddenTabContent({
  ids,
  onRestore,
  onRestoreAll,
}: {
  ids: number[]
  onRestore: (entryId: number) => void
  onRestoreAll: () => void
}) {
  const [loaded, setLoaded] = useState<EntryIdListContentProps['loaded']>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  useEffect(() => {
    if (ids.length === 0) {
      setLoaded([])
      setLoading(false)
      setLoadError(null)
      return
    }
    let alive = true
    setLoading(true)
    setLoadError(null)
    api
      .entriesByIds(ids)
      .then((rows) => {
        if (!alive) return
        setLoaded(
          rows.map((r) => ({
            id: r.id,
            title: r.title,
            url: r.url,
            source_name: r.source_name,
            published_at: r.published_at,
          })),
        )
      })
      .catch((e: Error) => {
        if (!alive) return
        setLoadError(e.message)
      })
      .finally(() => {
        if (!alive) return
        setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [ids.join(',')])
  return (
    <EntryIdListContent
      actionLabel="Restore"
      emptyHint="Right-click a card → Hide this entry (or press h) to dismiss it. Hidden entries stay in the database but don’t show on the dashboard."
      populatedHint="Tap Restore to show an entry on the dashboard again."
      bulkLabel="Restore all"
      onBulkAction={onRestoreAll}
      onRowAction={onRestore}
      countNoun={ids.length === 1 ? 'hidden entry' : 'hidden entries'}
      loaded={loaded}
      loading={loading}
      loadError={loadError}
    />
  )
}

// Thin wrapper for the Starred tab. Same shape as
// HiddenTabContent, different copy and a destructive
// (red) row action label.
function StarredTabContent({
  ids,
  onUnstar,
  onClearAll,
}: {
  ids: number[]
  onUnstar: (entryId: number) => void
  onClearAll: () => void
}) {
  const [loaded, setLoaded] = useState<EntryIdListContentProps['loaded']>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  useEffect(() => {
    if (ids.length === 0) {
      setLoaded([])
      setLoading(false)
      setLoadError(null)
      return
    }
    let alive = true
    setLoading(true)
    setLoadError(null)
    api
      .entriesByIds(ids)
      .then((rows) => {
        if (!alive) return
        setLoaded(
          rows.map((r) => ({
            id: r.id,
            title: r.title,
            url: r.url,
            source_name: r.source_name,
            published_at: r.published_at,
          })),
        )
      })
      .catch((e: Error) => {
        if (!alive) return
        setLoadError(e.message)
      })
      .finally(() => {
        if (!alive) return
        setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [ids.join(',')])
  return (
    <EntryIdListContent
      actionLabel="Remove"
      emptyHint="Right-click a card → Save for later (or press b) to bookmark an entry. Saved items surface in the Saved column at the top of the dashboard."
      populatedHint="The Saved column at the top of the dashboard shows your bookmarks, most recent first. Remove drops one star; Clear all drops every star."
      bulkLabel="Clear all"
      onBulkAction={onClearAll}
      onRowAction={onUnstar}
      countNoun={ids.length === 1 ? 'saved entry' : 'saved entries'}
      loaded={loaded}
      loading={loading}
      loadError={loadError}
    />
  )
}

