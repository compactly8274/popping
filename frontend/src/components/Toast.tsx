// Single-slot toast at the bottom of the viewport.
//
// Supports an optional action button (e.g. an Undo
// affordance for destructive actions). When the
// action is set, the auto-dismiss timer is extended
// to 5 seconds (Material guidance for snackbars with
// an action) so the user has time to read the
// message and decide whether to invoke the action.
//
// Auto-dismiss: 1.5s without an action, 5s with one.
// Errors render in red via the optional `kind` prop.
//
// The toast is a singleton — a new toast replaces
// the previous one. Multi-stack toast UX is a worse
// shape for a dashboard; one at a time keeps the
// noise down.

import { useEffect, useState } from 'react'

type ToastAction = {
  label: string
  onClick: () => void
}

type ToastEvent = {
  message: string
  kind?: 'info' | 'error'
  action?: ToastAction
}

type Listener = (e: ToastEvent) => void

const listeners = new Set<Listener>()

/** Imperative API. Call from anywhere — no React
 *  tree needed.
 *
 *  Two call signatures:
 *    toast(message, kind)              — legacy
 *    toast(message, { kind, action })  — with Undo
 *
 *  The legacy signature is preserved for callers
 *  that don't pass an action. The new signature
 *  adds an Undo-style right-aligned button to
 *  the toast. When action is set, the
 *  auto-dismiss timer extends to 5 seconds.
 */
export function toast(
  message: string,
  options:
    | ToastEvent['kind']
    | { kind?: ToastEvent['kind']; action?: ToastAction } = 'info',
): void {
  // Normalize both signatures into a single
  // ToastEvent shape. The old form passes a
  // string 'kind' directly; the new form passes
  // an options object.
  let kind: ToastEvent['kind']
  let action: ToastAction | undefined
  if (typeof options === 'string') {
    kind = options
  } else {
    kind = options.kind ?? 'info'
    action = options.action
  }
  const event: ToastEvent = { message, kind, action }
  for (const l of listeners) l(event)
}

export function ToastHost() {
  const [current, setCurrent] = useState<ToastEvent | null>(null)

  useEffect(() => {
    const onEvent: Listener = (e) => {
      setCurrent(e)
    }
    listeners.add(onEvent)
    return () => {
      listeners.delete(onEvent)
    }
  }, [])

  // Auto-dismiss. Cleanup ensures rapid back-to-back
  // toasts reset the timer rather than firing on the
  // stale one. The timer is 5s when an action is
  // present (the user needs time to read the message
  // + decide whether to invoke the action), 1.5s
  // otherwise (the prior behavior).
  useEffect(() => {
    if (!current) return
    const id = setTimeout(
      () => setCurrent(null),
      current.action ? 5000 : 1500,
    )
    return () => clearTimeout(id)
  }, [current])

  if (!current) return null
  // Two-tone color scheme. Errors use a warm red
  // fill; the default toast is a translucent elevated
  // card so it blends with the iOS-style surface
  // palette.
  const color =
    current.kind === 'error'
      ? 'bg-red-500/15 border-red-500/40 text-red-100 supports-[backdrop-filter]:backdrop-blur'
      : 'bg-bg-elevated/95 border-hairline text-label-primary supports-[backdrop-filter]:backdrop-blur'

  return (
    <div
      // Fixed bottom-center; pointer-events-none so the
      // wrapper never intercepts taps meant for
      // content underneath. The inner toast is
      // pointer-events-auto so the action button
      // (when present) is clickable.
      role="status"
      aria-live="polite"
      className="fixed inset-x-0 bottom-6 z-50 flex justify-center pointer-events-none"
    >
      <div
        className={`pointer-events-auto flex items-center gap-3 rounded-ios border px-4 py-2 text-ios-body shadow-glow-md max-w-[min(90vw,420px)] ${color}`}
      >
        <span className="flex-1 min-w-0 truncate">{current.message}</span>
        {current.action && (
          <button
            type="button"
            onClick={() => {
              // Fire the action then dismiss. The
              // auto-dismiss timer's cleanup function
              // ensures the toast stays dismissed even
              // if the timer would have fired later.
              current.action!.onClick()
              setCurrent(null)
            }}
            className="shrink-0 text-accent active:opacity-60 font-semibold min-h-[44px] -my-2 -mr-2 px-3 rounded-ios"
            aria-label={current.action.label}
          >
            {current.action.label}
          </button>
        )}
      </div>
    </div>
  )
}
