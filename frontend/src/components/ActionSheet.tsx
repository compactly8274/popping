// A card's ⋯ sheet (PR5a, D1): Hide, Edit feed, Copy link and Open in
// browser, as 44px rows rising from the bottom of the screen, under the
// story's title. ``openActionSheet({ title, actions })`` is the
// imperative API (as ``toast`` is); ``<ActionSheetHost/>`` renders it,
// once, in App.
//
// A dialog with a focus trap (the pattern in Drawer.tsx and
// TabsEditor.tsx), mounted only while open: Esc, a tap outside and
// Cancel close it. Esc is taken in the capture phase and stopped, so
// App's own Esc (which clears the search) never sees it. Focus starts
// on Cancel, as Drawer's starts on Done: a Space that opened the sheet
// from ⋯ lifts on a row that does nothing.
//
// Closing puts the focus back where it was, when that element is still
// in the page. After Hide the card that opened the sheet is gone, so it
// goes to the next card's headline, or the previous one's, or the tab.
// The cards are noted when the sheet opens, in order, and the first of
// them still in the page after the opener's place is the next.
//
// It counts in App's ``modalOpen`` (``onOpenChange``), so seen on
// scroll pauses while it is up.

import { useCallback, useEffect, useRef, useState } from 'react'

export type SheetAction = {
  label: string
  onClick: () => void
  // Greyed out and inert (a Share-like row for a story without an
  // http(s) address).
  disabled?: boolean
  // Red: the row takes something away (Hide).
  destructive?: boolean
}

type SheetRequest = {
  title: string
  actions: SheetAction[]
  // The control that opened the sheet (a card's ⋯). Focus goes back to
  // it on close, and its card is the one a Hide takes away. iOS gives
  // a tapped button no focus, so ``document.activeElement`` alone
  // wouldn't say which card it was.
  from?: Element | null
}

type Listener = (r: SheetRequest) => void

const listeners = new Set<Listener>()

/** Opens the sheet with ``actions`` under ``title``. The host closes it
 * after a row's ``onClick``. */
export function openActionSheet(request: SheetRequest): void {
  for (const l of listeners) l(request)
}

const HEADLINE = 'a[aria-label="open in new tab"]'

export function ActionSheetHost({ onOpenChange }: { onOpenChange?: (open: boolean) => void }) {
  const [request, setRequest] = useState<SheetRequest | null>(null)
  // Where the focus goes back to: the element that had it when the
  // sheet opened, and the cards on its list then, with the opener's
  // place among them.
  const returnTo = useRef<{ from: Element | null; cards: HTMLElement[]; at: number } | null>(null)

  useEffect(() => {
    const onRequest: Listener = (r) => {
      const from = r.from ?? document.activeElement
      const card = from?.closest<HTMLElement>('article[data-card-id]') ?? null
      const list = card?.closest<HTMLElement>('[data-scroller], main') ?? null
      const cards = list ? Array.from(list.querySelectorAll<HTMLElement>('article[data-card-id]')) : []
      returnTo.current = { from, cards, at: card ? cards.indexOf(card) : -1 }
      setRequest(r)
    }
    listeners.add(onRequest)
    return () => {
      listeners.delete(onRequest)
    }
  }, [])

  const open = request !== null
  useEffect(() => {
    onOpenChange?.(open)
  }, [open, onOpenChange])

  const close = useCallback(() => {
    setRequest(null)
    const back = returnTo.current
    returnTo.current = null
    if (!back) return
    // After React has taken the sheet (and a hidden card) out.
    requestAnimationFrame(() => {
      const { from, cards, at } = back
      // A row opened another sheet (Edit feed → Settings): that sheet
      // has the page now, and focus must not go to a control under it.
      // The closed Drawer is always mounted, inert, so it doesn't count.
      const covered = Array.from(document.querySelectorAll('[role="dialog"][aria-modal="true"]')).some(
        (d) => !d.hasAttribute('inert') && !(from && d.contains(from)),
      )
      if (covered) return
      if (from instanceof HTMLElement && from.isConnected && from.getClientRects().length) {
        from.focus({ preventScroll: true })
        return
      }
      const still = (c: HTMLElement) => c.isConnected && c.getClientRects().length > 0
      const next = cards.slice(at + 1).find(still) ?? cards.slice(0, Math.max(at, 0)).reverse().find(still)
      // No card left (Hide took the only search result): the search
      // field, which is there while the results stand in for the list
      // and the tab strip isn't; else the selected tab.
      const target =
        next?.querySelector<HTMLElement>(HEADLINE) ??
        document.getElementById('app-search') ??
        document.querySelector<HTMLElement>('[role="tablist"][aria-label="dashboard sections"] [role="tab"][aria-selected="true"]')
      target?.focus({ preventScroll: true })
    })
  }, [])

  if (!request) return null
  return <Sheet request={request} onClose={close} />
}

function Sheet({ request, onClose }: { request: SheetRequest; onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)
  // Slides up once mounted (a frame later, so the transition runs).
  const [entered, setEntered] = useState(false)

  useEffect(() => {
    const id = requestAnimationFrame(() => {
      setEntered(true)
      cancelRef.current?.focus()
    })
    return () => cancelAnimationFrame(id)
  }, [])

  // Esc closes. Capture phase, and stopped, so App's own Esc handling
  // (which clears the search) doesn't run too.
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
        aria-label="story actions"
        className={`absolute inset-x-0 bottom-0 mx-auto w-full max-w-lg max-h-[85dvh] flex flex-col bg-bg-app rounded-t-ios-lg shadow-2xl pb-[env(safe-area-inset-bottom)] motion-safe:transition-transform motion-safe:duration-300 ease-out ${
          entered ? 'translate-y-0' : 'translate-y-full'
        }`}
      >
        <div className="shrink-0 px-4 pt-3 pb-2 border-b border-hairline">
          <p className="text-ios-caption text-label-secondary line-clamp-2 break-words">{request.title}</p>
        </div>
        {/* The rows as an iOS grouped list (Grouped.tsx's look): a rounded
            card 16px in from the screen's edges, hairlines between. */}
        <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain px-4 py-3">
          <div className="rounded-ios bg-bg-surface overflow-hidden divide-y divide-hairline">
            {request.actions.map((a) => (
              <button
                key={a.label}
                type="button"
                data-sheet-action
                disabled={a.disabled}
                onClick={() => {
                  // The row's work first, then the sheet goes: both land in
                  // one render, so a Hide's card and the sheet leave together.
                  a.onClick()
                  onClose()
                }}
                className={`w-full min-h-[44px] px-4 text-left text-ios-body active:bg-bg-elevated disabled:opacity-40 ${
                  a.destructive ? 'text-red-400' : 'text-label-primary'
                }`}
              >
                {a.label}
              </button>
            ))}
          </div>
        </div>
        <div className="shrink-0 px-4 pb-3">
          <button
            ref={cancelRef}
            type="button"
            data-sheet-cancel
            onClick={onClose}
            className="w-full min-h-[44px] px-4 rounded-ios bg-bg-surface text-ios-body font-semibold text-accent active:bg-bg-elevated"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
