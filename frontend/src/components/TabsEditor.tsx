// Edit tabs: the tab order and which tabs the strip shows.
//
// A bottom sheet of grouped rows (Grouped.tsx), opened from the Edit pill
// at the end of the phone's tab strip and from the Menu's Edit tabs row.
// Brief is pinned first. Every other tab has ↑ / ↓ (disabled at the
// ends), Move to top (right after Brief) and a Show switch. Each change
// applies at once: App stores it (``category_layout``, one PUT) and the
// strip and the desktop grid follow. Done, Esc or a tap outside closes it.
//
// After a move, focus stays on the same arrow of the moved row, or goes
// to the opposite one when the pressed one is now disabled, and a polite
// live line (empty until then) says where the tab now is ("News, 3 of
// 8"). It is not ``role="status"``: nothing may keep one mounted while
// idle (smoke waits for none).
//
// A dialog with a focus trap (the pattern in Drawer.tsx). Mounted only
// while open; it slides up only under motion-safe.

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { Move } from '../lib/categories'
import { GroupedRow, GroupedSection } from './Grouped'

type Tab = { key: string; label: string }

type Props = {
  open: boolean
  onClose: () => void
  // Every tab after Brief, in order, shown and hidden alike.
  tabs: readonly Tab[]
  // The keys of the hidden tabs.
  hidden: ReadonlySet<string>
  onMove: (key: string, to: Move) => void
  onShow: (key: string, show: boolean) => void
}

// The control focus moves to when the pressed one is now disabled.
const OPPOSITE: Record<Move, Move> = { up: 'down', down: 'up', top: 'down' }

export function TabsEditor({ open, onClose, tabs, hidden, onMove, onShow }: Props) {
  if (!open) return null
  return <Sheet onClose={onClose} tabs={tabs} hidden={hidden} onMove={onMove} onShow={onShow} />
}

function Sheet({ onClose, tabs, hidden, onMove, onShow }: Omit<Props, 'open'>) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const doneRef = useRef<HTMLButtonElement | null>(null)
  // Slides up once mounted (a frame later, so the transition runs).
  const [entered, setEntered] = useState(false)
  const [announce, setAnnounce] = useState('')
  // The move just made, so focus can follow the row once it re-renders.
  const pending = useRef<{ key: string; to: Move } | null>(null)
  const controls = useRef(new Map<string, HTMLButtonElement | null>())
  const controlRef = (key: string, to: Move) => (el: HTMLButtonElement | null) => {
    controls.current.set(`${to}\u0000${key}`, el)
  }

  useEffect(() => {
    const id = requestAnimationFrame(() => {
      setEntered(true)
      doneRef.current?.focus()
    })
    return () => cancelAnimationFrame(id)
  }, [])

  // Esc closes. Capture phase, so App's own Esc handling doesn't run too.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      e.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])

  // Focus trap: Tab and Shift+Tab cycle inside the sheet.
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      const focusables = dialog.querySelectorAll<HTMLElement>('button:not([disabled]), [tabindex]:not([tabindex="-1"])')
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || !dialog.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !dialog.contains(active))) {
        e.preventDefault()
        first.focus()
      }
    }
    dialog.addEventListener('keydown', onKeyDown)
    return () => dialog.removeEventListener('keydown', onKeyDown)
  }, [])

  // A moved row's node is moved in the DOM, which drops its focus: put it
  // back on the same control, or on the opposite one when that is now
  // disabled (the row reached an end).
  useLayoutEffect(() => {
    const p = pending.current
    if (!p) return
    pending.current = null
    const at = (to: Move) => controls.current.get(`${to}\u0000${p.key}`)
    const same = at(p.to)
    const target = same && !same.disabled ? same : at(OPPOSITE[p.to])
    if (target && !target.disabled) target.focus()
  }, [tabs])

  // Every tab is in ``tabs``, so a move lands one place up or down, or
  // right after Brief. The place counts Brief, as the strip does: Move
  // to top makes a tab second.
  const move = (key: string, to: Move) => {
    const i = tabs.findIndex((t) => t.key === key)
    if (i < 0) return
    const at = to === 'top' ? 0 : to === 'up' ? i - 1 : i + 1
    pending.current = { key, to }
    setAnnounce(`${tabs[i].label}, ${at + 2} of ${tabs.length + 1}`)
    onMove(key, to)
  }

  return (
    <div className="fixed inset-0 z-40" role="presentation">
      <div
        aria-hidden="true"
        onClick={onClose}
        className={`absolute inset-0 bg-black/60 motion-safe:transition-opacity motion-safe:duration-200 ${entered ? 'opacity-100' : 'opacity-0'}`}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="edit tabs"
        className={`absolute inset-x-0 bottom-0 mx-auto w-full max-w-lg max-h-[85dvh] flex flex-col bg-bg-app rounded-t-ios-lg shadow-2xl pb-[env(safe-area-inset-bottom)] motion-safe:transition-transform motion-safe:duration-300 ease-out ${
          entered ? 'translate-y-0' : 'translate-y-full'
        }`}
      >
        <div className="shrink-0 flex items-center justify-between gap-3 pl-4 pr-2 pt-2 pb-2 border-b border-hairline">
          <h2 className="min-w-0 flex-1 truncate text-ios-body font-semibold text-label-primary">Edit tabs</h2>
          <button
            ref={doneRef}
            type="button"
            onClick={onClose}
            className="shrink-0 min-h-[44px] min-w-[44px] px-2 flex items-center justify-center rounded-ios text-ios-body font-semibold text-accent active:bg-bg-elevated"
          >
            Done
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain pb-4">
          <GroupedSection>
            <GroupedRow title="Brief" />
            {tabs.map((t, i) => {
              const shown = !hidden.has(t.key)
              return (
                <GroupedRow
                  key={t.key}
                  title={<span className={shown ? '' : 'text-label-secondary'}>{t.label}</span>}
                  trailing={
                    <>
                      <MoveButton
                        buttonRef={controlRef(t.key, 'up')}
                        label={`Move ${t.label} up`}
                        disabled={i === 0}
                        onClick={() => move(t.key, 'up')}
                        icon="up"
                      />
                      <MoveButton
                        buttonRef={controlRef(t.key, 'down')}
                        label={`Move ${t.label} down`}
                        disabled={i === tabs.length - 1}
                        onClick={() => move(t.key, 'down')}
                        icon="down"
                      />
                      <MoveButton
                        buttonRef={controlRef(t.key, 'top')}
                        label={`Move ${t.label} to top`}
                        disabled={i === 0}
                        onClick={() => move(t.key, 'top')}
                        icon="top"
                      />
                      <ShowSwitch label={`Show ${t.label}`} checked={shown} onChange={(next) => onShow(t.key, next)} />
                    </>
                  }
                />
              )
            })}
            {/* PR8a adds its New tab row here. */}
          </GroupedSection>
        </div>
        <p aria-live="polite" className="sr-only">
          {announce}
        </p>
      </div>
    </div>
  )
}

function MoveButton({
  buttonRef,
  label,
  disabled,
  onClick,
  icon,
}: {
  buttonRef: (el: HTMLButtonElement | null) => void
  label: string
  disabled: boolean
  onClick: () => void
  icon: Move
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      data-move={icon}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="w-11 h-11 shrink-0 flex items-center justify-center rounded-full text-accent active:bg-bg-elevated disabled:text-label-tertiary disabled:opacity-40"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="w-5 h-5"
        aria-hidden="true"
      >
        {icon === 'up' && <polyline points="6 15 12 9 18 15" />}
        {icon === 'down' && <polyline points="6 9 12 15 18 9" />}
        {icon === 'top' && (
          <>
            <line x1="5" y1="5" x2="19" y2="5" />
            <polyline points="7 15 12 10 17 15" />
            <line x1="12" y1="10" x2="12" y2="20" />
          </>
        )}
      </svg>
    </button>
  )
}

// An iOS-style switch: a 44px-tall button with role="switch".
function ShowSwitch({ label, checked, onChange }: { label: string; checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className="shrink-0 min-h-[44px] min-w-[51px] flex items-center justify-center"
    >
      <span
        aria-hidden="true"
        className={`relative inline-block w-[51px] h-[31px] rounded-full motion-safe:transition-colors ${checked ? 'bg-accent' : 'bg-bg-elevated'}`}
      >
        <span
          className={`absolute top-[2px] left-[2px] w-[27px] h-[27px] rounded-full bg-white shadow motion-safe:transition-transform ${
            checked ? 'translate-x-5' : 'translate-x-0'
          }`}
        />
      </span>
    </button>
  )
}
