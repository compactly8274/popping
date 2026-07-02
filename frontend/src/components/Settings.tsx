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

import { useEffect, useMemo, useState } from 'react'
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

export type SettingsTab = 'feeds' | 'llm' | 'notifications' | 'hidden' | 'starred' | 'reset'

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
}

const TAB_META: Record<SettingsTab, { label: string; icon: string }> = {
  feeds: { label: 'Feeds', icon: 'feeds' },
  llm: { label: 'LLM', icon: 'llm' },
  notifications: { label: 'Notifications', icon: 'bell' },
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
            Settings
          </h2>
        </div>
        {/* Tab strip. iOS segmented control style — same 4-cell
            grid the Drawer's tone picker uses, but extended to
            cover all settings tabs. Active tab uses bg-bg-elevated
            + text-accent; inactive is bg-bg-surface + text-label-
            primary. ``role="tablist"`` so screen readers
            announce it as a tab group. */}
        <div className="flex gap-1 mx-4 mt-3 rounded-ios overflow-hidden border border-hairline" role="tablist">
          {(['feeds', 'llm', 'notifications', 'hidden', 'starred', 'reset'] as SettingsTab[]).map((t) => {
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
            // No GroupedSection/GroupedRow in this file â the
            // primitives live in Drawer.tsx and we don't import them
            // (the Settings overlay's iOS look is a flat list, not
            // the grouped-cards style of the Drawer). Inline the
            // markup that fits the surrounding sections (rounded
            // card, label-uppercase header, row dividers).
            <div className="pt-4 px-4 space-y-3">
              <div className="rounded-ios bg-bg-surface border border-hairline p-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-ios-body text-label-primary">
                      {hiddenEntries.length === 0
                        ? 'No hidden entries'
                        : `${hiddenEntries.length} hidden ${hiddenEntries.length === 1 ? 'entry' : 'entries'}`}
                    </div>
                    <div className="text-ios-caption text-label-secondary mt-0.5">
                      {hiddenEntries.length === 0
                        ? 'Right-click a card â Hide this entry to dismiss it. Hidden entries stay in the database but donât show on the dashboard.'
                        : 'Tap Restore to show on the dashboard again.'}
                    </div>
                  </div>
                  {hiddenEntries.length > 0 && (
                    <button
                      onClick={onRestoreAllHidden}
                      className="text-ios-body text-accent active:opacity-60 shrink-0"
                      aria-label="restore all hidden entries"
                    >
                      Restore all
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          {tab === 'starred' && (
            // No GroupedSection/GroupedRow in this file — same
            // inlined-markup pattern as the hidden tab. Future
            // improvement: per-entry title display so the user
            // can see what they saved (would need an entries
            // fetch keyed by id).
            <div className="pt-4 px-4 space-y-3">
              <div className="rounded-ios bg-bg-surface border border-hairline p-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-ios-body text-label-primary">
                      {starredEntries.length === 0
                        ? 'No saved entries'
                        : `${starredEntries.length} saved ${starredEntries.length === 1 ? 'entry' : 'entries'}`}
                    </div>
                    <div className="text-ios-caption text-label-secondary mt-0.5">
                      {starredEntries.length === 0
                        ? 'Right-click a card → Save for later (or press s) to bookmark an entry. Saved items surface in the Saved column at the top of the dashboard.'
                        : 'The Saved column at the top of the dashboard shows your bookmarks, most recent first. Clear all removes every star.'}
                    </div>
                  </div>
                  {starredEntries.length > 0 && (
                    <button
                      onClick={onUnstarAll}
                      className="text-ios-body text-red-400 active:opacity-60 shrink-0"
                      aria-label="clear all saved entries"
                    >
                      Clear all
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

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




