// What the feed editor shows about a field's save: a small status at
// the end of its label ("Saving…", "Saved ✓") and, when a save fails,
// the reason under the field with Undo and Try again. The state comes
// from lib/useSaveState.ts.

import { type SaveState } from '../../lib/sourceSaves'

// Keeps focus in the field when a button is tapped (WebKit doesn't
// focus buttons on tap, so the field would blur and commit first).
const keepFocus = (e: React.SyntheticEvent) => e.preventDefault()

export function SaveStatus({ state }: { state: SaveState }) {
  const text =
    state.status === 'saving'
      ? 'Saving…'
      : state.status === 'slow'
        ? 'Still saving…'
        : state.status === 'saved'
          ? 'Saved ✓'
          : state.status === 'error'
            ? 'Not saved'
            : ''
  return (
    <span
      aria-live="polite"
      className={`shrink-0 whitespace-nowrap text-ios-caption ${
        state.status === 'error' ? 'text-red-300' : state.status === 'saved' ? 'text-emerald-300' : 'text-label-tertiary'
      }`}
    >
      {text}
    </span>
  )
}

/** A message under a field, with optional Undo and Try again. */
export function FieldError({
  message,
  onUndo,
  onRetry,
}: {
  message: string
  onUndo?: () => void
  onRetry?: () => void
}) {
  return (
    <div className="mt-1 space-y-1">
      <p role="alert" className="text-ios-caption text-red-300 break-words">
        {message}
      </p>
      {(onUndo || onRetry) && (
        <div className="flex flex-wrap gap-2">
          {onRetry && (
            <button
              type="button"
              onPointerDown={keepFocus}
              onMouseDown={keepFocus}
              onClick={onRetry}
              className="min-h-[44px] px-3 rounded-ios border border-hairline text-accent text-ios-body active:opacity-60"
            >
              Try again
            </button>
          )}
          {onUndo && (
            <button
              type="button"
              onPointerDown={keepFocus}
              onMouseDown={keepFocus}
              onClick={onUndo}
              className="min-h-[44px] px-3 rounded-ios border border-hairline text-label-primary text-ios-body active:opacity-60"
            >
              Undo
            </button>
          )}
        </div>
      )}
    </div>
  )
}
