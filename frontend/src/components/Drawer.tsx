// iOS-style full-screen grouped-list Drawer.
//
// On every viewport the Drawer is a right-side panel that slides in
// from the right edge: on mobile (<md) it fills the viewport from
// the right at near-full height (88vh), on desktop (md+) it's a
// 360px right sidebar. Anchoring to the right matches the position
// of the hamburger button in the top bar so tapping the hamburger
// makes the panel grow from beneath the tap target — no eye-jump
// from one side of the screen to the other.
//
// Swipe-to-dismiss on mobile: touch begins anywhere inside the
// drawer, drags leftward, releases past 100px → drawer closes.
// Mirrors the iOS right-edge gesture (Mail, Settings, Music).
//
// The body is three iOS-style grouped sections:
//
//   1. QUICK SETTINGS         — link to the full Settings overlay
//   2. JUMP TO COLUMN         — by-category navigation
//   3. SOURCES                — multi-select filter (tap-to-filter)
//
// Each section has a small uppercase ``UPPERCASE LABEL`` header in
// ``text-ios-caption text-label-tertiary`` (the iOS "section header"
// treatment). Rows are 44px tall, the iOS HIG minimum tap target.
//
// Feeds / LLM / Notifications / Reset are no longer in the Drawer —
// they moved to the Settings overlay (see ``Settings.tsx``). The
// Drawer stays slim so it's a quick filter panel, not a settings
// pile. Tap "All settings…" to open the full Settings.

import { useEffect, useRef, useState } from 'react'
import { api, type Source } from '../api'
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
  // Lifted brief-generation trigger from App. The Drawer's
  // "Generate brief now" button used to fire the POST inline and
  // close, leaving the user with no visible feedback while the
  // LLM roundtrip ran (3-10s on Ollama) and no surfaced error if
  // it failed. Now Drawer delegates to the same trigger App's
  // header uses, so both surfaces share the poll-until-done loop
  // and the same error path. ``generating`` is exposed so the
  // button label can show "Generating brief…" mid-flight without
  // each surface spinning up its own loading state.
  triggerGenerate: (
    tone: 'terse' | 'narrative' | 'alert',
    onError?: (msg: string) => void,
  ) => Promise<void>
  generating: boolean
  // Phase 5: FeedManager errors flow up to App's red banner for
  // a single, consistent error surface.
  onError: (msg: string) => void
  // Phase 5+: when the user renames a source via FeedManager's
  // inline edit, the Drawer bubbles the old→new mapping up so App
  // can remap ``activeSources`` in the same render cycle. Without
  // this, the chip bar briefly loses the renamed source during the
  // gap between PATCH and the next ``refresh()`` resolving.
  onSourceRenamed?: (oldName: string, newName: string) => void
  // Wipe namespaced localStorage keys + reload. App owns the
  // actual reset so the source of truth (App's ``useState``
  // mirrors) resets in lockstep. No longer used by the Drawer
  // itself (the action moved to Settings) but kept in the
  // signature so legacy callers don't fail to compile.
  onResetLocalState?: () => void
  // Open the full Settings overlay. Wired by App. The Drawer
  // uses this in its "All settings…" quick-action row.
  onOpenSettings?: () => void
}


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
  triggerGenerate,
  onError,
  onSourceRenamed,
  // Reset hook. No longer used inside the Drawer (the action
  // moved to Settings) but kept in the destructuring so the
  // function signature stays aligned with the Props type.
  onResetLocalState: _onResetLocalState,
  onOpenSettings,
}: Props) {
  const [sources, setSources] = useState<Source[]>([])
  const [sourcesError, setSourcesError] = useState<string | null>(null)

  // The brief-generation error and notifications / LLM state have
  // been moved to the Settings overlay. The Drawer is now slim —
  // Sources filter, Jump to column, and a link to Settings.

  // Each fetch function is its own retry-able handler. Storing them
  // as ``useCallback`` so the chip can call them directly on tap.
  //
  // Cancellation: a ref tracks whether the Drawer is still open
  // for the most recent fetch. The ref is set on each fetch and
  // cleared when the Drawer unmounts / closes; each .then
  // bail-out check skips setState on a stale fetch. Without this,
  // closing the Drawer mid-fetch leaves the promise in flight;
  // the later .then calls hit an unmounted component and React
  // logs a "state update on an unmounted component" warning.
  const aliveRef = useRef(true)
  useEffect(() => {
    // Mark every (re)open as a fresh "alive" generation. The
    // ref is updated synchronously on every render so the .then
    // closures see the current value.
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  })
  const refetchSources = (): Promise<void> => {
    setSourcesError(null)
    return api.sources().then((rows) => {
      if (!aliveRef.current) return
      setSources(rows)
    }).catch((err) => {
      if (!aliveRef.current) return
      setSources([])
      setSourcesError((err as Error).message)
    })
  }

  useEffect(() => {
    if (!open) return
    refetchSources()
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  // Esc dismisses the drawer. Mirrors the iOS sheet pattern where
  // the swipe-down and back-tap gestural dismiss are also Esc on a
  // keyboard. ``capture: true`` so the handler fires before any
  // inner element's keydown (e.g. a focused input's "submit on
  // Enter" listener). Stops the event so the App-level keyboard
  // effect doesn't also see Esc and fire its global close logic.
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

  // Swipe-left dismiss on mobile. Mirrors the iOS right-edge
  // gesture (Mail, Settings, Music): touch begins anywhere inside
  // the drawer's body, drags leftward, releases past a 100px
  // threshold → drawer closes. Set up via addEventListener (not
  // JSX onTouch*) so we can pass ``{ passive: false }`` and
  // ``preventDefault`` — JSX touch handlers default to passive on
  // touchmove for scroll performance. Without ``preventDefault`` the
  // page scrolls underneath the drawer mid-gesture, which feels
  // broken.
  //
  // ``SWIPE_DISMISS_PX`` is the drag distance (px) past which a
  // release fires the close. ``SWIPE_DIRECTION`` is the sign of
  // the delta we care about — negative for leftward swipes. The
  // reverse direction (rightward) is intentionally not handled
  // because the drawer is anchored to the right edge; pulling it
  // back toward the right doesn't make semantic sense.
  const SWIPE_DISMISS_PX = 100
  useEffect(() => {
    if (!open) return
    const drawer = drawerRef.current
    if (!drawer) return

    let startX = 0
    let armed = false

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) return
      startX = e.touches[0].clientX
      armed = true
    }
    const onTouchMove = (e: TouchEvent) => {
      if (!armed) return
      const dx = e.touches[0].clientX - startX
      if (dx < -10) {
        // First non-trivial leftward motion — block the underlying
        // page scroll from kicking in.
        e.preventDefault()
      }
    }
    const onTouchEnd = (e: TouchEvent) => {
      if (!armed) return
      armed = false
      // ``changedTouches`` (not ``touches``) because the gesture
      // ended and the finger is no longer on screen.
      const touch = e.changedTouches[0]
      const dx = touch.clientX - startX
      if (dx < -SWIPE_DISMISS_PX) onClose()
    }
    const onTouchCancel = () => {
      armed = false
    }
    drawer.addEventListener('touchstart', onTouchStart, { passive: true })
    drawer.addEventListener('touchmove', onTouchMove, { passive: false })
    drawer.addEventListener('touchend', onTouchEnd, { passive: true })
    drawer.addEventListener('touchcancel', onTouchCancel, { passive: true })
    return () => {
      drawer.removeEventListener('touchstart', onTouchStart)
      drawer.removeEventListener('touchmove', onTouchMove)
      drawer.removeEventListener('touchend', onTouchEnd)
      drawer.removeEventListener('touchcancel', onTouchCancel)
    }
  }, [open, onClose])

  // Focus trap. Move focus into the drawer on open and cycle it
  // between the first and last tabbable elements so Tab can't
  // escape into the dashboard behind the backdrop. Without this,
  // a keyboard-only user can Tab past the drawer's Done button
  // into the dashboard's hidden-but-still-tabbable columns.
  // The trap is opt-out for screen-reader users (the dialog's
  // aria-modal already announces modality to AT; the visual
  // focus ring still follows their focus).
  const drawerRef = useRef<HTMLElement | null>(null)
  const firstFocusableRef = useRef<HTMLElement | null>(null)
  useEffect(() => {
    if (!open) return
    const drawer = drawerRef.current
    if (!drawer) return

    // Defer to next frame so the slide-up animation hasn't
    // started yet — focusing inside a 0-opacity panel reads as
    // a no-op to assistive tech.
    const id = window.requestAnimationFrame(() => {
      firstFocusableRef.current?.focus()
    })

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      const focusables = drawer.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (e.shiftKey && active === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }
    drawer.addEventListener('keydown', onKeyDown)
    return () => {
      window.cancelAnimationFrame(id)
      drawer.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

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
        ref={(el) => {
          drawerRef.current = el
          // First focusable child: the Done button is rendered
          // before everything else in the header so it lands as
          // ``focusables[0]`` above. We capture it explicitly
          // because ``querySelectorAll`` runs against the live
          // tree which is fine for the Tab cycle but the
          // initial focus wants the explicit Done button —
          // not a child that may not exist on first render
          // (e.g. the "Clear local state" row is conditional).
          firstFocusableRef.current = el?.querySelector<HTMLElement>(
            'button[aria-label="close menu"]',
          ) ?? null
        }}
        role="dialog"
        aria-modal="true"
        aria-label="menu"
        className={`fixed z-40 bg-bg-app shadow-2xl flex flex-col
                    inset-y-0 right-0 top-0 left-auto h-full w-[88vw] max-w-[420px] rounded-l-ios-lg
                    md:w-[360px] md:rounded-none
                    transition-transform duration-300 ease-out
                    ${open
                      ? 'translate-x-0'
                      : 'translate-x-full'
                    }`}
      >
        {/* Vertical grabber on mobile — the small vertical line at
            the top-LEFT of the drawer, mirroring the iOS right-edge
            sheet's affordance. ``md:hidden`` because desktop drawers
            don't need a grabber (they have a visible edge against
            the dimmed backdrop). The element is not interactive; it
            just signals "this is a panel you can dismiss by
            swiping left." */}
        <div
          aria-hidden="true"
          className="md:hidden absolute top-3 left-2 h-10 w-1 rounded-full bg-label-tertiary"
        />
        {/* Header row — large title "Menu" on the right (matches
            the right-anchored panel), Done button on the left. The
            hairlines at the bottom is the standard iOS grouped-list
            section break. We use ``flex-row-reverse``-equivalent
            ordering by simply swapping the children: Done first,
            then the title. ``pl-10 md:pl-4`` on the Done button
            leaves room for the vertical grabber on mobile. */}
        <div className="flex items-center justify-between px-4 pt-3 pb-3 md:pt-5 md:pb-4 border-b border-hairline shrink-0">
          <button
            onClick={onClose}
            aria-label="close menu"
            className="min-h-[32px] min-w-[32px] flex items-center justify-center rounded-ios text-accent active:bg-bg-elevated pl-10 md:pl-0"
          >
            <span className="text-ios-body font-normal">Done</span>
          </button>
          <h2 className="text-2xl md:text-ios-large-title font-bold text-label-primary tracking-tight">
            Menu
          </h2>
        </div>

        {/* Body. ``min-h-0`` lets the nav actually shrink below its
            content height — without it, ``overflow-y-auto`` is a
            no-op because the flex item refuses to be smaller than
            its contents and the parent grows to fit (overflowing
            the viewport). ``p-4`` matches iOS grouped-list left/
            right margins. Background ``bg-bg-app`` keeps the section
            gaps from showing the sheet's underlying surface. */}
        <nav className="flex-1 min-h-0 overflow-y-auto bg-bg-app pb-8">
          <GroupedSection label="Quick settings">
            <GroupedRow
              onClick={() => {
                // The Settings overlay is owned by App. The Drawer
                // tells App to open it via the callback the parent
                // wires up; we close the Drawer so the user lands
                // on the Settings overlay immediately. This
                // pattern matches the column-jump row above.
                onOpenSettings?.()
                onClose()
              }}
              title="All settings…"
              subtitle="feeds · LLM · notifications · reset"
              showChevron
            />
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

