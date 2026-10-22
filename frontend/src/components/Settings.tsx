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
//   Reset          — moved from Drawer. Reset reading state, and the
//                    build this page runs (PR1r).
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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  api,
  safeExternalUrl,
  type LLMSettingsUpdate,
  type LLMTagsResponse,
  type LLMStatus,
  type NotificationStatus,
  type SettingsOut,
  type Source,
} from '../api'
import { FeedManager } from './FeedManager'
import { SETTINGS_TABS, type SettingsTab } from '../lib/settingsTabs'
import { flushEditors, setPageHiding, type SaveState } from '../lib/sourceSaves'
import { errorMessage } from '../lib/apiErrors'
import { FieldError, SaveStatus } from './feeds/SaveStatus'
import { useMediaQuery, useVisualViewport } from '../lib/visualViewport'
import { toast } from './Toast'
import { sourceLabel } from '../lib/labels'
import { usePreferences, type HistoryGroupByValue } from '../lib/preferences'
import { formatBuildTime, shortId } from '../lib/buildId'
import { liveBuildId } from '../lib/useUpdateCheck'

export type { SettingsTab }

type Props = {
  open: boolean
  // The active tab. App owns this (driven by the URL param) so the
  // overlay can be re-rendered with the right tab on deep links.
  tab: SettingsTab
  // Open the source list (passed to FeedManager so the "Add custom"
  // path can list existing names client-side).
  sources: Source[]
  onRefreshSources: () => Promise<void>
  onClose: () => void
  // Open this feed's row in Feeds (the Menu's ›, a ?source= link).
  // ``focusNonce`` changes on every request, so asking for the same
  // feed again while Settings is open still scrolls to it.
  focusSourceId?: number | null
  focusNonce?: number
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
  // A feed was added or deleted in Feeds, so App shows the change at
  // once; and the Edit action on an "Added …" toast.
  onSourceAdded?: (row: Source) => void
  onSourceDeleted?: (id: number) => void
  onEditSource?: (id: number) => void
  // Reset hooks. Wipes local namespaced keys and the reading-state
  // preferences (saves, hides and saved views stay); App owns the
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
  // List of currently-downvoted, not-yet-hidden entry ids. Shown on
  // the Voted tab so the user can review what they've downvoted and
  // clear the backlog in one action — separate from the vote itself
  // (voting is a pure taste signal, no view-state side effect;
  // hiding is a distinct, deliberate "get this off my screen" action
  // the user opts into from here, not something a downvote does
  // automatically).
  downvotedEntries: number[]
  onHideDownvoted: (entryId: number) => void
  onHideAllDownvoted: () => void
}

const TAB_META: Record<SettingsTab, { label: string; icon: string }> = {
  feeds: { label: 'Feeds', icon: 'feeds' },
  llm: { label: 'AI', icon: 'llm' },
  notifications: { label: 'Notifications', icon: 'bell' },
  history: { label: 'History', icon: 'history' },
  hidden: { label: 'Hidden', icon: 'eye-off' },
  starred: { label: 'Saved', icon: 'star' },
  voted: { label: 'Downvoted', icon: 'arrow-down' },
  reset: { label: 'Reset', icon: 'reset' },
}

export function Settings({
  open,
  tab,
  sources,
  onRefreshSources,
  onClose,
  focusSourceId,
  focusNonce,
  briefTone,
  onBriefToneChange,
  triggerGenerate,
  generating,
  onSourceAdded,
  onSourceDeleted,
  onEditSource,
  onResetLocalState,
  hiddenEntries,
  onRestoreHidden,
  onRestoreAllHidden,
  starredEntries,
  onUnstarAll,
  onUnstarEntry,
  downvotedEntries,
  onHideDownvoted,
  onHideAllDownvoted,
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
      .catch((e) => alive && setNotifError(errorMessage(e)))
    api
      .llmStatus()
      .then((s) => alive && setLlm(s))
      .catch((e) => alive && setLlmError(errorMessage(e)))
    return () => {
      alive = false
    }
  }, [open])

  // Reset's inline confirm; it starts over each time the tab opens.
  const [confirmReset, setConfirmReset] = useState(false)
  useEffect(() => {
    if (tab !== 'reset') setConfirmReset(false)
  }, [tab])

  // Errors inside Settings show as a toast over the sheet; App's banner
  // sits behind it, where nobody would see it.
  const reportError = useCallback((m: string) => toast(m, 'error'), [])

  // Done, the backdrop and Esc: the blur commits a text field being
  // edited, flushEditors commits any other unsaved draft, and the sheet
  // closes at once. Saves carry on after it has gone.
  const handleClose = useCallback(() => {
    ;(document.activeElement as HTMLElement | null)?.blur()
    flushEditors()
    onClose()
  }, [onClose])

  // Esc closes. Mirrors the Drawer's pattern; App's keyboard handler
  // also listens for Esc on the search input but this listener
  // captures first when the overlay is open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        e.preventDefault()
        handleClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, handleClose])

  // Switching apps mid-edit: commit what is being typed and send it at
  // once with keepalive. setPageHiding comes first so these commits go
  // straight out, whichever of App's listeners and these runs first.
  useEffect(() => {
    if (!open) return
    const hide = () => {
      setPageHiding(true)
      ;(document.activeElement as HTMLElement | null)?.blur()
      flushEditors()
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') hide()
    }
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', hide)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', hide)
    }
  }, [open])

  // The on-screen keyboard on a phone: the sheet shrinks to the visible
  // area, so Done and the focused field stay above the keyboard. The
  // desktop panel ignores this.
  const viewport = useVisualViewport()
  const phone = useMediaQuery('(max-width: 767px)')
  const keyboard = open && phone && viewport.keyboardOpen
  const panelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!keyboard) return
    const el = document.activeElement
    if (el instanceof HTMLElement && panelRef.current?.contains(el)) el.scrollIntoView({ block: 'nearest' })
  }, [keyboard, viewport.height, viewport.offsetTop])

  if (!open) return null

  const panelStyle = keyboard
    ? {
        top: `calc(env(safe-area-inset-top) + 0.75rem + ${viewport.offsetTop}px)`,
        bottom: `${Math.max(0, document.documentElement.clientHeight - (viewport.offsetTop + viewport.height))}px`,
      }
    : undefined

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="settings"
      className="fixed inset-0 z-50 bg-black/70 supports-[backdrop-filter]:backdrop-blur-sm animate-fade-in"
      onClick={(e) => {
        // Backdrop tap closes. The inner card has stopPropagation so
        // taps inside it don't close.
        if (e.target === e.currentTarget) handleClose()
      }}
    >
      <div
        ref={panelRef}
        style={panelStyle}
        // Right-side panel on desktop, full-viewport sheet on mobile.
        // The mobile sheet is pinned by its top edge, not a vh height:
        // iOS Safari's vh counts the space behind its toolbar, so a
        // 92vh sheet ran off the top of the visible screen.
        // The desktop panel is wider than the Drawer (480px) because
        // the LLM tab needs room for the provider/model pickers and
        // the Feeds tab's add-custom form has a wide textarea.
        // Side insets keep the sheet's content clear of the notch in
        // landscape; they are 0 in portrait.
        className="absolute inset-x-0 bottom-0 top-[calc(env(safe-area-inset-top)+0.75rem)] rounded-t-ios-lg bg-bg-app shadow-2xl flex flex-col
                   pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)]
                   motion-safe:animate-sheet-up md:animate-none
                   md:inset-y-0 md:right-0 md:left-auto md:top-0 md:bottom-auto md:h-full md:w-[480px] md:rounded-none"
      >
        {/* Header — iOS sheet layout: title on the leading edge, Done
            on the trailing edge. The title gives way (truncates) before
            Done ever shrinks. The selected tab pill already says which
            tab is open, so the title is always just "Settings". */}
        <div className="flex items-center justify-between gap-3 px-4 pt-3 pb-3 md:pt-5 md:pb-4 border-b border-hairline shrink-0">
          <h2 className="min-w-0 flex-1 truncate text-2xl md:text-ios-large-title font-bold text-label-primary tracking-tight">
            Settings
          </h2>
          <button
            onClick={handleClose}
            aria-label="close settings"
            className="shrink-0 min-h-[44px] min-w-[44px] px-2 flex items-center justify-center rounded-ios text-ios-body font-semibold text-accent active:bg-bg-elevated"
          >
            Done
          </button>
        </div>
        {/* Tab strip: a scrolling row of labelled pills, the same
            shape as the dashboard's category chips. Eight icon-only
            cells didn't fit a phone with their labels, and icons
            alone left the reader guessing. ``role="tablist"`` so
            screen readers announce it as a tab group. */}
        <div data-hscroll className="flex gap-1.5 overflow-x-auto px-4 mt-3 pb-1" role="tablist">
          {SETTINGS_TABS.map((t) => {
            const active = t === tab
            return (
              <button
                key={t}
                role="tab"
                aria-selected={active}
                onClick={() => {
                  // Tab changes are URL-driven. App owns the URL
                  // updater; we just bubble the new tab. The history
                  // state is kept: App's Done uses it to step back
                  // instead of adding an entry.
                  const u = new URL(window.location.href)
                  u.searchParams.set('tab', t)
                  u.searchParams.delete('source')
                  window.history.replaceState(window.history.state, '', u.toString())
                  // Force a re-render via a custom event so App
                  // re-reads the URL. Alternative: lift a callback
                  // to App and call it. The custom event is
                  // simpler and keeps the API minimal.
                  window.dispatchEvent(new Event('popstate'))
                }}
                className={`shrink-0 min-h-[44px] flex items-center gap-1.5 rounded-full px-3 text-ios-caption font-medium whitespace-nowrap transition ${
                  active
                    ? 'bg-accent text-white'
                    : 'bg-bg-surface text-label-secondary active:bg-bg-elevated'
                }`}
              >
                <TabIcon name={TAB_META[t].icon} active={active} />
                <span>{TAB_META[t].label}</span>
              </button>
            )
          })}
        </div>
        {/* Body. ``min-h-0`` lets the section actually scroll when
            the content overflows the viewport. Same pattern as the
            Drawer. */}
        <div
          className={`flex-1 min-h-0 overflow-y-auto overscroll-contain bg-bg-app ${
            keyboard ? 'pb-8' : 'pb-[calc(2rem+env(safe-area-inset-bottom))]'
          }`}
        >
          {tab === 'feeds' && (
            <div className="pt-4 px-4">
              <FeedManager
                sources={sources}
                onRefresh={onRefreshSources}
                onError={reportError}
                focusSourceId={focusSourceId}
                focusNonce={focusNonce}
                onSourceAdded={onSourceAdded}
                onSourceDeleted={onSourceDeleted}
                onEditSource={onEditSource}
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
                    .catch((e) => setLlmError(errorMessage(e)))
                }}
              />
              <div>
                <h3 className="text-ios-caption uppercase tracking-wide text-label-tertiary mb-2">
                  Brief tone
                </h3>
                <div className="grid grid-cols-3 gap-0 rounded-ios overflow-hidden border border-hairline">
                  {(
                    [
                      { value: 'terse' as const, label: 'Short' },
                      { value: 'narrative' as const, label: 'Story' },
                      { value: 'alert' as const, label: 'Alerts' },
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
                  <div className="text-ios-body text-red-200">Couldn't check notifications</div>
                  <div className="text-ios-caption text-red-300/80 break-words">{notifError}</div>
                  <button
                    onClick={() => {
                      setNotifError(null)
                      api
                        .notificationStatus()
                        .then((s) => setNotif(s))
                        .catch((e) => setNotifError(errorMessage(e)))
                    }}
                    className="mt-2 min-h-[44px] px-3 rounded-ios border border-hairline text-ios-body text-accent active:opacity-60"
                  >
                    Try again
                  </button>
                </div>
              ) : notif === null ? (
                <div className="text-ios-body text-label-secondary">Checking…</div>
              ) : notif.configured ? (
                <div className="rounded-ios bg-emerald-500/10 border border-emerald-500/30 p-3">
                  <div className="text-ios-body text-emerald-300">On</div>
                  <div className="text-ios-caption text-emerald-300/80">
                    {notif.backend} · {notif.scheme}
                  </div>
                </div>
              ) : (
                <div className="rounded-ios bg-amber-500/10 border border-amber-500/30 p-3">
                  <div className="text-ios-body text-amber-300">Off</div>
                  <div className="text-ios-caption text-amber-300/80">
                    Push notifications are off. They are turned on in the server&apos;s settings file; see
                    “Notifications” in the README.
                  </div>
                </div>
              )}
              <p className="text-ios-caption text-label-secondary">
                When they&apos;re on, Popping sends one for the alert sections of each new brief.
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

          {tab === 'voted' && (
            <DownvotedTabContent
              ids={downvotedEntries}
              onHide={onHideDownvoted}
              onHideAll={onHideAllDownvoted}
            />
          )}

          {tab === 'history' && <HistoryTabContent onError={reportError} />}

          {tab === 'reset' && (
            <div className="pt-4 px-4 space-y-3">
              <div className="rounded-ios bg-bg-surface border border-hairline p-3">
                <div className="text-ios-body text-label-primary">
                  Reset reading state
                </div>
                <p className="text-ios-caption text-label-secondary mt-1">
                  Clears read marks, each tab&apos;s sort and filter, last-visit times and folded
                  sections on all your devices, and this device&apos;s own settings. Saved and hidden
                  stories, votes, saved views, feeds and briefs stay.
                </p>
                {confirmReset ? (
                  <div className="mt-3 rounded-ios border border-red-500/40 p-3 space-y-2">
                    <p className="text-ios-body text-label-primary">
                      Reset reading state on all your devices? This can&apos;t be undone.
                    </p>
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => {
                          setConfirmReset(false)
                          onResetLocalState()
                        }}
                        className="min-h-[44px] px-3 rounded-ios bg-red-500 text-white text-ios-body active:opacity-80"
                      >
                        Reset
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmReset(false)}
                        className="min-h-[44px] px-3 rounded-ios border border-hairline text-label-primary text-ios-body active:opacity-60"
                      >
                        Keep
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirmReset(true)}
                    className="mt-3 w-full min-h-[44px] rounded-ios bg-red-500/15 border border-red-500/40 text-red-300 active:opacity-60"
                  >
                    Reset reading state
                  </button>
                )}
              </div>
              <BuildLine />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// Which build this page runs, e.g. "Build 3f9a1c2e · 19 Oct, 14:02", so
// a device can be checked against a deploy (PR1r). The id is the
// running page's, not the server's: a resumed page keeps its old build.
function BuildLine() {
  const line = useMemo(() => {
    const id = shortId(liveBuildId()) || 'dev'
    const built = formatBuildTime(__BUILD_TIME__)
    return built ? `Build ${id} · ${built}` : `Build ${id}`
  }, [])
  return <p className="px-1 text-ios-caption text-label-secondary">{line}</p>
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
    case 'arrow-down':
      // Down-arrow tab icon for the Voted tab — mirrors the vote
      // footer's downvote arrow (Card.tsx's ArrowDownIcon) so the
      // tab reads as "the downvote thing" at a glance.
      return (
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M10 4v12M4 10l6 6 6-6" />
        </svg>
      )
    default:

  }
}

// The four providers that take an API key, in the same order the
// provider <select> lists them (minus ollama, which needs no key).
// Field names line up with SettingsOut / LLMSettingsUpdate exactly —
// see api.ts.
type ApiKeyProvider = 'anthropic' | 'openai' | 'groq' | 'ollama_cloud'
const API_KEY_PROVIDERS: { id: ApiKeyProvider; label: string }[] = [
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'groq', label: 'Groq' },
  { id: 'ollama_cloud', label: 'Ollama Cloud' },
]
const emptyApiKeys: Record<ApiKeyProvider, string> = {
  anthropic: '',
  openai: '',
  groq: '',
  ollama_cloud: '',
}
const emptyApiKeySet: Record<ApiKeyProvider, boolean> = {
  anthropic: false,
  openai: false,
  groq: false,
  ollama_cloud: false,
}

// The AI tab's provider, models and keys. Every control saves on its
// own, like the feed editor: a provider or model saves when picked, a
// typed model name or key when the field is left. Each PUT carries
// only its own fields (routes/settings.py: a missing field is left
// alone), and they go out one at a time in the order made.
type AiField = 'provider' | 'model_brief' | 'model_scoring' | ApiKeyProvider
const OTHER = '__other__'
const isKeyProvider = (p: string): p is ApiKeyProvider => API_KEY_PROVIDERS.some((k) => k.id === p)
const keyLabel = (p: ApiKeyProvider) => API_KEY_PROVIDERS.find((k) => k.id === p)!.label
const keySetFrom = (s: SettingsOut): Record<ApiKeyProvider, boolean> => ({
  anthropic: s.anthropic_api_key_set,
  openai: s.openai_api_key_set,
  groq: s.groq_api_key_set,
  ollama_cloud: s.ollama_cloud_api_key_set,
})
const aiLabelClass = 'block text-ios-caption uppercase tracking-wide text-label-tertiary'
const aiFieldClass =
  'w-full min-w-0 min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary'
const aiSelectClass =
  'select-ios w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline pl-3 pr-8 text-ios-body text-label-primary'
const IDLE_STATE: SaveState = { status: 'idle' }

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
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  // What the server has saved ('' = the server's settings file).
  const [provider, setProvider] = useState('')
  const [modelBrief, setModelBrief] = useState('')
  const [modelScoring, setModelScoring] = useState('')
  const [tags, setTags] = useState<LLMTagsResponse | null>(null)
  const [scoringTags, setScoringTags] = useState<LLMTagsResponse | null>(null)
  const [tagsError, setTagsError] = useState<string | null>(null)
  const [tagsLoading, setTagsLoading] = useState(false)
  // A typed model name: "Other…" in a list, or the only way to name a
  // model for providers without a list. null = not typing one.
  const [briefDraft, setBriefDraft] = useState<string | null>(null)
  const [scoringDraft, setScoringDraft] = useState<string | null>(null)
  // Keys are write-only: the server only says whether one is set.
  const [keySet, setKeySet] = useState<Record<ApiKeyProvider, boolean>>(emptyApiKeySet)
  const [keyDrafts, setKeyDrafts] = useState<Record<ApiKeyProvider, string>>(emptyApiKeys)
  const [keySaved, setKeySaved] = useState<Partial<Record<ApiKeyProvider, boolean>>>({})
  const [confirmRemove, setConfirmRemove] = useState<ApiKeyProvider | null>(null)
  // Set when the chosen provider still has no usable key.
  const [noKeyFor, setNoKeyFor] = useState<ApiKeyProvider | null>(null)
  const [states, setStates] = useState<Partial<Record<AiField, SaveState>>>({})
  const stateOf = (f: AiField) => states[f] ?? IDLE_STATE
  const setFieldState = (f: AiField, s: SaveState) => setStates((prev) => ({ ...prev, [f]: s }))

  // Bumped on every tags fetch so a slow response for a provider the
  // user has already switched away from can't overwrite the newer list.
  const tagsRequest = useRef(0)
  const fetchTags = async (pinned: string) => {
    const request = ++tagsRequest.current
    const prov = pinned === 'ollama' ? 'ollama' : pinned === 'ollama_cloud' || pinned === '' ? 'ollama_cloud' : null
    let tb: LLMTagsResponse | null = null
    let ts: LLMTagsResponse | null = null
    setTagsError(null)
    if (prov) {
      setTagsLoading(true)
      try {
        ;[tb, ts] = await Promise.all([api.llmTags(prov, false, 'brief'), api.llmTags(prov, false, 'scoring')])
      } catch (err) {
        if (request === tagsRequest.current) setTagsError(`Couldn't load the model list. ${errorMessage(err)}`)
      }
    }
    if (request === tagsRequest.current) {
      setTags(tb)
      setScoringTags(ts)
      setTagsLoading(false)
    }
  }

  // The controls are always shown: load what is saved when the tab opens.
  useEffect(() => {
    let alive = true
    api
      .settings()
      .then(async (s) => {
        if (!alive) return
        setProvider(s.llm_provider || '')
        setModelBrief(s.llm_model_brief || '')
        setModelScoring(s.llm_model_scoring || '')
        setKeySet(keySetFrom(s))
        setLoaded(true)
        await fetchTags(s.llm_provider || '')
      })
      .catch((err) => alive && setLoadError(errorMessage(err)))
    return () => {
      alive = false
    }
    // Once per opening of the tab.
  }, [])

  // One PUT at a time, in the order the changes were made.
  const queue = useRef<Promise<void>>(Promise.resolve())
  const savedTimers = useRef(new Map<AiField, ReturnType<typeof setTimeout>>())
  useEffect(() => () => savedTimers.current.forEach((t) => clearTimeout(t)), [])
  const save = (
    field: AiField,
    body: LLMSettingsUpdate,
    { after, undo }: { after?: (status: LLMStatus | null) => void; undo?: () => void } = {},
  ) => {
    setFieldState(field, { status: 'saving' })
    queue.current = queue.current.then(async () => {
      try {
        const s = await api.updateLLMSettings(body)
        setKeySet(keySetFrom(s))
        const status = await api.llmStatus().catch(() => null)
        if (status) onChange(status)
        after?.(status)
        setFieldState(field, { status: 'saved' })
        clearTimeout(savedTimers.current.get(field))
        savedTimers.current.set(
          field,
          setTimeout(() => setStates((prev) => (prev[field]?.status === 'saved' ? { ...prev, [field]: IDLE_STATE } : prev)), 1500),
        )
      } catch (err) {
        undo?.()
        // Never echo a key: the message is about the save, not the value.
        setFieldState(field, { status: 'error', message: errorMessage(err) })
      }
    })
  }

  // A provider change clears both models (names are provider-specific)
  // in the same PUT, for every provider, and reloads the model lists.
  const changeProvider = (next: string) => {
    const before = { provider, modelBrief, modelScoring }
    setProvider(next)
    setModelBrief('')
    setModelScoring('')
    setBriefDraft(null)
    setScoringDraft(null)
    setNoKeyFor(null)
    void fetchTags(next)
    save(
      'provider',
      { provider: next, model_brief: '', model_scoring: '' },
      {
        after: (status) => {
          if (isKeyProvider(next) && status && !status.configured) setNoKeyFor(next)
        },
        undo: () => {
          setProvider(before.provider)
          setModelBrief(before.modelBrief)
          setModelScoring(before.modelScoring)
          void fetchTags(before.provider)
        },
      },
    )
  }

  const saveModel = (kind: 'brief' | 'scoring', value: string) => {
    const field = kind === 'brief' ? 'model_brief' : 'model_scoring'
    const saved = kind === 'brief' ? modelBrief : modelScoring
    const set = kind === 'brief' ? setModelBrief : setModelScoring
    if (value === saved) return
    set(value)
    save(field, { [field]: value }, { undo: () => set(saved) })
  }

  // A typed key saves when the field is left; an empty or untouched
  // field sends nothing ("" would delete the key).
  const commitKey = (id: ApiKeyProvider) => {
    const value = keyDrafts[id].trim()
    if (!value) return
    setKeyDrafts((prev) => ({ ...prev, [id]: '' }))
    setKeySaved((prev) => ({ ...prev, [id]: false }))
    save(id, { [`${id}_api_key`]: value } as LLMSettingsUpdate, {
      after: (status) => {
        setKeySaved((prev) => ({ ...prev, [id]: true }))
        if (noKeyFor === id && status?.configured) setNoKeyFor(null)
      },
      undo: () => setKeyDrafts((prev) => (prev[id] ? prev : { ...prev, [id]: value })),
    })
  }
  const removeKey = (id: ApiKeyProvider) => {
    setConfirmRemove(null)
    setKeySaved((prev) => ({ ...prev, [id]: false }))
    save(id, { [`${id}_api_key`]: '' } as LLMSettingsUpdate)
  }

  const keyInput = (id: ApiKeyProvider, labelledBy?: string) => (
    <input
      id={`ai-key-${id}`}
      type="password"
      // Not the sign-in password: "new-password" and the password
      // managers' ignore flags keep iOS and browser autofill from
      // offering the saved login here, which would then be saved and
      // sent to the AI provider as a key.
      autoComplete="new-password"
      data-1p-ignore=""
      data-lpignore="true"
      data-bwignore=""
      data-form-type="other"
      autoCapitalize="none"
      autoCorrect="off"
      spellCheck={false}
      enterKeyHint="done"
      aria-label={labelledBy ? undefined : `${keyLabel(id)} key`}
      value={keyDrafts[id]}
      onChange={(e) => setKeyDrafts((prev) => ({ ...prev, [id]: e.target.value }))}
      onBlur={() => commitKey(id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          e.currentTarget.blur()
        }
      }}
      placeholder={keySet[id] ? 'Saved. Paste to replace.' : 'Not set'}
      className={aiFieldClass}
    />
  )
  const fieldError = (f: AiField) => {
    const s = stateOf(f)
    return s.status === 'error' && s.message ? <FieldError message={s.message} /> : null
  }

  // One model picker: a list when the provider has one (with a saved
  // name missing from it kept first, and Other…), else a text field.
  const modelPicker = (kind: 'brief' | 'scoring') => {
    const field = kind === 'brief' ? 'model_brief' : 'model_scoring'
    const saved = kind === 'brief' ? modelBrief : modelScoring
    const draft = kind === 'brief' ? briefDraft : scoringDraft
    const setDraft = kind === 'brief' ? setBriefDraft : setScoringDraft
    const options = (kind === 'brief' ? tags : scoringTags)?.models ?? []
    const id = `ai-${field}`
    const label = kind === 'brief' ? 'Brief model' : 'Story-ranking model'
    const hasList = options.length > 0
    const typing = !hasList || draft !== null
    const recommended = options.some((m) => m.recommended)
    return (
      <div>
        <div className="flex items-center justify-between gap-2 mb-1">
          <label htmlFor={id} className={aiLabelClass}>
            {label}
          </label>
          <SaveStatus state={stateOf(field)} />
        </div>
        {typing ? (
          <input
            id={id}
            type="text"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            enterKeyHint="done"
            value={draft ?? saved}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => {
              const v = e.currentTarget.value.trim()
              // From a list, an empty name means "not typing one".
              if (hasList && !v) return setDraft(null)
              if (draft !== null) saveModel(kind, v)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                e.currentTarget.blur()
              }
            }}
            placeholder={hasList ? 'Model name' : "Empty uses this provider's default"}
            className={aiFieldClass}
          />
        ) : (
          <select
            id={id}
            value={saved}
            onChange={(e) => {
              if (e.target.value === OTHER) {
                flushSync(() => setDraft(''))
                document.getElementById(id)?.focus()
                return
              }
              saveModel(kind, e.target.value)
            }}
            className={aiSelectClass}
          >
            {saved && !options.some((m) => m.name === saved) && <option value={saved}>{saved}</option>}
            <option value="">Provider default</option>
            {options.map((m) => (
              <option key={m.name} value={m.name}>
                {m.recommended ? '★ ' : ''}
                {m.name}
                {m.recommended_note ? ` (${m.recommended_note})` : ''}
              </option>
            ))}
            <option value={OTHER}>Other…</option>
          </select>
        )}
        {hasList && typing && (
          <button
            type="button"
            onClick={() => setDraft(null)}
            className="mt-1 min-h-[44px] px-1 text-ios-caption text-accent active:opacity-60"
          >
            Pick from the list
          </button>
        )}
        {recommended && !typing && (
          <p className="mt-1 text-ios-caption text-label-tertiary">★ marks models recommended for this task</p>
        )}
        {fieldError(field)}
      </div>
    )
  }

  const needsKeyHere = isKeyProvider(provider) && !keySet[provider] ? provider : null

  return (
    <div className="rounded-ios bg-bg-surface border border-hairline p-3 space-y-4">
      <div className="flex items-center gap-2">
        {llmError ? (
          <button
            onClick={onRetry}
            className="flex-1 min-w-0 min-h-[44px] text-left text-red-400 active:bg-bg-elevated rounded-ios px-2"
          >
            <div className="text-ios-body truncate">Couldn't check the AI model</div>
            <div className="text-ios-caption text-label-secondary truncate">Tap to try again</div>
          </button>
        ) : llm === null ? (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-label-primary truncate">AI model</div>
            <div className="text-ios-caption text-label-secondary truncate">Checking…</div>
          </div>
        ) : llm.configured ? (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-label-primary truncate">{llm.backend}</div>
            <div className="text-ios-caption text-label-secondary truncate">{llm.model}</div>
          </div>
        ) : (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-amber-400 truncate">Not set up</div>
            <div className="text-ios-caption text-label-secondary">Pick a provider below and add its key.</div>
          </div>
        )}
      </div>

      {loadError ? (
        <p className="text-ios-caption text-red-400 break-words">Couldn't load the AI settings. {loadError}</p>
      ) : !loaded ? (
        <p className="text-ios-caption text-label-secondary">Loading…</p>
      ) : (
        <>
          <div>
            <div className="flex items-center justify-between gap-2 mb-1">
              <label htmlFor="ai-provider" className={aiLabelClass}>
                Provider
              </label>
              <SaveStatus state={stateOf('provider')} />
            </div>
            <select
              id="ai-provider"
              value={provider}
              onChange={(e) => changeProvider(e.target.value)}
              className={aiSelectClass}
            >
              <option value="">Default</option>
              <option value="ollama_cloud">Ollama Cloud</option>
              <option value="ollama">Ollama (local)</option>
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="groq">Groq</option>
            </select>
            {fieldError('provider')}
          </div>

          {needsKeyHere && (
            <div>
              <div className="flex items-center justify-between gap-2 mb-1">
                <label htmlFor={`ai-key-${needsKeyHere}`} className={aiLabelClass}>
                  {keyLabel(needsKeyHere)} key
                </label>
                <SaveStatus state={stateOf(needsKeyHere)} />
              </div>
              {keyInput(needsKeyHere, 'label')}
              <p className="mt-1 text-ios-caption text-label-secondary">
                If your {keyLabel(needsKeyHere)} key isn&apos;t in the server&apos;s settings file, paste it here.
              </p>
              {noKeyFor === needsKeyHere && (
                <p className="mt-1 text-ios-caption text-amber-400">
                  Popping has no {keyLabel(needsKeyHere)} key yet. Paste one above.
                </p>
              )}
              {fieldError(needsKeyHere)}
            </div>
          )}

          {modelPicker('brief')}
          {modelPicker('scoring')}
          {tagsLoading && <p className="text-ios-caption text-label-secondary">Loading models…</p>}
          {tagsError && <p className="text-ios-caption text-red-400 break-words">{tagsError}</p>}

          <div>
            <h3 className={`${aiLabelClass} mb-1`}>API keys</h3>
            <p className="text-ios-caption text-label-secondary mb-2">
              Paste a key and it saves when you leave the field. Leave it empty to keep the current one.
            </p>
            <div className="space-y-3">
              {API_KEY_PROVIDERS.filter(({ id }) => id !== needsKeyHere).map(({ id, label }) => (
                <div key={id}>
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <label htmlFor={`ai-key-${id}`} className="text-ios-caption text-label-secondary">
                      {label}
                    </label>
                    <SaveStatus state={stateOf(id)} />
                  </div>
                  <div className="flex items-center gap-2">
                    {keyInput(id, 'label')}
                    {keySet[id] && confirmRemove !== id && (
                      <button
                        type="button"
                        onClick={() => setConfirmRemove(id)}
                        className="shrink-0 min-h-[44px] px-3 rounded-ios border border-hairline text-ios-body text-red-300 active:opacity-60"
                        aria-label={`remove the ${label} key`}
                      >
                        Remove
                      </button>
                    )}
                  </div>
                  {keySaved[id] && <p className="mt-1 text-ios-caption text-emerald-300">Key saved</p>}
                  {confirmRemove === id && (
                    <div className="mt-2 rounded-ios border border-hairline p-3 space-y-2">
                      <p className="text-ios-body text-label-primary">
                        Remove the {label} key? Popping falls back to the server&apos;s settings file.
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => removeKey(id)}
                          className="min-h-[44px] px-3 rounded-ios bg-red-500 text-white text-ios-body active:opacity-80"
                        >
                          Remove
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmRemove(null)}
                          className="min-h-[44px] px-3 rounded-ios border border-hairline text-label-primary text-ios-body active:opacity-60"
                        >
                          Keep
                        </button>
                      </div>
                    </div>
                  )}
                  {fieldError(id)}
                </div>
              ))}
            </div>
          </div>
        </>
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
        setError(errorMessage(err))
        setLoading(false)
        setLoadingMore(false)
        onError(`Couldn't load your history. ${errorMessage(err)}`)
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
        Loading history…
      </div>
    )
  }
  if (error) {
    return (
      <div className="pt-4 px-4 text-ios-body text-red-400 break-words">
        Couldn&apos;t load your history. {error}
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
            Stories you hide or save show up here.
          </div>
        </div>
      </div>
    )
  }

  // Group items by type into Hidden / Saved buckets. The
  // "never" type fires on hide, "bookmark" on save. Views
  // (reads) are no longer listed: the server leaves them out
  // unless asked for them, so a Read group would always be
  // empty.
  type Group = { label: string; color: string; items: Item[] }
  const hidden: Item[] = items.filter((i) => i.type === 'never')
  const saved: Item[] = items.filter((i) => i.type === 'bookmark')
  const groups: Group[] = [
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
                  href={safeExternalUrl(i.entry_url)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block rounded-ios bg-bg-surface border border-hairline p-2.5
                             hover:bg-bg-elevated active:opacity-60 transition-colors"
                >
                  <div className="flex items-start gap-2">
                    <span className={`shrink-0 mt-0.5 text-ios-caption ${g.color}`} aria-hidden="true">
                      {g.label === 'Hidden' ? <TabIcon name="eye-off" active={false} /> : '☆'}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-ios-body text-label-primary line-clamp-2 break-words">
                        {i.entry_title}
                      </div>
                      <div className="text-ios-caption text-label-secondary mt-0.5 flex items-center gap-2">
                        <span className="min-w-0 truncate">{sourceLabel(i.source_name)}</span>
                        <span aria-hidden="true">·</span>
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
            ? 'Loading more…'
            : `${total - items.length} more. Scroll to load them.`}
        </div>
      )}
    </div>
  )
}

// Controls strip: a two-segment control for the group-by mode,
// iOS-style, each segment a full 44px target.
function Controls({
  groupBy,
  onGroupByChange,
}: {
  groupBy: 'none' | 'entry'
  onGroupByChange: (g: 'none' | 'entry') => void
}) {
  return (
    <div className="grid grid-cols-2 rounded-ios overflow-hidden border border-hairline" role="group" aria-label="group history by">
      {(
        [
          { value: 'entry' as const, label: 'By entry' },
          { value: 'none' as const, label: 'All events' },
        ]
      ).map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onGroupByChange(o.value)}
          aria-pressed={groupBy === o.value}
          className={`min-h-[44px] px-3 text-ios-body transition ${
            groupBy === o.value ? 'bg-bg-elevated text-accent' : 'bg-bg-surface text-label-primary active:bg-bg-elevated'
          }`}
        >
          {o.label}
        </button>
      ))}
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
              className="shrink-0 min-h-[44px] px-3 -mr-2 rounded-ios text-ios-body text-accent active:opacity-60"
              aria-label={bulkLabel}
            >
              {bulkLabel}
            </button>
          )}
        </div>
      </div>
      {loadError && (
        <div className="rounded-ios bg-red-500/10 border border-red-500/30 p-3 text-ios-caption text-red-400">
          Couldn’t load the list. {loadError}
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
                  href={safeExternalUrl(e.url)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block text-ios-body text-label-primary active:opacity-60 line-clamp-2 break-words"
                  title={e.title}
                >
                  {e.title}
                </a>
                <div className="text-ios-caption text-label-tertiary mt-0.5 truncate">
                  {sourceLabel(e.source_name)}
                  {e.published_at && (
                    <> · {timeAgo(e.published_at)}</>
                  )}
                </div>
              </div>
              <button
                onClick={() => onRowAction(e.id)}
                className="shrink-0 self-center min-h-[44px] px-3 -mr-2 rounded-ios text-ios-body text-accent active:opacity-60"
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
  // Depend on content, not array identity — ``ids`` is a fresh array
  // reference from the parent on every render even when its contents
  // haven't changed, which would otherwise re-fetch on every render.
  // Memoized (not inlined in the deps array below) so
  // react-hooks/exhaustive-deps can statically verify it.
  const idsKey = useMemo(() => ids.join(','), [ids])
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
        setLoadError(errorMessage(e))
      })
      .finally(() => {
        if (!alive) return
        setLoading(false)
      })
    return () => {
      alive = false
    }
    // Keyed on idsKey (content) deliberately instead of ids (reference);
    // see the comment above idsKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey])
  return (
    <EntryIdListContent
      actionLabel="Restore"
      emptyHint="Tap the eye on a card to hide it. Hidden stories stay in Popping but leave the dashboard."
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
  // Depend on content, not array identity — see the identical comment
  // in HiddenTabContent above.
  const idsKey = useMemo(() => ids.join(','), [ids])
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
        setLoadError(errorMessage(e))
      })
      .finally(() => {
        if (!alive) return
        setLoading(false)
      })
    return () => {
      alive = false
    }
    // Keyed on idsKey (content) deliberately instead of ids (reference).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey])
  return (
    <EntryIdListContent
      actionLabel="Remove"
      emptyHint="Tap the star on a card to save it for later. Saved stories show in the Saved column at the top of the dashboard."
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


// Thin wrapper for the Voted tab. Same shape as HiddenTabContent/
// StarredTabContent, but lists DOWNVOTED entries specifically (not
// upvoted — an upvote is "keep showing me this," there's nothing to
// clean up there) with a bulk "Hide all downvoted" action.
//
// Why this exists: voting and hiding are deliberately independent
// signals — a downvote alone doesn't remove anything from view, so a
// pattern of consistent downvotes on a source can otherwise pile up
// invisibly. This tab is the release valve: review what you've
// downvoted, then hide the backlog in one action instead of hunting
// each card down individually. The vote itself isn't cleared by
// hiding — a hidden entry that's still in this list just means "I
// downvoted it AND hid it," which the row's absence after hiding
// already communicates (hidden entries are excluded from ``ids``
// upstream in App.tsx).
function DownvotedTabContent({
  ids,
  onHide,
  onHideAll,
}: {
  ids: number[]
  onHide: (entryId: number) => void
  onHideAll: () => void
}) {
  const [loaded, setLoaded] = useState<EntryIdListContentProps['loaded']>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  // Depend on content, not array identity — see the identical comment
  // in HiddenTabContent above.
  const idsKey = useMemo(() => ids.join(','), [ids])
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
        setLoadError(errorMessage(e))
      })
      .finally(() => {
        if (!alive) return
        setLoading(false)
      })
    return () => {
      alive = false
    }
    // Keyed on idsKey (content) deliberately instead of ids (reference).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey])
  return (
    <EntryIdListContent
      actionLabel="Hide"
      emptyHint="Tap ▼ on a card to see fewer stories like it. Stories you've downvoted but not hidden show up here."
      populatedHint="Voting doesn't remove anything from view on its own — Hide (or Hide all downvoted) is the deliberate next step once you're sure you don't want to see more like these."
      bulkLabel="Hide all downvoted"
      onBulkAction={onHideAll}
      onRowAction={onHide}
      countNoun={ids.length === 1 ? 'downvoted entry' : 'downvoted entries'}
      loaded={loaded}
      loading={loading}
      loadError={loadError}
    />
  )
}
