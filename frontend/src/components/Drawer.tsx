// iOS-style full-screen grouped-list Drawer.
//
// On mobile (<md) the Drawer is a bottom sheet that slides up to fill
// the viewport, mimicking the iOS Settings app navigation. On
// desktop (md+) it slides in from the left as a 360px sidebar, like
// Apple Mail's column-show/hide popover or the menu pane in Music on
// macOS. The dual treatment comes from one component: the same
// ``<nav>`` renders inside different sized containers, with a
// different backdrop opacity and slide-in direction per breakpoint.
//
// The body is five iOS-style grouped sections:
//
//   1. NOTIFICATIONS          — backend status + retry
//   2. LLM                    — provider/model + tone + Generate now
//   3. CATEGORIES             — jump-to-column buttons
//   4. FEEDS                  — dynamic-source CRUD (FeedManager)
//   5. SOURCES                — multi-select filter (tap-to-filter)
//
// Each section has a small uppercase ``UPPERCASE LABEL`` header in
// ``text-ios-caption text-label-tertiary`` (the iOS "section header"
// treatment). Rows are 44px tall, the iOS HIG minimum tap target.
//
// The Drawer surfaces three categories of "failed to load" state:
//   - sources list (rendered with a retry button)
//   - notifications status chip (renders red, tap to retry)
//   - LLM status chip (renders red, tap to retry)
//
// The old code silently coerced all fetch failures into
// "{ configured: false }" which made a 401 look the same as a
// missing env var. Now each failure shows the actual error message
// and offers to retry.

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
import { SourceIcon } from './SourceIcon'

type Props = {
  open: boolean
  onClose: () => void
  categories: string[]
  // Active source filter (multi-select). Empty set = no filter.
  activeSources: Set<string>
  onSourceToggle: (name: string) => void
  // Wipe the active filter set in one call. Wired to the "Filtering:
  // …" header's clear-all button so the user doesn't have to untick
  // each row individually.
  onClearAllFilters?: () => void
  // "Scroll to column" support. The Drawer's category list calls
  // back with the category name; App owns the column refs and
  // scrolls the right one into view.
  onCategoryJump?: (category: string) => void
  // Active brief tone, lifted from App so the Drawer's "Generate
  // brief now" stays in sync with the BriefCard pills.
  briefTone: 'terse' | 'narrative' | 'alert'
  onBriefToneChange: (next: 'terse' | 'narrative' | 'alert') => void
  // Phase 5: FeedManager errors flow up to App's red banner for
  // a single, consistent error surface.
  onError: (msg: string) => void
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

// Same tone set as BriefCard. Kept in sync via duplication rather
// than a shared module — two constants, ~6 lines, no point in a new
// file.
const TONES: Array<{ value: 'terse' | 'narrative' | 'alert'; label: string }> = [
  { value: 'terse',     label: 'terse' },
  { value: 'narrative', label: 'narrative' },
  { value: 'alert',     label: 'alert' },
]

export function Drawer({
  open,
  onClose,
  categories,
  activeSources,
  onSourceToggle,
  onClearAllFilters,
  onCategoryJump,
  briefTone,
  onBriefToneChange,
  onError,
}: Props) {
  const [sources, setSources] = useState<Source[]>([])
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [notif, setNotif] = useState<NotificationStatus | null>(null)
  const [notifError, setNotifError] = useState<string | null>(null)
  const [llm, setLlm] = useState<LLMStatus | null>(null)
  const [llmError, setLlmError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)

  // Each fetch function is its own retry-able handler. Storing them
  // as ``useCallback`` so the chip can call them directly on tap.
  const refetchSources = (): Promise<void> => {
    setSourcesError(null)
    return api.sources().then(setSources).catch((err) => {
      setSources([])
      setSourcesError((err as Error).message)
    })
  }
  const refetchNotif = () => {
    setNotifError(null)
    api
      .notificationStatus()
      .then(setNotif)
      .catch((err) => {
        // Don't set a default ``notif`` — leaving it null and the
        // chip renders the retry path.
        setNotifError((err as Error).message)
      })
  }
  const refetchLlm = () => {
    setLlmError(null)
    api
      .llmStatus()
      .then(setLlm)
      .catch((err) => {
        setLlmError((err as Error).message)
      })
  }

  useEffect(() => {
    if (!open) return
    refetchSources()
    refetchNotif()
    refetchLlm()
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      {/* Backdrop. Same backdrop-blur treatment on both breakpoints;
          the only difference is the opacity — slightly heavier on
          mobile so the sheet reads as a clearly separate surface,
          lighter on desktop where the panel is a sidebar, not a
          takeover. z-30 sits above the header (z-20) but below the
          sheet chrome (z-40). */}
      <div
        onClick={onClose}
        aria-hidden="true"
        className={`fixed inset-0 z-30 bg-black/60 md:bg-black/40 supports-[backdrop-filter]:backdrop-blur-sm transition-opacity duration-200 ${
          open ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
      />

      {/* Mobile: bottom sheet, slides up to fill the viewport (the iOS
          Settings app presentation). Desktop (md+): slides in from
          the left as a 360px sidebar. The two animations are
          independent CSS classes so the swap is breakpoint-clean —
          the mobile sheet never shows on desktop and vice versa. */}
      <aside
        aria-label="menu"
        className={`fixed z-40 bg-bg-app shadow-2xl flex flex-col
                    inset-x-0 bottom-0 top-auto h-[90vh] rounded-t-ios-lg
                    md:inset-y-0 md:left-0 md:top-0 md:right-auto md:h-full md:w-[360px] md:rounded-none
                    transition-transform duration-300 ease-out
                    md:translate-x-0
                    ${open
                      ? 'translate-y-0 md:translate-x-0'
                      : 'translate-y-full md:-translate-x-full'
                    }`}
      >
        {/* Grabber on mobile — the small horizontal line at the top
            of an iOS sheet. ``md:hidden`` because desktop drawers
            don't show a grabber. The element is not interactive; it
            just signals "this is a sheet you can dismiss by swiping
            down" (a future iteration could add the swipe-down
            dismiss gesture — for now we rely on tapping the
            backdrop). */}
        <div
          aria-hidden="true"
          className="md:hidden mx-auto mt-2 mb-1 h-1 w-10 rounded-full bg-label-tertiary"
        />
        {/* Header row — large title "Settings" on mobile (Apple
            convention), or a smaller "Menu" on desktop where the
            large-title header is already in App. Trailing Done button
            mirrors iOS sheet dismissal. The hairlines at the bottom
            is the standard iOS grouped-list section break. */}
        <div className="flex items-center justify-between px-4 pt-3 pb-3 md:pt-5 md:pb-4 border-b border-hairline shrink-0">
          <h2 className="text-2xl md:text-ios-large-title font-bold text-label-primary tracking-tight">
            Menu
          </h2>
          <button
            onClick={onClose}
            aria-label="close menu"
            className="min-h-[32px] min-w-[32px] flex items-center justify-center rounded-ios text-accent active:bg-bg-elevated"
          >
            <span className="text-ios-body font-normal">Done</span>
          </button>
        </div>

        {/* Body. ``min-h-0`` lets the nav actually shrink below its
            content height — without it, ``overflow-y-auto`` is a
            no-op because the flex item refuses to be smaller than
            its contents and the parent grows to fit (overflowing
            the viewport). ``p-4`` matches iOS grouped-list left/
            right margins. Background ``bg-bg-app`` keeps the section
            gaps from showing the sheet's underlying surface. */}
        <nav className="flex-1 min-h-0 overflow-y-auto bg-bg-app pb-8">
          <GroupedSection label="Notifications">
            {notifError ? (
              <GroupedRow
                onClick={refetchNotif}
                title="Couldn't check status"
                subtitle={`tap to retry — ${notifError}`}
                tone="destructive"
              />
            ) : notif === null ? (
              <GroupedRow title="Notifications" subtitle="checking…" />
            ) : notif.configured ? (
              <GroupedRow
                title="Configured"
                subtitle={`${notif.backend} · ${notif.scheme}`}
                tone="success"
              />
            ) : (
              <GroupedRow
                title="Not configured"
                subtitle="set APPRISE_URL or PUSHOVER_* in .env"
                tone="warning"
              />
            )}
          </GroupedSection>

          <GroupedSection label="LLM">
            <LLMSection
              llm={llm}
              llmError={llmError}
              onChange={setLlm}
              onRetry={refetchLlm}
            />
            {/* Brief tone picker — lifted state. Same UX as
                BriefCard's pills but rendered inline in the Drawer
                so the user can pre-pick a tone before clicking
                "Generate brief now". */}
            <div className="mx-4 mt-3 grid grid-cols-3 gap-0 rounded-ios overflow-hidden border border-hairline">
              {TONES.map((t) => {
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
            <div className="px-4 pt-3">
              <button
                onClick={async () => {
                  setGenError(null)
                  setGenerating(true)
                  try {
                    await api.briefGenerate(briefTone)
                    onClose()
                  } catch (err) {
                    setGenError((err as Error).message)
                  } finally {
                    setGenerating(false)
                  }
                }}
                disabled={generating || (llm !== null && !llm.configured)}
                className="w-full min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white text-ios-body font-medium"
              >
                {generating ? 'Generating brief…' : 'Generate brief now'}
              </button>
              {genError && (
                <p className="mt-2 text-ios-caption text-red-400 break-words">
                  {genError}
                </p>
              )}
            </div>
          </GroupedSection>

          {categories.length > 0 && (
            <GroupedSection label="Jump to column">
              {categories.map((c) => (
                // Each category is its own row. Tap → close drawer
                // and scroll the desktop grid to that column.
                <GroupedRow
                  key={c}
                  title={c}
                  onClick={() => {
                    onCategoryJump?.(c)
                    onClose()
                  }}
                  showChevron
                />
              ))}
            </GroupedSection>
          )}

          <GroupedSection label="Feeds">
            <FeedManager
              sources={sources}
              onRefresh={refetchSources}
              onError={onError}
            />
          </GroupedSection>

          <GroupedSection
            label="Sources"
            footnote={
              activeSources.size > 0
                ? `filtering: ${Array.from(activeSources).join(', ')}`
                : undefined
            }
            action={
              activeSources.size > 0 && onClearAllFilters ? (
                <button
                  onClick={onClearAllFilters}
                  aria-label="clear all source filters"
                  className="text-ios-body text-accent active:opacity-60"
                >
                  Clear
                </button>
              ) : undefined
            }
          >
            {sourcesError ? (
              <GroupedRow
                onClick={refetchSources}
                title="Couldn't load sources"
                subtitle={`tap to retry — ${sourcesError}`}
                tone="destructive"
              />
            ) : sources.length === 0 ? (
              <p className="px-4 py-3 text-ios-body text-label-secondary">
                loading…
              </p>
            ) : (
              // The Sources list is a checkbox list, not a button
              // list. Visually honest: each row is a checkbox + label,
              // so the multi-select semantics match what the chip-bar
              // in the header does. ``<label>`` makes the entire row
              // clickable, which matches the previous tap-target size.
              // App's ``toggleSourceAndMaybeClose`` closes the drawer
              // on the first selection so the user can see the
              // filtered dashboard immediately — subsequent taps don't
              // close, so they can keep picking without the panel
              // ping-ponging shut.
              <>
                {sources.map((s) => {
                  const active = activeSources.has(s.name)
                  return (
                    <label
                      key={s.id}
                      className={`flex items-center gap-3 px-4 min-h-[44px] cursor-pointer transition border-b border-hairline last:border-b-0 ${
                        active ? 'bg-bg-elevated' : 'active:bg-bg-elevated'
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={() => onSourceToggle(s.name)}
                        className="shrink-0 h-4 w-4 accent-accent"
                        aria-label={`filter by ${s.name}`}
                      />
                      {/* SourceIcon. Falls back to a colored letter
                          when the favicon hasn't been fetched yet. */}
                      <SourceIcon src={s.favicon_path} name={s.name} size={18} />
                      <span className="flex-1 min-w-0 truncate text-ios-body text-label-primary">
                        {s.name}
                      </span>
                      <span className="shrink-0 text-ios-caption text-label-tertiary">
                        {s.category}
                      </span>
                    </label>
                  )
                })}
              </>
            )}
          </GroupedSection>
        </nav>
      </aside>
    </>
  )
}

// ---------------------------------------------------------------------------
// iOS grouped-list primitives
// ---------------------------------------------------------------------------

// Section. The iOS convention is to render an uppercase label in
// ``text-label-tertiary`` above a rounded card of rows. The label has
// generous left/right padding (``px-4``) and a small bottom margin.
// The card is a single rounded ``bg-bg-surface`` block; rows inside
// are separated by ``border-hairline`` so dividers extend to the
// edges of the card (not full bleed). ``footnote`` sits below the
// card in ``text-ios-caption text-label-secondary`` — useful for
// explanatory copy (e.g. "filtering: reuters, hackernews").
//
// ``action`` (e.g. a "Clear" button) renders on the right side of
// the label row, like the "Edit" button next to the "Reminders"
// header in iOS Settings.
function GroupedSection({
  label,
  footnote,
  action,
  children,
}: {
  label: string
  footnote?: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="mt-6 first:mt-2">
      <div className="flex items-end justify-between px-4 mb-2">
        <h3 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
          {label}
        </h3>
        {action}
      </div>
      {/* The rows go inside a single rounded card so the iOS grouped-
          list visual works: rounded card on the page, hairline
          dividers between rows, no outside border. ``overflow-hidden``
          keeps the first/last row from poking past the card corners. */}
      <div className="mx-4 rounded-ios bg-bg-surface overflow-hidden">
        {children}
      </div>
      {footnote && (
        <p className="px-4 mt-2 text-ios-caption text-label-secondary">
          {footnote}
        </p>
      )}
    </div>
  )
}

// Single iOS-style row. ``title`` is the primary text, ``subtitle``
// is the secondary line beneath it (font-300, ``label-secondary``).
// Renders as a 44px-tall button when ``onClick`` is set — the entire
// row is the tap target. When used as a static display (no
// ``onClick``) it falls back to a non-button ``<div>``.
//
// ``tone`` recolors the chevron-area / title when there's a state to
// surface (success / warning / destructive) — iOS uses these for
// health-check rows in Settings → Battery, for example.
function GroupedRow({
  title,
  subtitle,
  onClick,
  showChevron,
  tone,
}: {
  title: React.ReactNode
  subtitle?: React.ReactNode
  onClick?: () => void
  showChevron?: boolean
  tone?: 'success' | 'warning' | 'destructive'
}) {
  const toneClass =
    tone === 'success'
      ? 'text-emerald-400'
      : tone === 'warning'
      ? 'text-amber-400'
      : tone === 'destructive'
      ? 'text-red-400'
      : 'text-label-primary'
  const content = (
    <>
      <div className="flex-1 min-w-0">
        <div className={`text-ios-body ${toneClass} truncate`}>{title}</div>
        {subtitle && (
          <div className="text-ios-caption text-label-secondary truncate">
            {subtitle}
          </div>
        )}
      </div>
      {showChevron && (
        <ChevronRight className="shrink-0 w-4 h-4 text-label-tertiary" />
      )}
    </>
  )
  const baseClass =
    'flex items-center gap-3 px-4 min-h-[44px] border-b border-hairline last:border-b-0'
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${baseClass} w-full text-left active:bg-bg-elevated`}
      >
        {content}
      </button>
    )
  }
  return <div className={baseClass}>{content}</div>
}

// iOS chevron — used for navigation rows. Right-pointing, single 1.5
// stroke. Pulled inline (rather than into an icon library) because
// the Drawer is the only consumer.
function ChevronRight({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// LLM section: chip + inline picker
// ---------------------------------------------------------------------------

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
  // Two parallel tag lists: one annotated for brief, one for scoring.
  // The backend stamps ``recommended`` differently per task — same
  // model can be starred in one dropdown but not the other.
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

  // Fetch settings + tags when the picker is opened. We re-fetch on
  // open rather than on drawer-open because settings can change mid-
  // drawer-open (e.g. user saves, then opens picker again to verify).
  // Brief and scoring tags are fetched in parallel since they share the
  // same provider + base URL but different annotation overlays.
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

  const applySettingsToForm = (
    s: SettingsOut,
    t: LLMTagsResponse | null,
    ts: LLMTagsResponse | null,
  ) => {
    setProvider(s.llm_provider || '')
    setModelBrief(s.llm_model_brief || '')
    setModelScoring(s.llm_model_scoring || '')
    // Brief: if the saved model isn't in the tags list (e.g. user
    // typed it freehand last time), keep it as free text so they can
    // edit. Scoring has no free-text mode — the dropdown covers every
    // account-available model and the sentinel ``""`` maps to env.
    const tagNames = t?.models?.map((m) => m.name) ?? []
    const isFreeText =
      Boolean(s.llm_model_brief) && tagNames.length > 0 && !tagNames.includes(s.llm_model_brief || '')
    setUseFreeText(isFreeText)
    setFreeTextBrief(isFreeText ? s.llm_model_brief || '' : '')
    void ts // scoring form state doesn't need a free-text flip
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
        setScoringTags(null)
        return
      }
      const [tb, ts] = await Promise.all([
        api.llmTags(prov, true, 'brief'),
        api.llmTags(prov, true, 'scoring'),
      ])
      setTags(tb)
      setScoringTags(ts)
      // Re-evaluate free-text mode now that we have a fresh list. If
      // the model the user has in the form isn't in the freshly-
      // fetched list, switch to free-text so they can edit it.
      if (!useFreeText && modelBrief && !tb.models.some((m) => m.name === modelBrief)) {
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

  // Annotated list of model rows from the backend. The backend already
  // stamps ``recommended`` and ``recommended_note`` and sorts recommended
  // first — we just surface them in the dropdown with a ``★`` prefix and
  // an optional ``(thinking)`` suffix so the user can spot the curated
  // picks at a glance. ``tagNames`` is the bare-name view used by the
  // free-text toggle's membership checks (see ``applySettingsToForm`` and
  // the "type a name instead" handler below).
  const tagOptions = useMemo(
    () => tags?.models ?? [],
    [tags],
  )
  const tagNames = useMemo(() => tagOptions.map((m) => m.name), [tagOptions])
  const hasRecommendations = useMemo(
    () => tagOptions.some((m) => m.recommended),
    [tagOptions],
  )
  // Same shape for the scoring dropdown. Scoring has its own curated
  // list (``_RECOMMENDED_FOR['scoring']`` on the backend) so the
  // starred models here are different from the brief dropdown.
  const scoringOptions = useMemo(
    () => scoringTags?.models ?? [],
    [scoringTags],
  )
  const hasScoringRecommendations = useMemo(
    () => scoringOptions.some((m) => m.recommended),
    [scoringOptions],
  )

  return (
    <>
      {/* LLM status + edit affordance. Sits at the top of the LLM
          grouped section as a single iOS-style row — title is the
          status text, the trailing "edit" button is the secondary
          affordance. */}
      <div className="flex items-center gap-3 px-4 min-h-[44px] border-b border-hairline">
        {llmError ? (
          // Same shape as the notifications chip — error path with
          // a tap-to-retry affordance.
          <button
            onClick={onRetry}
            className="flex-1 min-w-0 text-left text-red-400 active:bg-bg-elevated"
            title={`Error: ${llmError}`}
          >
            <div className="text-ios-body truncate">Couldn't check</div>
            <div className="text-ios-caption text-label-secondary truncate">
              tap to retry
            </div>
          </button>
        ) : llm === null ? (
          <div className="flex-1 min-w-0">
            <div className="text-ios-body text-label-primary truncate">LLM</div>
            <div className="text-ios-caption text-label-secondary truncate">
              checking…
            </div>
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
            <div className="text-ios-body text-amber-400 truncate">
              Not configured
            </div>
            <div className="text-ios-caption text-label-secondary truncate">
              set LLM env vars in .env
            </div>
          </div>
        )}
        <button
          onClick={openPicker}
          className="shrink-0 text-ios-body text-accent active:opacity-60"
          aria-label="edit LLM settings"
        >
          {pickerOpen ? 'Close' : 'Edit'}
        </button>
      </div>

      {pickerOpen && (
        <div className="px-4 py-3 space-y-3 text-ios-body border-b border-hairline">
          <div>
            <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
              Provider
            </label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
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
              <label className="text-ios-caption uppercase tracking-wide text-label-tertiary">
                Model (brief)
              </label>
              <button
                onClick={refreshTags}
                disabled={tagsLoading}
                className="text-ios-caption text-accent disabled:opacity-40"
              >
                {tagsLoading ? 'refreshing…' : 'refresh list'}
              </button>
            </div>
            {tagsError ? (
              <p className="text-ios-caption text-amber-400 break-words mb-1">
                couldn’t load model list: {tagsError}
              </p>
            ) : null}
            {tags?.stale ? (
              <p className="text-ios-caption text-amber-400 mb-1">
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
                className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary placeholder:text-label-tertiary"
              />
            ) : (
              <select
                value={modelBrief}
                onChange={(e) => setModelBrief(e.target.value)}
                className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
              >
                <option value="">— pick a model —</option>
                {tagOptions.map((m) => {
                  // Label format: "★ name (thinking)" for recommended
                  // thinking models, "★ name" for other recommended,
                  // plain "name" for the rest. The (thinking) suffix
                  // tells the user the brief will include some
                  // chain-of-thought content from the model's thinking
                  // field rather than a clean summary.
                  const star = m.recommended ? '★ ' : ''
                  const note = m.recommended_note ? ` (${m.recommended_note})` : ''
                  return (
                    <option key={m.name} value={m.name}>
                      {star}{m.name}{note}
                    </option>
                  )
                })}
              </select>
            )}
            {hasRecommendations && tagOptions.length > 0 && (
              <p className="mt-1 text-ios-caption text-label-secondary">
                ★ = recommended for Ollama Cloud
              </p>
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
                className="mt-1 text-ios-caption text-accent active:opacity-60"
              >
                {useFreeText ? '← back to list' : 'type a name instead'}
              </button>
            )}
          </div>
          <div>
            <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
              Model (scoring)
            </label>
            {scoringOptions.length === 0 ? (
              // No tags yet (initial load before fetch lands, or fetch
              // failed, or pinned to a non-Ollama provider). Plain text
              // input as a fallback — still editable, just no dropdown.
              <input
                type="text"
                value={modelScoring}
                onChange={(e) => setModelScoring(e.target.value)}
                placeholder="env default (or model name)"
                className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary placeholder:text-label-tertiary"
              />
            ) : (
              <select
                value={modelScoring}
                onChange={(e) => setModelScoring(e.target.value)}
                className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
              >
                {/* Sentinel maps to ``""`` on save, which the backend
                    treats as "delete the runtime override → fall back
                    to env-driven scoring model". Different from the
                    brief dropdown's "pick a model" sentinel because
                    scoring's env default is the meaningful baseline
                    the user came from. */}
                <option value="">— env default —</option>
                {scoringOptions.map((m) => {
                  const star = m.recommended ? '★ ' : ''
                  const note = m.recommended_note ? ` (${m.recommended_note})` : ''
                  return (
                    <option key={m.name} value={m.name}>
                      {star}{m.name}{note}
                    </option>
                  )
                })}
              </select>
            )}
            {hasScoringRecommendations && scoringOptions.length > 0 && (
              <p className="mt-1 text-ios-caption text-label-secondary">
                ★ = recommended for scoring
              </p>
            )}
          </div>
          {saveError && <p className="text-ios-caption text-red-400 break-words">{saveError}</p>}
          <div className="flex gap-2 pt-1">
            <button
              onClick={save}
              disabled={saving}
              className="flex-1 min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white"
            >
              {saving ? 'saving…' : 'save'}
            </button>
            <button
              onClick={() => setPickerOpen(false)}
              disabled={saving}
              className="flex-1 min-h-[44px] rounded-ios bg-bg-elevated active:opacity-60 disabled:opacity-40 text-label-primary"
            >
              cancel
            </button>
          </div>
          <p className="text-ios-caption text-label-secondary leading-snug">
            Changes apply immediately — no restart needed. An empty
            value resets to the env default.
          </p>
        </div>
      )}
    </>
  )
}