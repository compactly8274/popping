#!/usr/bin/env node
// Popping iPhone overflow audit.
//
// Usage (from any cwd):
//   cd /home/user/popping/frontend && npm run build && (npx vite preview --port 4173 --host 127.0.0.1 &)
//   node /tmp/.../scratchpad/shots/capture.cjs [--base http://127.0.0.1:4173] [--only 390|320]
//
// Requires the `playwright` package resolvable via require() (it is, from
// /opt/node-tools/node_modules) and PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers.
'use strict'

const path = require('path')
const fs = require('fs')
const { chromium } = require('playwright')
const F = require('./fixtures.cjs')

const OUT = __dirname
const argv = process.argv.slice(2)
const argOf = (k, d) => {
  const i = argv.indexOf(k)
  return i >= 0 ? argv[i + 1] : d
}
const BASE = argOf('--base', 'http://127.0.0.1:4173')
const ONLY = argOf('--only', null)

const IOS_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1'

const DEVICES = [
  { tag: '390', viewport: { width: 390, height: 844 }, deviceScaleFactor: 3 },
  { tag: '320', viewport: { width: 320, height: 568 }, deviceScaleFactor: 2 },
].filter((d) => !ONLY || d.tag === ONLY)

// ---------------------------------------------------------------------------
// API mock
// ---------------------------------------------------------------------------
function svgImage(w, h, label, hue) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="${w}" height="${h}" fill="hsl(${hue} 40% 35%)"/><circle cx="${w * 0.3}" cy="${h * 0.4}" r="${Math.min(w, h) * 0.18}" fill="hsl(${(hue + 60) % 360} 60% 60%)"/><text x="${w / 2}" y="${h * 0.85}" font-family="sans-serif" font-size="${Math.max(10, h / 6)}" fill="#fff" text-anchor="middle">${label}</text></svg>`
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: { 'access-control-allow-origin': '*' },
    body: JSON.stringify(body),
  })
}

const log = []
// The set keys (routes/preferences.py SET_KEYS, from SET_KEY_CAPS): PATCH
// only. A PUT of one is a 409 with this detail and changes nothing.
const SET_KEY_PUT_DETAIL = 'Popping was updated. Reload to keep saving.'
const SET_KEY_RE = /^(?:starred_entries|hidden_entries|voted_entries|(?:read_entries|seen):[\s\S]+)$/
// Their caps, as SET_KEY_CAPS: the most a row holds, and the most ids one
// PATCH list may carry.
const setKeyCap = (key) => (key === 'voted_entries' ? 2000 : 1000)
// A PATCH body as schemas.UserPreferencePatchIn takes it (a 422 otherwise):
// only add, remove and set; ids are integers from 1 to 2**63-1, at most
// 1000 per list; set maps a canonical id string to up, down or null, at
// most 2000 of them.
const isEntryId = (x) => typeof x === 'number' && Number.isInteger(x) && x >= 1 && x <= 2 ** 63 - 1
const VOTE_ID_RE = /^[1-9][0-9]{0,18}$/
function patchBodyError(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return 'the body is not an object'
  const extra = Object.keys(body).filter((k) => !['add', 'remove', 'set'].includes(k))
  if (extra.length) return `unknown fields: ${extra.join(', ')}`
  for (const op of ['add', 'remove']) {
    if (!(op in body)) continue
    if (!Array.isArray(body[op]) || body[op].length > 1000 || !body[op].every(isEntryId)) return `${op} must be at most 1000 entry ids`
  }
  if ('set' in body) {
    const set = body.set
    if (!set || typeof set !== 'object' || Array.isArray(set)) return 'set must be an object'
    const entries = Object.entries(set)
    if (entries.length > 2000) return 'set takes at most 2000 votes'
    for (const [id, v] of entries) {
      if (!VOTE_ID_RE.test(id) || BigInt(id) > 2n ** 63n - 1n) return `set: ${id} is not an entry id`
      if (v !== null && v !== 'up' && v !== 'down') return `set: ${id} must be up, down or null`
    }
  }
  return null
}
// One GET /api/entries/by-ids row (schemas.EntryOut): the card fields of
// routes/entries.py's _CARD_COLUMNS (the meta keys a card renders are
// top-level fields there, as in the fixtures), the preview the projection
// computes from them (PR5a, D34; the fixtures' ``preview``) and the feed's
// name. Never the meta blob, so code that read it would come up empty
// here as it would against the server.
const BY_ID_FIELDS = [
  'id', 'source_id', 'title', 'url', 'published_at', 'fetched_at', 'composite_score', 'personal_score', 'raw_score',
  'cached_summary', 'image_url', 'image_path', 'story_cluster_id', 'reddit_thread_url', 'reddit_comment_count',
  'audio_url', 'duration_seconds', 'transcript_url', 'preview',
]
function byIdRow(e, sourceName) {
  const row = {}
  for (const k of BY_ID_FIELDS) row[k] = e[k] ?? null
  row.source_name = sourceName
  return row
}
// mockApi's preference store per context, for flows:
//   prefMock(context).seed({ key: value })  rows as if the server had
//       them (before the page loads, or before a reload)
//   .value(key), .keys()                     what the store holds
//   .fail(status, { method, key, times })    the next `times` (1)
//       `method` (PATCH) requests (to `key`, or any) answer `status`
//       and change nothing: 503 once for a retry, 405 for a server
//       rolled back to before PATCH
//   .faultsLeft()                            queued faults not yet used
const PREF_MOCKS = new WeakMap()
const prefMock = (context) => PREF_MOCKS.get(context)
// mockApi's feeds and stories per context, for flows that need rows the
// server's own cuts apply to (the window, the per-feed cap, the
// per-category limit), which a story added on the page's side
// (withStories) skips:
//   dbMock(context).addSources(rows)   feeds as GET /api/sources lists them
//   .addEntries(rows)                  stories as the server holds them
// Call it in a flow's `before`, before the page loads.
const DB_MOCKS = new WeakMap()
const dbMock = (context) => DB_MOCKS.get(context)
// Mock the backend for one browser context.
//   mockApi(context, { onUnmocked })
// The route is anchored on the pathname, so a page path such as
// /static/assets-x.js or a query string containing "/api/" is never caught.
// Each call gets its own deep copy of F.SOURCES and F.ENTRIES: POST, PATCH and
// DELETE on /api/sources mutate that copy and every GET reads it, so a flow
// sees its own edits and contexts never share state.
// Preferences are a store of the same kind, empty at the start: PUT,
// PATCH and DELETE change it, and GET /api/preferences (with an optional
// `since`, an ISO time) and GET /api/preferences/<key> read it, so a flow
// that reloads gets back what it wrote. PATCH works as on the server
// (routes/preferences.py): set keys only (400 otherwise), `add` /
// `remove` on the id lists and `set` on voted_entries (422 for the wrong
// pairing, and for a body the server's schema refuses), `remove` before
// `add`, `null` clears a vote, and the list is trimmed to its cap
// smallest id first. A PUT of a set key is a 409 (SET_KEY_PUT_DETAIL)
// and leaves the stored row alone, as on the server from PR2c. Each
// write's updated_at is F.NOW plus a counter, so they stay in order under
// the frozen clock.
// onUnmocked({ method, path, url }) is called for any request under
// /api, /auth or /assets that no branch below answers (smoke.cjs rule C8),
// and onSetKeyPut({ key }) for any whole-list PUT of a set key (rule C17:
// from PR2b no code path PUTs them; from PR2c the server refuses them, and
// so does this mock, after reporting).
// prefMock(context) is the flows' handle on the store (see below).
function mockApi(context, opts = {}) {
  const onUnmocked = typeof opts.onUnmocked === 'function' ? opts.onUnmocked : null
  const onSetKeyPut = typeof opts.onSetKeyPut === 'function' ? opts.onSetKeyPut : null
  const db = {
    sources: structuredClone(F.SOURCES),
    entries: structuredClone(F.ENTRIES),
    preferences: new Map(),
  }
  let prefWrites = 0
  const prefRow = (key, value) => {
    const row = { key, value, updated_at: new Date(F.NOW + ++prefWrites).toISOString() }
    db.preferences.set(key, row)
    return row
  }
  // Faults a flow queues: the next `times` requests of `method` (to `key`,
  // or any key) are answered with `status` and change nothing.
  const faults = []
  DB_MOCKS.set(context, {
    addSources: (rows) => db.sources.push(...structuredClone(rows)),
    addEntries: (rows) => db.entries.push(...structuredClone(rows)),
  })
  PREF_MOCKS.set(context, {
    seed: (rows) => {
      for (const [key, value] of Object.entries(rows)) prefRow(key, value)
    },
    value: (key) => (db.preferences.has(key) ? db.preferences.get(key).value : undefined),
    keys: () => [...db.preferences.keys()].sort(),
    fail: (status, { method = 'PATCH', key = null, times = 1 } = {}) => faults.push({ status, method, key, times }),
    faultsLeft: () => faults.reduce((n, f) => n + f.times, 0),
  })
  const takeFault = (method, key) => {
    const f = faults.find((x) => x.times > 0 && x.method === method && (x.key == null || x.key === key))
    if (!f) return null
    f.times--
    return f
  }
  // The ranking's order, paused feeds' stories included (dropped below).
  const forYouIds = F.FOR_YOU_RANKED.map((e) => e.id)
  const sourceById = (id) => db.sources.find((s) => s.id === id)
  const sourceName = (id) => (sourceById(id) || {}).name || 'unknown'
  const entryById = (id) => db.entries.find((e) => e.id === id)
  // The store's hidden_entries, which /api/entries (without q) and
  // /api/foryou leave out, as routes/entries.py and routes/foryou.py do.
  const hiddenIds = () => {
    const v = db.preferences.has('hidden_entries') ? db.preferences.get('hidden_entries').value : null
    return new Set(Array.isArray(v) ? v.filter(isEntryId) : [])
  }
  // A story fetched inside the last ``hours``. (The server's
  // ``fetched_at >=`` would drop a story with no fetched_at; none of the
  // fixtures lacks one.)
  const inWindow = (e, hours) => !e.fetched_at || Date.parse(e.fetched_at) >= F.NOW - hours * 3600000
  // Each feed's ``cap`` best stories: by composite_score, then
  // fetched_at, then id, descending (routes/entries.py's row_number).
  const capPerSource = (rows, cap) => {
    const ranked = [...rows].sort((a, b) => b.composite_score - a.composite_score || Date.parse(b.fetched_at) - Date.parse(a.fetched_at) || b.id - a.id)
    const seen = new Map()
    const keep = new Set()
    for (const e of ranked) {
      const n = (seen.get(e.source_id) || 0) + 1
      seen.set(e.source_id, n)
      if (n <= cap) keep.add(e.id)
    }
    return rows.filter((e) => keep.has(e.id))
  }
  // The server's orders (routes/entries.py): ``_arrival_order`` (newest
  // arrival first, then published_at, then id) once a window is set, so
  // a cut keeps the newest arrivals; ``_score_order`` (composite_score,
  // then published_at) without one; a search by published_at. A missing
  // time sorts last (``nullslast``).
  const ms = (iso) => (iso ? Date.parse(iso) : -Infinity)
  const desc = (x, y) => (x === y ? 0 : y > x ? 1 : -1)
  const byArrival = (a, b) => desc(ms(a.fetched_at), ms(b.fetched_at)) || desc(ms(a.published_at), ms(b.published_at)) || b.id - a.id
  const byScore = (a, b) => b.composite_score - a.composite_score || desc(ms(a.published_at), ms(b.published_at))
  const byPublished = (a, b) => desc(ms(a.published_at), ms(b.published_at))
  let interactionId = 1000
  return context.route((u) => /^\/(api|auth|assets)\//.test(new URL(u).pathname), async (route) => {
    const req = route.request()
    const u = new URL(req.url())
    const p = u.pathname
    const m = req.method()
    log.push(`${m} ${p}${u.search}`)

    // ---- assets (favicons + thumbnails) ----
    if (p.startsWith('/assets/')) {
      const name = p.split('/').pop()
      const n = parseInt(name, 10) || 1
      const isThumb = p.includes('/thumbs/')
      return route.fulfill({
        status: 200,
        contentType: 'image/svg+xml',
        body: isThumb ? svgImage(640, 360, name, (n * 47) % 360) : svgImage(32, 32, '', (n * 83) % 360),
      })
    }

    // ---- auth: OIDC disabled -> /auth/me 404 so App proceeds without a user ----
    if (p === '/auth/me') return json(route, { detail: 'Not Found' }, 404)
    if (p === '/auth/local/availability') return json(route, { enabled: false })
    if (p === '/auth/logout') return route.fulfill({ status: 204 })
    if (p.startsWith('/auth/')) return json(route, { detail: 'Not Found' }, 404)

    // ---- api ----
    if (p === '/api/health') {
      return json(route, { status: 'ok', sources: db.sources.length, entries: db.entries.length, db: 'ok', last_fetch: new Date(F.NOW - 120000).toISOString() })
    }
    if (p === '/api/sources' && m === 'GET') return json(route, db.sources)
    if (p === '/api/sources' && m === 'POST') {
      const body = req.postDataJSON() || {}
      const id = Math.max(0, ...db.sources.map((s) => s.id)) + 1
      const row = { ...structuredClone(F.SOURCES[0]), id, name: body.name || 'new_source', type: body.type || 'rss', category: body.category || 'other', url: body.url || '', refresh_interval_seconds: body.refresh_interval_seconds || 3600, custom_headers: body.custom_headers || null, sitemap_url: body.sitemap_url || null, link_pattern: body.link_pattern || null, favicon_path: null, favicon_url: null, last_fetch_at: null, net_vote_score: 0 }
      db.sources.push(row)
      return json(route, row)
    }
    if (p === '/api/sources/test') {
      return json(route, { ok: true, status_code: 200, item_count: 15, sample_titles: ['Sample one', 'Sample two', 'Sample three'], error_kind: null, error: null, resolved_url: null })
    }
    if (p === '/api/sources/auto') return json(route, { found: true, kind: 'rss', source: db.sources[0] })
    let mm = p.match(/^\/api\/sources\/(\d+)$/)
    if (mm) {
      const id = Number(mm[1])
      const i = db.sources.findIndex((s) => s.id === id)
      if (i < 0) return json(route, { detail: 'Source not found' }, 404)
      if (m === 'DELETE') {
        db.sources.splice(i, 1)
        return route.fulfill({ status: 204 })
      }
      if (m === 'PATCH') Object.assign(db.sources[i], req.postDataJSON() || {})
      return json(route, db.sources[i])
    }
    if (p === '/api/entries/by-ids') {
      // As routes/entries.py's entries_by_ids: at most 200 ids, in the
      // order asked, ids with no row (or whose feed is gone: the JOIN)
      // dropped, and each row the card projection (``_CARD_COLUMNS``,
      // no ``meta``, with ``preview``) plus the feed's name.
      const ids = (u.searchParams.get('ids') || '').split(',').map(Number).filter(isEntryId).slice(0, 200)
      return json(route, ids.map(entryById).filter((e) => e && sourceById(e.source_id)).map((e) => byIdRow(e, sourceName(e.source_id))))
    }
    if (p === '/api/entries' && m === 'GET') {
      let rows = db.entries.filter((e) => sourceById(e.source_id))
      const q = (u.searchParams.get('q') || '').toLowerCase()
      // As the server since PR4b: without q, the stories the store has
      // hidden are left out.
      if (!q) rows = rows.filter((e) => !hiddenIds().has(e.id))
      const cat = u.searchParams.get('category')
      const srcs = u.searchParams.getAll('source')
      if (q) rows = rows.filter((e) => e.title.toLowerCase().includes(q) || String(e.meta?.summary || '').toLowerCase().includes(q))
      if (cat) rows = rows.filter((e) => (sourceById(e.source_id) || {}).category === cat)
      if (srcs.length) rows = rows.filter((e) => srcs.includes(sourceName(e.source_id)))
      // The window (PR4b, PR4d): ``max_age_hours`` keeps the stories
      // fetched since then, against F.NOW (the page's clock), and
      // ``per_source_cap`` each feed's best-scoring ones in it (D28:
      // score, then fetched_at, then id). The cap needs the window (422
      // alone, as on the server). ``active_only`` leaves paused feeds out.
      const hours = Number(u.searchParams.get('max_age_hours') || 0)
      const cap = Number(u.searchParams.get('per_source_cap') || 0)
      if (cap && !hours) return json(route, { detail: 'per_source_cap needs max_age_hours' }, 422)
      if (u.searchParams.get('active_only') === 'true') rows = rows.filter((e) => sourceById(e.source_id).active)
      if (hours) rows = rows.filter((e) => inWindow(e, hours))
      if (cap) rows = capPerSource(rows, cap)
      // The dashboard load (``per_category_limit`` with no category,
      // source or q): each category's first ``per_category_limit``, by
      // arrival with a window (the per-feed cap above is the part that
      // ranks by score, D28), by score without one; ``limit`` is unused.
      // Otherwise the flat cut: ``limit`` (50 when unset, at most 50 for
      // a search) of the rows in the server's order.
      const perCategory = Number(u.searchParams.get('per_category_limit') || 0)
      if (perCategory && !cat && !srcs.length && !q) {
        const taken = new Map()
        rows = [...rows].sort(hours ? byArrival : byScore).filter((e) => {
          const c = sourceById(e.source_id).category
          taken.set(c, (taken.get(c) || 0) + 1)
          return taken.get(c) <= perCategory
        })
        return json(route, rows)
      }
      rows = [...rows].sort(hours ? byArrival : q ? byPublished : byScore)
      const limit = Number(u.searchParams.get('limit') || 0) || 50
      return json(route, rows.slice(0, q ? Math.min(limit, 50) : limit))
    }
    mm = p.match(/^\/api\/entries\/(\d+)\/(summary|podcast_summary|reddit_comment_summary|related)$/)
    if (mm) {
      const e = entryById(Number(mm[1]))
      const kind = mm[2]
      if (kind === 'summary') return json(route, { summary: e ? e.meta.summary : null, cached: false })
      if (kind === 'podcast_summary') return json(route, { summary: e ? e.meta.summary : null, cached: false, available: !!(e && e.transcript_url) })
      if (kind === 'reddit_comment_summary') return json(route, { summary: 'Commenters are split: half think the funding formula is overdue, the other half point out the consultation window is absurdly short. Top comment links the technical annex.', cached: false, available: true, rate_limited: false })
      if (kind === 'related') {
        if (!e || e.story_cluster_id == null) return json(route, null)
        const sib = db.entries.filter((x) => x.story_cluster_id === e.story_cluster_id && x.id !== e.id)
        return json(route, {
          cluster_id: e.story_cluster_id,
          wire_source: 'Canadian Press',
          first_seen_at: new Date(F.NOW - 3600000).toISOString(),
          articles: sib.map((x) => ({ entry_id: x.id, title: x.title, url: x.url, source_name: sourceName(x.source_id), favicon_path: (sourceById(x.source_id) || {}).favicon_path || null, published_at: x.published_at, framing_tone: 'alarmist' })),
        })
      }
    }
    if (p === '/api/foryou') {
      // As the server since PR4b: never a paused feed's story, nor a
      // hidden one; ``max_age_hours`` as for /api/entries.
      const hours = Number(u.searchParams.get('max_age_hours') || 0)
      let rows = forYouIds.map(entryById).filter((e) => e && sourceById(e.source_id) && sourceById(e.source_id).active && !hiddenIds().has(e.id))
      if (hours) rows = rows.filter((e) => inWindow(e, hours))
      return json(route, rows)
    }
    if (p === '/api/framing-clusters') return json(route, [])
    if (p === '/api/feed-recommendations' && m === 'GET') {
      return json(route, [
        { name: 'ars_technica', category: 'news', url: 'https://feeds.arstechnica.com/arstechnica/index', blurb: 'Deep technology reporting', type: 'rss', source: 'editorial' },
        { name: 'r_buildapcsales', category: 'deals', url: 'https://www.reddit.com/r/buildapcsales', blurb: 'PC part deals, US-centric', type: 'reddit', source: 'editorial' },
      ])
    }
    if (p === '/api/feed-recommendations/discover') return json(route, { category: 'news', added: 0, note: null })
    if (p === '/api/interactions') return json(route, { id: ++interactionId })
    if (p === '/api/interactions/batch') {
      const b = req.postDataJSON() || {}
      return json(route, { inserted: (b.events || []).length })
    }
    if (p === '/api/interactions/recent') return json(route, { items: [], total: 0, has_more: false })
    if (p === '/api/brief/latest') return json(route, [])
    if (p === '/api/brief/generate') return json(route, { job_id: 'job-1', tone: u.searchParams.get('tone') || 'terse' })
    if (p.startsWith('/api/brief/jobs/')) return json(route, { id: 'job-1', tone: 'terse', status: 'completed', brief: { id: 1, generated_at: new Date(F.NOW).toISOString(), tone: 'terse', content: '## Today\n\n- Nothing much.', delivered_at: null, meta: null }, error: null })
    if (p === '/api/notifications/status') return json(route, { configured: false, backend: null, scheme: null })
    if (p === '/api/llm/status') return json(route, { configured: true, backend: 'ollama_cloud', model: 'gpt-oss:120b-cloud' })
    if (p === '/api/llm/tags') return json(route, F.LLM_TAGS)
    if (p === '/api/settings' && m === 'GET') return json(route, F.SETTINGS)
    if (p === '/api/settings/llm') return json(route, F.SETTINGS)
    if (p === '/api/preferences' && m === 'GET') {
      const since = u.searchParams.get('since')
      const after = since ? Date.parse(since) : NaN
      const items = [...db.preferences.values()].filter((row) => Number.isNaN(after) || Date.parse(row.updated_at) > after)
      return json(route, { items })
    }
    mm = p.match(/^\/api\/preferences\/(.+)$/)
    if (mm) {
      const key = decodeURIComponent(mm[1])
      const fault = takeFault(m, key)
      if (fault) return json(route, { detail: `mocked ${fault.status}` }, fault.status)
      if (m === 'DELETE') {
        db.preferences.delete(key)
        return route.fulfill({ status: 204 })
      }
      if (m === 'GET') {
        const row = db.preferences.get(key)
        return row ? json(route, row) : json(route, { detail: `preference '${key}' not set for user` }, 404)
      }
      if (m === 'PUT') {
        if (SET_KEY_RE.test(key)) {
          if (onSetKeyPut) onSetKeyPut({ key })
          // As the server does (SET_KEYS, routes/preferences.py): set keys
          // change only by PATCH, and a whole-list PUT of one is refused
          // without touching the stored row.
          return json(route, { detail: SET_KEY_PUT_DETAIL }, 409)
        }
        return json(route, prefRow(key, (req.postDataJSON() || {}).value ?? null))
      }
      if (m === 'PATCH') {
        // In the server's order: the body's shape (422), the key (400),
        // then the change against the key (422).
        const body = req.postDataJSON()
        const bad = patchBodyError(body)
        if (bad) return json(route, { detail: bad }, 422)
        if (!SET_KEY_RE.test(key)) return json(route, { detail: `preference key '${key}' doesn't take PATCH` }, 400)
        const votes = key === 'voted_entries'
        const ops = Object.keys(body)
        if (votes ? ops.some((k) => k !== 'set') : ops.includes('set')) {
          return json(route, { detail: `wrong change for ${key}: ${ops.join(', ')}` }, 422)
        }
        const cap = setKeyCap(key)
        if (!votes && ((body.add || []).length > cap || (body.remove || []).length > cap)) {
          return json(route, { detail: `${key} takes at most ${cap} ids per list` }, 422)
        }
        const cur = db.preferences.has(key) ? db.preferences.get(key).value : undefined
        let value
        if (votes) {
          value = {}
          if (cur && typeof cur === 'object' && !Array.isArray(cur)) {
            for (const [id, v] of Object.entries(cur)) if (VOTE_ID_RE.test(id) && (v === 'up' || v === 'down')) value[id] = v
          }
          for (const [id, v] of Object.entries(body.set || {})) {
            if (v == null) delete value[id]
            else value[id] = v
          }
          for (const id of Object.keys(value).sort((a, b) => a - b).slice(0, Math.max(0, Object.keys(value).length - cap))) delete value[id]
        } else {
          const rm = new Set(body.remove || [])
          const ids = [...new Set(Array.isArray(cur) ? cur : [])].filter((id) => isEntryId(id) && !rm.has(id))
          for (const id of body.add || []) if (!ids.includes(id)) ids.push(id)
          const keep = new Set([...ids].sort((a, b) => a - b).slice(-cap))
          value = ids.filter((id) => keep.has(id))
        }
        return json(route, prefRow(key, value))
      }
    }
    mm = p.match(/^\/api\/ingest\/(.+)$/)
    if (mm) return json(route, { source: decodeURIComponent(mm[1]), fetched: 12, inserted: 2, duplicates: 10, error: null })

    log.push(`  !! UNMOCKED ${m} ${p}`)
    if (onUnmocked) onUnmocked({ method: m, path: p, url: req.url() })
    return json(route, { detail: `unmocked ${m} ${p}` }, 404)
  })
}

// ---------------------------------------------------------------------------
// DOM overflow scanner (runs in the page)
// ---------------------------------------------------------------------------
function scanDom() {
  const TW_LIMIT = 220
  const out = {
    viewport: { innerWidth: innerWidth, innerHeight: innerHeight, dpr: devicePixelRatio, visualScale: visualViewport ? visualViewport.scale : null },
    media: {
      pointerCoarse: matchMedia('(pointer: coarse)').matches,
      hoverNone: matchMedia('(hover: none)').matches,
      anyPointerCoarse: matchMedia('(any-pointer: coarse)').matches,
    },
    document: {
      htmlScrollWidth: document.documentElement.scrollWidth,
      htmlClientWidth: document.documentElement.clientWidth,
      bodyScrollWidth: document.body.scrollWidth,
      rootScrollWidth: document.getElementById('root')?.scrollWidth,
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth || document.body.scrollWidth > innerWidth,
    },
    fontSizes: {},
    smallFonts: [],
    inputsTallerThanRow: [],
    flagged: [],
    counts: { elementsScanned: 0, textNodesScanned: 0 },
  }

  const isVisible = (el, cs) => {
    if (!cs) cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden') return false
    const r = el.getBoundingClientRect()
    if (r.width === 0 && r.height === 0) return false
    return true
  }
  const short = (el) => {
    if (!el || el.nodeType !== 1) return ''
    let s = el.tagName.toLowerCase()
    if (el.id) s += '#' + el.id
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean)
    if (cls.length) s += '.' + cls.slice(0, 3).join('.')
    const al = el.getAttribute('aria-label')
    if (al) s += `[aria-label="${al.slice(0, 30)}"]`
    return s
  }
  const pathOf = (el) => {
    const parts = []
    let cur = el
    let depth = 0
    while (cur && cur.nodeType === 1 && depth < 5 && cur !== document.body) {
      parts.unshift(short(cur))
      cur = cur.parentElement
      depth++
    }
    return parts.join(' > ')
  }
  const tw = (el) => ((el && el.getAttribute && el.getAttribute('class')) || '').replace(/\s+/g, ' ').trim().slice(0, TW_LIMIT)
  const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60)
  const inWallpaper = (el) => !!el.closest('.wallpaper-blob-layer')
  const positionedAncestor = (el) => {
    let cur = el.parentElement
    while (cur && cur !== document.documentElement) {
      const cs = getComputedStyle(cur)
      if (cs.position !== 'static' || cs.transform !== 'none') return cur
      cur = cur.parentElement
    }
    return document.documentElement
  }
  // Visual clip boundary: nearest ancestor with overflow != visible on x.
  const clipAncestor = (el) => {
    let cur = el.parentElement
    while (cur && cur !== document.documentElement) {
      const cs = getComputedStyle(cur)
      if (cs.overflowX !== 'visible') return cur
      cur = cur.parentElement
    }
    return null
  }
  const rectOf = (r) => ({ l: +r.left.toFixed(1), t: +r.top.toFixed(1), r: +r.right.toFixed(1), b: +r.bottom.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1) })

  // Font size histogram per control type + global.
  const hist = (key, cs) => {
    const k = `${cs.fontSize} / lh ${cs.lineHeight}`
    out.fontSizes[key] = out.fontSizes[key] || {}
    out.fontSizes[key][k] = (out.fontSizes[key][k] || 0) + 1
  }

  const all = document.body.querySelectorAll('*')
  for (const el of all) {
    if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'SVG' || el.closest('svg')) continue
    if (inWallpaper(el)) continue
    const cs = getComputedStyle(el)
    if (!isVisible(el, cs)) continue
    out.counts.elementsScanned++
    const tag = el.tagName.toLowerCase()
    const r = el.getBoundingClientRect()

    // font-size bookkeeping
    const fs = parseFloat(cs.fontSize)
    if (['input', 'select', 'textarea', 'button'].includes(tag)) hist(tag, cs)
    else if (tag === 'p' || tag === 'span' || tag === 'a' || tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'label' || tag === 'time') hist('text:' + tag, cs)
    if (fs < 11 && (el.textContent || '').trim()) {
      out.smallFonts.push({ path: pathOf(el), text: txt(el), fontSize: cs.fontSize, lineHeight: cs.lineHeight, classes: tw(el) })
    }

    // Inputs taller than their row (direct parent box).
    if (['input', 'select', 'textarea'].includes(tag) && el.parentElement) {
      const pr = el.parentElement.getBoundingClientRect()
      if (r.height > pr.height + 1) {
        out.inputsTallerThanRow.push({ path: pathOf(el), inputH: +r.height.toFixed(1), rowH: +pr.height.toFixed(1), fontSize: cs.fontSize, classes: tw(el), parentClasses: tw(el.parentElement) })
      }
    }

    const reasons = []
    // (a) scrollable overflow
    if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0) {
      reasons.push(`scrollWidth ${el.scrollWidth} > clientWidth ${el.clientWidth}` + (cs.overflowX === 'auto' || cs.overflowX === 'scroll' ? ' (intentional scroller)' : cs.overflowX === 'hidden' || cs.overflowX === 'clip' ? ' (clipped)' : ' (VISIBLE OVERFLOW)'))
    }
    // (b) beyond parent / positioned ancestor by > 1px
    const par = el.parentElement
    if (par && par !== document.body) {
      const pr = par.getBoundingClientRect()
      const over = { l: pr.left - r.left, r: r.right - pr.right, t: pr.top - r.top, b: r.bottom - pr.bottom }
      const pcs = getComputedStyle(par)
      const overX = Math.max(over.l, over.r)
      const overY = Math.max(over.t, over.b)
      if (overX > 1 || overY > 1) {
        // Ignore negative-margin intent: tailwind -m*, -mx-*, -ml-*, -mr-*, -mt-*, -mb-*
        const cls = el.getAttribute('class') || ''
        const negMargin = /(^|\s)-m[xylrtb]?-/.test(cls)
        const translated = cs.transform !== 'none' || cs.position === 'fixed' || cs.position === 'absolute'
        reasons.push(`beyond parent by ${overX > 1 ? `x:${overX.toFixed(1)}` : ''}${overY > 1 ? ` y:${overY.toFixed(1)}` : ''}${negMargin ? ' (has negative margin class)' : ''}${translated ? ' (abs/fixed/transformed)' : ''}` + (pcs.overflowX !== 'visible' || pcs.overflowY !== 'visible' ? ` [parent overflow ${pcs.overflowX}/${pcs.overflowY}]` : ' [parent overflow visible -> VISIBLE]'))
      }
      const pa = positionedAncestor(el)
      if (pa && pa !== par && pa !== document.documentElement) {
        const ar = pa.getBoundingClientRect()
        const ox = Math.max(ar.left - r.left, r.right - ar.right)
        if (ox > 1 && cs.position !== 'fixed') {
          reasons.push(`beyond positioned ancestor ${short(pa)} by x:${ox.toFixed(1)}`)
        }
      }
    }
    if (reasons.length) {
      const ca = clipAncestor(el)
      out.flagged.push({
        kind: 'element',
        path: pathOf(el),
        text: txt(el),
        reasons,
        fontSize: cs.fontSize,
        lineHeight: cs.lineHeight,
        whiteSpace: cs.whiteSpace,
        overflow: `${cs.overflowX}/${cs.overflowY}`,
        minWidth: cs.minWidth,
        display: cs.display,
        flex: cs.flex,
        rect: rectOf(r),
        parentRect: par ? rectOf(par.getBoundingClientRect()) : null,
        classes: tw(el),
        parentClasses: par ? tw(par) : '',
        clippedBy: ca ? short(ca) : null,
      })
    }
  }

  // (c) text nodes whose rendered rect exceeds their block container.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT
      const p = n.parentElement
      if (!p || p.closest('script,style,svg,.wallpaper-blob-layer')) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    },
  })
  let n
  while ((n = walker.nextNode())) {
    const p = n.parentElement
    const pcs = getComputedStyle(p)
    if (!isVisible(p, pcs)) continue
    out.counts.textNodesScanned++
    const range = document.createRange()
    range.selectNodeContents(n)
    const tr = range.getBoundingClientRect()
    if (tr.width === 0) continue
    const pr = p.getBoundingClientRect()
    const overR = tr.right - pr.right
    const overL = pr.left - tr.left
    const overB = tr.bottom - pr.bottom
    if (overR > 1 || overL > 1 || overB > 1) {
      const ca = clipAncestor(n.parentElement) || (pcs.overflowX !== 'visible' ? p : null)
      out.flagged.push({
        kind: 'text',
        path: pathOf(p),
        text: n.nodeValue.replace(/\s+/g, ' ').trim().slice(0, 60),
        reasons: [`text rect exceeds its container by ${overR > 1 ? `right:${overR.toFixed(1)} ` : ''}${overL > 1 ? `left:${overL.toFixed(1)} ` : ''}${overB > 1 ? `bottom:${overB.toFixed(1)}` : ''}`.trim() + (pcs.textOverflow === 'ellipsis' && pcs.overflowX !== 'visible' ? ' (truncated with ellipsis)' : pcs.overflowX !== 'visible' ? ' (clipped)' : ca ? ` (clipped by ${short(ca)})` : ' (VISIBLE OVERFLOW)')],
        fontSize: pcs.fontSize,
        lineHeight: pcs.lineHeight,
        whiteSpace: pcs.whiteSpace,
        overflow: `${pcs.overflowX}/${pcs.overflowY}`,
        minWidth: pcs.minWidth,
        display: pcs.display,
        flex: pcs.flex,
        rect: rectOf(tr),
        parentRect: rectOf(pr),
        classes: tw(p),
        parentClasses: p.parentElement ? tw(p.parentElement) : '',
        clippedBy: ca ? short(ca) : null,
      })
    }
  }

  // Body text baseline.
  const bcs = getComputedStyle(document.body)
  out.bodyFont = { fontSize: bcs.fontSize, lineHeight: bcs.lineHeight, fontFamily: bcs.fontFamily.slice(0, 120) }
  return out
}

// ---------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function main() {
  const browser = await chromium.launch({ headless: true })
  const summary = {}
  for (const dev of DEVICES) {
    const tag = dev.tag
    const results = {}
    const context = await browser.newContext({
      viewport: dev.viewport,
      deviceScaleFactor: dev.deviceScaleFactor,
      isMobile: true,
      hasTouch: true,
      userAgent: IOS_UA,
      locale: 'en-CA',
      timezoneId: 'America/Vancouver',
      colorScheme: 'dark',
    })
    await mockApi(context)
    const page = await context.newPage()
    const pageErrors = []
    page.on('pageerror', (e) => pageErrors.push(String(e)))
    page.on('console', (msg) => {
      if (msg.type() === 'error' || msg.type() === 'warning') pageErrors.push(`[console.${msg.type()}] ${msg.text().slice(0, 200)}`)
    })

    const shot = async (name) => {
      const file = path.join(OUT, `${tag}-${name}.png`)
      await page.screenshot({ path: file, fullPage: true })
      // "tall" variant: grow the viewport so fixed/internal scrollers reveal everything.
      const need = await page.evaluate(() => {
        let h = document.documentElement.scrollHeight
        for (const el of document.querySelectorAll('*')) {
          const cs = getComputedStyle(el)
          if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 2) {
            const r = el.getBoundingClientRect()
            h = Math.max(h, r.top + scrollY + el.scrollHeight + 48)
          }
        }
        return Math.ceil(h)
      })
      if (need > dev.viewport.height + 8) {
        await page.setViewportSize({ width: dev.viewport.width, height: Math.min(need, 6000) })
        await sleep(250)
        await page.screenshot({ path: path.join(OUT, `${tag}-${name}-tall.png`), fullPage: true })
        await page.setViewportSize(dev.viewport)
        await sleep(150)
      }
      return file
    }
    const scan = async (name, note) => {
      const r = await page.evaluate(scanDom)
      r.note = note
      r.url = page.url()
      results[name] = r
      const visible = r.flagged.filter((f) => f.reasons.some((x) => x.includes('VISIBLE')))
      console.log(`[${tag}] ${name}: scanned ${r.counts.elementsScanned} els / ${r.counts.textNodesScanned} text; flagged ${r.flagged.length} (visible-overflow ${visible.length}); docOverflow=${r.document.horizontalOverflow} (${r.document.htmlScrollWidth}/${r.viewport.innerWidth}); pointer:coarse=${r.media.pointerCoarse} hover:none=${r.media.hoverNone}`)
      return r
    }
    const state = async (name, note) => {
      await sleep(350)
      await shot(name)
      await scan(name, note)
    }

    // ---- (1) dashboard ----
    // NB: App renders BOTH the desktop grid (<main class="hidden md:grid">,
    // display:none on phones) and the mobile <main class="md:hidden">, so
    // every card exists twice in the DOM. Scope to the visible mobile main.
    const mobileMain = page.locator('main.md\\:hidden')
    await page.goto(BASE + '/', { waitUntil: 'networkidle' })
    await page.waitForSelector('[role="tablist"][aria-label="dashboard sections"]', { timeout: 20000 })
    await mobileMain.locator('article[data-card-id]').first().waitFor({ state: 'visible', timeout: 20000 })
    await sleep(2200) // let any load-time toast go away
    await state('01-dashboard', 'Dashboard after load (mobile tab bar, first column = For You)')

    // ---- (2) card with summary expanded ----
    const card180 = mobileMain.locator('article[data-card-id="101"]')
    await card180.locator('button[aria-label="show summary"]').click()
    const cardTok = mobileMain.locator('article[data-card-id="102"]')
    await cardTok.locator('button[aria-label="show summary"]').click()
    // also open the related-coverage + reddit summary on 101 for coverage
    await card180.locator('button:has-text("Related coverage")').click().catch(() => {})
    await mobileMain.locator('article[data-card-id="104"] button:has-text("Summarize comments")').click().catch(() => {})
    await page.waitForFunction(() => {
      const el = document.querySelector('main.md\\:hidden article[data-card-id="101"]')
      return el && el.textContent.includes('needs-weighted formula')
    }, null, { timeout: 10000 })
    await sleep(300)
    await state('02-card-summary', 'Cards 101 (180-char title, image, cluster) and 102 (90-char token) with summary expanded; related coverage + reddit summary opened')

    // ---- (3) drawer ----
    await page.locator('button[aria-label="open menu"]').click()
    await page.waitForSelector('aside[aria-label="menu"]')
    await sleep(450) // slide-in transition
    await state('03-drawer', 'Drawer open')
    await page.locator('button[aria-label="close menu"]').click()
    await sleep(400)

    // ---- (4) settings / feeds ----
    // Menu → Settings: the top bar's one button since PR5.
    await page.locator('button[aria-label="open menu"]').click()
    await page.locator('aside[aria-label="menu"]').getByRole('button', { name: /^Settings/ }).click()
    await page.waitForSelector('[role="dialog"][aria-label="settings"]')
    await page.waitForSelector('[role="tablist"][aria-label="feed manager tabs"]')
    await sleep(300)
    await state('04-settings-feeds', 'Settings overlay, Feeds tab, My feeds list')

    // ---- (5) feed row expanded + inline edit ----
    const longLabel = 'The Extraordinarily Long Name'
    const row = page.locator(`[role="dialog"][aria-label="settings"] button[aria-expanded]:has-text("${longLabel}")`).first()
    await row.scrollIntoViewIfNeeded()
    await row.click()
    await page.locator('button[aria-label="edit refresh interval"]').first().click()
    await page.waitForSelector('select[aria-label="refresh interval"]')
    // Also expand the auto-disabled podcast row to show its chip cluster.
    const row2 = page.locator('[role="dialog"][aria-label="settings"] button[aria-expanded]:has-text("Risky Business Podcast")').first()
    await row2.click().catch(() => {})
    await sleep(200)
    await state('05a-feed-row', 'Feed row (60-char name) expanded with the refresh-interval <select> open; auto-disabled row expanded too')
    await page.locator(`button[aria-label="edit ${F.LONG_SOURCE_NAME}"]`).click()
    await page.waitForSelector('[role="dialog"][aria-label="settings"] textarea')
    await sleep(200)
    await state('05-feed-edit', 'Inline edit form open for the 60-char-name / 120-char-URL source (name, url, category inputs + custom headers textarea)')

    // ---- (6) Add custom, YouTube type ----
    await page.locator('[role="tablist"][aria-label="feed manager tabs"] button:has-text("Add custom")').click()
    await page.waitForSelector('#fm-name')
    await page.locator('input[type="radio"][value="youtube_channel"]').check()
    await page.fill('#fm-name', 'veritasium_clips_and_extras_channel')
    await page.fill('#fm-url', 'https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA/videos?view=0&sort=dd&shelf_id=0')
    await page.locator('#fm-url').blur().catch(() => {})
    await sleep(250)
    await state('06-add-youtube', 'Add custom form with YouTube type selected; name + long URL typed')

    // ---- (7) LLM tab ----
    await page.locator('[role="dialog"][aria-label="settings"] [role="tablist"] button[role="tab"]:has-text("AI")').click()
    await page.waitForSelector('text=Brief tone')
    await sleep(300)
    await page.waitForSelector('#ai-provider')
    await sleep(300)
    await state('07-llm', 'Settings → AI tab (provider, models and keys; each saves on its own)')
    const pw = page.locator('[role="dialog"][aria-label="settings"] input[type="password"]').first()
    await pw.fill('sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnop')
    await sleep(200)
    await state('07b-llm-picker', 'Settings → AI tab with one API key typed (it saves when the field is left)')

    // ---- (8) notifications ----
    await page.locator('[role="dialog"][aria-label="settings"] [role="tablist"] button[role="tab"]:has-text("Notifications")').click()
    await page.waitForSelector('text=Push notifications are off')
    await sleep(250)
    await state('08-notifications', 'Settings → Notifications tab')

    // ---- (9) search ----
    await page.locator('button[aria-label="close settings"]').click()
    await sleep(300)
    await page.locator('button[aria-label="open search"]').click()
    await page.waitForSelector('#app-search')
    await page.fill('#app-search', 'infrastructure funding formula')
    await page.waitForFunction(() => document.body.textContent.includes('Search results'), null, { timeout: 10000 })
    await sleep(900)
    await state('09-search', 'Search field open with a query typed; search results view')

    // Persist per-device JSON.
    const jsonPath = path.join(OUT, `${tag}-overflow-scan.json`)
    fs.writeFileSync(jsonPath, JSON.stringify({ device: dev, userAgent: IOS_UA, states: results, pageErrors }, null, 2))
    summary[tag] = { jsonPath, pageErrors: pageErrors.length, states: Object.fromEntries(Object.entries(results).map(([k, v]) => [k, { flagged: v.flagged.length, visible: v.flagged.filter((f) => f.reasons.some((x) => x.includes('VISIBLE'))).length, docOverflow: v.document.horizontalOverflow, smallFonts: v.smallFonts.length, inputsTallerThanRow: v.inputsTallerThanRow.length }])) }
    await context.close()
  }
  fs.writeFileSync(path.join(OUT, 'request-log.txt'), log.join('\n'))
  fs.writeFileSync(path.join(OUT, 'summary.json'), JSON.stringify(summary, null, 2))
  console.log(JSON.stringify(summary, null, 2))
  await browser.close()
}

module.exports = { mockApi, prefMock, dbMock, scanDom, IOS_UA, DEVICES, BASE, SET_KEY_RE }

if (require.main === module) {
  main().catch((e) => {
    console.error(e)
    process.exit(1)
  })
}
