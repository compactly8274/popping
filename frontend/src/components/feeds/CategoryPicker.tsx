// The category control for the Add form and the feed editor
// (REDESIGN_PLAN.md section 7). One native select, so iOS shows its
// own picker, grouped into the categories in use and the suggestions;
// "New category" swaps it for a text field. A suggestion list tied to
// a text field would be invisible on iOS Safari.
//
// Presentational: it reports the final value and the caller saves it.
// Return or Add commits a new category; leaving the field does not.

import { useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { CATEGORY_OPTIONS, categoryOptions, resolveNewCategory } from '../../lib/categories'
import { categoryLabel } from '../../lib/labels'

// Keeps focus in the field when a button is tapped, so the field
// doesn't blur first (WebKit doesn't focus buttons on tap).
const keepFocus = (e: React.SyntheticEvent) => e.preventDefault()

export function CategoryPicker({
  inputId,
  value,
  sources,
  onChange,
  disabled,
  defaultInSuggestions,
  onDraftChange,
  order,
}: {
  inputId: string
  value: string
  sources: ReadonlyArray<{ category: string }>
  onChange: (final: string) => void
  disabled?: boolean
  // The Add form keeps its default ("news") in Suggestions and marks a
  // typed category nobody uses yet "(new)".
  defaultInSuggestions?: boolean
  // True while the new-category field holds text not yet added.
  onDraftChange?: (pending: boolean) => void
  // The tab order (``category_layout``'s ``order``): "Yours" lists the
  // categories in it, as the strip does. Label order without it.
  order?: readonly string[]
}) {
  const [mode, setMode] = useState<'pick' | 'new'>('pick')
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const { yours, suggestions } = useMemo(
    () => categoryOptions(sources, value, categoryLabel, { defaultInSuggestions, order }),
    [sources, value, defaultInSuggestions, order],
  )
  const existing = useMemo(() => Array.from(new Set(sources.map((s) => s.category))), [sources])
  const resolved = draft.trim() ? resolveNewCategory(draft, existing, CATEGORY_OPTIONS, categoryLabel) : null

  // Reported from the handlers, not an effect, so the caller's Add is
  // disabled in the same render as the keystroke.
  const editDraft = (next: string) => {
    setDraft(next)
    onDraftChange?.(next.trim() !== '')
  }

  const startNew = () => {
    // Focus inside the same tap, or iOS won't raise the keyboard.
    flushSync(() => setMode('new'))
    inputRef.current?.focus()
  }
  const cancel = () => {
    setMode('pick')
    editDraft('')
  }
  const commit = () => {
    if (!resolved?.ok) return
    onChange(resolved.value)
    cancel()
  }

  if (mode === 'new') {
    return (
      <div className="space-y-2">
        <input
          ref={inputRef}
          id={inputId}
          type="text"
          autoCapitalize="words"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="done"
          placeholder="e.g. Hockey"
          value={draft}
          disabled={disabled}
          onChange={(e) => editDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              commit()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              cancel()
            }
          }}
          className="w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary"
        />
        {resolved && (
          <p className={`text-ios-caption break-words ${resolved.ok ? 'text-label-secondary' : 'text-red-300'}`}>
            {!resolved.ok
              ? resolved.message
              : resolved.reused === 'existing'
                ? `Using your existing “${resolved.text}” category`
                : `Shows as “${resolved.text}”`}
          </p>
        )}
        <div className="flex gap-2">
          <button
            type="button"
            onPointerDown={keepFocus}
            onMouseDown={keepFocus}
            onClick={commit}
            disabled={disabled || !resolved?.ok}
            className="flex-1 min-h-[44px] rounded-ios bg-accent text-white text-ios-body active:opacity-80 disabled:opacity-40"
          >
            Add
          </button>
          <button
            type="button"
            onPointerDown={keepFocus}
            onMouseDown={keepFocus}
            onClick={cancel}
            className="flex-1 min-h-[44px] rounded-ios border border-hairline text-label-primary text-ios-body active:opacity-60"
          >
            Cancel
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <select
        id={inputId}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="select-ios w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline pl-3 pr-8 text-ios-body text-label-primary disabled:opacity-50"
      >
        {yours.length > 0 && (
          <optgroup label="Your categories">
            {yours.map((o) => (
              <option key={o.value} value={o.value}>
                {o.text}
              </option>
            ))}
          </optgroup>
        )}
        {suggestions.length > 0 && (
          <optgroup label="Suggestions">
            {suggestions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.text}
              </option>
            ))}
          </optgroup>
        )}
      </select>
      <button
        type="button"
        onClick={startNew}
        disabled={disabled}
        className="min-h-[44px] px-3 rounded-ios border border-hairline text-accent text-ios-body active:opacity-60 disabled:opacity-40"
      >
        New category
      </button>
    </div>
  )
}
