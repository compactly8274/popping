// Request headers as rows instead of JSON (REDESIGN_PLAN.md section 8).
// The rules live in lib/headers.ts and mirror the backend's.
//
// Two modes:
// - ``local`` (the Add form): every change reports the map to send and
//   whether it can be sent; the form sends it with Test and Add.
// - ``autosave`` (the feed editor): the map is committed when focus
//   leaves the headers, a row is removed or a preset is tapped, never
//   per keystroke. A row with an error holds the whole commit; a
//   half-filled row is left out and waits.

import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  HEADERS_MAX,
  HEADER_PRESETS,
  blocksSave,
  canonicalHeaders,
  checkRows,
  rowsToMap,
  sameHeaders,
  type HeaderMap,
  type HeaderRow,
} from '../../lib/headers'

// Keeps focus in a header field when a button in the group is tapped,
// so no early blur fires (WebKit doesn't focus buttons on tap).
const keepFocus = (e: React.SyntheticEvent) => e.preventDefault()

const fieldClass =
  'w-full min-w-0 min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary disabled:opacity-50'

type Rows = HeaderRow[]

export function HeadersEditor({
  mode,
  value,
  onCommit,
  onValidChange,
  disabled,
  busy,
  failed,
  reseedToken,
}: {
  mode: 'local' | 'autosave'
  // Local: the rows to start from. Autosave: the saved map.
  value: HeaderMap | null
  // Local: called with the map to send whenever it changes.
  // Autosave: called with a map that differs from ``value``.
  onCommit: (map: HeaderMap | null) => void
  // Local only: false while any row is half-filled or breaks a rule.
  onValidChange?: (ok: boolean) => void
  disabled?: boolean
  // Autosave only: a headers save is queued or in flight, or failed.
  // Either way the rows aren't reseeded from ``value`` meanwhile.
  busy?: boolean
  failed?: boolean
  // Autosave only: a new value reseeds the rows from ``value`` (Undo).
  reseedToken?: number
}) {
  const nextKey = useRef(0)
  const toRows = (map: HeaderMap | null): Rows =>
    Object.entries(map ?? {}).map(([name, v]) => ({ key: nextKey.current++, name, value: v }))
  const [rows, setRows] = useState<Rows>(() => toRows(value))
  // "Fill in both" waits until focus has left the headers.
  const [focusInside, setFocusInside] = useState(false)
  const nameInputs = useRef(new Map<number, HTMLInputElement>())

  const check = checkRows(rows)

  // --- local mode --------------------------------------------------------
  const localOk = !blocksSave(check) && check.problems.length === 0
  const localMap = localOk ? rowsToMap(rows, null) : null
  const localKey = canonicalHeaders(localMap)
  const localMapRef = useRef(localMap)
  localMapRef.current = localMap
  useEffect(() => {
    if (mode === 'local') onValidChange?.(localOk)
  }, [mode, localOk, onValidChange])
  // Keyed on the map's content, so a re-render with the same rows
  // reports nothing new.
  useEffect(() => {
    if (mode === 'local') onCommit(localMapRef.current)
  }, [mode, localKey, onCommit])

  // --- autosave mode -----------------------------------------------------
  // The map the rows were last seeded from or committed, so the
  // server's echo of a commit doesn't reseed rows under the user.
  const lastSeeded = useRef(canonicalHeaders(value))
  const valueKey = canonicalHeaders(value)
  useEffect(() => {
    if (mode !== 'autosave' || focusInside || busy || failed) return
    if (valueKey === lastSeeded.current) return
    lastSeeded.current = valueKey
    // Keep the order of names still present; append new ones (Postgres
    // returns JSONB keys sorted by length, not as typed). A half-filled
    // row was never sent, so it stays as typed.
    const saved = Object.entries(value ?? {})
    setRows((prev) => {
      const kept = prev
        .filter(
          (r) =>
            saved.some(([n]) => n.toLowerCase() === r.name.trim().toLowerCase()) ||
            (r.name.trim() === '') !== (r.value.trim() === ''),
        )
        .map((r) => {
          const hit = saved.find(([n]) => n.toLowerCase() === r.name.trim().toLowerCase())
          return hit ? { key: r.key, name: hit[0], value: hit[1] } : r
        })
      const added = saved
        .filter(([n]) => !kept.some((r) => r.name.toLowerCase() === n.toLowerCase()))
        .map(([n, v]) => ({ key: nextKey.current++, name: n, value: v }))
      return [...kept, ...added]
    })
    // value is read through valueKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, valueKey, focusInside, busy, failed])
  // Undo: back to what is saved.
  const firstToken = useRef(reseedToken)
  useEffect(() => {
    if (reseedToken === firstToken.current) return
    firstToken.current = reseedToken
    lastSeeded.current = canonicalHeaders(value)
    setRows(toRows(value))
    // Only a new token reseeds.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reseedToken])

  const commit = (next: Rows) => {
    if (mode !== 'autosave' || disabled) return
    if (blocksSave(checkRows(next))) return
    const map = rowsToMap(next, value)
    if (sameHeaders(map, value)) return
    lastSeeded.current = canonicalHeaders(map)
    onCommit(map)
  }

  const update = (key: number, patch: Partial<HeaderRow>) =>
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)))

  const addRow = (name = '', v = '') => {
    const key = nextKey.current++
    const next = [...rows, { key, name, value: v }]
    // Focus inside the same tap, or iOS won't raise the keyboard.
    flushSync(() => setRows(next))
    if (!name) nameInputs.current.get(key)?.focus()
    else commit(next)
  }

  const removeRow = (key: number) => {
    const next = rows.filter((r) => r.key !== key)
    setRows(next)
    commit(next)
  }

  const has = (name: string) => rows.some((r) => r.name.trim().toLowerCase() === name.toLowerCase())
  const filled = rows.filter((r) => r.name.trim() || r.value.trim()).length

  return (
    <div
      className="space-y-3"
      onFocus={() => setFocusInside(true)}
      onBlur={(e) => {
        if (e.currentTarget.contains(e.relatedTarget as Node | null)) return
        setFocusInside(false)
        commit(rows)
      }}
    >
      {rows.map((row) => {
        const problem = check.problems.find((p) => p.key === row.key)
        const showProblem = problem && (!problem.incomplete || !focusInside)
        return (
          <div key={row.key} className="space-y-1">
            <div className="grid grid-cols-[1fr_auto] gap-2">
              <input
                ref={(el) => {
                  if (el) nameInputs.current.set(row.key, el)
                  else nameInputs.current.delete(row.key)
                }}
                type="text"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                enterKeyHint="next"
                placeholder="Header name"
                aria-label="header name"
                value={row.name}
                disabled={disabled}
                onChange={(e) => update(row.key, { name: e.target.value })}
                className={fieldClass}
              />
              <button
                type="button"
                onPointerDown={keepFocus}
                onMouseDown={keepFocus}
                onClick={() => removeRow(row.key)}
                disabled={disabled}
                aria-label={`remove header ${row.name.trim() || 'row'}`}
                className="w-11 h-11 flex items-center justify-center rounded-ios text-red-300 active:opacity-60 disabled:opacity-40"
              >
                <svg aria-hidden="true" viewBox="0 0 24 24" className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                  <circle cx="12" cy="12" r="9" />
                  <path d="M8 12h8" />
                </svg>
              </button>
              <input
                type="text"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                enterKeyHint="done"
                placeholder="Value"
                aria-label={`value for ${row.name.trim() || 'header'}`}
                value={row.value}
                disabled={disabled}
                onChange={(e) => update(row.key, { value: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    e.currentTarget.blur()
                  }
                }}
                className={`${fieldClass} col-span-2`}
              />
            </div>
            {showProblem && <p className="text-ios-caption text-red-300 break-words">{problem.message}</p>}
          </div>
        )
      })}
      {check.tooMany && <p className="text-ios-caption text-red-300">{check.tooMany}</p>}
      <div className="flex flex-wrap gap-2">
        {filled < HEADERS_MAX && (
          <button
            type="button"
            onPointerDown={keepFocus}
            onMouseDown={keepFocus}
            onClick={() => addRow()}
            disabled={disabled}
            className="min-h-[44px] px-3 rounded-ios border border-hairline text-accent text-ios-body active:opacity-60 disabled:opacity-40"
          >
            Add header
          </button>
        )}
        {HEADER_PRESETS.filter((p) => !has(p.name)).map((p) => (
          <button
            key={p.name}
            type="button"
            onPointerDown={keepFocus}
            onMouseDown={keepFocus}
            onClick={() => addRow(p.name, p.value)}
            disabled={disabled}
            className="min-h-[44px] px-3 rounded-ios border border-hairline text-accent text-ios-body active:opacity-60 disabled:opacity-40"
          >
            {p.label}
          </button>
        ))}
      </div>
      <p className="text-ios-caption text-label-secondary">
        Some sites block automated readers. A browser identity usually fixes it. Headers are saved as plain text that
        anyone who can open Popping can read, so don't put passwords or keys here.
      </p>
    </div>
  )
}
