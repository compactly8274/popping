// Single-slot toast at the bottom of the viewport. Used by:
//   - Card.tsx (long-press / context-menu "copy link" / "open in new tab")
//   - Drawer.tsx (error retry notifications, if we decide to surface them)
//
// Intentionally a singleton — calling `toast(msg)` while another toast
// is still on screen replaces it. Multi-stack toast UX is a different
// (worse, for a dashboard) shape; one at a time keeps the noise down.
//
// Auto-dismiss is on a fixed timer (no user-action dismiss needed) —
// the messages are confirmation, not errors that demand attention.
// Errors render in red via the optional `kind` prop.

import { useEffect, useState } from 'react'

type ToastEvent = {
  message: string
  kind?: 'info' | 'error'
}

type Listener = (e: ToastEvent) => void

const listeners = new Set<Listener>()

/** Imperative API. Call from anywhere — no React tree needed. */
export function toast(message: string, kind: ToastEvent['kind'] = 'info') {
  const event: ToastEvent = { message, kind }
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

  // Auto-dismiss. Cleanup ensures rapid back-to-back toasts reset the
  // timer rather than firing on the stale one.
  useEffect(() => {
    if (!current) return
    const id = setTimeout(() => setCurrent(null), 1500)
    return () => clearTimeout(id)
  }, [current])

  if (!current) return null
  // Two-tone color scheme. Errors use a warm red fill so the toast
  // reads as a "things went wrong" signal even from across the
  // screen; the default toast is a translucent elevated card so it
  // blends with the iOS-style surface palette.
  const color =
    current.kind === 'error'
      ? 'bg-red-500/15 border-red-500/40 text-red-100 supports-[backdrop-filter]:backdrop-blur'
      : 'bg-bg-elevated/95 border-hairline text-label-primary supports-[backdrop-filter]:backdrop-blur'

  return (
    <div
      // Fixed bottom-center; pointer-events-none so it never intercepts
      // taps meant for content underneath. Tailwind's `pointer-events-auto`
      // would only matter if the toast had buttons, which it doesn't.
      role="status"
      aria-live="polite"
      className="fixed inset-x-0 bottom-6 z-50 flex justify-center pointer-events-none"
    >
      <div
        className={`pointer-events-auto rounded-ios border px-4 py-2 text-ios-body shadow-glow-md ${color}`}
      >
        {current.message}
      </div>
    </div>
  )
}