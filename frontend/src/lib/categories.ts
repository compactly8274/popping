// Category rules shared by the category picker, the Add form and the
// Suggested and Auto-add paths, and the tab order (``orderColumns``).
// Only pure libs are imported, with their ``.ts`` extension, so the
// unit tests run it under strip-types; the label function is passed in.

import { BRIEF_KEY, FORYOU_KEY, SAVED_KEY, colKey } from './columns.ts'

// Categories offered when nothing else suggests one. The backend
// accepts any 40-character string; these keep new feeds in the
// usual columns instead of near-duplicates like "tech" and "Tech".
export const CATEGORY_OPTIONS: readonly string[] = [
  'news',
  'tech',
  'vulns',
  'science',
  'finance',
  'policy',
  'longform',
  'deals',
  'podcast',
  'video',
  // Leisure categories (0019_leisure_feed_recommendations) — kept
  // after the original work-adjacent set so the dropdown reads as
  // "the usual suspects, then the fun stuff" rather than an
  // alphabetical shuffle that'd bury either group.
  'sports',
  'entertainment',
  'gaming',
  'food',
  'music',
  'other',
]

// Mirrors _CATEGORY_MAX and _RESERVED_CATEGORY_NAMES in
// backend/app/routes/sources.py (a backend test compares them).
export const CATEGORY_MAX = 40
export const RESERVED_CATEGORIES: readonly string[] = ['saved', 'for you']

/** The backend's reserved-name normalisation: lower case, runs of
 *  underscores, dashes and spaces become one space. Every "same
 *  category" comparison uses it. */
export function fold(s: string): string {
  return s.toLowerCase().replace(/[_\s-]+/g, ' ').trim()
}

type Label = (name: string) => string

export type CategoryOption = { value: string; text: string }

/** The picker's two groups. "yours" holds every category a feed
 *  uses, as stored; "suggestions" holds the presets nobody uses yet
 *  (compared with ``fold``). ``current`` always gets an option, so
 *  the select never shows a different value from the one it holds:
 *  the Add form marks a typed-in category "(new)".
 *
 *  "yours" is in tab order when ``order`` (the stored tab order, keys
 *  as in ``category_layout``) is given: the categories it names first,
 *  as it orders them, then the rest by label, as the tabs place a
 *  category the order doesn't name yet. Without it, by label. */
export function categoryOptions(
  sources: ReadonlyArray<{ category: string }>,
  current: string,
  label: Label,
  opts: { defaultInSuggestions?: boolean; order?: readonly string[] } = {},
): { yours: Array<CategoryOption & { count: number }>; suggestions: CategoryOption[] } {
  const counts = new Map<string, number>()
  for (const s of sources) counts.set(s.category, (counts.get(s.category) ?? 0) + 1)
  const yoursFolds = new Set(Array.from(counts.keys(), fold))
  const suggestions = CATEGORY_OPTIONS.filter((c) => !yoursFolds.has(fold(c))).map((c) => ({ value: c, text: label(c) }))

  const added = !counts.has(current) && !suggestions.some((s) => s.value === current) && current !== ''
  if (added) counts.set(current, 0)

  const byLabel = new Map<string, number>()
  for (const raw of counts.keys()) byLabel.set(label(raw), (byLabel.get(label(raw)) ?? 0) + 1)
  const yours = Array.from(counts, ([value, count]) => {
    const base = label(value)
    let text = byLabel.get(base)! > 1 ? `${base} (“${value}”)` : base
    if (added && value === current && opts.defaultInSuggestions) text = `${text} (new)`
    return { value, text, count }
  })
  const rank = new Map<string, number>()
  opts.order?.forEach((key, i) => {
    if (!rank.has(key)) rank.set(key, i)
  })
  const placed = (value: string) => rank.get(colKey(value)) ?? Infinity
  yours.sort((a, b) => {
    const ra = placed(a.value)
    const rb = placed(b.value)
    if (ra !== rb) return ra < rb ? -1 : 1
    return labelOrder(label, a.value, b.value)
  })
  return { yours, suggestions }
}

// Label order, ignoring case; the stored text breaks a tie.
function labelOrder(label: Label, a: string, b: string): number {
  return label(a).localeCompare(label(b), undefined, { sensitivity: 'base' }) || a.localeCompare(b)
}

export type NewCategory =
  | { ok: true; value: string; reused: 'existing' | 'suggestion' | null; text: string }
  | { ok: false; message: string }

/** Check a typed category and say what will be stored. Keeps the case
 *  as typed ("NHL" stays "NHL"), but reuses an existing category or a
 *  suggestion that folds the same, so "NEWS" lands in "news". */
export function resolveNewCategory(
  input: string,
  existing: Iterable<string>,
  suggestions: Iterable<string>,
  label: Label,
): NewCategory {
  const s = input.trim().replace(/\s+/g, ' ')
  if (!s) return { ok: false, message: 'Type a name.' }
  // Code points, like Python's len and Postgres varchar(40).
  if (Array.from(s).length > CATEGORY_MAX) {
    return { ok: false, message: `Keep it to ${CATEGORY_MAX} characters or fewer.` }
  }
  const f = fold(s)
  if (RESERVED_CATEGORIES.includes(f)) {
    return { ok: false, message: `“${s}” is used by the app. Pick another name.` }
  }
  const existingList = Array.from(existing)
  const match = existingList.find((x) => x === s) ?? existingList.find((x) => fold(x) === f)
  if (match !== undefined) return { ok: true, value: match, reused: 'existing', text: label(match) }
  const suggestion = Array.from(suggestions).find((x) => fold(x) === f)
  if (suggestion !== undefined) return { ok: true, value: suggestion, reused: 'suggestion', text: label(suggestion) }
  return { ok: true, value: s, reused: null, text: label(s) }
}

// ---------------------------------------------------------------------------
// Tab order and hiding (``category_layout``)
// ---------------------------------------------------------------------------

/** The stored tab order: ``order`` lists tab keys front to back (Brief
 *  is always first and never listed), ``hidden`` the tabs left out of
 *  the strip. Keys of tabs that are gone for now stay, so a category
 *  that empties and comes back keeps its place and stays hidden. */
export type CategoryLayout = { v: 1; order: string[]; hidden: string[] }

/** A tab as the order sees it: its key and the name it shows. */
export type TabItem = { key: string; label: string }

// Longer than any key the app makes (see MAX_KEY_LENGTH in columns.ts),
// and more keys than any strip holds: anything past these isn't ours.
const MAX_LAYOUT_KEY = 200
const MAX_LAYOUT_KEYS = 500

function layoutKeys(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  const out: string[] = []
  const seen = new Set<string>()
  for (const k of raw) {
    if (typeof k !== 'string' || k === '' || k.length > MAX_LAYOUT_KEY || k === BRIEF_KEY || seen.has(k)) continue
    seen.add(k)
    out.push(k)
    if (out.length === MAX_LAYOUT_KEYS) break
  }
  return out
}

/** A stored ``category_layout`` value, checked: null unless it is a
 *  version 1 object; non-string, duplicate and over-long keys (and
 *  Brief, which is pinned) are dropped from both lists. */
export function normalizeLayout(raw: unknown): CategoryLayout | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  if (r.v !== 1) return null
  return { v: 1, order: layoutKeys(r.order), hidden: layoutKeys(r.hidden) }
}

// The order before anything is stored: Saved, For You, then the rest
// by label (the strip as it always was).
function defaultRank(t: TabItem): number {
  return t.key === SAVED_KEY ? 0 : t.key === FORYOU_KEY ? 1 : 2
}
function byDefault(a: TabItem, b: TabItem): number {
  return defaultRank(a) - defaultRank(b) || labelOrder((s) => s, a.label, b.label) || a.key.localeCompare(b.key)
}

/** Every key in order, Brief left out: the stored order (including tabs
 *  gone for now), then the tabs it doesn't name, in the default order.
 *  With nothing stored, the default order. This is the list ``moveKey``
 *  works on and the order a change stores. */
export function fullOrder(available: readonly TabItem[], layout: CategoryLayout | null): string[] {
  const stored = (layout?.order ?? []).filter((k) => k !== BRIEF_KEY)
  const named = new Set(stored)
  const rest = available.filter((t) => t.key !== BRIEF_KEY && !named.has(t.key)).sort(byDefault)
  return [...stored, ...rest.map((t) => t.key)]
}

/** The tabs in order, split into those the strip shows and those it
 *  hides (``all`` is both, in order, for the tab editor). Brief, when
 *  given, is first and always shown. With no layout: Brief, Saved, For
 *  You, then the categories by label, so a new category lands in label
 *  order. With one, a tab the layout doesn't name yet goes at the end. */
export function orderColumns<T extends TabItem>(
  available: readonly T[],
  layout: CategoryLayout | null,
): { all: T[]; shown: T[]; hidden: T[] } {
  const byKey = new Map<string, T>()
  for (const t of available) if (!byKey.has(t.key)) byKey.set(t.key, t)
  const brief = byKey.get(BRIEF_KEY)
  const all = [...(brief ? [brief] : []), ...fullOrder(available, layout).flatMap((k) => byKey.get(k) ?? [])]
  const hidden = new Set(layout?.hidden ?? [])
  hidden.delete(BRIEF_KEY)
  return {
    all,
    shown: all.filter((t) => !hidden.has(t.key)),
    hidden: all.filter((t) => hidden.has(t.key)),
  }
}

export type Move = 'up' | 'down' | 'top'

/** ``order`` (a ``fullOrder``) with ``key`` moved one place up or down,
 *  past the next key ``present`` accepts (a tab gone for now is stepped
 *  over, not swapped with), or to the top, right after Brief. Unchanged
 *  (a copy) at the ends or when ``key`` isn't there. */
export function moveKey(
  order: readonly string[],
  key: string,
  to: Move,
  present: (key: string) => boolean = () => true,
): string[] {
  const i = order.indexOf(key)
  if (i < 0) return [...order]
  let at = 0
  if (to === 'up') {
    let j = i - 1
    while (j >= 0 && !present(order[j])) j--
    if (j < 0) return [...order]
    at = j
  } else if (to === 'down') {
    let j = i + 1
    while (j < order.length && !present(order[j])) j++
    if (j >= order.length) return [...order]
    at = j
  }
  const out = order.filter((_, n) => n !== i)
  out.splice(at, 0, key)
  return out
}
