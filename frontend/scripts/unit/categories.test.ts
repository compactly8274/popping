import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CATEGORY_OPTIONS,
  categoryOptions,
  fold,
  fullOrder,
  moveKey,
  normalizeLayout,
  orderColumns,
  resolveNewCategory,
} from '../../src/lib/categories.ts'
import type { CategoryLayout, TabItem } from '../../src/lib/categories.ts'

// A copy of categoryLabel (src/lib/labels.ts) without its acronym list,
// enough for these cases: names with a capital are kept as they are.
const label = (name: string) =>
  /[A-Z]/.test(name)
    ? name
    : name
        .split(/[_\s-]+/)
        .filter(Boolean)
        .map((w) => (w === 'nhl' ? 'NHL' : w.charAt(0).toUpperCase() + w.slice(1)))
        .join(' ')

const resolve = (input: string, existing: string[] = []) => resolveNewCategory(input, existing, CATEGORY_OPTIONS, label)

test('fold matches the backend normalisation', () => {
  assert.equal(fold('For_You'), 'for you')
  assert.equal(fold('for-you'), 'for you')
  assert.equal(fold(' Saved_ '), 'saved')
  assert.equal(fold('Tech  News'), 'tech news')
})

test('a suggestion is reused by its slug', () => {
  assert.deepEqual(resolve('Tech'), { ok: true, value: 'tech', reused: 'suggestion', text: 'Tech' })
})

test('an existing category that folds the same is reused as stored', () => {
  assert.deepEqual(resolve('tech_news', ['Tech News']), { ok: true, value: 'Tech News', reused: 'existing', text: 'Tech News' })
  assert.deepEqual(resolve('Tech_News', ['tech news']), { ok: true, value: 'tech news', reused: 'existing', text: 'Tech News' })
  assert.deepEqual(resolve('NEWS', ['news']), { ok: true, value: 'news', reused: 'existing', text: 'News' })
})

test('an exact twin wins over a folded one', () => {
  const r = resolve('News', ['news', 'News'])
  assert.ok(r.ok && r.value === 'News')
})

test('length counts code points, like the backend', () => {
  assert.deepEqual(resolve('x'.repeat(41)), { ok: false, message: 'Keep it to 40 characters or fewer.' })
  const emoji = '\u{1F3D2}'.repeat(21) // 42 UTF-16 units
  const r = resolve(emoji)
  assert.ok(r.ok)
})

test('reserved names are refused in every spelling', () => {
  for (const name of ['for_you', 'For_You', 'For-You', 'saved_', ' Saved ', 'For You']) {
    const r = resolve(name)
    assert.equal(r.ok, false, name)
    assert.equal(!r.ok && r.message, `“${name.trim()}” is used by the app. Pick another name.`)
  }
})

test('inner whitespace collapses, case is kept, empty is refused', () => {
  assert.deepEqual(resolve('  Ice   Hockey '), { ok: true, value: 'Ice Hockey', reused: null, text: 'Ice Hockey' })
  assert.deepEqual(resolve('NHL'), { ok: true, value: 'NHL', reused: null, text: 'NHL' })
  assert.deepEqual(resolve('   '), { ok: false, message: 'Type a name.' })
})

test('yours lists every category in use; twins show their stored text', () => {
  const sources = [{ category: 'news' }, { category: 'News' }, { category: 'news ' }, { category: 'tech' }, { category: 'tech' }]
  const { yours } = categoryOptions(sources, 'tech', label)
  assert.deepEqual(
    yours.map((o) => [o.value, o.text, o.count]),
    [
      ['news', 'News (“news”)', 1],
      ['News', 'News (“News”)', 1],
      ['news ', 'News (“news ”)', 1],
      ['tech', 'Tech', 2],
    ],
  )
})

test('suggestions leave out anything already in use', () => {
  const { suggestions } = categoryOptions([{ category: 'Tech' }], 'Tech', label)
  assert.ok(!suggestions.some((s) => s.value === 'tech'))
  assert.ok(suggestions.some((s) => s.value === 'news'))
})

test("the Add form's default sits in Suggestions on a fresh install", () => {
  const { yours, suggestions } = categoryOptions([], 'news', label, { defaultInSuggestions: true })
  assert.deepEqual(yours, [])
  assert.deepEqual(suggestions.map((s) => s.value), CATEGORY_OPTIONS)
})

test('a current value no feed uses still gets an option', () => {
  const add = categoryOptions([{ category: 'news' }], 'Hockey', label, { defaultInSuggestions: true })
  assert.deepEqual(add.yours.find((o) => o.value === 'Hockey'), { value: 'Hockey', text: 'Hockey (new)', count: 0 })
  const editor = categoryOptions([{ category: 'news' }], 'Hockey', label)
  assert.deepEqual(editor.yours.find((o) => o.value === 'Hockey'), { value: 'Hockey', text: 'Hockey', count: 0 })
})

test('yours sorts by label, ignoring case', () => {
  const { yours } = categoryOptions([{ category: 'tech' }, { category: 'Hockey' }, { category: 'nhl' }, { category: 'apple' }], 'tech', label)
  assert.deepEqual(yours.map((o) => o.value), ['apple', 'Hockey', 'nhl', 'tech'])
})

test('yours follows the tab order when one is given, then the rest by label', () => {
  const sources = [{ category: 'tech' }, { category: 'Hockey' }, { category: 'nhl' }, { category: 'apple' }]
  const order = ['meta:saved', 'cat:tech', 'meta:foryou', 'cat:nhl']
  const { yours } = categoryOptions(sources, 'tech', label, { order })
  assert.deepEqual(yours.map((o) => o.value), ['tech', 'nhl', 'apple', 'Hockey'])
  // An empty order, or none, is label order.
  assert.deepEqual(categoryOptions(sources, 'tech', label, { order: [] }).yours.map((o) => o.value), ['apple', 'Hockey', 'nhl', 'tech'])
  // A typed-in category the order doesn't name yet goes after the ordered ones.
  const add = categoryOptions(sources, 'Curling', label, { order, defaultInSuggestions: true })
  assert.deepEqual(add.yours.map((o) => o.value), ['tech', 'nhl', 'apple', 'Curling', 'Hockey'])
})

// --- tab order (category_layout) ---------------------------------------------

const t = (key: string, text?: string): TabItem => ({ key, label: text ?? label(key.replace(/^cat:/, '')) })
const BRIEF = t('meta:brief', 'Brief')
const SAVED = t('meta:saved', 'Saved')
const FORYOU = t('meta:foryou', 'For You')
const NEWS = t('cat:news')
const DEALS = t('cat:deals')
const NHL = t('cat:nhl')
const HOCKEY = t('cat:Hockey')
const keys = (items: TabItem[]) => items.map((i) => i.key)
const layout = (order: string[], hidden: string[] = []): CategoryLayout => ({ v: 1, order, hidden })

test('no layout: Brief, Saved, For You, then the categories by label', () => {
  const r = orderColumns([NHL, NEWS, FORYOU, HOCKEY, SAVED, BRIEF, DEALS], null)
  assert.deepEqual(keys(r.shown), ['meta:brief', 'meta:saved', 'meta:foryou', 'cat:deals', 'cat:Hockey', 'cat:news', 'cat:nhl'])
  assert.deepEqual(r.hidden, [])
  assert.deepEqual(r.all, r.shown)
  // A new category lands in label order.
  const more = orderColumns([BRIEF, SAVED, FORYOU, NEWS, DEALS, t('cat:finance')], null)
  assert.deepEqual(keys(more.shown).slice(3), ['cat:deals', 'cat:finance', 'cat:news'])
})

test('a layout orders the tabs; tabs it does not name go at the end, by label', () => {
  const l = layout(['cat:news', 'meta:foryou', 'meta:saved', 'cat:deals'])
  const r = orderColumns([BRIEF, SAVED, FORYOU, NEWS, DEALS, NHL, HOCKEY], l)
  assert.deepEqual(keys(r.shown), ['meta:brief', 'cat:news', 'meta:foryou', 'meta:saved', 'cat:deals', 'cat:Hockey', 'cat:nhl'])
  // A key the layout names but that isn't a tab now keeps its place in the full order.
  const gone = layout(['cat:news', 'cat:gone', 'cat:deals'])
  assert.deepEqual(fullOrder([BRIEF, SAVED, FORYOU, NEWS, DEALS], gone), ['cat:news', 'cat:gone', 'cat:deals', 'meta:saved', 'meta:foryou'])
  assert.deepEqual(keys(orderColumns([BRIEF, SAVED, FORYOU, NEWS, DEALS], gone).shown), [
    'meta:brief',
    'cat:news',
    'cat:deals',
    'meta:saved',
    'meta:foryou',
  ])
})

test('hidden tabs leave the strip but keep their place; Brief is never hidden or moved', () => {
  const l = layout(['meta:saved', 'cat:news', 'meta:foryou', 'cat:deals'], ['cat:news', 'meta:saved', 'meta:brief'])
  const r = orderColumns([BRIEF, SAVED, FORYOU, NEWS, DEALS], l)
  assert.deepEqual(keys(r.shown), ['meta:brief', 'meta:foryou', 'cat:deals'])
  assert.deepEqual(keys(r.hidden), ['meta:saved', 'cat:news'])
  assert.deepEqual(keys(r.all), ['meta:brief', 'meta:saved', 'cat:news', 'meta:foryou', 'cat:deals'])
  // Brief in a stored order is ignored: it stays first.
  assert.deepEqual(keys(orderColumns([BRIEF, NEWS, DEALS], layout(['cat:deals', 'meta:brief', 'cat:news'])).all), ['meta:brief', 'cat:deals', 'cat:news'])
})

test('moveKey: one place up or down, to the top, unchanged at the ends', () => {
  const order = ['meta:saved', 'meta:foryou', 'cat:deals', 'cat:news']
  assert.deepEqual(moveKey(order, 'cat:deals', 'up'), ['meta:saved', 'cat:deals', 'meta:foryou', 'cat:news'])
  assert.deepEqual(moveKey(order, 'cat:deals', 'down'), ['meta:saved', 'meta:foryou', 'cat:news', 'cat:deals'])
  // Move to top puts the tab right after Brief, which is never in the order.
  assert.deepEqual(moveKey(order, 'cat:news', 'top'), ['cat:news', 'meta:saved', 'meta:foryou', 'cat:deals'])
  assert.deepEqual(moveKey(order, 'meta:saved', 'up'), order)
  assert.deepEqual(moveKey(order, 'cat:news', 'down'), order)
  assert.deepEqual(moveKey(order, 'meta:saved', 'top'), order)
  assert.deepEqual(moveKey(order, 'cat:missing', 'up'), order)
  assert.notEqual(moveKey(order, 'cat:news', 'down'), order, 'a copy, never the same array')
})

test('moveKey steps over keys that are not tabs now', () => {
  const order = ['cat:news', 'cat:gone', 'cat:deals', 'cat:old']
  const present = (k: string) => k !== 'cat:gone' && k !== 'cat:old'
  assert.deepEqual(moveKey(order, 'cat:deals', 'up', present), ['cat:deals', 'cat:news', 'cat:gone', 'cat:old'])
  assert.deepEqual(moveKey(order, 'cat:news', 'down', present), ['cat:gone', 'cat:deals', 'cat:news', 'cat:old'])
  // Nothing present below: unchanged.
  assert.deepEqual(moveKey(order, 'cat:deals', 'down', present), order)
})

test('moving the last tab to second place takes one Move to top', () => {
  const available = [BRIEF, SAVED, FORYOU, DEALS, NEWS, NHL]
  const order = fullOrder(available, null)
  const next = layout(moveKey(order, 'cat:nhl', 'top'))
  assert.deepEqual(keys(orderColumns(available, next).shown).slice(0, 2), ['meta:brief', 'cat:nhl'])
})

test('normalizeLayout drops non-string, duplicate and over-long keys, and Brief', () => {
  assert.deepEqual(normalizeLayout({ v: 1, order: ['cat:news', 3, null, 'cat:news', '', 'meta:brief', 'x'.repeat(201), 'meta:saved'], hidden: ['cat:deals', 'cat:deals', {}] }), {
    v: 1,
    order: ['cat:news', 'meta:saved'],
    hidden: ['cat:deals'],
  })
  assert.deepEqual(normalizeLayout({ v: 1 }), { v: 1, order: [], hidden: [] })
  assert.deepEqual(normalizeLayout({ v: 1, order: 'cat:news', hidden: { a: 1 } }), { v: 1, order: [], hidden: [] })
  for (const raw of [null, undefined, 'x', 3, [], ['cat:news'], { order: ['cat:news'] }, { v: 2, order: ['cat:news'] }]) {
    assert.equal(normalizeLayout(raw), null, JSON.stringify(raw))
  }
  const many = Array.from({ length: 700 }, (_, i) => `cat:c${i}`)
  assert.equal(normalizeLayout({ v: 1, order: many, hidden: [] })!.order.length, 500)
})
