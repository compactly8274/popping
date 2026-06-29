// Keyboard shortcuts overlay. Modal-style sheet that lists every
// keyboard binding the dashboard supports, in the order a power
// user would scan them. Mirrors the iOS "press ? to show keyboard
// shortcuts" pattern (Finder, GitHub, Slack all do this).
//
// Why a sheet rather than inline text:
//   - Discoverability without crowding the UI: the shortcuts live
//     behind a single key (the user invokes it once, reads the
//     list, then closes it).
//   - One-shot learning surface — opening the Drawer would muddle
//     the bindings with the source-list sections.
//
// Rendered into the App tree via a portal-ish approach (sibling of
// the Drawer) but kept simple as a regular child — the App-level
// z-stack already isolates it under the same z as the Drawer.

import { useEffect } from 'react'

type Props = {
  open: boolean
  onClose: () => void
}



type Binding = { key: string; label: string }

const GLOBAL_BINDINGS: Binding[] = [
  { key: '?', label: 'show this shortcuts sheet' },
  { key: '/', label: 'focus search' },
  { key: 'r', label: 'refresh dashboard' },
  { key: 'Esc', label: 'close drawer / clear search' },
]

const NAVIGATION_BINDINGS: Binding[] = [
  { key: '← →', label: 'move column selection' },
  { key: '↑ ↓', label: 'move card selection within column' },
  { key: 'Enter', label: 'open the selected card' },
]

const CARD_BINDINGS: Binding[] = [
  { key: 'm', label: 'mark selected card read' },
  { key: 's', label: 'toggle inline summary on selected card' },
]

export function ShortcutsSheet({ open, onClose }: Props) {
  // Esc closes — matches Drawer's behaviour. Use a capture-phase
  // listener so we beat the global Esc handler in App.tsx when both
  // the sheet and the Drawer could be open.
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
      // Backdrop. Click-outside closes. z-40 sits above the Drawer
      // (z-30) so the user can't reach the Drawer while the sheet
      // is showing.
      className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        // Card. The rounded-ios-lg matches the drawer popover
        // treatment for visual consistency. ``onClick`` is a no-op
        // so a click inside the card doesn't bubble to the backdrop
        // and close the sheet.
        className="w-full max-w-md max-h-[80vh] overflow-y-auto bg-bg-elevated rounded-ios-lg p-4 space-y-4"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="keyboard shortcuts"
      >
        <header className="flex items-center justify-between">
          <h2 className="text-ios-body text-label-primary font-semibold">
            Keyboard shortcuts
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="close shortcuts"
            className="text-label-secondary active:text-label-primary min-h-[32px] min-w-[32px] flex items-center justify-center"
          >
            ✕
          </button>
        </header>
        <Section title="General" bindings={GLOBAL_BINDINGS} />
        <Section title="Navigation" bindings={NAVIGATION_BINDINGS} />
        <Section title="Cards" bindings={CARD_BINDINGS} />
        <p className="text-ios-caption text-label-tertiary">
          Shortcuts are disabled while a text field has focus.
        </p>
      </div>
    </div>
  )
}

function Section({ title, bindings }: { title: string; bindings: Binding[] }) {
  return (
    <section className="space-y-1">
      <h3 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
        {title}
      </h3>
      <ul className="space-y-1">
        {bindings.map((b) => (
          <li
            key={b.key}
            className="flex items-center justify-between gap-3 text-ios-caption"
          >
            <span className="text-label-secondary">{b.label}</span>
            <KeyCap>{b.key}</KeyCap>
          </li>
        ))}
      </ul>
    </section>
  )
}

// Visually-presented key cap. Inline-block pill with a subtle hairline
// border, monospace-style via the SF Mono fallback in our font stack
// (system-ui picks up the user's chosen monospace). Kept inline (no
// separate component file) because it's only used here.
function KeyCap({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="shrink-0 rounded-ios bg-bg-surface border border-hairline px-2 py-0.5 text-ios-caption text-label-primary font-mono">
      {children}
    </kbd>
  )
}