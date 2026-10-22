// The Menu: the panel behind the one top-bar button (≡).
//
// On every viewport it is a right-side panel that slides in from the
// right edge: 88vw (max 420px) on phones, a 360px sidebar on desktop.
// Anchoring it to the right puts it under the ≡ button, so the panel
// grows from beneath the tap target.
//
// Swipe to dismiss on phones: a mostly sideways drag of more than
// 100px either way closes it (rightward is the way it slides out).
//
// The body is iOS-style grouped sections:
//
//   1. (no label)        — Settings, Edit tabs, and Save current view
//   2. JUMP TO COLUMN    — every tab the strip shows, in its order
//   3. SOURCES           — the multi-select filter; each row's › opens
//                          that feed in Settings
//
// Rows are at least 44px tall, the iOS HIG minimum tap target. While
// closed the panel is inert, so Tab never lands in it.

import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import type { Source } from '../api'
import { SourceIcon } from './SourceIcon'
import { ChevronRight, GroupedRow, GroupedSection } from './Grouped'
import { categoryLabel, sourceLabel } from '../lib/labels'
import type { SettingsTab } from './Settings'

type Props = {
  open: boolean
  onClose: () => void
  // The tabs the strip shows after Brief, in strip order (App's
  // ``orderColumns().shown``), so a hidden tab is never offered.
  tabs: ReadonlyArray<{ key: string; label: string }>
  // Source list, owned by App (the same fetch that already feeds
  // FeedManager/Settings — see App.tsx's `refresh()`). The Drawer
  // used to fetch its own copy on every open, which meant an extra
  // network round-trip every time plus a staleness window against
  // whatever Settings/FeedManager had just changed. Passed as a
  // prop instead, same pattern as <Settings sources={sources} />.
  sources: Source[]
  // Re-run App's own source fetch. Wired to the "Couldn't load
  // sources" retry row below, same as FeedManager's onRefresh.
  onRefreshSources?: () => Promise<void>
  // Active source filter (multi-select). Empty set = no filter.
  activeSources: Set<string>
  onSourceToggle: (name: string) => void
  // Wipe the active filter set in one call. Wired to the "Filtering:
  // …" header's clear-all button so the user doesn't have to untick
  // each row individually.
  onClearAllFilters?: () => void
  // "Scroll to column" support. The Jump to column list calls back
  // with the tab's key; App owns the column refs and scrolls the
  // right one into view.
  onCategoryJump?: (key: string) => void
  // Open the Settings overlay (the Settings row).
  onOpenSettings?: (tab?: SettingsTab) => void
  // Open the tab editor (the Edit tabs row). It takes focus itself.
  onEditTabs?: () => void
  // Open one feed in Settings → Feeds (each source row's ›).
  onEditSource?: (id: number) => void
  // Save the current source filter + column prefs as a
  // named preset. The Drawer collects the name via a
  // native prompt and calls the callback with the
  // trimmed name. Caller is responsible for actually
  // writing the preset to localStorage.
  // Save the current filter and column settings as a named view.
  onSavePreset?: (name: string) => void
}


export function Drawer({
  open,
  onClose,
  tabs,
  sources,
  onRefreshSources,
  activeSources,
  onSourceToggle,
  onClearAllFilters,
  onCategoryJump,
  onOpenSettings,
  onEditTabs,
  onEditSource,
  // App wires this to ``savePreset`` and a name prompt, and toasts
  // the new preset's name.
  onSavePreset,
}: Props) {
  // Set by a row that hands over to Settings, which takes focus
  // itself; otherwise closing returns focus to the ≡ button.
  const handingOff = useRef(false)

  // Save current view: an inline name field, no browser prompt.
  const [namingView, setNamingView] = useState(false)
  const [viewName, setViewName] = useState('')
  const viewNameRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (open) return
    setNamingView(false)
    setViewName('')
  }, [open])
  const saveView = () => {
    const name = viewName.trim()
    if (!name || !onSavePreset) return
    onSavePreset(name)
    onClose()
  }

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

  // Swipe to dismiss on phones: a touch anywhere in the panel that
  // drags mostly sideways, more than 100px either way, closes it.
  // Rightward is the direction the right-anchored panel slides out;
  // leftward is kept for those used to it. Set up via
  // addEventListener (not JSX onTouch*) so touchmove can be
  // ``{ passive: false }`` and call ``preventDefault`` — without it
  // the page scrolls underneath mid-gesture. Mostly vertical drags
  // are left alone, so the list still scrolls.
  const SWIPE_DISMISS_PX = 100
  useEffect(() => {
    if (!open) return
    const drawer = drawerRef.current
    if (!drawer) return

    let startX = 0
    let startY = 0
    let armed = false

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) return
      startX = e.touches[0].clientX
      startY = e.touches[0].clientY
      armed = true
    }
    const onTouchMove = (e: TouchEvent) => {
      if (!armed) return
      const dx = e.touches[0].clientX - startX
      const dy = e.touches[0].clientY - startY
      if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy)) {
        // A sideways drag: keep the page from scrolling underneath.
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
      const dy = touch.clientY - startY
      if (Math.abs(dx) > SWIPE_DISMISS_PX && Math.abs(dx) > Math.abs(dy)) onClose()
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

  // Closed: inert and hidden from assistive tech (React 18 has no
  // ``inert`` prop). Closing returns focus to the ≡ button unless a
  // row is handing over to Settings.
  const wasOpen = useRef(open)
  useEffect(() => {
    const el = drawerRef.current
    if (el) {
      el.toggleAttribute('inert', !open)
      if (open) el.removeAttribute('aria-hidden')
      else el.setAttribute('aria-hidden', 'true')
    }
    if (wasOpen.current && !open && !handingOff.current) {
      document.querySelector<HTMLElement>('button[aria-label="open menu"]')?.focus()
    }
    handingOff.current = false
    wasOpen.current = open
  }, [open])
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
        className={`fixed inset-0 z-30 bg-black/60 md:bg-black/40 supports-[backdrop-filter]:backdrop-blur-sm motion-safe:transition-opacity motion-safe:duration-200 ${
          open ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
      />

      {/* A panel anchored to the right edge on every width: 88vw (max
          420px) on phones, 360px on desktop. It slides in from the
          right with a transform; the same classes serve both widths. */}
      <aside
        ref={(el) => {
          drawerRef.current = el
          // Initial focus goes to the Done button, found by its label
          // rather than by position (it sits after the title in the
          // header), so focus never starts on a row that may not
          // exist on first render.
          firstFocusableRef.current = el?.querySelector<HTMLElement>(
            'button[aria-label="close menu"]',
          ) ?? null
        }}
        role="dialog"
        aria-modal="true"
        aria-label="menu"
        // ``touch-pan-y`` matches the swipe-left dismiss handler
        // below: horizontal pans are handled in JS (the dismiss
        // gesture), vertical scrolling of the drawer body stays
        // native. Without it a mostly-horizontal touch can be
        // claimed by the browser for scrolling an ancestor before
        // the JS preventDefault engages.
        className={`fixed z-40 bg-bg-app shadow-2xl flex flex-col touch-pan-y-zoom
                    inset-y-0 right-0 top-0 left-auto h-full w-[88vw] max-w-[420px] rounded-l-ios-lg
                    pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]
                    pr-[env(safe-area-inset-right)]
                    md:w-[360px] md:rounded-none
                    motion-safe:transition-transform motion-safe:duration-300 ease-out
                    ${open
                      ? 'translate-x-0'
                      : 'translate-x-full'
                    }`}
      >
        {/* Vertical grabber on phones — the small line at the top-left
            of the panel, the iOS side-sheet affordance that says "swipe
            to dismiss". Below the status bar on notched phones.
            ``md:hidden``: the desktop sidebar has a visible edge. */}
        <div
          aria-hidden="true"
          className="md:hidden absolute top-[calc(env(safe-area-inset-top)+0.75rem)] left-2 h-10 w-1 rounded-full bg-label-tertiary"
        />
        {/* Header row — iOS sheet layout: title "Menu" on the leading
            edge, Done on the trailing edge, with the standard hairline
            under it. ``pl-10 md:pl-0`` on the title leaves room for the
            vertical grabber on phones. */}
        <div className="flex items-center justify-between gap-3 px-4 pt-3 pb-3 md:pt-5 md:pb-4 border-b border-hairline shrink-0">
          <h2 className="min-w-0 flex-1 truncate pl-10 md:pl-0 text-2xl md:text-ios-large-title font-bold text-label-primary tracking-tight">
            Menu
          </h2>
          <button
            onClick={onClose}
            aria-label="close menu"
            className="shrink-0 min-h-[44px] min-w-[44px] px-2 flex items-center justify-center rounded-ios text-ios-body font-semibold text-accent active:bg-bg-elevated"
          >
            Done
          </button>
        </div>

        {/* Body. ``min-h-0`` lets the nav actually shrink below its
            content height — without it, ``overflow-y-auto`` is a
            no-op because the flex item refuses to be smaller than
            its contents and the parent grows to fit (overflowing
            the viewport). ``p-4`` matches iOS grouped-list left/
            right margins. Background ``bg-bg-app`` keeps the section
            gaps from showing the sheet's underlying surface. */}
        <nav className="flex-1 min-h-0 overflow-y-auto overscroll-contain bg-bg-app pb-8">
          <GroupedSection>
            <GroupedRow
              onClick={() => {
                handingOff.current = true
                onOpenSettings?.('feeds')
                onClose()
              }}
              title="Settings"
              subtitle="Feeds, AI, history, saved"
              showChevron
            />
            {onEditTabs && (
              <GroupedRow
                onClick={() => {
                  handingOff.current = true
                  onEditTabs()
                  onClose()
                }}
                title="Edit tabs"
                showChevron
              />
            )}
            {/* Saves the current source filter and column settings as
                a named view; its chip appears above the dashboard. */}
            {onSavePreset &&
              (namingView ? (
                <div className="flex items-center gap-2 px-4 py-1 border-b border-hairline last:border-b-0">
                  <input
                    ref={viewNameRef}
                    type="text"
                    aria-label="name for this view"
                    placeholder="Name this view"
                    enterKeyHint="done"
                    autoCorrect="off"
                    value={viewName}
                    onChange={(e) => setViewName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault()
                        saveView()
                      }
                    }}
                    className="flex-1 min-w-0 min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary"
                  />
                  <button
                    type="button"
                    onPointerDown={(e) => e.preventDefault()}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={saveView}
                    disabled={!viewName.trim()}
                    className="shrink-0 min-h-[44px] px-3 rounded-ios bg-accent text-white text-ios-body font-medium active:opacity-80 disabled:opacity-40"
                  >
                    Save
                  </button>
                </div>
              ) : (
                <GroupedRow
                  onClick={() => {
                    // Focus inside the same tap, or iOS won't raise the
                    // keyboard.
                    flushSync(() => setNamingView(true))
                    viewNameRef.current?.focus()
                  }}
                  title="Save current view"
                  subtitle="keep this layout to come back to"
                  showChevron
                />
              ))}
          </GroupedSection>

          {tabs.length > 0 && (
            <GroupedSection label="Jump to column">
              {tabs.map((t) => (
                // Each tab is its own row. Tap → close drawer and select
                // that tab (the phone's strip, the desktop grid's column).
                <GroupedRow
                  key={t.key}
                  title={t.label}
                  onClick={() => {
                    onCategoryJump?.(t.key)
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
                ? `Showing only: ${Array.from(activeSources).map(sourceLabel).join(', ')}`
                : undefined
            }
            action={
              activeSources.size > 0 && onClearAllFilters ? (
                <button
                  onClick={onClearAllFilters}
                  aria-label="clear all source filters"
                  className="min-h-[44px] px-2 -mr-2 text-ios-body text-accent active:opacity-60"
                >
                  Clear
                </button>
              ) : undefined
            }
          >
            {sources.length === 0 ? (
              onRefreshSources ? (
                <GroupedRow
                  onClick={() => { void onRefreshSources() }}
                  title="No sources loaded"
                  subtitle="Tap to try again"
                />
              ) : (
                <p className="px-4 py-3 text-ios-body text-label-secondary">
                  No feeds yet.
                </p>
              )
            ) : (
              // A checkbox list: each row's label toggles the filter, the
              // same multi-select as the chip bar in the header. App's
              // ``toggleSourceAndMaybeClose`` closes the Menu on the
              // first pick so the filtered dashboard shows; later taps
              // keep it open. The › beside each row is a separate
              // button (not inside the label), so it never toggles the
              // filter: it opens that feed in Settings.
              <>
                {sources.map((s) => {
                  const active = activeSources.has(s.name)
                  return (
                    <div
                      key={s.id}
                      className={`flex items-center border-b border-hairline last:border-b-0 ${active ? 'bg-bg-elevated' : ''}`}
                    >
                      <label
                        className={`flex flex-1 min-w-0 items-center gap-3 pl-4 ${onEditSource ? '' : 'pr-4'} min-h-[44px] cursor-pointer transition ${
                          active ? '' : 'active:bg-bg-elevated'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={active}
                          onChange={() => onSourceToggle(s.name)}
                          className="shrink-0 h-4 w-4 accent-accent"
                          aria-label={`filter by ${sourceLabel(s.name)}`}
                        />
                        {/* SourceIcon. Falls back to a colored letter
                            when the favicon hasn't been fetched yet. */}
                        <SourceIcon src={s.favicon_path} name={s.name} size={18} />
                        <span className="flex-1 min-w-0 truncate text-ios-body text-label-primary">
                          {sourceLabel(s.name)}
                        </span>
                        {/* Desktop only: on phones the panel is too narrow
                            for a name, a category and the › side by side,
                            and the name matters more for picking. */}
                        <span className="hidden md:inline shrink min-w-0 max-w-[40%] truncate text-ios-caption text-label-tertiary">
                          {categoryLabel(s.category)}
                        </span>
                      </label>
                      {onEditSource && (
                        <button
                          type="button"
                          aria-label={`edit ${sourceLabel(s.name)}`}
                          onClick={() => {
                            handingOff.current = true
                            onEditSource(s.id)
                            onClose()
                          }}
                          className="w-11 h-11 shrink-0 flex items-center justify-center text-label-tertiary active:bg-bg-elevated"
                        >
                          <ChevronRight className="w-4 h-4" />
                        </button>
                      )}
                    </div>
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
