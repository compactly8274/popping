// Behaviour flows for smoke.cjs (REDESIGN_PLAN.md section 12). Each flow
// has the same shape as a smoke state, with `checks: false` and a `run`
// that calls fail(rule, detail) for each problem. They run at 390 only.
//
// Flows record the requests they care about with page routes, which run
// before capture.cjs's context mock and then fall through to it.
// smoke.cjs requires this file, so it must not require smoke.cjs.
//
// Counting taps (H1). A tap-count flow proves "this takes at most N taps":
// - tap(page, locator) is locator.tap() (phone contexts have hasTouch) and
//   adds 1 to page._taps.
// - taps(page, max, id) fails the flow under rule `id` when page._taps is
//   over max. The count starts at 0 when `run` starts.
// - A swipe through touchDrag() counts as 1. Typing with fill() is free.
// - A tap-count flow starts from its URL and uses only tap, touchDrag and
//   fill to get there: no evaluate, pushState, .click(), .check(),
//   selectOption or startPoll shortcuts. The older flows below use those
//   freely; they count requests, not taps.
//
// Every flow also gets installHarness() before the page loads: window.open,
// links that open a new tab (a story's headline), navigator.share and the
// clipboard are stubbed (nothing leaves the page),
// and fetches to /api/preferences and /api/interactions are recorded with
// their keepalive flag, which Playwright's Request does not expose, and
// whether the app has read the answer (harnessLog(), answered()).
//
// Preferences. capture.cjs keeps a preference store per context; a flow
// seeds it with prefMock(context).seed({ key: value }) (rows as if the
// server had them) and queues faults with prefMock(context).fail(...).
// Answering GET /api/preferences with a fixed body instead would bring
// that body back on every poll and on every return to the front, which
// now re-read the preferences. Any whole-list PUT of a set key fails
// the flow under smoke rule C17.
//
// Time. Flows that assert dwell, batch, toast, poll or arrival timing call
// installClock(page) in `before` and move time with page.clock.runFor (as
// flow-fn-quiet-poll does). runFor also fires the regular visible poll, so
// GET counts allow for it. Where real time is kept, pause at least twice
// the delay under test. On the phone a card is used up by being read on
// screen and scrolled past, or by staying on screen with the marker
// (PR3c): a flow that counts unseen stories while cards or the marker are
// on screen holds the page's clock (holdClock) before it opens the tab.
'use strict'

const F = require('./fixtures.cjs')
const { mockApi, prefMock, dbMock } = require('./capture.cjs')

const SETTINGS = '[role="dialog"][aria-label="settings"]'
const FEED_TABS = `${SETTINGS} [role="tablist"][aria-label="feed manager tabs"]`
const CHANNEL_FEED = 'https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv'

async function openAddTab(page) {
  await page.locator(SETTINGS).waitFor({ state: 'visible' })
  await page.locator(`${FEED_TABS} [role="tab"]`).nth(2).click()
  await page.locator('#fm-url').waitFor({ state: 'visible' })
}

// Records the JSON bodies of `method` requests to `pathname`; `reply`
// answers them instead of the shared mock when given.
async function record(page, method, pathname, reply) {
  const bodies = []
  await page.route(
    (u) => new URL(u).pathname === pathname,
    async (route) => {
      const req = route.request()
      if (req.method() !== method) return route.fallback()
      bodies.push(req.postDataJSON() || {})
      if (reply) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(reply(req.postDataJSON() || {})) })
      return route.fallback()
    },
  )
  return bodies
}

const testOk = (resolved) => () => ({
  ok: true,
  status_code: 200,
  item_count: 15,
  sample_titles: ['First video', 'Second video'],
  error_kind: null,
  error: null,
  resolved_url: resolved,
})

// Polls a sync or async condition; the UI settles over a render or two.
async function waitFor(fn, ms = 5000) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) {
    if (await fn()) return true
    await new Promise((r) => setTimeout(r, 50))
  }
  return !!(await fn())
}

const isDisabled = (page, name) => page.getByRole('button', { name, exact: true }).isDisabled()

const MENU = 'aside[aria-label="menu"]'

async function openMenu(page) {
  await page.locator('button[aria-label="open menu"]').click()
  await menuOpened(page)
}

// The Menu has slid in and takes input.
async function menuOpened(page) {
  await page.waitForFunction(() => {
    const a = document.querySelector('aside[aria-label="menu"]')
    if (!a || a.hasAttribute('inert')) return false
    const r = a.getBoundingClientRect()
    return r.right <= innerWidth + 0.5 && a.getAnimations().every((x) => x.playState !== 'running')
  }, null, { timeout: 5000 })
}

// A feed row is open when its controls show: the expanded row, or (until
// PR6 replaces it) the old edit form the Menu's › lands in.
const rowOpen = (page, id) =>
  page.locator(`#feed-${id}`).locator('button[aria-expanded="true"], input[type="text"], input[type="url"]').count().then((n) => n > 0)

// True when el's box sits inside the visible part of its scroller.
async function inView(page, selector, scrollerSelector) {
  return page.evaluate(([sel, scSel]) => {
    const el = document.querySelector(sel)
    const sc = el && (scSel ? el.closest(scSel) : null)
    if (!el) return false
    const r = el.getBoundingClientRect()
    const box = sc ? sc.getBoundingClientRect() : { top: 0, left: 0, bottom: innerHeight, right: innerWidth }
    return r.top >= box.top - 1 && r.left >= box.left - 1 && r.right <= box.right + 1 && r.top < box.bottom
  }, [selector, scrollerSelector])
}

// --- H1: harness helpers ----------------------------------------------------

// Runs in the page before the app (addInitScript; a CSP doesn't apply).
// window.__harness collects what the stubs and the fetch wrapper saw.
function harnessInit() {
  const h = (window.__harness = { fetches: [], opened: [], shared: [], copied: [] })
  const realFetch = window.fetch
  window.fetch = function (input, init) {
    let rec = null
    try {
      const req = input instanceof Request ? input : null
      const url = new URL(req ? req.url : String(input), location.href)
      if (/^\/api\/(preferences|interactions)(\/|$)/.test(url.pathname)) {
        rec = {
          method: String((init && init.method) || (req && req.method) || 'GET').toUpperCase(),
          path: url.pathname,
          keepalive: !!(init && 'keepalive' in init ? init.keepalive : req && req.keepalive),
          answered: false,
        }
        h.fetches.push(rec)
      }
    } catch {
      // A URL the wrapper can't read is still fetched as usual.
    }
    const res = realFetch.apply(this, arguments)
    if (rec) {
      // `answered` turns true once the app has read the body (or the
      // request failed). These callbacks run before the app's own, and
      // everything the app does with the answer up to its next await on
      // the network is microtasks, so a later evaluate that sees
      // `answered` sees the app's handling of it too.
      res.then(
        (r) => {
          const json = r.json
          r.json = function () {
            const body = json.apply(this, arguments)
            const done = () => {
              rec.answered = true
            }
            body.then(done, done)
            return body
          }
        },
        () => {
          rec.answered = true
        },
      )
    }
    return res
  }
  window.open = (url, target) => {
    h.opened.push({ url: url == null ? '' : String(url), target: target == null ? '' : String(target) })
    return null
  }
  // A link that opens a new tab (a card's headline) is recorded the same
  // way and goes nowhere. This listener runs after the app's own (window
  // is past React's root in the bubble phase); a link whose handler
  // already called the navigation off opened its tab through
  // window.open, which recorded it.
  // A middle-click (auxclick, button 1) on any link opens a background
  // tab by itself: recorded with `middle: true`, and kept on the page too.
  const keep = (e, middle) => {
    const a = e.target && e.target.closest ? e.target.closest('a[target="_blank"][href]') : null
    if (!a || e.defaultPrevented) return
    e.preventDefault()
    h.opened.push(middle ? { url: a.href, target: '_blank', middle: true } : { url: a.href, target: '_blank' })
  }
  window.addEventListener('click', (e) => keep(e, false))
  window.addEventListener('auxclick', (e) => {
    if (e.button === 1) keep(e, true)
  })
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: async (data) => {
      h.shared.push({ ...(data || {}) })
    },
  })
  Object.defineProperty(navigator, 'canShare', { configurable: true, value: () => true })
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (text) => {
        h.copied.push(String(text))
      },
      readText: async () => h.copied[h.copied.length - 1] || '',
    },
  })
}

const installHarness = (page) => page.addInitScript(harnessInit)

// What the stubs and the fetch wrapper recorded so far:
// { fetches: [{ method, path, keepalive, answered }], opened, shared, copied }.
const harnessLog = (page) => page.evaluate(() => window.__harness)
// The app has taken in the answer to its latest `method` request to `path`
// (a flow waits for this before moving the clock or the page's
// visibility, so a retry or a return doesn't race the answer).
const answered = (page, method, path) =>
  harnessLog(page).then((h) => {
    const sent = h.fetches.filter((f) => f.method === method && f.path === path)
    return sent.length > 0 && sent[sent.length - 1].answered
  })

const isChromium = (page) => page.context().browser().browserType().name() === 'chromium'

// Installs the fake clock at F.NOW. touchDrag then waits in Node instead
// of on animation frames, which a paused clock never delivers.
async function installClock(page, time = F.NOW) {
  page._clockInstalled = true
  await page.clock.install({ time })
}

async function tap(page, locator, opts) {
  await locator.tap(opts)
  page._taps = (page._taps || 0) + 1
}

// False (and one failure under `id`) when more than `max` taps were used.
function taps(page, max, id) {
  const n = page._taps || 0
  if (n <= max) return true
  page._fail(id, `${n} taps, expected at most ${max}`)
  return false
}

// One finger from `from` to `to` ({ x, y } in CSS pixels) through CDP touch
// events: `steps` moves `stepMs` apart, then `holdMs` still before the
// finger lifts. Counts as one tap. Chromium only (the WebKit run is report
// only): elsewhere it does nothing and returns false. Assert outcomes (a
// request sent or not, a card hidden, a transform, defaultPrevented), never
// exact scroll offsets. Chromium doesn't deliver page touch moves under
// about 40px, so rules about small movements belong in unit tests.
async function touchDrag(page, { from, to, steps = 10, stepMs = 50, holdMs = 150 }) {
  if (!isChromium(page)) return false
  // A drag right after load sometimes didn't scroll; two frames plus
  // 300 ms always did.
  if (!page._clockInstalled) {
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))))
  }
  await sleep(300)
  const cdp = await page.context().newCDPSession(page)
  const point = (x, y) => [{ x: Math.round(x), y: Math.round(y), id: 1, radiusX: 1, radiusY: 1, force: 1 }]
  try {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(from.x, from.y) })
    for (let i = 1; i <= steps; i++) {
      await sleep(stepMs)
      const x = from.x + ((to.x - from.x) * i) / steps
      const y = from.y + ((to.y - from.y) * i) / steps
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: point(x, y) })
    }
    await sleep(holdMs)
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  } finally {
    await cdp.detach().catch(() => {})
  }
  page._taps = (page._taps || 0) + 1
  return true
}

// One finger put down near the top of `locator` and left there (a CDP
// touchStart, no move): a pointerdown and a touchstart, no tap, no
// scroll. The function it returns lifts the finger with a touchCancel, so
// no click follows either. Chromium only; elsewhere a held Shift key (a
// keydown) stands in for it.
async function restFinger(page, locator) {
  const box = await locator.boundingBox()
  if (!isChromium(page) || !box) {
    await page.keyboard.down('Shift')
    return () => page.keyboard.up('Shift')
  }
  const cdp = await page.context().newCDPSession(page)
  const x = Math.round(box.x + box.width / 2)
  const y = Math.round(box.y + Math.min(box.height / 2, 40))
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1, radiusX: 1, radiusY: 1, force: 1 }] })
  return async () => {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] }).catch(() => {})
    await cdp.detach().catch(() => {})
  }
}

// Leaving the app and coming back: iOS sets both visibilityState and
// hidden, then fires visibilitychange (the app reads visibilityState).
async function setHidden(page, hidden) {
  await page.evaluate((h) => {
    const state = h ? 'hidden' : 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => h })
    document.dispatchEvent(new Event('visibilitychange'))
  }, hidden)
}

const flow = (name, fields) => ({
  name,
  url: '/?view=settings&tab=feeds',
  widths: [390],
  checks: false,
  ...fields,
  before: async (ctx) => {
    await installHarness(ctx.page)
    if (fields.before) await fields.before(ctx)
  },
  run:
    fields.run &&
    (async (ctx) => {
      ctx.page._taps = 0
      ctx.page._fail = ctx.fail
      return fields.run(ctx)
    }),
})

// PR5 flows run from the dashboard.
const dashFlow = (name, fields) => flow(name, { url: '/', ...fields })

// --- PR8: the AI tab --------------------------------------------------------

// Opens Settings › AI with the server's AI settings as `opts` describe
// (provider: null = Default; anthropic: whether its key is saved here;
// configured: what /api/llm/status says). PUTs are recorded in
// page._puts and change the settings the way the server would.
const aiFlow = (name, opts, fields) =>
  flow(name, {
    url: '/?view=settings&tab=llm',
    before: async ({ page }) => {
      const state = { ...F.SETTINGS }
      if ('provider' in opts) Object.assign(state, { llm_provider: opts.provider, llm_model_brief: null, llm_model_scoring: null })
      if ('anthropic' in opts) state.anthropic_api_key_set = opts.anthropic
      const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      await page.route(
        (u) => new URL(u).pathname === '/api/settings',
        (route) => (route.request().method() === 'GET' ? route.fulfill(json(state)) : route.fallback()),
      )
      if ('configured' in opts) {
        await page.route(
          (u) => new URL(u).pathname === '/api/llm/status',
          (route) => route.fulfill(json({ configured: opts.configured, backend: null, model: null })),
        )
      }
      page._puts = await record(page, 'PUT', '/api/settings/llm', (body) => {
        if ('provider' in body) state.llm_provider = body.provider || null
        if ('model_brief' in body) state.llm_model_brief = body.model_brief || null
        if ('model_scoring' in body) state.llm_model_scoring = body.model_scoring || null
        for (const id of ['anthropic', 'openai', 'groq', 'ollama_cloud']) {
          if (`${id}_api_key` in body) state[`${id}_api_key_set`] = !!body[`${id}_api_key`]
        }
        return state
      })
    },
    ...fields,
  })

// --- PR6: the feed editor ---------------------------------------------------

const MOBILE_MAIN = 'main.md\\:hidden'
const STRIP = '[role="tablist"][aria-label="dashboard sections"]'
const ROW = (id) => `#feed-${id}`
const editorUrl = (id) => `/?view=settings&tab=feeds&source=${id}`
// Starts on a feed's editor through its link.
const editorFlow = (name, id, fields) => flow(name, { url: editorUrl(id), ...fields })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitEditor(page, id) {
  await page.locator(SETTINGS).waitFor({ state: 'visible' })
  await page.locator(`${ROW(id)} button[aria-expanded="true"]`).waitFor({ state: 'visible' })
}

// Opens a feed's editor from the dashboard the way App does (one history
// entry), without going through the Menu.
async function openEditor(page, id) {
  await page.evaluate((u) => {
    history.pushState({ popping: 'settings' }, '', u)
    dispatchEvent(new PopStateEvent('popstate'))
  }, editorUrl(id))
  await waitEditor(page, id)
}

async function closeSettings(page) {
  await page.locator('button[aria-label="close settings"]').click()
  await page.locator(SETTINGS).waitFor({ state: 'detached' })
}

const field = (page, id, label) => page.locator(ROW(id)).getByLabel(label, { exact: true })
const rowButton = (page, id, name) => page.locator(ROW(id)).getByRole('button', { name, exact: true })
const savedShown = (page, id) => page.locator(ROW(id)).getByText('Saved ✓').first().isVisible()

// Holds each PATCH to /api/sources/<id> for `ms` before it goes on;
// page._patchesReleased counts the ones let through.
async function delayPatches(page, id, ms) {
  page._patchesReleased = 0
  await page.route(
    (u) => new URL(u).pathname === `/api/sources/${id}`,
    async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      await sleep(ms)
      page._patchesReleased++
      return route.fallback()
    },
  )
}

// The next GET of `pathname` waits `ms` and answers `body`: a poll that
// started before a change and lands after it.
async function stalePoll(page, pathname, body, ms) {
  await page.route(
    (u) => new URL(u).pathname === pathname,
    async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      await sleep(ms)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    },
    { times: 1 },
  )
}

// What the app does when it comes back to the front: a quiet poll.
const startPoll = (page) => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))

const selectedTab = async (page) => ((await page.locator(`${STRIP} [aria-selected="true"]`).first().textContent()) || '').trim()
const tabTexts = (page) => page.locator(`${STRIP} [role="tab"]`).allTextContents()
// The phone tab whose text matches `re`, and the tab key the phone stored.
const phoneTab = (page, re) => page.locator(`${STRIP} [role="tab"]`).filter({ hasText: re }).first()
const storedTab = (page) => page.evaluate(() => localStorage.getItem('popping.v1.mobileTab'))

// The element at the centre of `locator`'s box is it or inside it.
const onTop = (locator) =>
  locator.evaluate((el) => {
    const r = el.getBoundingClientRect()
    const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return !!at && (at === el || el.contains(at))
  })

// --- PR1r: reload on a new build ----------------------------------------------

// The server "deploys" a new build: fetches of / (the update check's, not
// page loads) get index.html with its entry script renamed to
// page._nextScript (index-next*.js), which answers HEAD with a script (or
// a 404 when its name says "gone"). With page._nextScript null they get
// the build the page runs. page._hold, when set, is a promise each such
// fetch waits for (counted before it waits); one the page called off
// meanwhile is dropped. Under vite preview a real reload still loads the old build,
// so the reloaded page runs the old id against the new one again.
// The log: { checks, heads, navs, events } counts the update check's
// fetches of /, the HEADs of a new script and the page loads after the
// first; events lists 'put-done' and 'nav' in the order they happened.
async function newBuild(page) {
  const log = { checks: 0, heads: [], navs: 0, events: [] }
  page._nextScript = '/static/index-next.js'
  await page.route(
    (u) => new URL(u).pathname === '/',
    async (route) => {
      if (route.request().resourceType() !== 'fetch') return route.fallback()
      log.checks++
      if (page._hold) await page._hold
      try {
        const res = await route.fetch()
        const html = await res.text()
        const next = page._nextScript
        await route.fulfill({ response: res, body: next ? html.replace(/\/static\/index-[^"]+\.js/, next) : html })
      } catch {
        // The page aborted the fetch while it was held (a hide).
      }
    },
  )
  await page.route(
    (u) => /^\/static\/index-(next\d*|gone)\.js$/.test(new URL(u).pathname),
    (route) => {
      const p = new URL(route.request().url()).pathname
      log.heads.push(`${route.request().method()} ${p}`)
      if (p.includes('gone')) return route.fulfill({ status: 404, contentType: 'text/plain', body: '' })
      return route.fulfill({ status: 200, contentType: 'text/javascript', body: '' })
    },
  )
  let loads = 0
  page.on('request', (r) => {
    if (r.resourceType() !== 'document' || r.frame() !== page.mainFrame()) return
    if (++loads > 1) {
      log.navs++
      log.events.push('nav')
    }
  })
  return log
}

// Away from the app for `minutes` of the page's clock, then back.
async function away(page, minutes) {
  await setHidden(page, true)
  const now = await page.evaluate(() => Date.now())
  await page.clock.setSystemTime(now + minutes * 60_000)
  await setHidden(page, false)
}

// --- PR1c: preferences and the brief -----------------------------------------

// A brief the mock's /api/brief/latest can answer with.
const BRIEF = {
  id: 1,
  generated_at: new Date(F.NOW - 2 * 3600000).toISOString(),
  tone: 'terse',
  content: 'TODAY IN ONE SENTENCE\nA quiet day.\nHIGHLIGHTS\n- Nothing much happened.',
  delivered_at: null,
  meta: null,
}

// Writes `rows` ({ key: value }) into the mock's preference store, then
// reloads, so the app reads them the way it would from the server.
async function putPrefs(page, rows) {
  prefMock(page.context()).seed(rows)
  await page.reload()
  await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible' })
}

// The ids of the cards the phone draws now, in order, joined by commas.
const phoneIds = (page) =>
  page.locator(`${MOBILE_MAIN} article[data-card-id]`).evaluateAll((els) => els.map((el) => el.getAttribute('data-card-id')).join(','))

// The page's own fetch drops `ids` from /api/entries and /api/foryou, so
// those stories never reach a tab: only GET /api/entries/by-ids brings
// them (PR4c's Saved by id). setKeptOut(page, ids) changes the ids from
// then on: a poll that stops returning a story, as one past its
// category's limit is.
async function keepOutOfTabs(page, ids) {
  await page.addInitScript((away) => {
    window.__keptOut = away
    const orig = window.fetch
    window.fetch = async (...args) => {
      const res = await orig(...args)
      const path = new URL(typeof args[0] === 'string' ? args[0] : args[0].url, location.href).pathname
      if (path !== '/api/entries' && path !== '/api/foryou') return res
      const rows = await res.clone().json()
      if (!Array.isArray(rows)) return res
      const out = rows.filter((e) => !window.__keptOut.includes(e.id))
      return new Response(JSON.stringify(out), { status: res.status, statusText: res.statusText, headers: res.headers })
    }
  }, ids)
}
const setKeptOut = (page, ids) => page.evaluate((away) => { window.__keptOut = away }, ids)

// Records the `ids` of every GET /api/entries/by-ids in page._byIds.
// While holdByIds holds them, the answers wait; each is then what
// page._byIdsAnswer says: 'mock' (the mock's rows), 'offline' (the
// request fails) or 'gone' (no rows: the server no longer has them).
async function recordByIds(page) {
  page._byIds = []
  page._byIdsAnswer = 'mock'
  page._byIdsHold = null
  await page.route(
    (u) => new URL(u).pathname === '/api/entries/by-ids',
    async (route) => {
      page._byIds.push(new URL(route.request().url()).searchParams.get('ids'))
      if (page._byIdsHold) await page._byIdsHold
      if (page._byIdsAnswer === 'offline') return route.abort('internetdisconnected')
      if (page._byIdsAnswer === 'gone') return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      return route.fallback()
    },
  )
}
// Holds the by-ids answers from now on; call what it returns to let them go.
function holdByIds(page) {
  let release
  page._byIdsHold = new Promise((r) => {
    release = r
  })
  return () => {
    page._byIdsHold = null
    release()
  }
}
// Whether by-ids was ever asked for `id`.
const byIdsAsked = (page, id) => page._byIds.some((ids) => ids.split(',').includes(String(id)))

// A quiet poll (startPoll) that has landed: the bodies of its
// GET /api/entries and /api/foryou as the mock sent them, once the app has
// taken them in. refresh sets the stories and clears `refreshing` (which
// disables the phone's refresh button) in one render, so the button is
// enabled again only then. A For You request that failed gives `forYou`
// [] and `forYouOk` false. Null when the poll never finished.
async function pollDone(page) {
  const answer = (p) => page.waitForResponse((r) => r.request().method() === 'GET' && new URL(r.url()).pathname === p)
  const entries = answer('/api/entries')
  const forYou = answer('/api/foryou')
  await startPoll(page)
  const [e, fy] = await Promise.all([entries, forYou])
  const button = page.locator('header[data-app-header] button[aria-label="refresh"]')
  if (!(await waitFor(() => button.isEnabled(), 5000))) return null
  return { entries: await e.json(), forYou: fy.ok() ? await fy.json() : [], forYouOk: fy.ok() }
}
// Whether a poll's answers (pollDone) hold any of `ids`.
const pollHas = (got, ids) => [...got.entries, ...got.forYou].some((e) => ids.includes(e.id))

// Opens the phone tab `re` and returns the ids it draws (numbers, in
// order) once it is the selected tab and has drawn its cards, or its
// empty state.
async function openedIds(page, re) {
  await tap(page, phoneTab(page, re))
  const empty = page.locator(MOBILE_MAIN).getByText('No stories yet', { exact: true })
  let ids = ''
  await waitFor(async () => re.test(await selectedTab(page)) && ((ids = await phoneIds(page)) !== '' || (await empty.count()) > 0), 3000)
  return ids ? ids.split(',').map(Number) : []
}

// With the clock held from the load (holdClockFromLoad in `before`: each
// 1 s batch goes only on runFor, and no card has a second on screen
// before the flow's first step), on For You: story `n` is hidden, its
// PATCH reaches the store, and a poll comes back without it (the server
// leaves hidden stories out, PR4b). Hidden the way a finger does since
// PR5a: ⋯, then Hide (hideViaSheetHeld). The badge must drop by one and
// no more: a card read for a dwell and scrolled off the top by the tap
// (the ⋯ is brought into view) would be marked seen (PR3c) and leave For
// You's order, which the flows read whole afresh after the restore. False,
// after a failure under `id`, when a step didn't happen.
async function hideAndPoll(page, context, id, n) {
  const card = page.locator(`${MOBILE_MAIN} article[data-card-id="${n}"]`)
  const hidden = () => prefMock(context).value('hidden_entries') || []
  const no = (detail) => {
    page._fail(id, detail)
    return false
  }
  if (!(await waitFor(async () => /^for you/i.test(await selectedTab(page)), 3000))) return no(`the phone opened on ${JSON.stringify(await selectedTab(page))}`)
  await card.first().waitFor({ state: 'visible' })
  const before = Number(await badge(page, /^for you/i))
  await hideViaSheetHeld(page, card)
  if (!(await waitFor(async () => (await card.count()) === 0, 3000))) return no(`the hidden ${n} is still drawn`)
  await page.clock.runFor(1000)
  if (!(await waitFor(() => hidden().includes(n), 3000))) return no('the hide never reached the store')
  const after = Number(await badge(page, /^for you/i))
  if (after !== before - 1) return no(`after hiding ${n} the For You badge reads ${after}, expected ${before - 1}: a story read on screen was scrolled off the top by the tap and marked seen; the flow needs none`)
  const got = await pollDone(page)
  if (!got) return no('the poll after the hide never finished')
  if (pollHas(got, [n])) return no(`the poll returned the hidden ${n}; the flow needs it left out`)
  return true
}
const tapUndo = (page) => tap(page, page.getByRole('button', { name: 'Undo', exact: true }))

// Every interaction event the app POSTs (one or a batch) from now on,
// in page._events; they go on to the mock.
async function recordInteractions(page) {
  page._events = []
  await page.route(
    (u) => /^\/api\/interactions(\/batch)?$/.test(new URL(u).pathname),
    (route) => {
      const req = route.request()
      if (req.method() === 'POST') {
        const b = req.postDataJSON() || {}
        for (const e of b.events || [b]) page._events.push(e)
      }
      return route.fallback()
    },
  )
}
const bookmarksFor = (page, id) => page._events.filter((e) => e.type === 'bookmark' && e.entry_id === id).length

// Every PUT, PATCH and DELETE to /api/preferences/<key> from now on, in
// the order sent, as { method, key, value, body } (`value` a PUT's,
// `body` a PATCH's change); they go on to the mock.
async function prefWrites(page) {
  const out = []
  await page.route(
    (u) => new URL(u).pathname.startsWith('/api/preferences/'),
    (route) => {
      const req = route.request()
      const method = req.method()
      if (method === 'PUT' || method === 'PATCH' || method === 'DELETE') {
        const key = decodeURIComponent(new URL(req.url()).pathname.slice('/api/preferences/'.length))
        const body = method === 'DELETE' ? undefined : req.postDataJSON() || {}
        out.push({ method, key, value: method === 'PUT' ? body.value : undefined, body: method === 'PATCH' ? body : undefined })
      }
      return route.fallback()
    },
  )
  return out
}

// The ids a recorded PATCH body adds to (or removes from) its key.
const patched = (writes, key, op) => writes.filter((w) => w.method === 'PATCH' && w.key === key).flatMap((w) => w.body[op] || [])

// --- PR7a: tab order and hiding -----------------------------------------------

const TABS_EDITOR = '[role="dialog"][aria-label="edit tabs"]'
const editPill = (page) => page.locator('button[data-edit-tabs]')
const editorButton = (page, name) => page.locator(TABS_EDITOR).getByRole('button', { name, exact: true })
const showSwitch = (page, label) => page.locator(TABS_EDITOR).getByRole('switch', { name: `Show ${label}`, exact: true })
const focusedLabel = (page) => page.evaluate(() => (document.activeElement && document.activeElement.getAttribute('aria-label')) || '')
const announced = async (page) => ((await page.locator(`${TABS_EDITOR} [aria-live="polite"]`).textContent()) || '').trim()
// The strip's tab labels, without their counts.
const stripLabels = async (page) => (await tabTexts(page)).map((t) => t.replace(/\d+$/, '').trim())
// The Menu's Jump to column rows, in order (the Menu is open).
const jumpLabels = async (page) =>
  (await page.locator(MENU).locator('h3:text-matches("jump to column", "i")').locator('xpath=../..').getByRole('button').allTextContents()).map((t) => t.trim())
// The desktop grid's column headings, in order.
const gridLabels = async (page) => (await page.locator('main.grid section header h2').allTextContents()).map((t) => t.trim())
const layoutWrites = (writes) => writes.filter((w) => w.key === 'category_layout')
// The tab order a move stores, as every tab but Brief in the default
// order: Saved, For You, then the fixture's categories by label (from
// PR4d every active feed's category, so CATEGORY_40, with no stories,
// has a tab too).
const DEFAULT_ORDER = ['meta:saved', 'meta:foryou', 'cat:deals', 'cat:nhl', 'cat:news', 'cat:podcast', `cat:${F.CATEGORY_40}`, 'cat:security', 'cat:video']

async function openTabsEditor(page) {
  await tap(page, editPill(page))
  await page.locator(TABS_EDITOR).waitFor({ state: 'visible' })
}
async function closeTabsEditor(page) {
  await tap(page, editorButton(page, 'Done'))
  await page.locator(TABS_EDITOR).waitFor({ state: 'detached' })
}

// Moves the podcast feed (11) into a new "Hockey" category from its
// editor, then back to the dashboard: the new category's tab appears.
async function newHockeyCategory(page, fail, id) {
  page._patches11 = page._patches11 || []
  await openEditor(page, 11)
  await rowButton(page, 11, 'New category').click()
  await page.fill('#fe-category-11', 'Hockey')
  await rowButton(page, 11, 'Add').click()
  if (!(await waitFor(() => page._patches11.length === 1))) return fail(id, 'no PATCH for the new category')
  await waitFor(() => savedShown(page, 11), 3000)
  await closeSettings(page)
  if (!(await waitFor(async () => (await stripLabels(page)).includes('Hockey'), 3000))) return fail(id, `no Hockey tab after the move: ${JSON.stringify(await stripLabels(page))}`)
  return true
}

// --- PR3a: desktop single scroller and batching --------------------------------

// `n` more stories from nhl_news (16) beyond the fixture's 3, ids from
// 7001, each a few minutes older than the last, so the NHL tab holds
// 3 + n stories.
const moreNhl = (n) => {
  const like = F.ENTRIES.find((e) => e.id === 602)
  return Array.from({ length: n }, (_, i) => {
    const at = new Date(F.NOW - (500 + i * 3) * 60000).toISOString()
    return { ...like, id: 7001 + i, title: `NHL story ${i + 1}`, url: `https://example.com/16/more-${7001 + i}`, published_at: at, fetched_at: at }
  })
}

// Adds `extra` to every story list the app loads for its tabs (not
// search or a filter). The mock is a context route, so the page's own
// fetch appends them on the way in, as keepOutOfTabs drops stories.
async function withStories(page, extra) {
  await page.addInitScript((rows) => {
    const orig = window.fetch
    window.fetch = async (...args) => {
      const res = await orig(...args)
      const u = new URL(typeof args[0] === 'string' ? args[0] : args[0].url, location.href)
      if (u.pathname !== '/api/entries' || u.searchParams.get('q') || u.searchParams.getAll('source').length) return res
      const got = await res.clone().json()
      if (!Array.isArray(got)) return res
      return new Response(JSON.stringify([...got, ...rows]), { status: res.status, statusText: res.statusText, headers: res.headers })
    }
  }, extra)
}

// The cards a Column has rendered so far.
const cardCount = (column) => column.locator('article[data-card-id]').count()
// Scrolls the sentinel's scroller (the nearest `[data-scroller]` around
// it) until the sentinel sits `px` below the scroller's visible bottom:
// inside the 600px margin, still out of sight. False when the column
// has no sentinel.
const sentinelBelow = (column, px) =>
  column.evaluate((col, d) => {
    const el = col.querySelector('[data-more]')
    const sc = el && el.closest('[data-scroller]')
    if (!sc) return false
    sc.scrollTop += el.getBoundingClientRect().top - sc.getBoundingClientRect().bottom - d
    return true
  }, px)

// --- PR3b: the read model ------------------------------------------------------

// The phone list's cards above the marker ("unseen") or under it, in
// Earlier, by id, in order.
const groupIds = (page, group) =>
  page.locator(`${MOBILE_MAIN} [data-group="${group}"] article[data-card-id]`).evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-card-id'))))
// The trimmed text of the first element matching `selector`, '' when
// there is none (read at once: a locator would wait for it to appear).
const textNow = (page, selector) => page.evaluate((sel) => ((document.querySelector(sel) || {}).textContent || '').trim(), selector)
const PHONE_MARKER = `${MOBILE_MAIN} [data-caught-up]`
const markerText = (page) => textNow(page, PHONE_MARKER)
const PHONE_EARLIER = `${MOBILE_MAIN} button[data-earlier]`
const PHONE_LIST = `${MOBILE_MAIN} section [data-scroller]`
// After a scroll that puts the marker above the top of `list` (a
// [data-scroller]): true once the app's own marker observer has seen it
// there, so the hold is on. An IntersectionObserver made now, on the same
// root, waits for its first report of the marker above the screen. The
// app's observer saw the crossing in the same rendering update or an
// earlier one, so its callback runs in the same notification task or an
// earlier one: nothing the flow sends next is handled before it has run. A fixed sleep
// was shorter than that callback's delay on a loaded machine. False
// after 5 s.
const markerAbove = (list) =>
  Promise.race([
    list
      .evaluate(
        (el) =>
          new Promise((resolve) => {
            const m = el.querySelector('[data-caught-up]')
            if (!m) return resolve(false)
            const io = new IntersectionObserver((entries) => {
              const e = entries[entries.length - 1]
              const top = e.rootBounds ? e.rootBounds.top : el.getBoundingClientRect().top
              if (e.isIntersecting || e.boundingClientRect.bottom > top) return
              io.disconnect()
              resolve(true)
            }, { root: el })
            io.observe(m)
          }),
      )
      .catch(() => false),
    sleep(5000).then(() => false),
  ])
// Scrolls `list` to `y` and resolves once its scroll event has been sent
// (with the next frame; the app's capture listener on the document hears
// it first), so what comes next can't overtake it. False after 3 s.
const scrolledTo = (list, y) =>
  Promise.race([
    list
      .evaluate(
        (el, to) =>
          new Promise((resolve) => {
            el.addEventListener('scroll', () => resolve(true), { once: true })
            el.scrollTo(0, to)
          }),
        y,
      )
      .catch(() => false),
    sleep(3000).then(() => false),
  ])
// What has the focus now: 'BODY', or the element's tag and its data-*
// attributes (e.g. 'DIV[data-caught-up]').
const focusedNow = (page) =>
  page.evaluate(() => {
    const a = document.activeElement
    if (!a || a === document.body) return 'BODY'
    return a.tagName + [...a.attributes].filter((x) => x.name.startsWith('data-') || x.name === 'aria-label').map((x) => (x.name === 'aria-label' ? `[aria-label="${x.value}"]` : `[${x.name}]`)).join('')
  })
// A phone tab's badge (the count of its stories not seen yet), or ''.
const badge = (page, re) => phoneTab(page, re).evaluate((el) => ((el.querySelector('[data-badge]') || {}).textContent || '').trim())
// The fixture's story ids of one category, in id order.
const idsOf = (category) =>
  F.ENTRIES.filter((e) => (F.SOURCES.find((s) => s.id === e.source_id) || {}).category === category).map((e) => e.id)
// The ids the PATCHes in `writes` (prefWrites) add to (or remove from)
// `seen:<category>`.
const seenAdded = (writes, category) => patched(writes, `seen:${category}`, 'add')
const seenRemoved = (writes, category) => patched(writes, `seen:${category}`, 'remove')
// Opens a story the way a finger does: its headline (a new-tab link the
// harness keeps on the page).
const openStory = (page, id) => tap(page, page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] a[aria-label="open in new tab"]`).first())
// Scrolls the phone list to its end, a step at a time, until every
// batch has loaded (no "load more" sentinel left).
async function phoneListToEnd(page) {
  for (let i = 0; i < 10; i++) {
    await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, el.scrollHeight))
    await sleep(250)
    if (!(await page.locator(`${MOBILE_MAIN} [data-more]`).count())) return true
  }
  return false
}
// Stories that "arrive" on the next poll: everything pushed onto
// window.__arrivals is added to the tab story lists the page loads (as
// withStories does, through the page's own fetch).
async function withArrivals(page) {
  await page.addInitScript(() => {
    window.__arrivals = []
    const orig = window.fetch
    window.fetch = async (...args) => {
      const res = await orig(...args)
      const u = new URL(typeof args[0] === 'string' ? args[0] : args[0].url, location.href)
      if (u.pathname !== '/api/entries' || u.searchParams.get('q') || u.searchParams.getAll('source').length || !window.__arrivals.length) return res
      const got = await res.clone().json()
      if (!Array.isArray(got)) return res
      return new Response(JSON.stringify([...window.__arrivals, ...got]), { status: res.status, statusText: res.statusText, headers: res.headers })
    }
  })
}
// A story that arrives now (newer than every other), from the feed of
// fixture story `like` (601: nhl_news; 103: bbc_news).
const arrival = (id, like = 601) => {
  const from = F.ENTRIES.find((e) => e.id === like)
  const at = new Date(F.NOW - 60000).toISOString()
  return { ...from, id, title: `Breaking story ${id}`, url: `https://example.com/${from.source_id}/arrival-${id}`, image_url: null, image_path: null, published_at: at, fetched_at: at }
}
const arrivalNhl = (id) => arrival(id, 601)

// --- PR3c: seen on scroll ------------------------------------------------------

// lib/seen.ts: a card qualifies after this long at least half on screen;
// scroll-seen stories are collected this long before they are written.
// Read from its source, so a retune at Gate 1 (SEEN_DWELL_MS 700 or 1500)
// is one edit there.
function seenConstant(name) {
  const src = require('fs').readFileSync(require('path').join(__dirname, '..', '..', 'src', 'lib', 'seen.ts'), 'utf8')
  const m = src.match(new RegExp(`export const ${name} = (\\d+)\\b`))
  if (!m) throw new Error(`flows.cjs: no ${name} in src/lib/seen.ts`)
  return Number(m[1])
}
const SEEN_DWELL_MS = seenConstant('SEEN_DWELL_MS')
const SEEN_BATCH_MS = seenConstant('SEEN_BATCH_MS')
// Holds the page's timers where they are (installClock in `before`
// first): nothing that waits on a timer runs until the flow moves the
// clock with runFor. On the phone a card on screen for SEEN_DWELL_MS
// and then scrolled past is used up (PR3c), and so are the cards on
// screen with the marker; a flow that counts unseen stories with cards
// or the marker on screen holds the clock, so it counts what it tests
// and not how long a step took. It pauses a second ahead, as the other
// flows do: the page's clock keeps running until the pause lands, so a
// smaller step can land in its past. Hold before the tap that opens the
// tab under test (its cards then start with no time on screen).
async function holdClock(page) {
  const now = await page.evaluate(() => Date.now())
  await page.clock.pauseAt(now + 1000)
}
// For a flow whose tab under test is the one the page opens on (a new
// device opens on For You, where the hide flows hide): the clock is
// installed and held before the page loads, so no card has a second on
// screen before the flow's first step, and only the flow's `runFor`s give
// the cards time. `holdClock` can't do this: it pauses a second ahead, and
// the cards on screen since the load got their dwell in that second. The
// page loads held (its fetches and renders wait on no timer; PR3c's
// flow-new-arrival-appends reloads under a held clock the same way).
// Called in `before`, in place of `installClock`.
async function holdClockFromLoad(page) {
  await installClock(page)
  await page.clock.pauseAt(F.NOW + 1000)
}
// Resolves once every IntersectionObserver has reported the page as it
// is now: a new observer's first report comes in the same task as every
// older observer's (one task notifies them all, oldest first), so the
// app's have run by then. Works with the clock held (no rAF needed).
const observersSettled = (page) =>
  page.evaluate(
    () =>
      new Promise((resolve) => {
        const io = new IntersectionObserver(() => {
          io.disconnect()
          resolve()
        })
        io.observe(document.body)
      }),
  )
// The phone list's cards above the marker, by id: at least half on
// screen now (`half`), or entirely above its top (`above`).
const listCards = (page) =>
  page.locator(PHONE_LIST).evaluate((el) => {
    const box = el.getBoundingClientRect()
    const half = []
    const above = []
    for (const a of el.querySelectorAll('[data-group="unseen"] article[data-card-id]')) {
      const r = a.getBoundingClientRect()
      const id = Number(a.getAttribute('data-card-id'))
      const shown = Math.max(0, Math.min(r.bottom, box.bottom) - Math.max(r.top, box.top))
      if (r.height > 0 && shown >= 0.5 * Math.min(r.height, box.height)) half.push(id)
      if (r.bottom <= box.top) above.push(id)
    }
    return { half, above }
  })
// Scrolls the phone list by `px` and waits for the observers to see it.
async function scrollListBy(page, px) {
  await page.locator(PHONE_LIST).evaluate((el, d) => {
    el.scrollTop += d
  }, px)
  await observersSettled(page)
}
// Scrolls the phone list so card `id`'s top is at the list's top: every
// card before it is above the top.
async function scrollCardToTop(page, id) {
  await page.locator(PHONE_LIST).evaluate((el, cid) => {
    const card = el.querySelector(`article[data-card-id="${cid}"]`)
    el.scrollTop += card.getBoundingClientRect().top - el.getBoundingClientRect().top
  }, id)
  await observersSettled(page)
}
// The cards on screen stay there for the dwell (they qualify).
const dwell = (page) => page.clock.runFor(SEEN_DWELL_MS + 100)
// The scroll-seen batch, then prefSync's PATCH batch, go out, and every
// request sent is answered, so `prefWrites` holds it: a check that
// nothing was written can follow.
async function writeSeen(page) {
  await page.clock.runFor(SEEN_BATCH_MS + 50)
  await page.clock.runFor(1100)
  await requestsSettled(page)
}
// Every request to /api/preferences and /api/interactions the page has
// sent is answered (the harness's fetch log): one a timer just sent has
// reached the flow's routes.
const requestsSettled = (page) => waitFor(() => harnessLog(page).then((h) => h.fetches.every((f) => f.answered)), 5000)
const sortedIds = (a) => [...a].sort((x, y) => x - y)

// --- PR5a: the Reddit card -----------------------------------------------------

// The card's ⋯ sheet (ActionSheet.tsx).
const SHEET = '[role="dialog"][aria-label="story actions"]'
const sheetRow = (page, name) => page.locator(`${SHEET} button[data-sheet-action]`).filter({ hasText: new RegExp(`^${name}$`) })
// Opens `card`'s ⋯ sheet and waits for its slide-up to end.
async function openSheet(page, card) {
  await tap(page, card.locator('button[aria-label="more actions"]'))
  await page.locator(SHEET).waitFor({ state: 'visible' })
  await page.waitForFunction((sel) => {
    const d = document.querySelector(sel)
    return !!d && d.getAnimations().every((a) => a.playState !== 'running') && Math.abs(d.getBoundingClientRect().bottom - innerHeight) < 1
  }, SHEET, { timeout: 5000 })
}
// Hides `card` the way a finger does: ⋯, then Hide.
async function hideViaSheet(page, card) {
  await openSheet(page, card)
  await tap(page, sheetRow(page, 'Hide'))
}
// hideViaSheet under a held clock (holdClock, holdClockFromLoad or a
// flow's own pauseAt): the sheet starts its slide-up a frame after it
// mounts, and the held clock holds requestAnimationFrame too, so the
// clock is stepped a frame at a time until the sheet is up. Those
// frames are far short of a dwell, and seen on scroll is paused while
// the sheet is open (App's modalOpen), so they mark no card seen. The
// sheet's focus move after the Hide waits for the flow's next runFor.
async function hideViaSheetHeld(page, card) {
  await tap(page, card.locator('button[aria-label="more actions"]'))
  await page.locator(SHEET).waitFor({ state: 'visible' })
  const up = () =>
    page.evaluate((sel) => {
      const d = document.querySelector(sel)
      return !!d && d.getAnimations().every((a) => a.playState !== 'running') && Math.abs(d.getBoundingClientRect().bottom - innerHeight) < 1
    }, SHEET)
  for (let i = 0; i < 10 && !(await waitFor(up, i === 0 ? 50 : 1000)); i++) await page.clock.runFor(16)
  await tap(page, sheetRow(page, 'Hide'))
}
const neversFor = (page, id) => page._events.filter((e) => e.type === 'never' && e.entry_id === id).length
const eventsFor = (page, id, type) => page._events.filter((e) => e.type === type && e.entry_id === id).length
// The toast's Undo.
const undoButton = (page) => page.locator('[role="status"] button[aria-label="Undo"]')
// A horizontal finger drag across `card`'s headline by `dx` pixels
// (negative: left). Starts on the title text, not on a control.
async function swipeCard(page, card, dx) {
  const box = await card.locator('a[aria-label="open in new tab"]').first().boundingBox()
  if (!box) return false
  const from = { x: box.x + Math.min(box.width * 0.5, 120), y: box.y + Math.min(box.height / 2, 14) }
  return touchDrag(page, { from, to: { x: from.x + dx, y: from.y }, steps: 12, stepMs: 30 })
}

// --- PR4d: the window ------------------------------------------------------------

// A device that first loaded the read model a month ago: D33's baseline
// (stories fetched more than 48 h before the first load count as seen)
// then leaves the fixture's 3- and 10-day-old stories unseen, as a
// story that arrived since the deploy is on a phone.
const oldDevice = (page) =>
  page.addInitScript((at) => {
    localStorage.setItem('popping.v1.firstRunAt', String(at))
  }, F.NOW - 30 * 24 * 3600000)

module.exports = [
  flow('flow-fj-youtube-handle', {
    before: async ({ page }) => {
      page._tests = await record(page, 'POST', '/api/sources/test', testOk(CHANNEL_FEED))
      page._posts = await record(page, 'POST', '/api/sources')
    },
    run: async ({ page, fail }) => {
      await openAddTab(page)
      await page.locator('input[type="radio"][value="youtube_channel"]').check({ force: true })
      await page.fill('#fm-url', '@somehandle')
      if (await isDisabled(page, 'Test')) fail('FJ', 'Test is disabled with @handle and no name')
      if (await isDisabled(page, 'Add feed')) fail('FJ', 'Add feed is disabled with @handle and no name')
      await page.getByRole('button', { name: 'Test', exact: true }).click()
      await page.locator('[data-test-result="ok"]').waitFor({ state: 'visible' })
      await page.getByRole('button', { name: 'Add feed', exact: true }).click()
      if (!(await waitFor(() => page._posts.length === 1))) return fail('FJ', `expected 1 POST, saw ${page._posts.length}`)
      const body = page._posts[0]
      if (body.url !== CHANNEL_FEED) fail('FJ', `POST url is ${body.url}, expected the resolved feed`)
      if (body.name !== 'somehandle_yt') fail('FJ', `POST name is ${body.name}, expected somehandle_yt`)
      if (body.type !== 'youtube_channel') fail('FJ', `POST type is ${body.type}`)
      if (page._tests.length !== 1) fail('FJ', `expected 1 Test request, saw ${page._tests.length}`)
    },
  }),

  flow('flow-fj2-youtube-video-link', {
    before: async ({ page }) => {
      page._tests = await record(page, 'POST', '/api/sources/test', testOk(CHANNEL_FEED))
      page._posts = await record(page, 'POST', '/api/sources')
    },
    run: async ({ page, fail }) => {
      await openAddTab(page)
      await page.locator('input[type="radio"][value="youtube_channel"]').check({ force: true })
      await page.fill('#fm-url', 'https://www.youtube.com/watch?v=abcdefghijk')
      if (!(await page.getByText('Name is chosen after Test.').isVisible())) fail('FJ2', 'no "Name is chosen after Test." hint')
      await page.getByRole('button', { name: 'Add feed', exact: true }).click()
      if (!(await waitFor(() => page._posts.length === 1))) return fail('FJ2', `expected 1 POST, saw ${page._posts.length}`)
      if (page._tests.length !== 1) fail('FJ2', `expected Add to run 1 Test first, saw ${page._tests.length}`)
      const body = page._posts[0]
      if (body.url !== CHANNEL_FEED) fail('FJ2', `POST url is ${body.url}, expected the resolved feed`)
      if (body.name !== 'channel_qrstuv_yt') fail('FJ2', `POST name is ${body.name}, expected channel_qrstuv_yt`)
    },
  }),

  flow('flow-fp-auto-subreddit', {
    before: async ({ page }) => {
      page._auto = await record(page, 'POST', '/api/sources/auto')
      page._posts = await record(page, 'POST', '/api/sources')
    },
    run: async ({ page, fail }) => {
      await openAddTab(page)
      await page.fill('input[aria-label="link to add"]', 'r/python')
      await page.getByRole('button', { name: 'Auto-add', exact: true }).click()
      await page.waitForTimeout(300)
      if (!(await page.locator('input[type="radio"][value="reddit"]').isChecked())) fail('FP', 'the form did not switch to Subreddit')
      const value = await page.locator('#fm-url').inputValue()
      if (value !== 'r/python') fail('FP', `the address field holds ${JSON.stringify(value)}`)
      if (page._auto.length || page._posts.length) fail('FP', 'a request was sent for a subreddit paste')
    },
  }),

  flow('flow-fq-new-category', {
    before: async ({ page }) => {
      page._posts = await record(page, 'POST', '/api/sources')
    },
    run: async ({ page, fail }) => {
      await openAddTab(page)
      await page.fill('#fm-url', 'https://example.com/feed.xml')
      await page.getByRole('button', { name: 'New category', exact: true }).click()
      await page.fill('#fm-category', 'NEWS')
      if (!(await isDisabled(page, 'Add feed'))) fail('FQ', 'Add feed is enabled while a new category is pending')
      if (!(await page.getByText('Finish or cancel the new category.').isVisible())) fail('FQ', 'no hint while a new category is pending')
      if (!(await page.getByText('Using your existing “News” category').isVisible())) fail('FQ', 'no "Using your existing" line for NEWS')
      await page.locator('#fm-category').press('Enter')
      const picked = await page.locator('select#fm-category').inputValue()
      if (picked !== 'news') fail('FQ', `the picker holds ${JSON.stringify(picked)} after NEWS, expected "news"`)
      await page.getByRole('button', { name: 'Add feed', exact: true }).click()
      if (!(await waitFor(() => page._posts.length === 1))) return fail('FQ', 'no POST after Add feed')
      if (page._posts[0].category !== 'news') fail('FQ', `POST category is ${JSON.stringify(page._posts[0].category)}`)

      // The form clears once the first feed is added.
      if (!(await waitFor(async () => (await page.locator('#fm-url').inputValue()) === ''))) return fail('FQ', 'the form did not clear after Add')
      await page.fill('#fm-url', 'https://example.org/hockey.xml')
      await page.getByRole('button', { name: 'New category', exact: true }).click()
      await page.fill('#fm-category', 'Hockey')
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      const select = page.locator('select#fm-category')
      const value = await select.inputValue()
      const text = await select.evaluate((el) => el.options[el.selectedIndex]?.textContent || '')
      if (value !== 'Hockey' || text !== 'Hockey (new)') fail('FQ', `selected option is ${JSON.stringify(text)} (${JSON.stringify(value)})`)
      await page.getByRole('button', { name: 'Add feed', exact: true }).click()
      if (!(await waitFor(() => page._posts.length === 2))) return fail('FQ', 'no second POST')
      if (page._posts[1].category !== 'Hockey') fail('FQ', `second POST category is ${JSON.stringify(page._posts[1].category)}`)
    },
  }),

  flow('flow-fr-headers', {
    before: async ({ page }) => {
      page._posts = await record(page, 'POST', '/api/sources')
    },
    run: async ({ page, fail }) => {
      await openAddTab(page)
      await page.fill('#fm-url', 'https://example.com/feed.xml')
      await page.locator('button[aria-controls="fm-headers"]').click()
      await page.getByRole('button', { name: 'Add header', exact: true }).click()
      await page.locator('#fm-headers input[aria-label="header name"]').last().fill('X-Test')
      await page.locator('#fm-url').focus()
      if (!(await page.getByText('Fill in both to save this header.').isVisible())) fail('FR', 'no row error for a half-filled header')
      if (!(await waitFor(() => isDisabled(page, 'Test'), 2000))) fail('FR', 'Test is enabled with a half-filled header')
      if (!(await waitFor(() => isDisabled(page, 'Add feed'), 2000))) fail('FR', 'Add feed is enabled with a half-filled header')
      await page.locator('#fm-headers input[placeholder="Value"]').last().fill('abc')
      if (!(await waitFor(async () => !(await isDisabled(page, 'Add feed')), 2000))) fail('FR', 'Add feed stays disabled once the header is complete')
      await page.locator('input[type="radio"][value="reddit"]').check({ force: true })
      await page.fill('#fm-url', 'r/python')
      await page.getByRole('button', { name: 'Add feed', exact: true }).click()
      if (!(await waitFor(() => page._posts.length === 1))) return fail('FR', 'no POST for the subreddit')
      if ('custom_headers' in page._posts[0]) fail('FR', `the subreddit POST carries custom_headers ${JSON.stringify(page._posts[0].custom_headers)}`)
    },
  }),

  dashFlow('flow-fg-settings-back', {
    run: async ({ page, fail }) => {
      // An app entry to come back to, so Back stays inside the app.
      await page.evaluate(() => history.pushState(null, '', '/?start=1'))
      const before = await page.evaluate(() => history.length)
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: /^Settings/ }).click()
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      const tab = await page.locator(`${SETTINGS} [role="tablist"]:not([aria-label]) [aria-selected="true"]`).first().textContent()
      if (!/Feeds/.test(tab || '')) fail('FG', `Menu → Settings opened ${JSON.stringify(tab)}, expected Feeds`)
      await page.locator('button[aria-label="close settings"]').click()
      await page.locator(SETTINGS).waitFor({ state: 'detached' })
      const after = await page.evaluate(() => history.length)
      if (after - before > 1) fail('FG', `history grew by ${after - before} for one open and close`)
      await page.goBack()
      await page.waitForTimeout(400)
      if (await page.locator(SETTINGS).count()) fail('FG', 'Back after Done reopened Settings')
      const u = new URL(page.url())
      if (!/^https?:$/.test(u.protocol)) fail('FG', `Back left the app: ${page.url()}`)
      for (const k of ['view', 'tab', 'source']) if (u.searchParams.has(k)) fail('FG', `the URL still has ${k}: ${page.url()}`)
    },
  }),

  dashFlow('flow-ft-menu-edit-feed', {
    run: async ({ page, fail }) => {
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: 'edit The Verge', exact: true }).click()
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      await page.locator('#feed-4').waitFor({ state: 'visible' })
      if (!(await waitFor(() => rowOpen(page, 4), 3000))) fail('FT', 'the_verge is not open after its ›')
      await page.waitForTimeout(300)
      if (!(await inView(page, '#feed-4', '.overflow-y-auto'))) fail('FT', 'the_verge is not scrolled into view')
      if (!/source=4/.test(page.url())) fail('FT', `the URL has no source=4: ${page.url()}`)
      // Another feed while Settings stays open, through the URL.
      await page.evaluate(() => {
        const u = new URL(location.href)
        u.searchParams.set('source', '11')
        history.replaceState(history.state, '', u.toString())
        dispatchEvent(new PopStateEvent('popstate'))
      })
      if (!(await waitFor(() => rowOpen(page, 11), 3000))) fail('FT', 'darknet_diaries did not open from the URL')
      await page.waitForTimeout(300)
      if (!(await inView(page, '#feed-11', '.overflow-y-auto'))) fail('FT', 'darknet_diaries is not scrolled into view')
      if (await rowOpen(page, 4)) fail('FT', 'the_verge stayed open')
    },
  }),

  dashFlow('flow-fu-jump-to-column', {
    run: async ({ page, fail }) => {
      const STRIP = '[role="tablist"][aria-label="dashboard sections"]'
      await openMenu(page)
      // The last column, which starts outside the strip's visible area.
      const rows = page.locator(MENU).locator('h3:text-matches("jump to column", "i")').locator('xpath=../..').getByRole('button')
      const last = rows.last()
      const label = ((await last.textContent()) || '').trim()
      await last.click()
      await page.waitForTimeout(600)
      const selected = page.locator(`${STRIP} [aria-selected="true"]`).first()
      const text = ((await selected.textContent()) || '').trim()
      if (!text.startsWith(label)) fail('FU', `selected tab is ${JSON.stringify(text)}, expected ${JSON.stringify(label)}`)
      if (!(await inView(page, `${STRIP} [aria-selected="true"]`, STRIP))) fail('FU', 'the selected tab is outside the strip')
    },
  }),

  dashFlow('flow-menu-closed-inert', {
    run: async ({ page, fail }) => {
      await openMenu(page)
      await page.locator('button[aria-label="close menu"]').click()
      await page.waitForTimeout(500)
      const focusedMenu = await page.evaluate(() => document.activeElement?.getAttribute('aria-label'))
      if (focusedMenu !== 'open menu') fail('MENU', `focus went to ${JSON.stringify(focusedMenu)} after closing, expected the ≡ button`)
      for (let i = 0; i < 40; i++) {
        await page.keyboard.press('Tab')
        const inMenu = await page.evaluate(() => !!document.activeElement?.closest('aside[aria-label="menu"]'))
        if (inMenu) return fail('MENU', `Tab reached the closed Menu after ${i + 1} presses`)
      }
    },
  }),

  flow('flow-fs-auto-already-added', {
    run: async ({ page, fail }) => {
      await openAddTab(page)
      // capture.cjs answers Auto-add with the first fixture source (BBC News).
      await page.fill('input[aria-label="link to add"]', 'https://www.bbc.co.uk/news')
      await page.getByRole('button', { name: 'Auto-add', exact: true }).click()
      const ok = await page
        .getByText('Already added: BBC News')
        .waitFor({ state: 'visible', timeout: 5000 })
        .then(() => true, () => false)
      if (!ok) fail('FS', 'no "Already added: BBC News" toast')
    },
  }),
  dashFlow('flow-fa-category-pick', {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/4')
    },
    run: async ({ page, fail }) => {
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: 'edit The Verge', exact: true }).click()
      await waitEditor(page, 4)
      await page.locator('#fe-category-4').selectOption('tech')
      if (!(await waitFor(() => page._patches.length >= 1))) return fail('FA', 'no PATCH after picking Tech')
      if (!(await waitFor(() => savedShown(page, 4), 3000))) fail('FA', 'no "Saved ✓" after the category save')
      await sleep(300)
      if (page._patches.length !== 1) fail('FA', `expected 1 PATCH, saw ${page._patches.length}`)
      if (JSON.stringify(page._patches[0]) !== '{"category":"tech"}') fail('FA', `PATCH body is ${JSON.stringify(page._patches[0])}`)
    },
  }),

  dashFlow('flow-fa2-new-category-existing', {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/11')
    },
    run: async ({ page, fail }) => {
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: 'edit Darknet Diaries', exact: true }).click()
      await waitEditor(page, 11)
      await rowButton(page, 11, 'New category').click()
      await page.fill('#fe-category-11', 'NEWS')
      if (!(await page.locator(ROW(11)).getByText('Using your existing “News” category').isVisible())) {
        fail('FA2', 'no "Using your existing “News” category" line')
      }
      await rowButton(page, 11, 'Add').click()
      if (!(await waitFor(() => page._patches.length >= 1))) return fail('FA2', 'no PATCH after Add')
      await sleep(300)
      if (page._patches.length !== 1) fail('FA2', `expected 1 PATCH, saw ${page._patches.length}`)
      if (JSON.stringify(page._patches[0]) !== '{"category":"news"}') fail('FA2', `PATCH body is ${JSON.stringify(page._patches[0])}`)
    },
  }),

  editorFlow('flow-fa3-new-category-reserved', 4, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/4')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 4)
      await rowButton(page, 4, 'New category').click()
      for (const name of ['For You', 'for_you']) {
        await page.fill('#fe-category-4', name)
        if (!(await page.locator(ROW(4)).getByText(/is used by the app/).isVisible())) fail('FA3', `no reserved-name error for ${JSON.stringify(name)}`)
        if (!(await rowButton(page, 4, 'Add').isDisabled())) fail('FA3', `Add is enabled for ${JSON.stringify(name)}`)
        await page.locator('#fe-category-4').press('Enter')
      }
      await sleep(300)
      if (page._patches.length) fail('FA3', `a reserved category sent ${page._patches.length} PATCH(es)`)
    },
  }),

  editorFlow('flow-fb-address-commits-on-leave', 4, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/4')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 4)
      const addr = field(page, 4, 'Address')
      await addr.click()
      await addr.press('End')
      await addr.pressSequentially('abcdefghij')
      await sleep(400)
      if (page._patches.length) fail('FB', `typing sent ${page._patches.length} PATCH(es)`)
      await addr.press('Enter')
      if (!(await waitFor(() => page._patches.length >= 1))) return fail('FB', 'no PATCH after Enter')
      await addr.focus()
      await addr.blur()
      await sleep(400)
      if (page._patches.length !== 1) fail('FB', `expected 1 PATCH after Enter and a blur, saw ${page._patches.length}`)
      if (!String(page._patches[0].url || '').endsWith('abcdefghij')) fail('FB', `PATCH body is ${JSON.stringify(page._patches[0])}`)
    },
  }),

  editorFlow('flow-fc-category-refused', 4, {
    expectedFailedLoads: ['/api/sources/4'],
    before: async ({ page }) => {
      await page.route(
        (u) => new URL(u).pathname === '/api/sources/4',
        (route) =>
          route.request().method() === 'PATCH'
            ? route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: "category 'For You' is reserved for a built-in column" }) })
            : route.fallback(),
      )
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 4)
      await page.locator('#fe-category-4').selectOption('tech')
      const msg = page.locator(ROW(4)).getByText(/is used by the app\. Pick another name\./)
      if (!(await waitFor(() => msg.isVisible(), 3000))) return fail('FC', 'no message for the refused category')
      const value = await page.locator('#fe-category-4').inputValue()
      if (value !== 'news') fail('FC', `the picker shows ${JSON.stringify(value)}, expected the saved "news"`)
      await msg.scrollIntoViewIfNeeded()
      if (!(await onTop(msg))) fail('FC', 'the message is covered')
    },
  }),

  editorFlow('flow-fd-network-retry', 6, {
    expectedFailedLoads: ['/api/sources/6'],
    before: async ({ page }) => {
      page._attempts = 0
      page._abort = true
      await page.route(
        (u) => new URL(u).pathname === '/api/sources/6',
        (route) => {
          if (route.request().method() !== 'PATCH') return route.fallback()
          page._attempts++
          return page._abort ? route.abort('failed') : route.fallback()
        },
      )
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      const addr = field(page, 6, 'Address')
      const typed = 'https://slickdeals.net/newsearch.php?rss=2'
      await addr.fill(typed)
      await addr.press('Enter')
      const msg = page.locator(ROW(6)).getByText("Couldn't reach Popping. Check your connection.")
      if (!(await waitFor(() => msg.isVisible(), 6000))) return fail('FD', 'no network message after the save and its retry failed')
      if (page._attempts !== 2) fail('FD', `expected 2 tries (one retry), saw ${page._attempts}`)
      if ((await addr.inputValue()) !== typed) fail('FD', `the field holds ${JSON.stringify(await addr.inputValue())}, not what was typed`)
      const retry = rowButton(page, 6, 'Try again')
      if (!(await retry.isVisible())) return fail('FD', 'no Try again')
      page._abort = false
      await retry.click()
      if (!(await waitFor(() => page._attempts === 3))) return fail('FD', `Try again sent ${page._attempts - 2} request(s)`)
      if (!(await waitFor(() => savedShown(page, 6), 3000))) fail('FD', 'no "Saved ✓" after Try again')
    },
  }),

  editorFlow('flow-fe-browser-identity', 4, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/4')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 4)
      // The preset among the headers (the Last problem box has one too,
      // used in FE3).
      await rowButton(page, 4, 'Use a browser identity').last().click()
      if (!(await waitFor(() => page._patches.length >= 1))) return fail('FE', 'no PATCH after Use a browser identity')
      const headers = page._patches[0].custom_headers || {}
      if (!('User-Agent' in headers)) fail('FE', `PATCH body is ${JSON.stringify(page._patches[0])}`)
      const names = page.locator(`${ROW(4)} input[aria-label="header name"]`)
      if (!(await waitFor(async () => (await names.count()) === 1 && (await names.first().inputValue()) === 'User-Agent', 3000))) {
        fail('FE', 'the headers did not show the saved User-Agent')
      }
      await rowButton(page, 4, 'Add header').click()
      await names.last().fill('Cookie')
      await names.last().blur()
      if (!(await page.locator(ROW(4)).getByText("Popping can't send a Cookie header.").isVisible())) fail('FE', 'no message for a Cookie header')
      await sleep(300)
      if (page._patches.length !== 1) fail('FE', `a refused header sent ${page._patches.length - 1} PATCH(es)`)
    },
  }),

  editorFlow('flow-fe2-add-header-keeps-focus', 6, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      await rowButton(page, 6, 'Add header').click()
      await page.locator(`${ROW(6)} input[aria-label="header name"]`).last().fill('X-Test')
      await rowButton(page, 6, 'Add header').click()
      await sleep(300)
      const focused = await page.evaluate(() => document.activeElement?.getAttribute('aria-label'))
      if (focused !== 'header name') fail('FE2', `focus is on ${JSON.stringify(focused)}, expected the new header name`)
      if (page._patches.length) fail('FE2', `sent ${page._patches.length} PATCH(es)`)
      if (await page.locator(ROW(6)).getByText('Fill in both to save this header.').count()) fail('FE2', '"Fill in both" shows while still typing')
    },
  }),

  editorFlow('flow-fe3-preset-skips-half-row', 4, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/4')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 4)
      await rowButton(page, 4, 'Add header').click()
      await page.locator(`${ROW(4)} input[aria-label="header name"]`).last().fill('X-Half')
      // The Last problem box's button: it saves from outside the headers,
      // so they reload from the saved map once the save lands.
      await rowButton(page, 4, 'Use a browser identity').first().click()
      if (!(await waitFor(() => page._patches.length >= 1))) return fail('FE3', 'no PATCH after the preset')
      await sleep(300)
      if (page._patches.length !== 1) fail('FE3', `expected 1 PATCH, saw ${page._patches.length}`)
      const headers = page._patches[0].custom_headers || {}
      if (!('User-Agent' in headers)) fail('FE3', `no User-Agent in ${JSON.stringify(headers)}`)
      if ('X-Half' in headers) fail('FE3', `the half-filled row was sent: ${JSON.stringify(headers)}`)
      // The reload keeps the row still being typed.
      const names = page.locator(`${ROW(4)} input[aria-label="header name"]`)
      await waitFor(async () => (await names.evaluateAll((els) => els.map((e) => e.value))).includes('User-Agent'), 3000)
      const kept = await page.locator(`${ROW(4)} input[aria-label="header name"]`).evaluateAll((els) => els.map((e) => e.value))
      if (!kept.includes('X-Half')) fail('FE3', `the half-filled row was dropped: ${JSON.stringify(kept)}`)
    },
  }),

  editorFlow('flow-ff-done-while-saving', 6, {
    before: async ({ page }) => {
      await delayPatches(page, 6, 4000)
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      const typed = 'https://slickdeals.net/ff.xml'
      await field(page, 6, 'Address').fill(typed)
      await page.locator('button[aria-label="close settings"]').click()
      await page.locator(SETTINGS).waitFor({ state: 'detached', timeout: 6000 })
      // Done must not wait for the save: the sheet is gone while the
      // server is still holding the PATCH (whatever the machine's load).
      if (page._patchesReleased) fail('FF', 'the sheet closed only after the save finished')
      if (!(await waitFor(() => page._patches.length === 1, 2000))) return fail('FF', 'Done did not save the address')
      if (!(await waitFor(() => page._patchesReleased === 1, 6000))) return fail('FF', 'the delayed save never finished')
      await sleep(300)
      await openEditor(page, 6)
      const value = await field(page, 6, 'Address').inputValue()
      if (value !== typed) fail('FF', `reopened with ${JSON.stringify(value)}`)
    },
  }),

  editorFlow('flow-ff2-done-save-fails', 6, {
    expectedFailedLoads: ['/api/sources/6'],
    before: async ({ page }) => {
      await page.route(
        (u) => new URL(u).pathname === '/api/sources/6',
        (route) => (route.request().method() === 'PATCH' ? route.abort('failed') : route.fallback()),
      )
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      const typed = 'https://slickdeals.net/ff2.xml'
      await field(page, 6, 'Address').fill(typed)
      await closeSettings(page)
      const toastText = page.getByText(/^Couldn't save the address for Slickdeals Frontpage: Couldn't reach Popping/)
      if (!(await waitFor(() => toastText.isVisible(), 6000))) return fail('FF', 'no toast for the failed save')
      if ((await page.locator('[role="status"]').count()) !== 1) fail('FF', 'more than one toast')
      await page.getByRole('button', { name: 'Open', exact: true }).click()
      await waitEditor(page, 6)
      const value = await field(page, 6, 'Address').inputValue()
      if (value !== typed) fail('FF', `Open shows ${JSON.stringify(value)}, not the typed address`)
      if (!(await page.locator(ROW(6)).getByText("Couldn't reach Popping. Check your connection.").isVisible())) fail('FF', 'Open does not show the reason')
    },
  }),

  editorFlow('flow-fh-interval-coalesce', 6, {
    before: async ({ page }) => {
      await delayPatches(page, 6, 800)
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      const select = page.locator('#fe-refresh-6')
      const current = await select.inputValue()
      const values = await select.evaluate((el) => [...el.options].map((o) => o.value))
      const picks = values.filter((v) => v !== current).slice(0, 3)
      for (const v of picks) await select.selectOption(v)
      await waitFor(() => page._patches.length >= 2, 4000)
      await sleep(1200)
      if (page._patches.length !== 2) return fail('FH', `expected 2 PATCHes for 3 quick picks, saw ${page._patches.length}`)
      const last = page._patches[1].refresh_interval_seconds
      if (last !== Number(picks[2])) fail('FH', `the second PATCH carries ${last}, expected ${picks[2]}`)
    },
  }),

  editorFlow('flow-fi-stale-poll-keeps-save', 6, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      const stale = await page.evaluate(() => fetch('/api/sources').then((r) => r.json()))
      await stalePoll(page, '/api/sources', stale, 1500)
      await startPoll(page)
      await sleep(100)
      await page.locator('#fe-category-6').selectOption('tech')
      if (!(await waitFor(() => page._patches.length === 1))) return fail('FI', 'no PATCH')
      await sleep(2200)
      const value = await page.locator('#fe-category-6').inputValue()
      if (value !== 'tech') fail('FI', `a poll from before the save put the category back to ${JSON.stringify(value)}`)
    },
  }),

  editorFlow('flow-fk-fixed-fields', 1, {
    run: async ({ page, fail }) => {
      await waitEditor(page, 1)
      if ((await field(page, 1, 'Address').getAttribute('readonly')) == null) fail('FK', "the built-in's Address is editable")
      // The name is only a label, so a built-in can be renamed too.
      if ((await field(page, 1, 'Name').getAttribute('readonly')) != null) fail('FK', "the built-in's Name is read-only")
      if (await page.locator('#fe-refresh-1').count()) fail('FK', 'the built-in has an interval picker')
      if (!(await page.locator(ROW(1)).getByText(/\(built-in feed\)$/).isVisible())) fail('FK', 'no "(built-in feed)" interval text')
      if (await rowButton(page, 1, 'Add header').count()) fail('FK', 'the built-in has a Headers section')
      await page.locator(`${ROW(7)} button[aria-expanded]`).click()
      await waitEditor(page, 7)
      if (await rowButton(page, 7, 'Add header').count()) fail('FK', 'the subreddit has a Headers section')
      if (!(await field(page, 7, 'Subreddit').isVisible())) fail('FK', 'the subreddit address is not labelled Subreddit')
    },
  }),

  dashFlow('flow-fl-rename-keeps-filter-and-view', {
    before: async ({ page, context }) => {
      const preset = { id: 'p1', name: 'Deals view', activeSources: ['slickdeals_frontpage', 'rfd_hot_deals'], columnPrefs: {} }
      prefMock(context).seed({ filter_presets: [preset] })
      page._puts = await record(page, 'PUT', '/api/preferences/filter_presets')
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await openMenu(page)
      await page.locator(`${MENU} input[aria-label="filter by Slickdeals Frontpage"]`).check()
      await page.locator('button[aria-label="remove Slickdeals Frontpage from filter"]').waitFor({ state: 'visible' })
      await openEditor(page, 6)
      const name = field(page, 6, 'Name')
      // Typed as the user wants it shown: capitals and spaces kept.
      await name.fill('Slickdeals Renamed')
      await name.press('Enter')
      if (!(await waitFor(() => page._patches.length === 1))) return fail('FL', 'no PATCH for the rename')
      const body = page._patches[0]
      if (body.display_name !== 'Slickdeals Renamed' || 'name' in body) fail('FL', `the rename sent ${JSON.stringify(body)}`)
      await waitFor(() => savedShown(page, 6), 3000)
      if ((await name.inputValue()) !== 'Slickdeals Renamed') fail('FL', `the field shows ${JSON.stringify(await name.inputValue())}`)
      await closeSettings(page)
      if (!(await page.locator('button[aria-label="remove Slickdeals Renamed from filter"]').isVisible())) fail('FL', 'the filter chip did not show the new name')
      if (await page.locator('button[aria-label="remove Slickdeals Frontpage from filter"]').count()) fail('FL', 'the old name is still in the filter')
      // The feed's internal id didn't change, so the saved view needs no rewrite.
      await sleep(1500)
      if (page._puts.length) fail('FL', 'the saved view was rewritten for a label change')
    },
  }),

  dashFlow('flow-fm-phone-tab-follows', {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/16')
      page._patches11 = await record(page, 'PATCH', '/api/sources/11')
    },
    run: async ({ page, fail }) => {
      // The only feed in its column moves to another category.
      await page.locator(`${STRIP} [role="tab"]`).filter({ hasText: /nhl/i }).first().click()
      if (!/nhl/i.test(await selectedTab(page))) return fail('FM', 'could not select the NHL tab')
      await openEditor(page, 16)
      await page.locator('#fe-category-16').selectOption('news')
      if (!(await waitFor(() => page._patches.length === 1))) return fail('FM', 'no PATCH for the category')
      await waitFor(() => savedShown(page, 16), 3000)
      await closeSettings(page)
      await sleep(300)
      const tabs = await tabTexts(page)
      if (!tabs.length) return fail('FM', 'the tab bar is gone')
      if (tabs.some((t) => /nhl/i.test(t))) fail('FM', `the emptied NHL tab is still there: ${JSON.stringify(tabs)}`)
      const now = await selectedTab(page)
      if (!now || /nhl/i.test(now)) fail('FM', `selected tab is ${JSON.stringify(now)}`)
      if (!(await page.locator(`${MOBILE_MAIN} article[data-card-id]`).count())) fail('FM', 'the selected column shows nothing')

      // Another feed moves into a new column; the phone stays on Video.
      await page.locator(`${STRIP} [role="tab"]`).filter({ hasText: /video/i }).first().click()
      await openEditor(page, 11)
      await rowButton(page, 11, 'New category').click()
      await page.fill('#fe-category-11', 'Hockey')
      await rowButton(page, 11, 'Add').click()
      if (!(await waitFor(() => page._patches11.length === 1))) return fail('FM', 'no PATCH for the new category')
      await waitFor(() => savedShown(page, 11), 3000)
      await closeSettings(page)
      await sleep(300)
      if (!(await tabTexts(page)).some((t) => /hockey/i.test(t))) fail('FM', 'no Hockey tab after the move')
      const after = await selectedTab(page)
      if (!/video/i.test(after)) fail('FM', `the phone moved to ${JSON.stringify(after)} instead of staying on Video`)
    },
  }),

  // --- PR1b: tabs by key ------------------------------------------------------

  // Saving a story fills the Saved tab in front of News: the phone stays
  // on News, stores it by key, and opens it again after a reload.
  dashFlow('flow-tab-stays-on-star', {
    run: async ({ page, fail, shot }) => {
      await tap(page, phoneTab(page, /^news/i))
      if (!/^news/i.test(await selectedTab(page))) return fail('TAB-STAR', 'could not select the News tab')
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      await tap(page, card.locator('button[aria-label="save"]'))
      const saving = () => harnessLog(page).then((h) => h.fetches.some((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries'))
      if (!(await waitFor(saving, 3000))) return fail('TAB-STAR', 'the save sent no PATCH')
      await sleep(300)
      const now = await selectedTab(page)
      if (!/^news/i.test(now)) fail('TAB-STAR', `saving moved the phone to ${JSON.stringify(now)}`)
      const heading = ((await page.locator(`${MOBILE_MAIN} section header h2`).first().textContent()) || '').trim()
      if (heading !== 'News') fail('TAB-STAR', `the phone shows the ${JSON.stringify(heading)} column under the News tab`)
      await shot('saved')
      if ((await storedTab(page)) !== 'cat:news') fail('TAB-STAR', `the stored tab is ${JSON.stringify(await storedTab(page))}`)
      // The save is in the store before the reload.
      if (!(await waitFor(() => (prefMock(page.context()).value('starred_entries') || []).length === 1, 3000))) return fail('TAB-STAR', 'the save never reached the store')
      await page.reload()
      await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible' })
      // The preferences read back: Saved holds the story.
      if (!(await waitFor(() => page.evaluate(() => !!document.querySelector('[aria-label="unsave"]')), 3000))) return fail('TAB-STAR', 'the save was not read back after the reload')
      await sleep(300)
      const reloaded = await selectedTab(page)
      if (!/^news/i.test(reloaded)) fail('TAB-STAR', `the reload opened ${JSON.stringify(reloaded)}`)
    },
  }),

  // Unsaving the only saved story empties Saved, which stays (PR7a): the
  // phone stays on it, it says "No stories yet", and a save from For You
  // fills it again without moving the phone.
  dashFlow('flow-unsave-last-saved', {
    before: async ({ context }) => {
      prefMock(context).seed({ starred_entries: [103] })
    },
    run: async ({ page, fail, shot }) => {
      const savedTab = phoneTab(page, /^saved/i)
      if (!(await waitFor(() => answered(page, 'GET', '/api/preferences'), 3000))) return fail('UNSAVE', 'the preferences were not read')
      // A new phone opens on For You, even with Saved in front of it.
      const first = await selectedTab(page)
      if (!/^for you/i.test(first)) fail('UNSAVE', `a new phone opened on ${JSON.stringify(first)}`)
      await tap(page, savedTab)
      const cards = page.locator(`${MOBILE_MAIN} article[data-card-id]`)
      if ((await cards.count()) !== 1) return fail('UNSAVE', `Saved shows ${await cards.count()} stories, expected 1`)
      await tap(page, cards.first().locator('button[aria-label="unsave"]'))
      if (!(await waitFor(async () => (await cards.count()) === 0, 3000))) return fail('UNSAVE', 'Saved still shows the story after unsaving it')
      await sleep(300)
      if (!(await savedTab.count())) return fail('UNSAVE', 'the Saved tab went with its last story')
      const stayed = await selectedTab(page)
      if (!/^saved/i.test(stayed)) return fail('UNSAVE', `after unsaving, the phone shows ${JSON.stringify(stayed)}, expected Saved`)
      if ((await storedTab(page)) !== 'meta:saved') fail('UNSAVE', `the stored tab is ${JSON.stringify(await storedTab(page))} after unsaving`)
      if (!(await page.locator(MOBILE_MAIN).getByText('No stories yet', { exact: true }).isVisible())) fail('UNSAVE', 'the empty Saved tab does not say "No stories yet"')
      // Feeds of your own: no first-feed button.
      if (await page.locator(`${MOBILE_MAIN} [data-first-feed]`).count()) fail('UNSAVE', 'an empty Saved offers "Add your first feed" although there are feeds')
      await shot('empty')
      await tap(page, phoneTab(page, /^for you/i))
      await tap(page, cards.first().locator('button[aria-label="save"]'))
      await sleep(300)
      const now = await selectedTab(page)
      if (!/^for you/i.test(now)) fail('UNSAVE', `saving from For You moved the phone to ${JSON.stringify(now)}`)
      if ((await storedTab(page)) !== 'meta:foryou') fail('UNSAVE', `the stored tab is ${JSON.stringify(await storedTab(page))}`)
      await shot('end')
    },
  }),

  // A phone that stored the old column index opens on the tab at that
  // place, stores it as a key and drops the index, but only once the
  // preferences have loaded: here they arrive 1.5 s late, after the
  // stories, and bring the saved story that put Saved at index 0. Migrated
  // before them, index 0 would be For You.
  dashFlow('flow-tab-migrates-index', {
    // The strip, not a card: a wrong migration can land on any tab.
    wait: `${STRIP} [aria-selected="true"]`,
    before: async ({ page, context }) => {
      await page.addInitScript(() => localStorage.setItem('popping.v1.mobileCol.last', '0'))
      prefMock(context).seed({ starred_entries: [103] })
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences',
        async (route) => {
          if (route.request().method() !== 'GET') return route.fallback()
          await sleep(1500)
          return route.fallback()
        },
      )
    },
    run: async ({ page, fail }) => {
      const stored = () => page.evaluate(() => [localStorage.getItem('popping.v1.mobileTab'), localStorage.getItem('popping.v1.mobileCol.last')])
      const hasSaved = async () => (await tabTexts(page)).some((t) => /^saved/i.test(t))
      if (!(await waitFor(hasSaved, 5000))) return fail('TAB-MIGRATE', 'no Saved tab once the preferences loaded')
      if (!(await waitFor(async () => (await stored())[0] !== null, 3000))) return fail('TAB-MIGRATE', 'the old index was never migrated')
      await sleep(300)
      // Index 0 was Saved, the first column when it has stories.
      const now = await selectedTab(page)
      if (!/^saved/i.test(now)) fail('TAB-MIGRATE', `the old index 0 opened ${JSON.stringify(now)}, expected Saved`)
      const after = await stored()
      if (after[0] !== 'meta:saved') fail('TAB-MIGRATE', `the stored tab is ${JSON.stringify(after[0])}`)
      if (after[1] !== null) fail('TAB-MIGRATE', `the old index is still stored: ${JSON.stringify(after[1])}`)
    },
  }),

  // A stored tab whose category went while the phone was away opens on
  // For You (the fresh-device tab), not on Brief, which is all the strip
  // holds before the first refresh; For You is then the stored tab.
  dashFlow('flow-tab-gone-on-load', {
    wait: `${STRIP} [aria-selected="true"]`,
    before: async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('popping.v1.mobileTab', 'cat:gone'))
    },
    run: async ({ page, fail }) => {
      if (!(await waitFor(async () => (await storedTab(page)) !== 'cat:gone', 3000))) return fail('TAB-GONE', 'the missing tab is still stored')
      const now = await selectedTab(page)
      if (!/^for you/i.test(now)) fail('TAB-GONE', `a stored tab that went opened ${JSON.stringify(now)}, expected For You`)
      if ((await storedTab(page)) !== 'meta:foryou') fail('TAB-GONE', `the stored tab is ${JSON.stringify(await storedTab(page))}`)
    },
  }),

  // Clearing the source filter brings the phone back to its tab, even while
  // the unfiltered stories are still on the way: until they land the strip
  // holds only the filtered feed's category, so News looks gone.
  dashFlow('flow-filter-clear-keeps-tab', {
    run: async ({ page, fail }) => {
      await tap(page, phoneTab(page, /^news/i))
      if (!/^news/i.test(await selectedTab(page))) return fail('FILTER-TAB', 'could not select the News tab')
      await openMenu(page)
      await page.locator(`${MENU} input[aria-label="filter by Slickdeals Frontpage"]`).check()
      await page.locator('button[aria-label="remove Slickdeals Frontpage from filter"]').waitFor({ state: 'visible' })
      // The filtered refresh has landed: Brief and the filter's column.
      if (!(await waitFor(async () => (await tabTexts(page)).length === 2, 3000))) return fail('FILTER-TAB', `the filter left ${JSON.stringify(await tabTexts(page))}`)
      await sleep(500)
      if ((await storedTab(page)) !== 'cat:news') fail('FILTER-TAB', `the filter replaced the stored tab with ${JSON.stringify(await storedTab(page))}`)
      await page.route(
        (u) => new URL(u).pathname === '/api/entries',
        async (route) => {
          await sleep(1500)
          return route.fallback()
        },
      )
      await tap(page, page.getByRole('button', { name: 'clear all' }))
      // The unfiltered refresh is still on the way.
      await sleep(700)
      if ((await storedTab(page)) !== 'cat:news') fail('FILTER-TAB', `clearing the filter stored ${JSON.stringify(await storedTab(page))} before the stories came back`)
      if (!(await waitFor(async () => (await tabTexts(page)).some((t) => /^news/i.test(t)), 5000))) return fail('FILTER-TAB', 'no News tab after clearing the filter')
      await sleep(300)
      const now = await selectedTab(page)
      if (!/^news/i.test(now)) fail('FILTER-TAB', `after clearing the filter the phone shows ${JSON.stringify(now)}, expected News`)
      if ((await storedTab(page)) !== 'cat:news') fail('FILTER-TAB', `the stored tab is ${JSON.stringify(await storedTab(page))}`)
    },
  }),

  // A For You request that fails empties the tab's list, but the tab
  // stays (PR7a): the phone stays on For You, keeps it as its tab, and
  // shows its stories again when For You loads. PR3b: a poll that drops
  // stories leaves them in the tab until it starts over, so the stories
  // shown stay through the failed poll; the empty tab shows once it starts
  // over (another tab and back).
  dashFlow('flow-foryou-fails-keeps-tab', {
    expectedFailedLoads: ['/api/foryou'],
    run: async ({ page, fail }) => {
      if (!/^for you/i.test(await selectedTab(page))) return fail('FY-FAIL', `a new phone opened on ${JSON.stringify(await selectedTab(page))}`)
      if (!(await waitFor(async () => (await storedTab(page)) === 'meta:foryou', 3000))) return fail('FY-FAIL', `the stored tab is ${JSON.stringify(await storedTab(page))}`)
      let failing = true
      await page.route(
        (u) => new URL(u).pathname === '/api/foryou',
        (route) => (failing ? route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' }) : route.fallback()),
      )
      const cards = page.locator(`${MOBILE_MAIN} article[data-card-id]`)
      const shown = await cards.count()
      const failed = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/foryou')
      await startPoll(page)
      await failed
      await sleep(500)
      if ((await cards.count()) !== shown) fail('FY-FAIL', `the failed poll changed For You from ${shown} stories to ${await cards.count()}; they stay until the tab starts over`)
      if (!/^for you/i.test(await selectedTab(page)) || (await storedTab(page)) !== 'meta:foryou') fail('FY-FAIL', `after the failed poll the phone shows ${JSON.stringify(await selectedTab(page))} and stores ${JSON.stringify(await storedTab(page))}`)
      // Another tab and back: For You starts over, empty.
      await phoneTab(page, /^deals/i).click()
      await phoneTab(page, /^for you/i).click()
      if (!(await waitFor(async () => (await cards.count()) === 0, 3000))) return fail('FY-FAIL', 'For You kept its stories after its request failed and the tab started over')
      await sleep(300)
      if (!(await tabTexts(page)).some((t) => /^for you/i.test(t))) fail('FY-FAIL', 'the For You tab went while its request fails')
      if (!/^for you/i.test(await selectedTab(page))) fail('FY-FAIL', `while For You fails the phone shows ${JSON.stringify(await selectedTab(page))}`)
      if ((await storedTab(page)) !== 'meta:foryou') fail('FY-FAIL', `a failed For You request stored ${JSON.stringify(await storedTab(page))}`)
      failing = false
      await startPoll(page)
      if (!(await waitFor(async () => (await cards.count()) > 0, 3000))) return fail('FY-FAIL', 'For You did not show its stories again')
      await sleep(300)
      const now = await selectedTab(page)
      if (!/^for you/i.test(now)) fail('FY-FAIL', `when For You came back the phone stayed on ${JSON.stringify(now)}`)
    },
  }),

  // While For You stays down (an empty tab, PR7a), only what hangs on it
  // waits: a new phone shows its first tab with stories without storing
  // it, but a tap stores a tab at once, and unsaving Saved's last story
  // leaves the phone on Saved.
  dashFlow('flow-foryou-down-settles', {
    expectedFailedLoads: ['/api/foryou'],
    before: async ({ page, context }) => {
      await page.route(
        (u) => new URL(u).pathname === '/api/foryou',
        (route) => route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' }),
      )
      prefMock(context).seed({ starred_entries: [103] })
    },
    run: async ({ page, fail, shot }) => {
      const cards = page.locator(`${MOBILE_MAIN} article[data-card-id]`)
      if (!(await waitFor(() => page.evaluate(() => !!document.querySelector('[aria-label="unsave"]')), 3000))) return fail('FY-DOWN', 'the saved story was not read')
      if (!(await tabTexts(page)).some((t) => /^for you/i.test(t))) return fail('FY-DOWN', 'no For You tab while its request fails')
      await sleep(500)
      const first = await selectedTab(page)
      if (!/^saved/i.test(first)) fail('FY-DOWN', `with For You down a new phone opened on ${JSON.stringify(first)}, expected Saved`)
      if ((await storedTab(page)) !== null) fail('FY-DOWN', `a new phone stored ${JSON.stringify(await storedTab(page))} while For You is down`)
      await tap(page, phoneTab(page, /^saved/i))
      if ((await storedTab(page)) !== 'meta:saved') return fail('FY-DOWN', `a tap on Saved stored ${JSON.stringify(await storedTab(page))}`)
      await tap(page, cards.first().locator('button[aria-label="unsave"]'))
      if (!(await waitFor(async () => (await cards.count()) === 0, 3000))) return fail('FY-DOWN', 'Saved still shows the story after unsaving it')
      await sleep(300)
      const now = await selectedTab(page)
      if (!/^saved/i.test(now)) fail('FY-DOWN', `unsaving moved the phone to ${JSON.stringify(now)}`)
      if ((await storedTab(page)) !== 'meta:saved') fail('FY-DOWN', `after unsaving the stored tab is ${JSON.stringify(await storedTab(page))}`)
      await shot('empty')
    },
  }),

  // While For You stays down, a phone with nothing stored (or only the old
  // index) shows a tab without storing it. Saving a story there fills
  // Saved in front of it; the phone stays on that tab and still stores
  // nothing.
  ...[
    ['flow-foryou-down-holds-fresh', null],
    ['flow-foryou-down-holds-index', '1'],
  ].map(([name, index]) =>
    dashFlow(name, {
      expectedFailedLoads: ['/api/foryou'],
      before: async ({ page }) => {
        if (index != null) await page.addInitScript((i) => localStorage.setItem('popping.v1.mobileCol.last', i), index)
        await page.route(
          (u) => new URL(u).pathname === '/api/foryou',
          (route) => route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' }),
        )
      },
      run: async ({ page, fail }) => {
        if (!(await tabTexts(page)).some((t) => /^for you/i.test(t))) return fail('FY-HOLD', 'no For You tab while its request fails')
        await sleep(500)
        const before = await selectedTab(page)
        if (/^(brief|saved|for you)/i.test(before)) return fail('FY-HOLD', `the phone opened on ${JSON.stringify(before)}, an empty tab`)
        if ((await storedTab(page)) !== null) fail('FY-HOLD', `stored ${JSON.stringify(await storedTab(page))} while For You is down`)
        const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
        await tap(page, card.locator('button[aria-label="save"]'))
        if (!(await waitFor(() => (prefMock(page.context()).value('starred_entries') || []).length === 1, 3000))) return fail('FY-HOLD', 'the save never reached the store')
        await sleep(300)
        const now = await selectedTab(page)
        if (now !== before) fail('FY-HOLD', `saving moved the phone from ${JSON.stringify(before)} to ${JSON.stringify(now)}`)
        if ((await storedTab(page)) !== null) fail('FY-HOLD', `saving stored ${JSON.stringify(await storedTab(page))} while For You is down`)
        if (index != null) {
          const old = await page.evaluate(() => localStorage.getItem('popping.v1.mobileCol.last'))
          if (old !== index) fail('FY-HOLD', `the old index was migrated while For You is down (now ${JSON.stringify(old)})`)
        }
      },
    }),
  ),

  // A failed GET /api/preferences leaves Saved unread, not gone: a phone
  // that stored Saved keeps it stored, and (from PR7a, where Saved is
  // always a tab) shows it, empty, until the preferences load.
  dashFlow('flow-prefs-fail-keeps-saved', {
    expectedFailedLoads: ['/api/preferences'],
    // Saved is empty while its preferences are unread: no card to wait for.
    wait: `${STRIP} [aria-selected="true"]`,
    before: async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('popping.v1.mobileTab', 'meta:saved'))
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences',
        (route) =>
          route.request().method() === 'GET'
            ? route.fulfill({ status: 500, contentType: 'text/plain', body: 'error' })
            : route.fallback(),
      )
    },
    run: async ({ page, fail }) => {
      await sleep(1000)
      const now = await selectedTab(page)
      if (!/^saved/i.test(now)) fail('PREFS-FAIL', `the phone shows ${JSON.stringify(now)}, expected Saved`)
      if ((await storedTab(page)) !== 'meta:saved') fail('PREFS-FAIL', `a failed preferences load stored ${JSON.stringify(await storedTab(page))}`)
      // A later poll doesn't settle it either.
      await startPoll(page)
      await sleep(1000)
      if ((await storedTab(page)) !== 'meta:saved') fail('PREFS-FAIL', `after a poll the stored tab is ${JSON.stringify(await storedTab(page))}`)
    },
  }),

  // Tapping the tab already shown scrolls its list to the top: smoothly,
  // or at once under reduced motion. Another tab opens at its own top.
  // Only the phone list carries data-scroller.
  dashFlow('flow-tap-tab-scrolls-top', {
    run: async ({ page, fail, shot }) => {
      await tap(page, phoneTab(page, /^news/i))
      const count = await page.locator('[data-scroller]').count()
      if (count !== 1) fail('TAP-TOP', `${count} elements carry data-scroller, expected 1`)
      const list = page.locator(`${MOBILE_MAIN} section [data-scroller]`)
      const top = () => list.evaluate((el) => el.scrollTop)
      const down = async () => {
        await list.evaluate((el) => el.scrollTo(0, el.scrollHeight))
        return waitFor(async () => (await top()) > 200, 2000)
      }
      if (!(await down())) return fail('TAP-TOP', 'the News list did not scroll down')
      await tap(page, phoneTab(page, /^news/i))
      if (!(await waitFor(async () => (await top()) === 0, 3000))) fail('TAP-TOP', `a tap on the shown tab left the list at ${await top()}`)
      if (!/^news/i.test(await selectedTab(page))) fail('TAP-TOP', `the tap moved the phone to ${JSON.stringify(await selectedTab(page))}`)
      await shot('top')

      await page.emulateMedia({ reducedMotion: 'reduce' })
      if (!(await down())) return fail('TAP-TOP', 'the News list did not scroll down again')
      await tap(page, phoneTab(page, /^news/i))
      const at = await top()
      if (at !== 0) fail('TAP-TOP', `under reduced motion the list is at ${at} right after the tap, expected 0`)
      await page.emulateMedia({ reducedMotion: null })

      if (!(await down())) return fail('TAP-TOP', 'the News list did not scroll down a third time')
      await tap(page, phoneTab(page, /^deals/i))
      if (!/^deals/i.test(await selectedTab(page))) return fail('TAP-TOP', 'could not select the Deals tab')
      await sleep(100)
      const deals = await top()
      if (deals !== 0) fail('TAP-TOP', `Deals opened at ${deals}, not at its top`)
    },
  }),

  // --- PR1c: Newest | Best, Reset scope, Brief needs an AI provider ------------

  // Newest | Best reorders the tab, is stored as the tab's row, and a
  // saved view's chip no longer overwrites it. For You opens on Best,
  // which is the order the server sent.
  dashFlow('flow-sort-best', {
    run: async ({ page, fail, shot }) => {
      // A saved view that captured News as Newest (what an older build's
      // chip would have put back) and no source filter.
      await putPrefs(page, {
        filter_presets: [{ id: 'p-1', name: 'Morning', activeSources: [], columnPrefs: { news: { sort: 'newest', minScore: 0, maxAgeHours: null } } }],
      })
      const writes = await prefWrites(page)
      const ids = () => page.locator(`${MOBILE_MAIN} article[data-card-id]`).evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-card-id'))))
      const entry = (id) => F.ENTRIES.find((e) => e.id === id)
      const byTime = (list) => [...list].sort((a, b) => Date.parse(entry(b).published_at) - Date.parse(entry(a).published_at))
      const byScore = (list) => [...list].sort((a, b) => entry(b).composite_score - entry(a).composite_score)

      // For You opens on Best: the server's order, untouched.
      if (!/^for you/i.test(await selectedTab(page))) return fail('SORT', `the phone opened on ${JSON.stringify(await selectedTab(page))}`)
      const fy = await ids()
      const fyServer = F.FOR_YOU.map((e) => e.id).filter((id) => fy.includes(id))
      if (JSON.stringify(fy) !== JSON.stringify(fyServer)) fail('SORT', `For You shows ${fy}, the server sent ${fyServer}`)
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      const pressed = (v) => page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-sort="${v}"]`).getAttribute('aria-pressed')
      if ((await pressed('best')) !== 'true') fail('SORT', 'For You does not show Best as its sort')
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))

      await tap(page, phoneTab(page, /^news/i))
      const before = await ids()
      if (before.length < 5) return fail('SORT', `News shows ${before.length} stories`)
      if (JSON.stringify(before) !== JSON.stringify(byTime(before))) fail('SORT', `News opens as ${before}, not newest first`)
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      if ((await pressed('newest')) !== 'true') fail('SORT', 'News does not show Newest as its sort')
      if (await page.locator(`${MOBILE_MAIN} [data-column-popover] input[type="range"]`).count()) fail('SORT', 'the minimum score slider is still there')
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-sort="best"]`))
      if (!(await waitFor(async () => JSON.stringify(await ids()) === JSON.stringify(byScore(before)), 3000))) {
        return fail('SORT', `after Best, News shows ${await ids()}, expected ${byScore(before)}`)
      }
      if ((await pressed('best')) !== 'true') fail('SORT', 'Best is not shown as chosen')
      await shot('best')
      if (!(await waitFor(() => writes.some((w) => w.key === 'column_prefs:news'), 3000))) return fail('SORT', 'Best sent no column_prefs PUT')
      const row = writes.find((w) => w.key === 'column_prefs:news')
      if (row.method !== 'PUT' || !row.value || row.value.sort !== 'best') fail('SORT', `the News row was written as ${JSON.stringify(row)}`)

      // The chip changes the filter only.
      writes.length = 0
      await tap(page, page.locator('[role="toolbar"][aria-label="saved filter presets"] button', { hasText: 'Morning' }).first())
      await sleep(1200)
      const prefsPuts = writes.filter((w) => w.key.startsWith('column_prefs:'))
      if (prefsPuts.length) fail('SORT', `the chip wrote ${JSON.stringify(prefsPuts)}`)
      const after = await ids()
      if (JSON.stringify(after) !== JSON.stringify(byScore(before))) fail('SORT', `after the chip, News shows ${after}, not Best`)

      // Saved has no sort to pick (from PR4d no ⋯ at all: nothing in it
      // would apply).
      await tap(page, page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().locator('button[aria-label="save"]'))
      if (!(await waitFor(async () => (await tabTexts(page)).some((t) => /^saved/i.test(t)), 3000))) return fail('SORT', 'no Saved tab after saving')
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => /^saved/i.test(await selectedTab(page)), 3000))) return fail('SORT', 'Saved did not open')
      if (await page.locator(`${MOBILE_MAIN} [data-column-action="prefs"]`).count()) fail('SORT', 'Saved has a ⋯ (Newest | Best)')
    },
  }),

  // Saved lists saves newest save first (PR4c): the order they were
  // saved in, latest first, whatever their stories' dates (by date they
  // would read 101,103,105,111).
  dashFlow('flow-saved-order', {
    run: async ({ page, fail }) => {
      await putPrefs(page, { starred_entries: [103, 105, 111, 101] })
      await tap(page, phoneTab(page, /^saved/i))
      const want = '101,111,105,103'
      if (!(await waitFor(async () => (await phoneIds(page)) === want, 3000))) fail('SAVED-ORDER', `Saved shows ${await phoneIds(page)}, expected ${want}`)
    },
  }),

  // Saved has no window (D5): it lists every save, however long ago the
  // story arrived. 505 (2.8 days old) and 605 (10 days) are outside the
  // 2 days every tab loads, so only by-ids brings them, and Saved shows
  // them in save order (newest save first) next to 103 (40 minutes).
  dashFlow('flow-saved-ignores-window', {
    before: async ({ page, context }) => {
      prefMock(context).seed({ starred_entries: [605, 505, 103] })
      await recordByIds(page)
    },
    run: async ({ page, fail }) => {
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => (await phoneIds(page)) === '103,505,605', 3000))) {
        return fail('SAVED-WINDOW', `Saved shows ${await phoneIds(page)}, expected 103,505,605 (every save, the ones older than 2 days included)`)
      }
      if (JSON.stringify(page._byIds) !== JSON.stringify(['505,605'])) fail('SAVED-WINDOW', `by-ids was asked for ${JSON.stringify(page._byIds)}, expected 505,605 (the saves no tab holds)`)
    },
  }),

  // Saved by id (PR4c, D5): Saved lists every save, whatever the tabs
  // loaded. Here 503 and 402 never reach a tab (the page's fetch drops
  // them from /api/entries and /api/foryou), so only GET
  // /api/entries/by-ids brings them: once, for just the ids no tab holds
  // (103 and 105 are held), newest save first. 999999 has no row: it is
  // asked for once and never again, poll after poll. A save made on
  // another device arrives with the preferences' resync and is fetched
  // too, once: a poll that lands while its answer is out doesn't ask for
  // it again. An unsave takes the story out; its Undo puts it back from
  // the by-id cache, in its old place while the unsave is still in its
  // 1 s batch, at the top once the unsave reached the server (the clock
  // is held from the unsave on, so each case is the one the flow says).
  dashFlow('flow-saved-by-id', {
    before: async ({ page, context }) => {
      await installClock(page)
      prefMock(context).seed({ starred_entries: [103, 503, 999999, 105] })
      await keepOutOfTabs(page, [503, 402])
      await recordByIds(page)
    },
    run: async ({ page, context, fail, shot }) => {
      const asked = () => JSON.stringify(page._byIds)
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => (await phoneIds(page)) === '105,503,103', 3000))) return fail('SAVED-BY-ID', `Saved shows ${await phoneIds(page)}, expected 105,503,103`)
      // The story fetched by id is a full card: its thumbnail loads.
      const thumb = page.locator(`${MOBILE_MAIN} article[data-card-id="503"] img[src="/assets/thumbs/503.jpg"]`)
      if (!(await waitFor(() => thumb.evaluate((img) => img.complete && img.naturalWidth > 0).catch(() => false), 3000))) fail('SAVED-BY-ID', 'story 503 shows no thumbnail')
      // And its preview under the headline (PR5a, D34): by-ids carries it.
      if (!(await page.locator(`${MOBILE_MAIN} article[data-card-id="503"] [data-preview]`).count())) fail('SAVED-BY-ID', 'story 503 shows no preview')
      if (asked() !== JSON.stringify(['999999,503'])) fail('SAVED-BY-ID', `by-ids was asked for ${asked()}, expected only the saves no tab holds, newest first`)
      await shot('saved')

      // A save made on another device: the next resync brings it, and
      // Saved fetches it (and only it) at the top. Its answer is held
      // while one more poll lands: 402 is already being fetched, so the
      // poll's stories don't ask for it again.
      const release = holdByIds(page)
      prefMock(context).seed({ starred_entries: [103, 503, 999999, 105, 402] })
      if (!(await pollDone(page))) return fail('SAVED-BY-ID', 'the poll never finished')
      if (!(await waitFor(() => page._byIds.length >= 2, 3000))) fail('SAVED-BY-ID', `after the other device's save by-ids was asked for ${asked()}, expected 402 next`)
      if (!(await pollDone(page))) return fail('SAVED-BY-ID', 'the second poll never finished')
      await sleep(300)
      if (asked() !== JSON.stringify(['999999,503', '402'])) fail('SAVED-BY-ID', `with 402's answer still out, a poll left by-ids asked for ${asked()}, expected 402 once (and 999999 not again)`)
      release()
      if (!(await waitFor(async () => (await phoneIds(page)) === '402,105,503,103', 3000))) fail('SAVED-BY-ID', `after the other device's save Saved shows ${await phoneIds(page)}, expected 402 first`)

      // An unsave takes the story out. Its Undo brings it back at once
      // from the by-id cache: offline, and with no request. The clock is
      // held from here: the unsave's 1 s batch goes only on runFor.
      const saves = () => prefMock(context).value('starred_entries') || []
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const unsave503 = page.locator(`${MOBILE_MAIN} article[data-card-id="503"] button[aria-label="unsave"]`)
      page._byIdsAnswer = 'offline'
      const undoCase = async (label, want) => {
        await tapUndo(page)
        if (!(await waitFor(async () => (await phoneIds(page)) === want, 3000))) fail('SAVED-BY-ID', `offline, ${label}: Saved shows ${await phoneIds(page)}, expected ${want}`)
        await sleep(300)
        if (asked() !== JSON.stringify(['999999,503', '402'])) fail('SAVED-BY-ID', `the unsave's Undo (${label}) asked by-ids again: ${asked()}`)
      }
      // Undo before the unsave's batch: the two cancel out, and 503 is
      // back in its old place.
      await tap(page, unsave503)
      if (!(await waitFor(async () => (await phoneIds(page)) === '402,105,103', 3000))) fail('SAVED-BY-ID', `after unsaving 503 Saved shows ${await phoneIds(page)}`)
      await undoCase('Undo before the batch', '402,105,503,103')
      // Undo after the unsave reached the server: the save appends 503,
      // so it comes back at the top (Risks).
      await tap(page, unsave503)
      if (!(await waitFor(async () => (await phoneIds(page)) === '402,105,103', 3000))) fail('SAVED-BY-ID', `after unsaving 503 a second time Saved shows ${await phoneIds(page)}`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !saves().includes(503), 3000))) return fail('SAVED-BY-ID', `the unsave never reached the store: ${JSON.stringify(saves())}`)
      await undoCase('Undo after the batch', '503,402,105,103')
      page._byIdsAnswer = 'mock'

      // Unsaved again: it stays out through a poll, and nothing is asked
      // again.
      await tap(page, unsave503)
      if (!(await waitFor(async () => (await phoneIds(page)) === '402,105,103', 3000))) fail('SAVED-BY-ID', `after unsaving 503 again Saved shows ${await phoneIds(page)}`)
      if (!(await pollDone(page))) return fail('SAVED-BY-ID', 'the last poll never finished')
      await sleep(300)
      if (asked() !== JSON.stringify(['999999,503', '402'])) fail('SAVED-BY-ID', `after the unsave and a poll by-ids was asked for ${asked()}`)
      if ((await phoneIds(page)) !== '402,105,103') fail('SAVED-BY-ID', `after the last poll Saved shows ${await phoneIds(page)}`)
    },
  }),

  // A hide's Undo puts the story back in News and For You (PR4c), at once
  // and over a poll sent before the Undo's change went out, and in For You
  // at its place. From PR4b the server leaves hidden stories out of its
  // lists (the mock too), so a poll between the hide and the Undo drops
  // the story from the stories the tabs are built from; only a drawn
  // tab's snapshot (PR3b) still has it. A tab opened afresh is built from
  // those stories, so each check opens a tab that wasn't drawn when they
  // last changed: News (never drawn before), then For You (left for News,
  // it starts over). For You draws the server's order, so it must read
  // exactly as before the hide; the story is 102, in the middle of For
  // You (101, 301, 102, …), so a put-back at the top or at the end both
  // show. The clock is held: each 1 s batch goes only when the flow lets
  // it (runFor), and the toast stays up. Once two minutes have passed
  // since the Undo, a story the server still doesn't return goes again.
  // The clock is held from the load (holdClockFromLoad): scrolling 102
  // into view takes the cards above it off the top, and one of those
  // read for a second would be marked seen (PR3c) and leave For You's
  // order. 102 is hidden with ⋯ → Hide (PR5a, hideViaSheetHeld), and is
  // itself read for a dwell at the top
  // of the screen before the hide, so the Undo puts back a story that had
  // qualified: the put-back must not move the list (the story goes back
  // into the snapshot's place; only a new snapshot starts the list over at
  // its top) and must not mark it (the seen batch runs and the badge is as
  // before the hide).
  dashFlow('flow-hide-undo-after-poll', {
    before: async ({ page }) => {
      await holdClockFromLoad(page)
      await keepOutOfTabs(page, [])
      await recordInteractions(page)
    },
    run: async ({ page, context, fail, settle }) => {
      const id = 'HIDE-UNDO'
      const n = 102
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id="${n}"]`)
      const list = page.locator(PHONE_LIST)
      const hidden = () => prefMock(context).value('hidden_entries') || []
      const order = F.FOR_YOU.map((e) => e.id).join(',')
      // Opens the tab `re` and tells whether it draws the story.
      const opened = async (re, want = true) => {
        await tap(page, phoneTab(page, re))
        return waitFor(async () => (await card.count()) === (want ? 1 : 0), 3000)
      }
      // Opens For You afresh and tells whether it reads as before the hide.
      const forYouAsBefore = async () => (await opened(/^for you/i)) && (await phoneIds(page)) === order
      if (!(await waitFor(async () => /^for you/i.test(await selectedTab(page)), 3000))) return fail(id, `the phone opened on ${JSON.stringify(await selectedTab(page))}`)
      if (!(await waitFor(async () => (await phoneIds(page)) === order, 3000))) return fail(id, `For You opened as ${await phoneIds(page)}; the flow needs ${order}`)
      // 102 at the top of the screen, read for a dwell. The cards above it
      // left the top with no time on screen, so nothing is marked. A
      // sliver of 301 (the card above) stays on screen: Chromium anchors
      // the scroll on it, so neither the hide nor the put-back moves the
      // cards below. Anchored on 201 instead (102 exactly at the top),
      // the hide would scroll the list back to show 301 under the top
      // edge for the hide's second, and the put-back push it out above
      // the top again: a scroll past in the tracker's eyes, and a mark for
      // 301. Chromium only; WebKit has no scroll anchoring.
      await scrollCardToTop(page, n)
      await scrollListBy(page, -30)
      await dwell(page)
      const unseenBefore = await badge(page, /^for you/i)
      await hideViaSheetHeld(page, card)
      if (!(await waitFor(async () => (await card.count()) === 0, 3000))) return fail(id, 'the hidden story is still drawn')
      await page.clock.runFor(1000)
      if (!(await waitFor(() => hidden().includes(n), 3000))) return fail(id, 'the hide never reached the store')
      if ((await badge(page, /^for you/i)) !== String(Number(unseenBefore) - 1)) return fail(id, `after the hide the For You badge reads ${await badge(page, /^for you/i)}, expected ${Number(unseenBefore) - 1}; the flow needs no story seen on scroll`)
      // A poll while hidden: the server leaves 102 out of both lists.
      let got = await pollDone(page)
      if (!got) return fail(id, 'the poll never finished')
      if (pollHas(got, [n])) return fail(id, `the poll returned the hidden ${n}; the flow needs it left out`)
      // The card being read now: the first one wholly below the top edge
      // (at the top of the screen since the hide; the short 301 still
      // shows its bottom above it).
      const reading = await list.evaluate((el) => {
        const top = el.getBoundingClientRect().top
        for (const a of el.querySelectorAll('[data-group="unseen"] article[data-card-id]')) {
          if (a.getBoundingClientRect().top >= top - 1) return Number(a.getAttribute('data-card-id'))
        }
        return null
      })
      await tapUndo(page)
      if (!(await waitFor(async () => (await card.count()) === 1, 3000))) fail(id, `Undo did not bring ${n} back to For You`)
      // The put-back redraws 102 where it was: no new snapshot, so the
      // list isn't started over at its top (PR3c) and the card being read
      // is still on screen (Chromium's scroll anchoring keeps it in place
      // and 102 goes in above the top edge; without anchoring 102 shows
      // at the top and pushes it down, still on screen). The story isn't
      // marked seen by coming back: the seen batch runs (the Undo's own
      // 1 s batch is still held) and the badge is as before the hide.
      await observersSettled(page)
      const after = await list.evaluate((el) => el.scrollTop)
      if (after === 0) fail(id, "the Undo's put-back started For You over: the list is at its top")
      if (!(await listCards(page)).half.includes(reading)) fail(id, `the Undo's put-back moved the list (now at ${after}): ${reading}, being read, is off the screen`)
      await page.clock.runFor(SEEN_BATCH_MS + 50)
      if ((await badge(page, /^for you/i)) !== unseenBefore) fail(id, `after the Undo and the seen batch the For You badge reads ${await badge(page, /^for you/i)}, expected ${unseenBefore}: the put-back ${n} counts as not seen`)
      // The Undo itself put 102 back in both lists, and in For You at its place.
      if (!(await opened(/^news/i))) fail(id, `right after the Undo, News shows ${await phoneIds(page)}, without ${n}`)
      if (!(await forYouAsBefore())) fail(id, `right after the Undo, For You opened again reads ${await phoneIds(page)}, expected ${order} (as before the hide)`)
      // A poll before the Undo's change is sent (its 1 s batch is held):
      // the server still has 102 hidden and leaves it out.
      got = await pollDone(page)
      if (!got) return fail(id, 'the second poll never finished')
      if (pollHas(got, [n])) return fail(id, `the second poll already returned ${n}; the flow needs it left out`)
      if (!(await opened(/^news/i))) fail(id, `after the Undo and a poll that left it out, News shows ${await phoneIds(page)}, without ${n}`)
      if (!(await forYouAsBefore())) fail(id, `after the Undo and a poll that left it out, For You reads ${await phoneIds(page)}, expected ${order} (as before the hide)`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(n), 3000))) return fail(id, 'the Undo never reached the store')

      // The server stops returning 102 (as past its category's limit):
      // the put-back story stays over polls for two minutes after the
      // Undo, then goes.
      await setKeptOut(page, [n])
      if (!(await pollDone(page))) return fail(id, 'the third poll never finished')
      if (!(await opened(/^news/i))) fail(id, `a poll inside the two minutes took ${n} out of News: ${await phoneIds(page)}`)
      await page.clock.runFor(120_000)
      await settle()
      if (!(await pollDone(page))) return fail(id, 'the poll after two minutes never finished')
      if (!(await opened(/^for you/i, false))) fail(id, `two minutes after the Undo, For You still shows ${n}, which no poll returns`)
      if (!(await opened(/^news/i, false))) fail(id, `two minutes after the Undo, News still shows ${n}, which no poll returns`)
      // The Undo, tapped after a poll and putting 102 back through PR4c's
      // put-back, also took back the hide's held `never` (PR5a, D25):
      // long past the toast's window, none has gone.
      await requestsSettled(page)
      if (neversFor(page, n) !== 0) fail(id, `Hide, a poll, then Undo sent ${neversFor(page, n)} never events for ${n}`)
    },
  }),

  // Saved leaves out a hidden save (hide wins over saved) and a save
  // whose feed is gone (PR4c). Saved alone draws these: it isn't in the
  // tabs' hidden filter, and 503 comes only from the by-id cache. 105,
  // held by the tabs, is hidden from its card in Saved: it leaves at
  // once, while the hide's 1 s batch is still held (the clock is held,
  // as in flow-hide-undo-after-poll; the tabs still hold 105 until a
  // poll), and stays out after the poll, which no longer returns it;
  // Saved doesn't fetch it by id either (it wouldn't draw it). Then
  // 503's feed (veritasium_yt, 14) is deleted elsewhere: the next poll's
  // feeds lack it, and Saved drops 503 though the cache still holds it.
  dashFlow('flow-saved-drops-hidden-and-deleted', {
    before: async ({ page, context }) => {
      await installClock(page)
      prefMock(context).seed({ starred_entries: [103, 105, 503] })
      await keepOutOfTabs(page, [503])
      await recordByIds(page)
    },
    run: async ({ page, context, fail }) => {
      const id = 'SAVED-DROPS'
      const card = (n) => page.locator(`${MOBILE_MAIN} article[data-card-id="${n}"]`)
      const hidden = () => prefMock(context).value('hidden_entries') || []
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => (await phoneIds(page)) === '503,105,103', 3000))) return fail(id, `Saved shows ${await phoneIds(page)}, expected 503,105,103`)
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      await hideViaSheetHeld(page, card(105))
      if (!(await waitFor(async () => (await phoneIds(page)) === '503,103', 1000))) fail(id, `after hiding 105 Saved shows ${await phoneIds(page)}, with the hide's batch not sent yet`)
      if (hidden().includes(105)) fail(id, "the hide reached the store before its batch went; the flow needs it held")
      await page.clock.runFor(1000)
      if (!(await waitFor(() => hidden().includes(105), 3000))) return fail(id, 'the hide never reached the store')
      const got = await pollDone(page)
      if (!got) return fail(id, 'the poll never finished')
      if (pollHas(got, [105])) return fail(id, 'the poll returned the hidden 105; the flow needs it left out')
      await sleep(300)
      if ((await phoneIds(page)) !== '503,103') fail(id, `after a poll Saved shows ${await phoneIds(page)}, expected the hidden 105 still out`)
      if (byIdsAsked(page, 105)) fail(id, `by-ids was asked for the hidden 105: ${JSON.stringify(page._byIds)}`)

      const deleted = await page.evaluate(() => fetch('/api/sources/14', { method: 'DELETE' }).then((r) => r.status))
      if (deleted !== 204) return fail(id, `deleting feed 14 answered ${deleted}`)
      if (!(await pollDone(page))) return fail(id, 'the poll after the delete never finished')
      if (!(await waitFor(async () => (await phoneIds(page)) === '103', 3000))) fail(id, `after its feed was deleted Saved shows ${await phoneIds(page)}, expected 503 gone`)
    },
  }),

  // A save a poll stops returning (as past its category's limit) stays
  // in Saved while its by-id fetch is out: Saved draws the copy it drew
  // last (PR4c). When by-ids then finds no row for it (the server no
  // longer has the story), the next poll takes it out of Saved, and it
  // isn't asked for again.
  dashFlow('flow-saved-keeps-a-dropped-save', {
    before: async ({ page, context }) => {
      prefMock(context).seed({ starred_entries: [103, 105] })
      await keepOutOfTabs(page, [])
      await recordByIds(page)
    },
    run: async ({ page, fail }) => {
      const id = 'SAVED-DROPPED'
      const asked = () => JSON.stringify(page._byIds)
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => (await phoneIds(page)) === '105,103', 3000))) return fail(id, `Saved shows ${await phoneIds(page)}, expected 105,103`)
      if (page._byIds.length) return fail(id, `by-ids was asked for ${asked()}, though the tabs hold both saves`)
      // The next poll leaves 103 out, and its by-ids answer is held.
      const release = holdByIds(page)
      await setKeptOut(page, [103])
      if (!(await pollDone(page))) return fail(id, 'the poll never finished')
      if (!(await waitFor(() => asked() === JSON.stringify(['103']), 3000))) {
        release()
        return fail(id, `after the poll by-ids was asked for ${asked()}, expected 103`)
      }
      for (let i = 0; i < 4; i++) {
        const shown = await phoneIds(page)
        if (shown !== '105,103') {
          fail(id, `while 103's by-ids answer was out Saved showed ${shown}, expected 105,103`)
          break
        }
        await sleep(100)
      }
      // The server has no row for 103 any more.
      page._byIdsAnswer = 'gone'
      const answered = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries/by-ids')
      release()
      await answered
      if (!(await pollDone(page))) return fail(id, 'the second poll never finished')
      if (!(await waitFor(async () => (await phoneIds(page)) === '105', 3000))) fail(id, `after by-ids found no row for 103 and a poll, Saved shows ${await phoneIds(page)}, expected 105`)
      if (!(await pollDone(page))) return fail(id, 'the third poll never finished')
      await sleep(300)
      if (asked() !== JSON.stringify(['103'])) fail(id, `by-ids was asked for ${asked()}: 103 again after it found no row`)
    },
  }),

  // "Hide all downvoted" (Settings › Voted) is undone like a single hide
  // (PR4c): a poll between the hide and the Undo drops the stories (the
  // server leaves hidden ones out), and the Undo puts them back in the
  // stories the tabs are built from, also over a poll the server answered
  // before the Undo's change reached it. News and For You are drawn only
  // after both polls, so only those stories can bring 102 and 103 back
  // there; For You (which holds 102, in its middle) must read as it did
  // before the hide. The clock is held, as in flow-hide-undo-after-poll.
  flow('flow-hide-all-downvoted-undo', {
    url: '/?view=settings&tab=voted',
    before: async ({ page, context }) => {
      await installClock(page)
      prefMock(context).seed({ voted_entries: { 102: 'down', 103: 'down' } })
    },
    run: async ({ page, context, fail }) => {
      const id = 'HIDE-ALL-UNDO'
      const hidden = () => prefMock(context).value('hidden_entries') || []
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const hideAll = page.locator(`${SETTINGS} button[aria-label="Hide all downvoted"]`)
      await hideAll.waitFor({ state: 'visible' })
      await tap(page, hideAll)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => [102, 103].every((n) => hidden().includes(n)), 3000))) return fail(id, `the hide never reached the store: ${JSON.stringify(hidden())}`)
      let got = await pollDone(page)
      if (!got) return fail(id, 'the poll never finished')
      if (pollHas(got, [102, 103])) return fail(id, 'the poll returned a hidden story; the flow needs them left out')
      await tap(page, page.getByRole('button', { name: 'Undo', exact: true }))
      // A poll before the Undo's change is sent (its 1 s batch is held).
      got = await pollDone(page)
      if (!got) return fail(id, 'the second poll never finished')
      if (pollHas(got, [102, 103])) return fail(id, 'the second poll already returned a story; the flow needs them left out')
      await closeSettings(page)
      await tap(page, phoneTab(page, /^news/i))
      const want = async () => {
        const shown = (await phoneIds(page)).split(',')
        return shown.includes('102') && shown.includes('103')
      }
      if (!(await waitFor(want, 3000))) fail(id, `after Undo and a poll that left them out, News shows ${await phoneIds(page)}, without 102 and 103`)
      const order = F.FOR_YOU.map((e) => e.id).join(',')
      const forYou = (await openedIds(page, /^for you/i)).join(',')
      if (forYou !== order) fail(id, `after Undo and a poll that left them out, For You reads ${forYou}, expected ${order} (as before the hide)`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(102) && !hidden().includes(103), 3000))) fail(id, 'the Undo never reached the store')
    },
  }),

  // Settings › Hidden's Restore all puts the stories back as any hide's
  // Undo does (PR4c): after a poll dropped the hidden 101, Restore all
  // puts it back in the stories the tabs are built from, so News and For
  // You, each opened afresh once Settings closes, show it before the
  // restore's batch reaches the server. The clock is held from the load,
  // as in flow-hide-undo-after-poll.
  dashFlow('flow-hidden-restore-all-after-poll', {
    before: async ({ page }) => {
      await holdClockFromLoad(page)
    },
    run: async ({ page, context, fail }) => {
      const id = 'RESTORE-ALL'
      const hidden = () => prefMock(context).value('hidden_entries') || []
      if (!(await hideAndPoll(page, context, id, 101))) return
      await page.evaluate((u) => {
        history.pushState({ popping: 'settings' }, '', u)
        dispatchEvent(new PopStateEvent('popstate'))
      }, '/?view=settings&tab=hidden')
      const restoreAll = page.locator(`${SETTINGS} button[aria-label="Restore all"]`)
      await restoreAll.waitFor({ state: 'visible' })
      await tap(page, restoreAll)
      await closeSettings(page)
      // News (never drawn before), then For You (left for News, so it
      // starts over): each is built from the stories Restore all put 101
      // back in.
      let ids = await openedIds(page, /^news/i)
      if (!ids.includes(101)) fail(id, `after Restore all, News shows ${ids}, without 101`)
      ids = await openedIds(page, /^for you/i)
      if (!ids.includes(101)) fail(id, `after Restore all, For You shows ${ids}, without 101`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(101), 3000))) fail(id, 'Restore all never reached the store')
    },
  }),

  // The lay-over of a put-back story lasts until a poll returns it
  // (PR4c): once one has, a later poll that drops it again (as past its
  // category's limit) takes it out of News and For You, though two
  // minutes haven't passed since the Undo. The clock is held from the
  // load, as in flow-hide-undo-after-poll.
  dashFlow('flow-hide-undo-until-returned', {
    before: async ({ page }) => {
      await holdClockFromLoad(page)
      await keepOutOfTabs(page, [])
    },
    run: async ({ page, context, fail }) => {
      const id = 'UNDO-RETURNED'
      const hidden = () => prefMock(context).value('hidden_entries') || []
      if (!(await hideAndPoll(page, context, id, 101))) return
      await tapUndo(page)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(101), 3000))) return fail(id, 'the Undo never reached the store')
      let got = await pollDone(page)
      if (!got) return fail(id, 'the poll after the Undo never finished')
      if (!pollHas(got, [101])) return fail(id, 'the poll after the Undo reached the store left 101 out; the flow needs it returned')
      // The page's fetch drops 101 from here on (pollDone's bodies are
      // the mock's, so they still hold it).
      await setKeptOut(page, [101])
      if (!(await pollDone(page))) return fail(id, 'the poll that drops 101 never finished')
      let ids = await openedIds(page, /^news/i)
      if (ids.includes(101)) fail(id, `a poll returned 101 and the next one left it out, but News still shows it: ${ids}`)
      ids = await openedIds(page, /^for you/i)
      if (ids.includes(101)) fail(id, `a poll returned 101 and the next one left it out, but For You still shows it: ${ids}`)
    },
  }),

  // A put-back story whose feed is gone stays out (PR4c): 101's feed (3)
  // is deleted in the store (another device), after the Undo reached the
  // server or between the hide and the Undo. The story is neither laid
  // over a poll that no longer has the feed nor put back by an Undo once
  // the feed is gone, so no "Other" tab turns up for it. The clock is held
  // from the load, as in flow-hide-undo-after-poll.
  ...['after', 'before'].map((when) =>
    dashFlow(`flow-hide-undo-feed-gone-${when}`, {
      before: async ({ page }) => {
        await holdClockFromLoad(page)
      },
      run: async ({ page, context, fail }) => {
        const id = 'UNDO-FEED-GONE'
        const hidden = () => prefMock(context).value('hidden_entries') || []
        if (!(await hideAndPoll(page, context, id, 101))) return
        const deleteFeed = async () => {
          const status = await page.evaluate(() => fetch('/api/sources/3', { method: 'DELETE' }).then((r) => r.status))
          if (status === 204) return true
          fail(id, `deleting feed 3 answered ${status}`)
          return false
        }
        if (when === 'after') {
          await tapUndo(page)
          await page.clock.runFor(1000)
          if (!(await waitFor(() => !hidden().includes(101), 3000))) return fail(id, 'the Undo never reached the store')
          if (!(await deleteFeed())) return
          if (!(await pollDone(page))) return fail(id, 'the poll after the delete never finished')
        } else {
          if (!(await deleteFeed())) return
          if (!(await pollDone(page))) return fail(id, 'the poll after the delete never finished')
          await tapUndo(page)
        }
        const other = async () => (await tabTexts(page)).some((t) => /^other/i.test(t))
        await sleep(300)
        if (await other()) fail(id, `feed 3 is gone (${when} the Undo), and the strip reads ${JSON.stringify(await tabTexts(page))}`)
        if (!(await pollDone(page))) return fail(id, 'the next poll never finished')
        if (await other()) fail(id, `feed 3 is gone (${when} the Undo), and after the next poll the strip reads ${JSON.stringify(await tabTexts(page))}`)
      },
    }),
  ),

  // A For You request that fails inside the lay-over's two minutes isn't
  // an answer without the put-back story (PR4c): For You starts over
  // empty, as after any failed For You request
  // (flow-foryou-fails-keeps-tab), not with 101 alone. The next good
  // answer, still sent before the Undo's batch, gets 101 laid over it.
  // The clock is held from the load, as in flow-hide-undo-after-poll.
  dashFlow('flow-hide-undo-foryou-fails', {
    expectedFailedLoads: ['/api/foryou'],
    before: async ({ page }) => {
      await holdClockFromLoad(page)
      page._fyFailing = false
      await page.route(
        (u) => new URL(u).pathname === '/api/foryou',
        (route) => (page._fyFailing ? route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' }) : route.fallback()),
      )
    },
    run: async ({ page, context, fail }) => {
      const id = 'UNDO-FY-FAILS'
      const hidden = () => prefMock(context).value('hidden_entries') || []
      if (!(await hideAndPoll(page, context, id, 101))) return
      await tapUndo(page)
      page._fyFailing = true
      let got = await pollDone(page)
      if (!got) return fail(id, 'the poll with For You failing never finished')
      if (got.forYouOk) return fail(id, 'For You answered; the flow needs it to fail')
      if (pollHas(got, [101])) return fail(id, 'the poll returned 101 before the Undo was sent; the flow needs it left out')
      // Another tab and back: For You starts over, from the failed answer.
      await openedIds(page, /^deals/i)
      let ids = await openedIds(page, /^for you/i)
      if (ids.length) fail(id, `after For You failed, For You started over with ${ids}, expected no stories`)
      page._fyFailing = false
      got = await pollDone(page)
      if (!got || !got.forYouOk) return fail(id, 'the poll with For You back never finished')
      if (pollHas(got, [101])) return fail(id, 'the poll returned 101 before the Undo was sent; the flow needs it left out')
      await openedIds(page, /^deals/i)
      ids = await openedIds(page, /^for you/i)
      if (!ids.includes(101)) fail(id, `when For You answered again (before the Undo's batch), For You shows ${ids}, without 101`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(101), 3000))) fail(id, 'the Undo never reached the store')
    },
  }),

  // A by-ids request that fails (a network blip) is asked again (PR4c):
  // the next poll asks for the save, and Saved shows it then.
  dashFlow('flow-saved-retries-a-failed-fetch', {
    expectedFailedLoads: ['/api/entries/by-ids'],
    before: async ({ page, context }) => {
      prefMock(context).seed({ starred_entries: [103, 503] })
      await keepOutOfTabs(page, [503])
      await recordByIds(page)
      page._byIdsAnswer = 'offline'
    },
    run: async ({ page, fail }) => {
      const id = 'SAVED-RETRY'
      const asked = () => JSON.stringify(page._byIds)
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(() => page._byIds.length >= 1, 3000))) return fail(id, 'by-ids was never asked for 503')
      await sleep(300)
      if ((await phoneIds(page)) !== '103') fail(id, `with by-ids failing Saved shows ${await phoneIds(page)}, expected 103`)
      const failed = page._byIds.length
      page._byIdsAnswer = 'mock'
      if (!(await pollDone(page))) return fail(id, 'the poll never finished')
      if (!(await waitFor(async () => (await phoneIds(page)) === '503,103', 3000))) fail(id, `after a failed by-ids and a poll Saved shows ${await phoneIds(page)}; by-ids was asked for ${asked()}`)
      if (page._byIds.length !== failed + 1 || page._byIds[failed] !== '503') fail(id, `by-ids was asked for ${asked()}, expected 503 once more after the poll`)
    },
  }),

  // Pruning a seen: key past its cap (1000 marks) keeps the marks of the
  // stories App holds (PR4c's keep set, built in App): here a save only
  // the saves hold (402: hidden, so never fetched) and a story unsaved
  // but still in Saved's by-id cache (503, which its Undo would draw
  // again). seen:news holds their marks as its two oldest, then 998 marks
  // of stories nothing holds. Opening 101 adds a mark, and the oldest of
  // those 998 goes instead of either.
  dashFlow('flow-seen-prune-keeps-saved', {
    before: async ({ page, context }) => {
      const nobody = Array.from({ length: 998 }, (_, i) => 2_000_001 + i)
      prefMock(context).seed({ starred_entries: [402, 503], hidden_entries: [402], 'seen:news': [402, 503, ...nobody] })
      await keepOutOfTabs(page, [503])
    },
    run: async ({ page, context, fail }) => {
      const id = 'SEEN-PRUNE'
      const seen = () => prefMock(context).value('seen:news') || []
      const saves = () => prefMock(context).value('starred_entries') || []
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => (await phoneIds(page)) === '503', 3000))) return fail(id, `Saved shows ${await phoneIds(page)}, expected 503 (fetched by id)`)
      await tap(page, page.locator(`${MOBILE_MAIN} article[data-card-id="503"] button[aria-label="unsave"]`))
      if (!(await waitFor(() => !saves().includes(503), 3000))) return fail(id, 'the unsave never reached the store')
      if (!(await openedIds(page, /^news/i)).includes(101)) return fail(id, `News shows ${await phoneIds(page)}, without 101`)
      await openStory(page, 101)
      if (!(await waitFor(() => seen().includes(101), 3000))) return fail(id, 'opening 101 never reached the store')
      const s = seen()
      if (s.length !== 1000) fail(id, `seen:news holds ${s.length} marks, expected its cap of 1000`)
      if (!s.includes(402)) fail(id, 'pruning seen:news dropped the mark of 402, a save no tab or cache holds')
      if (!s.includes(503)) fail(id, 'pruning seen:news dropped the mark of 503, held only by the by-id cache')
      if (s.includes(2_000_001)) fail(id, 'pruning seen:news kept the oldest mark of a story nothing holds')
    },
  }),

  // Two hides on For You with no poll between them, then a poll that
  // leaves both out, and Undo on the toast (the second hide's): 102 goes
  // back where it was, after 301 (PR4c). For You's list still held the
  // hidden 101 when 102 was hidden (hidden stories are left out only
  // where they are drawn), so 102's index then (2) is a place off in the
  // list the poll left (301, 201, …); its neighbours, 301 before it and
  // 201 after, still name its place. For You is opened afresh (another
  // tab, then back), so it is drawn from that list, not from PR3b's
  // snapshot; again after a poll sent before the Undo's batch (the
  // lay-over). The clock is held from the load, as in
  // flow-hide-undo-after-poll.
  dashFlow('flow-hide-two-undo-the-second', {
    before: async ({ page }) => {
      await holdClockFromLoad(page)
    },
    run: async ({ page, context, fail }) => {
      const id = 'HIDE-TWO-UNDO'
      const hidden = () => prefMock(context).value('hidden_entries') || []
      const order = F.FOR_YOU.map((e) => e.id).filter((n) => n !== 101).join(',')
      const forYouAfresh = async () => {
        await openedIds(page, /^deals/i)
        return (await openedIds(page, /^for you/i)).join(',')
      }
      if (!(await waitFor(async () => /^for you/i.test(await selectedTab(page)), 3000))) return fail(id, `the phone opened on ${JSON.stringify(await selectedTab(page))}`)
      for (const n of [101, 102]) {
        const card = page.locator(`${MOBILE_MAIN} article[data-card-id="${n}"]`)
        await card.first().waitFor({ state: 'visible' })
        await hideViaSheetHeld(page, card)
        if (!(await waitFor(async () => (await card.count()) === 0, 3000))) return fail(id, `the hidden ${n} is still drawn`)
      }
      await page.clock.runFor(1000)
      if (!(await waitFor(() => [101, 102].every((n) => hidden().includes(n)), 3000))) return fail(id, `the hides never reached the store: ${JSON.stringify(hidden())}`)
      let got = await pollDone(page)
      if (!got) return fail(id, 'the poll never finished')
      if (pollHas(got, [101, 102])) return fail(id, 'the poll returned a hidden story; the flow needs both left out')
      await tapUndo(page)
      let ids = await forYouAfresh()
      if (ids !== order) fail(id, `after the Undo of 102, For You opened afresh reads ${ids}, expected ${order}`)
      got = await pollDone(page)
      if (!got) return fail(id, 'the second poll never finished')
      if (pollHas(got, [102])) return fail(id, 'the second poll already returned 102; the flow needs it left out')
      ids = await forYouAfresh()
      if (ids !== order) fail(id, `after the Undo of 102 and a poll that left it out, For You opened afresh reads ${ids}, expected ${order}`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(102), 3000))) fail(id, 'the Undo never reached the store')
      if (!hidden().includes(101)) fail(id, 'the Undo of 102 took the hide of 101 away too')
    },
  }),

  // Two hides on For You with a poll after each, then Settings › Hidden:
  // Restore all, or Restore on each row, the older hide first. Both go
  // back where they were (PR4c), and For You reads as before the hides.
  // 'all' and 'rows' hide 101, then 102: the poll after the first hide
  // dropped 101 before 102 was hidden, so 102's index then (1) is a place
  // off once 101 is back; its neighbours, 301 before it and 201 after,
  // still name its place. 'rows-adjacent' hides 102, then 301 just above
  // it, and restores 102 first: 301's place is taken with the kept 102
  // back in it, so 301 goes back above 102, not under it. For You is
  // opened afresh once Settings closes (another tab, then back), so it is
  // drawn from the list the restore put them back in. The clock is held
  // from the load, as in flow-hide-undo-after-poll.
  ...[
    { how: 'all', hides: [101, 102] },
    { how: 'rows', hides: [101, 102] },
    { how: 'rows-adjacent', hides: [102, 301] },
  ].map(({ how, hides }) =>
    dashFlow(`flow-two-hides-restore-${how}`, {
      before: async ({ page }) => {
        await holdClockFromLoad(page)
      },
      run: async ({ page, context, fail }) => {
        const id = 'TWO-HIDES-RESTORE'
        const hidden = () => prefMock(context).value('hidden_entries') || []
        const order = F.FOR_YOU.map((e) => e.id).join(',')
        for (const n of hides) if (!(await hideAndPoll(page, context, id, n))) return
        await page.evaluate((u) => {
          history.pushState({ popping: 'settings' }, '', u)
          dispatchEvent(new PopStateEvent('popstate'))
        }, '/?view=settings&tab=hidden')
        if (how === 'all') {
          const restoreAll = page.locator(`${SETTINGS} button[aria-label="Restore all"]`)
          await restoreAll.waitFor({ state: 'visible' })
          await tap(page, restoreAll)
        } else {
          for (const n of hides) {
            const title = F.ENTRIES.find((e) => e.id === n).title
            const restore = page.locator(SETTINGS).getByRole('button', { name: `Restore: ${title}`, exact: true })
            await restore.waitFor({ state: 'visible' })
            await tap(page, restore)
            if (!(await waitFor(async () => (await restore.count()) === 0, 3000))) return fail(id, `${n}'s row is still in Settings › Hidden after its Restore`)
          }
        }
        await closeSettings(page)
        await openedIds(page, /^deals/i)
        const ids = (await openedIds(page, /^for you/i)).join(',')
        const what = `${how === 'all' ? 'Restore all' : 'Restore on each row'} of ${hides.join(' and ')}`
        if (ids !== order) fail(id, `after ${what}, For You opened afresh reads ${ids}, expected ${order} (as before the hides)`)
        await page.clock.runFor(1000)
        if (!(await waitFor(() => hides.every((n) => !hidden().includes(n)), 3000))) fail(id, `${what} never reached the store`)
      },
    }),
  ),

  // An Undo while For You's last request had failed (PR4c): For You is []
  // until it answers, as after any failed For You request
  // (flow-foryou-fails-keeps-tab), so the put-back story isn't put in
  // there on its own; the next good answer, still sent before the Undo's
  // batch, gets it laid over it, at its place. The story is 102, in the
  // middle of For You. (flow-hide-undo-foryou-fails has the failure after
  // the Undo.) The clock is held from the load, as in
  // flow-hide-undo-after-poll.
  dashFlow('flow-hide-foryou-fails-then-undo', {
    expectedFailedLoads: ['/api/foryou'],
    before: async ({ page }) => {
      await holdClockFromLoad(page)
      page._fyFailing = false
      await page.route(
        (u) => new URL(u).pathname === '/api/foryou',
        (route) => (page._fyFailing ? route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' }) : route.fallback()),
      )
    },
    run: async ({ page, context, fail }) => {
      const id = 'FY-FAILS-UNDO'
      const hidden = () => prefMock(context).value('hidden_entries') || []
      const order = F.FOR_YOU.map((e) => e.id).join(',')
      if (!(await hideAndPoll(page, context, id, 102))) return
      page._fyFailing = true
      let got = await pollDone(page)
      if (!got) return fail(id, 'the poll with For You failing never finished')
      if (got.forYouOk) return fail(id, 'For You answered; the flow needs it to fail')
      await tapUndo(page)
      // Another tab and back: For You starts over, from the failed answer.
      await openedIds(page, /^deals/i)
      let ids = await openedIds(page, /^for you/i)
      if (ids.length) fail(id, `after an Undo while For You's last request had failed, For You opened afresh shows ${ids}, expected no stories until it answers`)
      page._fyFailing = false
      got = await pollDone(page)
      if (!got || !got.forYouOk) return fail(id, 'the poll with For You back never finished')
      if (pollHas(got, [102])) return fail(id, 'the poll returned 102 before the Undo was sent; the flow needs it left out')
      await openedIds(page, /^deals/i)
      ids = (await openedIds(page, /^for you/i)).join(',')
      if (ids !== order) fail(id, `when For You answered again (before the Undo's batch), For You reads ${ids}, expected ${order}`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => !hidden().includes(102), 3000))) fail(id, 'the Undo never reached the store')
    },
  }),

  // Saves no tab holds that land by id after Saved is drawn go through
  // Saved's 30-card batches (PR3a, PR4c): the phone reopens on Saved,
  // which draws the two saves the tabs hold (105, 103, saved last); the
  // 248 older ones come from by-ids (200, then 48) once the first refresh
  // is done, and their answers are held until Saved is drawn. Saved then
  // draws 30 cards, and the next 30 only once its list is near the end.
  dashFlow('flow-saved-batches-late-saves', {
    before: async ({ page, context }) => {
      const old = Array.from({ length: 248 }, (_, i) => 900_001 + i)
      prefMock(context).seed({ starred_entries: [...old, 103, 105] })
      await page.addInitScript(() => localStorage.setItem('popping.v1.mobileTab', 'meta:saved'))
      // The by-ids projection (capture.cjs's byIdRow): the card fields,
      // never meta, and the feed's name.
      const { meta: _meta, ...like } = F.ENTRIES.find((e) => e.id === 503)
      const row = (n) => ({ ...like, id: n, title: `Saved story ${n}`, url: `https://example.com/saved/${n}`, source_name: F.sourceName(like.source_id) })
      page._byIds = []
      const held = new Promise((r) => {
        page._releaseByIds = r
      })
      await page.route(
        (u) => new URL(u).pathname === '/api/entries/by-ids',
        async (route) => {
          const asked = new URL(route.request().url()).searchParams.get('ids')
          page._byIds.push(asked)
          await held
          const rows = asked.split(',').map(Number).filter((n) => n > 900_000).map(row)
          return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) })
        },
      )
    },
    run: async ({ page, fail }) => {
      const id = 'SAVED-BATCHES'
      const cards = () => page.locator(`${MOBILE_MAIN} article[data-card-id]`).count()
      if (!(await waitFor(async () => /^saved/i.test(await selectedTab(page)) && (await phoneIds(page)) === '105,103', 3000))) {
        page._releaseByIds()
        return fail(id, `the phone opened on ${JSON.stringify(await selectedTab(page))}, drawing ${await phoneIds(page)}; the flow needs Saved with 105,103`)
      }
      const asked = await waitFor(() => page._byIds.length === 2, 3000)
      page._releaseByIds()
      if (!asked) return fail(id, `by-ids was asked ${page._byIds.length} times, expected 2 (200 ids, then 48)`)
      if (!(await waitFor(async () => (await cards()) > 2, 3000))) return fail(id, 'the saves fetched by id never showed')
      await sleep(500)
      const n = await cards()
      if (n !== 30) fail(id, `once 248 saves landed by id, Saved draws ${n} cards, expected its first 30`)
      // The next batch still comes near the end of the list.
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, el.scrollHeight))
      if (!(await waitFor(async () => (await cards()) === 60, 3000))) fail(id, `at the end of its first 30 cards Saved draws ${await cards()}, expected 60`)
    },
  }),

  // With no AI provider, every Generate and Regenerate (and the desktop
  // header's Brief) reads "Brief needs an AI provider · Set up"; Set up
  // opens Settings › AI, and closing Settings after setting one up
  // brings the buttons back without a reload.
  dashFlow('flow-brief-needs-ai', {
    before: async ({ page }) => {
      page._llm = { configured: false, brief: false, statusGets: 0 }
      const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      await page.route(
        (u) => new URL(u).pathname === '/api/llm/status',
        (route) => {
          page._llm.statusGets++
          return route.fulfill(json({ configured: page._llm.configured, backend: null, model: null }))
        },
      )
      await page.route(
        (u) => new URL(u).pathname === '/api/brief/latest',
        (route) => route.fulfill(json(page._llm.brief ? [BRIEF] : [])),
      )
    },
    run: async ({ page, fail, shot }) => {
      const setUp = page.locator(`${MOBILE_MAIN} [data-brief-action="set-up-ai"]`)
      const action = (name) => page.locator(`${MOBILE_MAIN} [data-brief-action="${name}"]`)
      const label = 'Brief needs an AI provider · Set up'
      const check = async (state, gone) => {
        if (!(await waitFor(() => setUp.isVisible(), 3000))) {
          fail('BRIEF-AI', `${state}: no Set up button`)
          return false
        }
        const text = ((await setUp.textContent()) || '').trim()
        if (text !== label) fail('BRIEF-AI', `${state}: the button reads ${JSON.stringify(text)}`)
        const h = (await setUp.boundingBox())?.height ?? 0
        if (h < 43.5) fail('BRIEF-AI', `${state}: the button is ${h}px tall`)
        if (await action(gone).count()) fail('BRIEF-AI', `${state}: ${gone} is still there`)
        return true
      }
      // The chevron: the middle of the header may be a button of its own.
      const collapse = () => tap(page, page.locator(`${MOBILE_MAIN} [aria-expanded="true"][role="button"] > span[aria-hidden="true"]`).first())

      // 1. No brief: the phone's Brief starts expanded, then collapsed.
      await tap(page, phoneTab(page, /^brief/i))
      if (!(await check('no brief, expanded', 'generate'))) return
      await shot('no-brief')
      await collapse()
      if (!(await check('no brief, collapsed', 'generate-compact'))) return

      // 2. One brief: both Regenerate states.
      page._llm.brief = true
      await tap(page, phoneTab(page, /^for you/i))
      await tap(page, phoneTab(page, /^brief/i))
      if (!(await waitFor(() => page.locator(`${MOBILE_MAIN} header[role="button"]`).isVisible(), 3000))) return fail('BRIEF-AI', 'the brief did not load')
      if (!(await check('a brief, expanded', 'regenerate'))) return
      await collapse()
      if (!(await check('a brief, collapsed', 'regenerate-compact'))) return

      // 3. Set up opens Settings › AI; a provider is set up there; Done.
      await tap(page, setUp)
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      const tab = await page.evaluate(() => new URL(location.href).searchParams.get('tab'))
      if (tab !== 'llm') fail('BRIEF-AI', `Set up opened Settings on ${JSON.stringify(tab)}`)
      page._llm.configured = true
      await closeSettings(page)
      if (!(await waitFor(() => action('regenerate-compact').isVisible(), 3000))) return fail('BRIEF-AI', 'Regenerate did not come back after closing Settings')
      if (await setUp.count()) fail('BRIEF-AI', 'Set up stayed after closing Settings')

      // 4. Desktop: the header's Brief, after the app comes back to the
      // front with the provider gone again.
      page._llm.configured = false
      const gets = page._llm.statusGets
      await setHidden(page, true)
      await setHidden(page, false)
      if (!(await waitFor(() => page._llm.statusGets > gets, 3000))) return fail('BRIEF-AI', 'coming back did not read the AI status again')
      await page.setViewportSize({ width: 1280, height: 800 })
      const header = page.locator('header[data-app-header] [data-brief-action="set-up-ai"]')
      if (!(await waitFor(() => header.isVisible(), 5000))) return fail('BRIEF-AI', 'the desktop header has no Set up')
      if (((await header.textContent()) || '').trim() !== label) fail('BRIEF-AI', `the header reads ${JSON.stringify(await header.textContent())}`)
      if (await page.locator('header[data-app-header]').getByRole('button', { name: 'Brief', exact: true }).count()) fail('BRIEF-AI', 'the header still offers Brief')
      await header.click()
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      if ((await page.evaluate(() => new URL(location.href).searchParams.get('tab'))) !== 'llm') fail('BRIEF-AI', 'the header Set up did not open Settings › AI')
      page._llm.configured = true
      await closeSettings(page)
      if (!(await waitFor(() => page.locator('header[data-app-header]').getByRole('button', { name: 'Brief', exact: true }).isVisible(), 3000))) fail('BRIEF-AI', 'the header Brief did not come back')
    },
  }),

  // A save records `bookmark`; an unsave (the bookmark again) doesn't.
  // Keyboard `b` is a desktop path: flow-bookmark-on-save-desktop.
  dashFlow('flow-bookmark-on-save', {
    before: async ({ page }) => {
      await recordInteractions(page)
    },
    run: async ({ page, fail }) => {
      await tap(page, phoneTab(page, /^news/i))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      const bookmarks = () => bookmarksFor(page, id)
      await tap(page, card.locator('button[aria-label="save"]'))
      if (!(await waitFor(() => bookmarks() === 1, 3000))) return fail('BOOKMARK', `a save sent ${bookmarks()} bookmark events, expected 1`)
      await tap(page, page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="unsave"]`))
      await sleep(800)
      if (bookmarks() !== 1) fail('BOOKMARK', `an unsave sent a bookmark (${bookmarks()} in all)`)
    },
  }),

  // On the desktop the card's bookmark records `bookmark` once and
  // unsaving doesn't (PR5a took Save out of the right-click menu);
  // keyboard `b` twice (save, then unsave) records one.
  dashFlow('flow-bookmark-on-save-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await recordInteractions(page)
    },
    run: async ({ page, fail }) => {
      const card = page.locator('main.grid article[data-card-id]').first()
      const id = Number(await card.getAttribute('data-card-id'))
      await card.locator('button[aria-label="save"]').click()
      if (!(await waitFor(() => bookmarksFor(page, id) === 1, 3000))) return fail('BOOKMARK', `the bookmark sent ${bookmarksFor(page, id)} bookmark events, expected 1`)
      await page.locator(`main.grid article[data-card-id="${id}"] button[aria-label="unsave"]`).first().click()
      await sleep(800)
      if (bookmarksFor(page, id) !== 1) fail('BOOKMARK', `unsaving sent a bookmark (${bookmarksFor(page, id)} in all)`)

      // Keyboard: select a card, then b (save) and b (unsave). The arrow
      // keys walk the grid's columns, and the first is Saved, empty again
      // (PR7a keeps it): step to the next column first.
      await page.keyboard.press('ArrowRight')
      await page.keyboard.press('ArrowDown')
      const sel = 'article[data-card-id][tabindex="0"]'
      if (!(await waitFor(async () => (await page.locator(sel).count()) > 0, 3000))) return fail('BOOKMARK', 'ArrowDown selected no card')
      const kid = Number(await page.locator(sel).first().getAttribute('data-card-id'))
      const before = bookmarksFor(page, kid)
      await page.keyboard.press('b')
      if (!(await waitFor(() => bookmarksFor(page, kid) === before + 1, 3000))) return fail('BOOKMARK', `b (save) sent ${bookmarksFor(page, kid) - before} bookmark events, expected 1`)
      await waitFor(async () => (await page.locator(sel).count()) > 0, 2000)
      await page.keyboard.press('b')
      await sleep(800)
      if (bookmarksFor(page, kid) !== before + 1) fail('BOOKMARK', `b (unsave) sent a bookmark (${bookmarksFor(page, kid) - before} in all)`)
    },
  }),

  // Reset takes the reading state (seen marks and older builds' read
  // marks, last visits, tab prefs, section folds, History grouping) and
  // keeps saves, hides, votes and saved views. Of a seen mark (a story
  // opened) and a save still queued when Reset is tapped, the save goes
  // before the DELETEs and is kept; the seen mark is dropped, and nothing
  // for a key Reset deletes goes after the DELETEs (the reload's leave
  // flush would otherwise write the marks back).
  dashFlow('flow-reset-keeps-saves', {
    before: async ({ page }) => {
      await installClock(page)
    },
    run: async ({ page, fail }) => {
      await putPrefs(page, {
        starred_entries: [103],
        hidden_entries: [104],
        voted_entries: { 105: 'up' },
        filter_presets: [{ id: 'p-1', name: 'Morning', activeSources: [], columnPrefs: {} }],
        history_group_by: 'none',
        'read_entries:news': [101],
        'seen:news': [109],
        'last_viewed:news': new Date(F.NOW).toISOString(),
        'column_prefs:news': { sort: 'best', minScore: 0, maxAgeHours: null },
        'column_sections:news': { newCollapsed: false, historyCollapsed: true },
      })
      const writes = await prefWrites(page)
      // A seen mark (the story opened) and a save wait in the 1 s batch
      // (the clock is held).
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      await tap(page, phoneTab(page, /^news/i))
      const id = await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().getAttribute('data-card-id')
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`)
      await tap(page, card.locator('button[aria-label="save"]'))
      await tap(page, card.locator('a[aria-label="open in new tab"]').first())
      await sleep(300)
      if (writes.length) return fail('RESET', `the queued writes went before Reset: ${JSON.stringify(writes)}`)
      await page.evaluate((u) => {
        history.pushState({ popping: 'settings' }, '', u)
        dispatchEvent(new PopStateEvent('popstate'))
      }, '/?view=settings&tab=reset')
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      await page.locator(SETTINGS).getByRole('button', { name: 'Reset reading state', exact: true }).click()
      const loaded = page.waitForEvent('load')
      await page.locator(SETTINGS).getByRole('button', { name: 'Reset', exact: true }).click()
      await loaded
      await page.clock.resume()
      await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible' })
      const firstDelete = writes.findIndex((w) => w.method === 'DELETE')
      const resetKey = (k) => k === 'history_group_by' || /^(seen|read_entries|last_viewed|column_prefs|column_sections):/.test(k)
      const after = writes.slice(firstDelete).filter((w) => w.method !== 'DELETE' && resetKey(w.key))
      if (after.length) fail('RESET', `writes after the DELETEs: ${JSON.stringify(after)}`)
      const saveAt = writes.findIndex((w) => w.method === 'PATCH' && w.key === 'starred_entries' && (w.body.add || []).includes(Number(id)))
      if (saveAt < 0) fail('RESET', `the queued save was never sent: ${JSON.stringify(writes)}`)
      else if (saveAt > firstDelete) fail('RESET', 'the queued save went after the DELETEs, not before them')
      const saves = prefMock(page.context()).value('starred_entries') || []
      if (!saves.includes(103) || !saves.includes(Number(id))) fail('RESET', `the store's saves are ${JSON.stringify(saves)}, expected 103 and ${id}`)
      if (writes.some((w) => resetKey(w.key) && w.method !== 'DELETE')) fail('RESET', `a write for a key Reset deletes went: ${JSON.stringify(writes.filter((w) => resetKey(w.key) && w.method !== 'DELETE'))}`)
      const deleted = writes.filter((w) => w.method === 'DELETE').map((w) => w.key)
      for (const k of ['starred_entries', 'hidden_entries', 'filter_presets', 'voted_entries']) {
        if (deleted.includes(k)) fail('RESET', `Reset deleted ${k}`)
      }
      for (const k of ['seen:news', 'read_entries:news', 'last_viewed:news', 'column_prefs:news', 'column_sections:news', 'history_group_by']) {
        if (!deleted.includes(k)) fail('RESET', `Reset kept ${k} (deleted: ${JSON.stringify(deleted)})`)
      }
      const left = await page.evaluate(async () => ((await (await fetch('/api/preferences')).json()).items || []).map((r) => r.key).sort())
      const kept = ['filter_presets', 'hidden_entries', 'starred_entries', 'voted_entries']
      if (JSON.stringify(left) !== JSON.stringify(kept)) fail('RESET', `after Reset the server holds ${JSON.stringify(left)}`)
      if (!(await waitFor(async () => (await tabTexts(page)).some((t) => /^saved/i.test(t)), 3000))) fail('RESET', 'no Saved tab after Reset')
    },
  }),

  // --- PR2b: preferences sync by change ------------------------------------

  // A GET that can't know about a hide still on its way doesn't bring the
  // story back: one answered while the hide's PATCH is held, and one sent
  // while it was held but answered with the old set after the PATCH's
  // answer (sync rule 5).
  dashFlow('flow-prefs-resync', {
    before: async ({ page, context }) => {
      // A row for the hides already exists, without the story.
      prefMock(context).seed({ hidden_entries: [999] })
      page._held = []
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences/hidden_entries',
        async (route) => {
          if (route.request().method() !== 'PATCH') return route.fallback()
          await new Promise((r) => page._held.push(r))
          return route.fallback()
        },
      )
    },
    run: async ({ page, context, fail, shot }) => {
      const gets = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'GET' && f.path === '/api/preferences').length)
      await tap(page, phoneTab(page, /^news/i))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      const shown = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`).count().then((n) => n > 0)
      await hideViaSheet(page, card)
      if (!(await waitFor(async () => !(await shown()), 2000))) return fail('PREFS-RESYNC', `story ${id} still shows after Hide`)
      if (!(await waitFor(() => page._held.length === 1, 3000))) return fail('PREFS-RESYNC', 'the hide sent no PATCH')

      // 1. Back to the front (a full GET) while the PATCH is held: the
      // store still has only 999.
      let before = await gets()
      await startPoll(page)
      if (!(await waitFor(async () => (await gets()) > before, 3000))) return fail('PREFS-RESYNC', 'coming back sent no GET /api/preferences')
      await sleep(500)
      if (await shown()) return fail('PREFS-RESYNC', `a GET answered while the hide was on its way brought story ${id} back`)

      // 2. A GET sent now, answered with the old set only after the
      // PATCH's answer has been taken in.
      let release
      const stale = new Promise((r) => (release = r))
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences',
        async (route) => {
          if (route.request().method() !== 'GET') return route.fallback()
          await stale
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ items: [{ key: 'hidden_entries', value: [999], updated_at: new Date(F.NOW).toISOString() }] }),
          })
        },
        { times: 1 },
      )
      before = await gets()
      await startPoll(page)
      if (!(await waitFor(async () => (await gets()) > before, 3000))) return fail('PREFS-RESYNC', 'the second return sent no GET')
      page._held[0]()
      const stored = () => (prefMock(context).value('hidden_entries') || []).includes(id)
      if (!(await waitFor(stored, 3000))) return fail('PREFS-RESYNC', 'the PATCH never reached the store')
      await sleep(500)
      release()
      await sleep(800)
      if (await shown()) return fail('PREFS-RESYNC', `a GET sent before the hide's answer, answered with the old set, brought story ${id} back`)
      await shot('hidden')

      // 3. A GET sent after the answer is taken in as it is.
      before = await gets()
      await startPoll(page)
      if (!(await waitFor(async () => (await gets()) > before, 3000))) return fail('PREFS-RESYNC', 'the third return sent no GET')
      await sleep(500)
      if (await shown()) fail('PREFS-RESYNC', `story ${id} came back after the hide was saved`)
    },
  }),

  // Leaving the app right after a save loses nothing: hiding the page
  // sends the save at once, with keepalive, inside its 1 s batch.
  dashFlow('flow-visibility-flush', {
    before: async ({ page }) => {
      await installClock(page)
    },
    run: async ({ page, context, fail }) => {
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      await tap(page, card.locator('button[aria-label="save"]'))
      await sleep(400)
      const patches = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries'))
      if ((await patches()).length) return fail('VIS-FLUSH', 'the save went out before its batch')
      await setHidden(page, true)
      if (!(await waitFor(async () => (await patches()).length === 1, 3000))) return fail('VIS-FLUSH', 'hiding the page sent no PATCH of the save')
      const [sent] = await patches()
      if (!sent.keepalive) fail('VIS-FLUSH', 'the PATCH sent when the page hid has no keepalive')
      if (!(await waitFor(() => (prefMock(context).value('starred_entries') || []).includes(id), 3000))) fail('VIS-FLUSH', `story ${id} is not in the store`)
      // Answered while hidden: coming back doesn't send it again.
      if (!(await waitFor(() => answered(page, 'PATCH', '/api/preferences/starred_entries'), 3000))) return fail('VIS-FLUSH', 'the PATCH was never answered')
      await setHidden(page, false)
      await page.clock.runFor(5000)
      await sleep(300)
      if ((await patches()).length !== 1) fail('VIS-FLUSH', `${(await patches()).length} PATCHes of one save`)
    },
  }),

  // A save that the server can't take (503) is tried again after 2 s,
  // with a save made meanwhile folded in: both end up stored, with no
  // error toast.
  dashFlow('flow-prefs-retry', {
    expectedFailedLoads: ['/api/preferences/starred_entries'],
    before: async ({ page, context }) => {
      await installClock(page)
      prefMock(context).fail(503, { key: 'starred_entries' })
    },
    run: async ({ page, context, fail }) => {
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const patches = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries').length)
      const cards = page.locator(`${MOBILE_MAIN} article[data-card-id]`)
      const a = Number(await cards.nth(0).getAttribute('data-card-id'))
      const b = Number(await cards.nth(1).getAttribute('data-card-id'))
      await tap(page, cards.nth(0).locator('button[aria-label="save"]'))
      await page.clock.runFor(1000)
      if (!(await waitFor(() => prefMock(context).faultsLeft() === 0, 3000))) return fail('PREFS-RETRY', 'the first save was not sent')
      // Its retry is timed from the 503's arrival, so the clock waits for it.
      if (!(await waitFor(() => answered(page, 'PATCH', '/api/preferences/starred_entries'), 3000))) return fail('PREFS-RETRY', 'the 503 was never taken in')
      await tap(page, page.locator(`${MOBILE_MAIN} article[data-card-id="${b}"] button[aria-label="save"]`))
      await page.clock.runFor(1000)
      await sleep(300)
      if ((await patches()) !== 1) return fail('PREFS-RETRY', `${await patches()} PATCHes before the retry, expected 1 (the second save waits for it)`)
      await page.clock.runFor(1500)
      if (!(await waitFor(async () => (await patches()) === 2, 3000))) return fail('PREFS-RETRY', `no retry 2 s after the 503 (${await patches()} PATCHes)`)
      const stored = () => prefMock(context).value('starred_entries') || []
      if (!(await waitFor(() => stored().includes(a) && stored().includes(b), 3000))) fail('PREFS-RETRY', `the store holds ${JSON.stringify(stored())}, expected ${a} and ${b}`)
      for (const id of [a, b]) {
        if (!(await page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="unsave"]`).count())) fail('PREFS-RETRY', `story ${id} doesn't show as saved`)
      }
      if (await page.getByText(/^Couldn't save/).count()) fail('PREFS-RETRY', 'an error toast for a save that got through')
    },
  }),

  // A server rolled back to before PATCH answers 405: the save stays on
  // screen and queued (no "saved" state flips back), is tried again after
  // 2, 4 and 8 s, then shows one "Couldn't save" toast; once the server
  // takes PATCH again, leaving the page saves it.
  dashFlow('flow-prefs-405', {
    expectedFailedLoads: ['/api/preferences/starred_entries'],
    before: async ({ page, context }) => {
      await installClock(page)
      prefMock(context).fail(405, { key: 'starred_entries', times: 4 })
    },
    run: async ({ page, context, fail, shot }) => {
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const patches = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries').length)
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      const saved = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="unsave"]`).count().then((n) => n > 0)
      await tap(page, card.locator('button[aria-label="save"]'))
      await page.clock.runFor(1000)
      if (!(await waitFor(async () => (await patches()) === 1, 3000))) return fail('PREFS-405', 'the save was not sent')
      for (const [i, wait] of [[2, 2000], [3, 4000], [4, 8000]]) {
        // Each retry is timed from the 405's arrival.
        if (!(await waitFor(() => answered(page, 'PATCH', '/api/preferences/starred_entries'), 3000))) return fail('PREFS-405', `answer ${i - 1} was never taken in`)
        if (!(await saved())) return fail('PREFS-405', `after ${i - 1} answers of 405 the story no longer shows as saved`)
        await page.clock.runFor(wait)
        if (!(await waitFor(async () => (await patches()) === i, 3000))) return fail('PREFS-405', `no retry ${wait / 1000} s after a 405 (${await patches()} PATCHes)`)
      }
      if (!(await waitFor(() => page.getByText(/^Couldn't save your latest changes/).count().then((n) => n === 1), 3000))) fail('PREFS-405', 'no toast once the retries were used up')
      if (!(await saved())) fail('PREFS-405', 'the save no longer shows after the retries')
      if (prefMock(context).value('starred_entries') !== undefined) fail('PREFS-405', 'a 405 stored the save')
      await shot('unsaved')
      await page.clock.runFor(30_000)
      await sleep(300)
      if ((await patches()) !== 4) fail('PREFS-405', `${await patches()} PATCHes; no timed retry after the third`)
      // The server takes PATCH again: leaving sends the queued save.
      await setHidden(page, true)
      if (!(await waitFor(() => (prefMock(context).value('starred_entries') || []).includes(id), 3000))) fail('PREFS-405', 'leaving did not save the queued change')
      await setHidden(page, false)
    },
  }),

  // The preferences GET fails, so the page doesn't know the saves the
  // server holds. A save then adds to them (a PATCH) instead of writing a
  // one-story list over them (PR1b's notes), and the next return reads
  // them all in.
  dashFlow('flow-prefs-fail-save-keeps-list', {
    expectedFailedLoads: ['/api/preferences'],
    before: async ({ page, context }) => {
      prefMock(context).seed({ starred_entries: [103] })
      page._prefsDown = true
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences',
        (route) =>
          route.request().method() === 'GET' && page._prefsDown
            ? route.fulfill({ status: 500, contentType: 'text/plain', body: 'error' })
            : route.fallback(),
      )
    },
    run: async ({ page, context, fail, shot }) => {
      const writes = await prefWrites(page)
      // Unread: no story shows as saved.
      if (await page.locator('[aria-label="unsave"]').count()) return fail('PREFS-FAIL-SAVE', 'a story shows as saved although the preferences failed')
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]:not([data-card-id="103"])`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      await tap(page, card.locator('button[aria-label="save"]'))
      const stored = () => prefMock(context).value('starred_entries') || []
      if (!(await waitFor(() => stored().includes(id), 3000))) return fail('PREFS-FAIL-SAVE', `the save of ${id} never reached the store`)
      if (!stored().includes(103)) fail('PREFS-FAIL-SAVE', `the save wrote over the server's saves: the store holds ${JSON.stringify(stored())}`)
      const sent = writes.filter((w) => w.key === 'starred_entries').map((w) => `${w.method} ${JSON.stringify(w.body ?? w.value)}`)
      if (sent.join() !== `PATCH {"add":[${id}]}`) fail('PREFS-FAIL-SAVE', `the save sent ${JSON.stringify(sent)}, expected one PATCH adding ${id}`)
      // The server is back: the next return reads every save in.
      page._prefsDown = false
      await startPoll(page)
      await tap(page, phoneTab(page, /^saved/i))
      for (const want of [103, id]) {
        if (!(await waitFor(() => page.locator(`${MOBILE_MAIN} article[data-card-id="${want}"]`).count().then((n) => n > 0), 3000))) fail('PREFS-FAIL-SAVE', `story ${want} is not in Saved`)
      }
      await shot('saved')
    },
  }),

  // Back in front with the hide's keepalive PATCH still unanswered (not
  // yet at the server): it is given up on (sync rule 4) and not sent again,
  // also once it does get through.
  dashFlow('flow-prefs-return-before-answer', {
    before: async ({ page }) => {
      await installClock(page)
      page._held = []
      page._holdPatch = false
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences/starred_entries',
        async (route) => {
          if (route.request().method() !== 'PATCH' || !page._holdPatch) return route.fallback()
          page._holdPatch = false
          await new Promise((r) => page._held.push(r))
          return route.fallback()
        },
      )
    },
    run: async ({ page, context, fail }) => {
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const patches = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries'))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      page._holdPatch = true
      await tap(page, card.locator('button[aria-label="save"]'))
      await sleep(400)
      if ((await patches()).length) return fail('PREFS-RETURN', 'the save went out before its batch')
      await setHidden(page, true)
      if (!(await waitFor(() => page._held.length === 1, 3000))) return fail('PREFS-RETURN', 'hiding the page sent no PATCH')
      await setHidden(page, false)
      await page.clock.runFor(5000)
      await sleep(500)
      const atReturn = (await patches()).length
      page._held[0]()
      if (!(await waitFor(() => (prefMock(context).value('starred_entries') || []).includes(id), 3000))) fail('PREFS-RETURN', `story ${id} is not in the store`)
      await page.clock.runFor(35_000)
      await sleep(500)
      const n = (await patches()).length
      if (n !== 1) fail('PREFS-RETURN', `${n} PATCHes of one save (${atReturn} by the return)`)
    },
  }),

  // The hide's keepalive PATCH lands but its answer is held (iOS suspends
  // the page); meanwhile another device unsaves the story; then the page
  // comes back. The other device's unsave stands: the save is not sent
  // again, and the story shows unsaved after the return's full GET.
  dashFlow('flow-prefs-landed-unread', {
    before: async ({ page, context }) => {
      await installClock(page)
      page._held = []
      page._holdAnswer = false
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences/starred_entries',
        async (route) => {
          if (route.request().method() !== 'PATCH' || !page._holdAnswer) return route.fallback()
          page._holdAnswer = false
          const body = route.request().postDataJSON() || {}
          const cur = prefMock(context).value('starred_entries') || []
          const next = cur.filter((x) => !(body.remove || []).includes(x))
          for (const x of body.add || []) if (!next.includes(x)) next.push(x)
          prefMock(context).seed({ starred_entries: next })
          await new Promise((r) => page._held.push(r))
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ key: 'starred_entries', value: next, updated_at: new Date(F.NOW + 50).toISOString() }),
          })
        },
      )
    },
    run: async ({ page, context, fail }) => {
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const patches = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries'))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      const saved = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="unsave"]`).count().then((c) => c > 0)
      page._holdAnswer = true
      await tap(page, card.locator('button[aria-label="save"]'))
      await sleep(400)
      await setHidden(page, true)
      if (!(await waitFor(() => page._held.length === 1, 3000))) return fail('PREFS-LANDED', 'hiding the page sent no PATCH')
      if (!(prefMock(context).value('starred_entries') || []).includes(id)) return fail('PREFS-LANDED', 'the keepalive PATCH did not land')
      // Another device unsaves it while this page is away.
      prefMock(context).seed({ starred_entries: [] })
      await setHidden(page, false)
      await page.clock.runFor(5000)
      await sleep(800)
      page._held[0]()
      await page.clock.runFor(35_000)
      await sleep(800)
      const n = (await patches()).length
      const stored = prefMock(context).value('starred_entries') || []
      if (stored.includes(id)) fail('PREFS-LANDED', `the other device's unsave was undone: the store holds ${JSON.stringify(stored)} after ${n} PATCHes`)
      if (n !== 1) fail('PREFS-LANDED', `${n} PATCHes of one save`)
      if (await saved()) fail('PREFS-LANDED', `story ${id} still shows as saved after the return`)
    },
  }),

  // The save's PATCH has gone (its 1 s batch ran) but not been answered
  // when the story opens, with nothing else queued. Hiding the page sends
  // the save again with keepalive, which gets through; the first request
  // only fails after the return (iOS cut it off), when sync rule 4 has
  // given it up. The save is stored, and nothing more goes.
  dashFlow('flow-prefs-hide-while-sending', {
    expectedFailedLoads: ['/api/preferences/starred_entries'],
    before: async ({ page }) => {
      await installClock(page)
      page._held = []
      page._holdPatch = false
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences/starred_entries',
        async (route) => {
          if (route.request().method() !== 'PATCH' || !page._holdPatch) return route.fallback()
          page._holdPatch = false
          await new Promise((r) => page._held.push(r))
          return route.abort('failed')
        },
      )
    },
    run: async ({ page, context, fail }) => {
      const now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      const patches = () => harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'PATCH' && f.path === '/api/preferences/starred_entries'))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      const saved = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="unsave"]`).count().then((c) => c > 0)
      const stored = () => (prefMock(context).value('starred_entries') || []).includes(id)
      page._holdPatch = true
      await tap(page, card.locator('button[aria-label="save"]'))
      await page.clock.runFor(1000)
      if (!(await waitFor(() => page._held.length === 1, 3000))) return fail('PREFS-HIDE-SENDING', 'the save sent no PATCH after its batch')
      const [first] = await patches()
      if (first.keepalive) return fail('PREFS-HIDE-SENDING', 'the batch PATCH was sent with keepalive')
      await setHidden(page, true)
      if (!(await waitFor(async () => (await patches()).length === 2, 3000))) return fail('PREFS-HIDE-SENDING', 'hiding the page did not send the save on its way again')
      if (!(await patches())[1].keepalive) fail('PREFS-HIDE-SENDING', 'the second PATCH has no keepalive')
      if (!(await waitFor(stored, 3000))) return fail('PREFS-HIDE-SENDING', `story ${id} is not in the store after the keepalive PATCH`)
      if (!(await waitFor(() => answered(page, 'PATCH', '/api/preferences/starred_entries'), 3000))) return fail('PREFS-HIDE-SENDING', 'the keepalive PATCH was never answered')
      await setHidden(page, false)
      await page.clock.runFor(2000)
      await sleep(300)
      // The first request fails only now.
      page._held[0]()
      await sleep(300)
      await page.clock.runFor(35_000)
      await sleep(500)
      const n = (await patches()).length
      if (n !== 2) fail('PREFS-HIDE-SENDING', `${n} PATCHes of one save, expected 2`)
      if (!stored()) fail('PREFS-HIDE-SENDING', `story ${id} is no longer in the store`)
      if (!(await saved())) fail('PREFS-HIDE-SENDING', `story ${id} doesn't show as saved after the return`)
      if (await page.getByText(/^Couldn't save/).count()) fail('PREFS-HIDE-SENDING', 'an error toast for a save that got through')
    },
  }),

  dashFlow('flow-fn-quiet-poll', {
    before: async ({ page }) => {
      await installClock(page)
    },
    run: async ({ page, fail }) => {
      let polls = 0
      page.on('request', (r) => {
        if (r.method() === 'GET' && new URL(r.url()).pathname === '/api/sources') polls++
      })
      await page.clock.runFor(65_000)
      await sleep(800)
      if (!polls) fail('FN', 'no poll ran in 65 s')
      if (await page.locator('[role="status"]').getByText(/^(\d+ new|Nothing new)$/).count()) fail('FN', 'the background poll showed a toast')
    },
  }),

  editorFlow('flow-fo-legacy-header', 17, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/17')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 17)
      if (!(await page.locator(ROW(17)).getByText('Header names use letters, numbers and dashes.').isVisible())) {
        fail('FO', 'the stored bad header shows no error on open')
      }
      await rowButton(page, 17, 'Add header').click()
      await page.locator(`${ROW(17)} input[aria-label="header name"]`).last().fill('X-Test')
      const value = page.locator(`${ROW(17)} input[placeholder="Value"]`).last()
      await value.fill('1')
      await value.blur()
      await sleep(300)
      if (page._patches.length) return fail('FO', 'a header was saved while another row is refused')
      await page.locator(`${ROW(17)} button[aria-label="remove header Bad Header"]`).click()
      if (!(await waitFor(() => page._patches.length === 1))) return fail('FO', 'removing the bad row saved nothing')
      const sent = JSON.stringify(page._patches[0].custom_headers)
      if (sent !== '{"X-Test":"1"}') fail('FO', `PATCH headers are ${sent}`)
    },
  }),

  dashFlow('flow-fv-card-source-tap', {
    run: async ({ page, fail }) => {
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const button = card.locator('button[aria-label^="edit feed"]')
      const headline = card.locator('a[aria-label="open in new tab"]').first()
      // PR5a: the feed button sits on the header line above the headline
      // and must not reach the headline's top edge.
      const above = await headline.evaluate((a) => {
        const h = a.getBoundingClientRect()
        const b = a.closest('article').querySelector('button[aria-label^="edit feed"]')
        return !!b && b.getBoundingClientRect().bottom <= h.top + 0.5
      })
      if (!above) fail('FV', 'the feed button is not above the headline')
      const clear = await headline.evaluate((a) => {
        const r = a.getBoundingClientRect()
        const at = document.elementFromPoint(r.left + r.width / 2, r.top + 1)
        return !(at && at.closest('button[aria-label^="edit feed"]'))
      })
      if (!clear) fail('FV', "the feed button reaches into the headline's top edge")
      await button.click()
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      const m = /[?&]source=(\d+)/.exec(page.url())
      if (!m) return fail('FV', `no source in the URL: ${page.url()}`)
      if (!(await waitFor(() => page.locator(`${ROW(m[1])} button[aria-expanded="true"]`).count().then((n) => n > 0), 3000))) {
        fail('FV', `feed ${m[1]}'s editor did not open`)
      }
    },
  }),

  dashFlow('flow-fg2-toast-edit-reentry', {
    run: async ({ page, fail }) => {
      await page.evaluate(() => history.pushState(null, '', '/?start=1'))
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: /^Settings/ }).click()
      await openAddTab(page)
      // capture.cjs answers Auto-add with BBC News, which is already added.
      await page.fill('input[aria-label="link to add"]', 'https://www.bbc.co.uk/news')
      await page.getByRole('button', { name: 'Auto-add', exact: true }).click()
      await page.getByText('Already added: BBC News').waitFor({ state: 'visible', timeout: 5000 })
      await page.getByRole('button', { name: 'Edit', exact: true }).click()
      await waitEditor(page, 1)
      await closeSettings(page)
      await page.goBack()
      await sleep(400)
      if (await page.locator(SETTINGS).count()) fail('FG2', 'Back after Done reopened Settings')
      const u = new URL(page.url())
      if (!/^https?:$/.test(u.protocol)) fail('FG2', `Back left the app: ${page.url()}`)
      for (const k of ['view', 'tab', 'source']) if (u.searchParams.has(k)) fail('FG2', `the URL still has ${k}: ${page.url()}`)
    },
  }),

  editorFlow('flow-fw-delete-during-stale-poll', 16, {
    run: async ({ page, fail }) => {
      await waitEditor(page, 16)
      const [sources, entries] = await page.evaluate(() =>
        Promise.all([fetch('/api/sources').then((r) => r.json()), fetch('/api/entries?perCategoryLimit=100').then((r) => r.json())]),
      )
      if (!entries.some((e) => e.source_id === 16)) return fail('FW', 'the fixtures have no NHL stories to check')
      // Polls after the delete are slow, so the stale one lands first and
      // the app itself has to keep the deleted feed out.
      for (const pathname of ['/api/sources', '/api/entries']) {
        await page.route(
          (u) => new URL(u).pathname === pathname,
          async (route) => {
            if (route.request().method() !== 'GET') return route.fallback()
            await sleep(6000)
            return route.fallback()
          },
        )
      }
      await stalePoll(page, '/api/sources', sources, 1500)
      await stalePoll(page, '/api/entries', entries, 1500)
      await startPoll(page)
      await sleep(100)
      await rowButton(page, 16, 'Delete feed…').click()
      await rowButton(page, 16, 'Delete').click()
      await page.locator(ROW(16)).waitFor({ state: 'detached', timeout: 3000 })
      await sleep(2000)
      if (await page.locator(ROW(16)).count()) fail('FW', 'the stale poll brought the deleted feed back')
      await closeSettings(page)
      const tabs = await tabTexts(page)
      if (tabs.some((t) => /^other/i.test(t.trim()))) fail('FW', `an "Other" column appeared: ${JSON.stringify(tabs)}`)
      if (tabs.some((t) => /nhl/i.test(t))) fail('FW', `the deleted feed's column is still there: ${JSON.stringify(tabs)}`)
    },
  }),
  // Switching apps mid-edit (the field still focused) saves what was typed.
  editorFlow('flow-save-app-switch', 6, {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await waitEditor(page, 6)
      const typed = 'https://slickdeals.net/switch.xml'
      const addr = field(page, 6, 'Address')
      await addr.fill(typed)
      await page.evaluate(() => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
        document.dispatchEvent(new Event('visibilitychange'))
      })
      if (!(await waitFor(() => page._patches.length === 1, 2000))) return fail('SAVE-SWITCH', `switching apps sent ${page._patches.length} PATCH(es), expected 1`)
      if (page._patches[0].url !== typed) fail('SAVE-SWITCH', `PATCH body is ${JSON.stringify(page._patches[0])}`)
    },
  }),

  // Back (Safari tab, desktop) closes Settings without a blur; the typed
  // address is saved anyway.
  dashFlow('flow-save-back', {
    before: async ({ page }) => {
      page._patches = await record(page, 'PATCH', '/api/sources/6')
    },
    run: async ({ page, fail }) => {
      await openEditor(page, 6)
      const typed = 'https://slickdeals.net/back.xml'
      await field(page, 6, 'Address').fill(typed)
      await page.goBack()
      await page.locator(SETTINGS).waitFor({ state: 'detached', timeout: 3000 })
      if (!(await waitFor(() => page._patches.length === 1, 2000))) return fail('SAVE-BACK', `Back sent ${page._patches.length} PATCH(es), expected 1`)
      if (page._patches[0].url !== typed) fail('SAVE-BACK', `PATCH body is ${JSON.stringify(page._patches[0])}`)
    },
  }),
  // --- PR8: the AI tab saves on its own ------------------------------------

  // Switching provider is one PUT that also clears both models.
  aiFlow('flow-fx-ai-provider', { provider: null }, {
    run: async ({ page, fail }) => {
      await page.locator('#ai-provider').waitFor({ state: 'visible' })
      await page.locator('#ai-provider').selectOption('ollama')
      if (!(await waitFor(() => page._puts.length >= 1))) return fail('FX', 'no PUT after picking Ollama (local)')
      await sleep(400)
      if (page._puts.length !== 1) fail('FX', `expected 1 PUT, saw ${page._puts.length}`)
      const body = JSON.stringify(page._puts[0])
      if (body !== '{"provider":"ollama","model_brief":"","model_scoring":""}') fail('FX', `PUT body is ${body}`)
    },
  }),

  // A provider whose key isn't saved here asks for it, and the key is
  // one more PUT with only that key.
  aiFlow('flow-fx2-ai-provider-needs-key', { provider: null, anthropic: false, configured: false }, {
    run: async ({ page, fail }) => {
      await page.locator('#ai-provider').waitFor({ state: 'visible' })
      await page.locator('#ai-provider').selectOption('anthropic')
      if (!(await waitFor(() => page._puts.length >= 1))) return fail('FX2', 'no PUT after picking Anthropic')
      const first = JSON.stringify(page._puts[0])
      if (first !== '{"provider":"anthropic","model_brief":"","model_scoring":""}') fail('FX2', `provider PUT body is ${first}`)
      const settings = page.locator(SETTINGS)
      if (!(await settings.getByText("If your Anthropic key isn't in the server's settings file, paste it here.").isVisible())) {
        fail('FX2', 'no key field hint for Anthropic')
      }
      if (!(await waitFor(() => settings.getByText('Popping has no Anthropic key yet. Paste one above.').isVisible(), 3000))) {
        fail('FX2', 'no "Popping has no Anthropic key yet" line')
      }
      const key = page.locator('#ai-key-anthropic')
      await key.fill('sk-ant-test-not-real')
      await key.blur()
      if (!(await waitFor(() => page._puts.length >= 2))) return fail('FX2', 'no PUT after leaving the key field')
      await sleep(400)
      if (page._puts.length !== 2) fail('FX2', `expected 2 PUTs, saw ${page._puts.length}`)
      const keys = Object.keys(page._puts[1])
      if (JSON.stringify(keys) !== '["anthropic_api_key"]') fail('FX2', `key PUT has fields ${JSON.stringify(keys)}`)
    },
  }),

  // A key saves on leave: an empty field sends nothing; a typed one sends
  // exactly that key, then the field empties and says "Key saved".
  aiFlow('flow-fy-ai-key-on-leave', {}, {
    run: async ({ page, fail }) => {
      const key = page.locator('#ai-key-groq')
      await key.waitFor({ state: 'visible' })
      await key.focus()
      await key.blur()
      await sleep(300)
      if (page._puts.length) return fail('FY', `an empty key field sent ${page._puts.length} PUT(s)`)
      await key.fill('gsk-test-not-real')
      await key.blur()
      if (!(await waitFor(() => page._puts.length >= 1))) return fail('FY', 'no PUT after leaving a typed key')
      await sleep(300)
      if (page._puts.length !== 1) fail('FY', `expected 1 PUT, saw ${page._puts.length}`)
      const keys = Object.keys(page._puts[0])
      if (JSON.stringify(keys) !== '["groq_api_key"]') fail('FY', `PUT has fields ${JSON.stringify(keys)}`)
      if ((await key.inputValue()) !== '') fail('FY', 'the key stays in the field after saving')
      if (!(await waitFor(() => page.locator(SETTINGS).getByText('Key saved').isVisible(), 3000))) fail('FY', 'no "Key saved"')
    },
  }),

  // Reset asks inline, never with a browser dialog.
  flow('flow-fz-reset-inline', {
    url: '/?view=settings&tab=reset',
    run: async ({ page, fail }) => {
      let dialogs = 0
      page.on('dialog', () => dialogs++)
      await page.locator(SETTINGS).getByRole('button', { name: 'Reset reading state', exact: true }).click()
      const keep = page.locator(SETTINGS).getByRole('button', { name: 'Keep', exact: true })
      if (!(await keep.isVisible())) return fail('FZ', 'no inline confirm after Reset reading state')
      if (!(await page.locator(SETTINGS).getByRole('button', { name: 'Reset', exact: true }).isVisible())) fail('FZ', 'the confirm has no Reset')
      await keep.click()
      if (await keep.count()) fail('FZ', 'Keep did not close the confirm')
      if (dialogs) fail('FZ', `a browser dialog opened ${dialogs} time(s)`)
    },
  }),
  // --- PR9: no browser prompts ---------------------------------------------

  // Save current view names the view in the Menu itself; renaming a
  // chip is the same inline field.
  dashFlow('flow-fpr-save-view-inline', {
    run: async ({ page, fail }) => {
      let dialogs = 0
      page.on('dialog', () => dialogs++)
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: /^Save current view/ }).click()
      const name = page.locator(`${MENU} input[aria-label="name for this view"]`)
      await name.waitFor({ state: 'visible' })
      const focused = await page.evaluate(() => document.activeElement?.getAttribute('aria-label'))
      if (focused !== 'name for this view') fail('FPR', `focus is on ${JSON.stringify(focused)} after the tap, expected the name field`)
      if (!(await page.locator(MENU).getByRole('button', { name: 'Save', exact: true }).isDisabled())) fail('FPR', 'Save is enabled with an empty name')
      await name.fill('Hockey')
      await name.press('Enter')
      const chip = page.locator('[role="toolbar"][aria-label="saved filter presets"]').getByRole('button', { name: 'Hockey', exact: true })
      if (!(await waitFor(() => chip.isVisible(), 3000))) return fail('FPR', 'no "Hockey" chip after saving')
      if (!(await page.getByText('Saved view “Hockey”').isVisible())) fail('FPR', 'no "Saved view “Hockey”" toast')

      // Rename: the field starts with the name; leaving it unchanged does nothing.
      await page.getByRole('button', { name: 'options for preset Hockey', exact: true }).click()
      await page.getByRole('button', { name: 'rename preset Hockey', exact: true }).click()
      const rename = page.locator('input[aria-label="new name for Hockey"]')
      await rename.waitFor({ state: 'visible' })
      if ((await rename.inputValue()) !== 'Hockey') fail('FPR', `the rename field holds ${JSON.stringify(await rename.inputValue())}`)
      await rename.press('Enter')
      await sleep(200)
      if (await page.getByText(/^Renamed to/).count()) fail('FPR', 'an unchanged name was saved as a rename')
      if (!(await chip.isVisible())) fail('FPR', 'the chip is gone after an unchanged rename')
      if (dialogs) fail('FPR', `a browser dialog opened ${dialogs} time(s)`)
    },
  }),

  // --- H1: the harness checks itself -----------------------------------------

  // A known 2-tap path (≡, then Settings) counts 2, passes taps(…, 2) and
  // fails taps(…, 1).
  dashFlow('flow-h1-taps', {
    run: async ({ page, fail }) => {
      await tap(page, page.locator('button[aria-label="open menu"]'))
      await menuOpened(page)
      await tap(page, page.locator(MENU).getByRole('button', { name: /^Settings/ }))
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      if (page._taps !== 2) return fail('H1-TAPS', `Menu → Settings counted ${page._taps} taps, expected 2`)
      if (!taps(page, 2, 'H1-TAPS')) return
      const real = page._fail
      const caught = []
      page._fail = (rule, detail) => caught.push([rule, detail])
      const passed = taps(page, 1, 'H1-TAPS')
      page._fail = real
      if (passed || caught.length !== 1 || caught[0][0] !== 'H1-TAPS') {
        fail('H1-TAPS', `taps(page, 1) after 2 taps returned ${passed} and reported ${JSON.stringify(caught)}`)
      }
    },
  }),

  // touchDrag does nothing off Chromium; on Chromium an upward drag
  // scrolls the phone list and counts as one tap.
  dashFlow('flow-h1-touch-drag', {
    run: async ({ page, fail }) => {
      const webkit = { context: () => ({ browser: () => ({ browserType: () => ({ name: () => 'webkit' }) }) }) }
      // A stand-in page that only answers the browser check: anything
      // past the early return throws.
      const ran = await touchDrag(webkit, { from: { x: 100, y: 600 }, to: { x: 100, y: 200 } }).catch((e) => `a throw (${e.message})`)
      if (ran !== false || webkit._taps) fail('H1-DRAG', `off Chromium touchDrag returned ${ran} and counted ${webkit._taps || 0} taps`)
      if (!isChromium(page)) return
      const scroller = page.locator(`${MOBILE_MAIN} section div.overflow-y-auto`).first()
      const box = await scroller.boundingBox()
      if (!box) return fail('H1-DRAG', 'no phone list to drag')
      const top = () => scroller.evaluate((el) => el.scrollTop)
      const start = await top()
      const x = box.x + box.width / 2
      if (!(await touchDrag(page, { from: { x, y: box.y + box.height * 0.8 }, to: { x, y: box.y + box.height * 0.2 } }))) {
        return fail('H1-DRAG', 'touchDrag did not run on Chromium')
      }
      if (!(await waitFor(async () => (await top()) > start, 2000))) fail('H1-DRAG', 'an upward drag did not scroll the phone list')
      if (page._taps !== 1) fail('H1-DRAG', `a drag counted ${page._taps} taps, expected 1`)
    },
  }),

  // setHidden sets both visibilityState and hidden; coming back runs the
  // app's quiet poll.
  dashFlow('flow-h1-set-hidden', {
    run: async ({ page, fail }) => {
      let polls = 0
      page.on('request', (r) => {
        if (r.method() === 'GET' && new URL(r.url()).pathname === '/api/sources') polls++
      })
      const read = () => page.evaluate(() => JSON.stringify([document.visibilityState, document.hidden]))
      await setHidden(page, true)
      if ((await read()) !== '["hidden",true]') fail('H1-HIDDEN', `hidden page reads ${await read()}`)
      await sleep(300)
      const hiddenPolls = polls
      await setHidden(page, false)
      if ((await read()) !== '["visible",false]') fail('H1-HIDDEN', `shown page reads ${await read()}`)
      if (!(await waitFor(() => polls > hiddenPolls, 3000))) fail('H1-HIDDEN', 'coming back to the page sent no quiet poll')
    },
  }),

  // The fetch wrapper records keepalive: the load-time GET without it, and
  // the views still queued when the page hides with it (beacon refused).
  // Since PR3c a card sends its view when it is seen on scroll, not when
  // it is drawn: one read on screen and scrolled off the top queues one.
  dashFlow('flow-h1-keepalive', {
    before: async ({ page }) => {
      // Views stay queued (no idle callback) and the beacon is refused, so
      // hiding the page has to send them with fetch and keepalive.
      await page.addInitScript(() => {
        window.requestIdleCallback = () => 0
        Object.defineProperty(navigator, 'sendBeacon', { configurable: true, value: () => false })
      })
    },
    run: async ({ page, fail }) => {
      const { fetches } = await harnessLog(page)
      const load = fetches.find((f) => f.method === 'GET' && f.path === '/api/preferences')
      if (!load) fail('H1-KEEPALIVE', `the load-time GET /api/preferences was not recorded: ${JSON.stringify(fetches)}`)
      else if (load.keepalive) fail('H1-KEEPALIVE', 'the load-time GET /api/preferences was recorded with keepalive')
      if (fetches.some((f) => f.keepalive)) fail('H1-KEEPALIVE', `a keepalive fetch before the page hid: ${JSON.stringify(fetches)}`)
      // Real time: twice the dwell on screen, then the top card scrolled
      // off the top.
      await sleep(2 * SEEN_DWELL_MS + 200)
      const { half } = await listCards(page)
      if (half.length < 2) return fail('H1-KEEPALIVE', `the shown tab has ${JSON.stringify(half)} on screen, expected two cards or more`)
      await scrollCardToTop(page, half[1])
      await setHidden(page, true)
      const sent = () => harnessLog(page).then((h) => h.fetches.some((f) => f.method === 'POST' && f.path === '/api/interactions/batch' && f.keepalive))
      if (!(await waitFor(sent, 3000))) {
        fail('H1-KEEPALIVE', `hiding the page sent no keepalive POST /api/interactions/batch: ${JSON.stringify((await harnessLog(page)).fetches)}`)
      }
    },
  }),

  // capture.cjs keeps preferences: a PUT is read back (all, since, by
  // key), a DELETE removes it, and a reload still sees what was written.
  // PATCH changes a set key as the server does (PR2b): `remove` before
  // `add`, `set` with null clearing a vote, 400 for a key that isn't a
  // set key, 422 for the wrong change; a queued fault answers once. A
  // PUT of a set key is a 409 and changes nothing, as on the server
  // (PR2c), and is still reported for C17.
  dashFlow('flow-h1-pref-store', {
    // The read after DELETE is a 404 on purpose, and so are the refused
    // PATCHes.
    expectedFailedLoads: ['/api/preferences/column_prefs:news', '/api/preferences/filter_presets', '/api/preferences/starred_entries', '/api/preferences/voted_entries'],
    run: async ({ page, context, fail }) => {
      const call = (method, path, body) =>
        page.evaluate(
          async ([m, p, b]) => {
            const r = await fetch(p, { method: m, headers: { 'Content-Type': 'application/json' }, body: b === undefined ? undefined : JSON.stringify(b) })
            return { status: r.status, body: r.status === 204 ? null : await r.json() }
          },
          [method, path, body],
        )
      const keys = (res) => res.body.items.map((i) => i.key).sort().join(',')
      const a = await call('PUT', '/api/preferences/column_prefs:news', { value: { sort: 'best', minScore: 0, maxAgeHours: null } })
      const b = await call('PUT', '/api/preferences/filter_presets', { value: [] })
      if (a.body.updated_at >= b.body.updated_at) fail('H1-PREFS', `writes are out of order: ${a.body.updated_at}, ${b.body.updated_at}`)
      const all = await call('GET', '/api/preferences')
      if (keys(all) !== 'column_prefs:news,filter_presets') fail('H1-PREFS', `GET lists ${keys(all)}`)
      const since = await call('GET', `/api/preferences?since=${encodeURIComponent(a.body.updated_at)}`)
      if (keys(since) !== 'filter_presets') fail('H1-PREFS', `GET since the first write lists ${keys(since)}`)
      const one = await call('GET', '/api/preferences/filter_presets')
      if (JSON.stringify(one.body.value) !== '[]') fail('H1-PREFS', `GET by key returns ${JSON.stringify(one.body)}`)
      await call('DELETE', '/api/preferences/column_prefs:news')
      if ((await call('GET', '/api/preferences/column_prefs:news')).status !== 404) fail('H1-PREFS', 'a deleted key still reads back')

      // PATCH, as on the server.
      const patch = (key, body) => call('PATCH', `/api/preferences/${encodeURIComponent(key)}`, body)
      await patch('starred_entries', { add: [3, 1, 2] })
      const changed = await patch('starred_entries', { add: [1, 4], remove: [1, 2] })
      if (JSON.stringify(changed.body.value) !== '[3,1,4]') fail('H1-PREFS', `remove before add gave ${JSON.stringify(changed.body.value)}, expected [3,1,4]`)
      await patch('voted_entries', { set: { 5: 'up', 6: 'down' } })
      const votes = await patch('voted_entries', { set: { 5: null, 6: 'up' } })
      if (JSON.stringify(votes.body.value) !== '{"6":"up"}') fail('H1-PREFS', `votes ${JSON.stringify(votes.body.value)}, expected {"6":"up"}`)
      const seen = await patch('seen:News/Tech', { add: [9] })
      if (seen.status !== 200 || JSON.stringify(prefMock(context).value('seen:News/Tech')) !== '[9]') fail('H1-PREFS', `seen:News/Tech PATCH: ${JSON.stringify(seen)}`)
      if ((await patch('filter_presets', { add: [1] })).status !== 400) fail('H1-PREFS', 'PATCH of a key that is not a set key was not a 400')
      if ((await patch('starred_entries', { set: { 1: 'up' } })).status !== 422) fail('H1-PREFS', 'set on an id list was not a 422')
      if ((await patch('voted_entries', { add: [1] })).status !== 422) fail('H1-PREFS', 'add on voted_entries was not a 422')
      for (const [key, body] of [
        ['starred_entries', { add: [0] }],
        ['starred_entries', { add: ['5'] }],
        ['starred_entries', { add: [1], extra: true }],
        ['voted_entries', { set: { 5: 'sideways' } }],
        ['voted_entries', { set: { '05': 'up' } }],
      ]) {
        if ((await patch(key, body)).status !== 422) fail('H1-PREFS', `PATCH ${key} ${JSON.stringify(body)} was not a 422`)
      }
      prefMock(context).fail(503, { key: 'starred_entries' })
      if ((await patch('starred_entries', { add: [7] })).status !== 503) fail('H1-PREFS', 'the queued fault did not answer')
      if ((await patch('starred_entries', { add: [7] })).status !== 200) fail('H1-PREFS', 'the fault answered twice')
      if (JSON.stringify(prefMock(context).value('starred_entries')) !== '[3,1,4,7]') fail('H1-PREFS', `starred_entries is ${JSON.stringify(prefMock(context).value('starred_entries'))}`)

      await page.reload()
      await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible' })
      const loaded = await harnessLog(page).then((h) => h.fetches.filter((f) => f.method === 'GET' && f.path === '/api/preferences').length)
      if (loaded !== 1) fail('H1-PREFS', `the reload sent ${loaded} GET /api/preferences`)
      const kept = 'filter_presets,seen:News/Tech,starred_entries,voted_entries'
      if (keys(await call('GET', '/api/preferences')) !== kept) fail('H1-PREFS', 'the store did not survive a reload')

      // A whole-list PUT of a set key, as an old bundle sends it: 409 with
      // the server's detail, the stored row untouched, no row created. It
      // runs in a context of its own with the same mock, because in this
      // one smoke reports the PUT as C17 (as it should when the app sends
      // it); here onSetKeyPut records it instead.
      const probe = await context.browser().newContext()
      try {
        const reported = []
        await mockApi(probe, { onSetKeyPut: ({ key }) => reported.push(key) })
        prefMock(probe).seed({ hidden_entries: [101, 102] })
        const stored = JSON.stringify(prefMock(probe).value('hidden_entries'))
        const probePage = await probe.newPage()
        await probePage.goto(new URL('/api/preferences', page.url()).href)
        const put = (key) =>
          probePage.evaluate(async (k) => {
            const r = await fetch(`/api/preferences/${encodeURIComponent(k)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: [999] }) })
            return { status: r.status, body: await r.json() }
          }, key)
        for (const key of ['hidden_entries', 'seen:News/Tech']) {
          const res = await put(key)
          if (res.status !== 409 || res.body.detail !== 'Popping was updated. Reload to keep saving.') {
            fail('H1-PREFS', `PUT of the set key ${key} answered ${res.status} ${JSON.stringify(res.body)}, expected 409`)
          }
        }
        if (JSON.stringify(prefMock(probe).value('hidden_entries')) !== stored) fail('H1-PREFS', `the refused PUT changed hidden_entries to ${JSON.stringify(prefMock(probe).value('hidden_entries'))}`)
        if (prefMock(probe).keys().join() !== 'hidden_entries') fail('H1-PREFS', `the refused PUTs left the store holding ${prefMock(probe).keys().join()}`)
        if (reported.join() !== 'hidden_entries,seen:News/Tech') fail('H1-PREFS', `onSetKeyPut reported ${JSON.stringify(reported)}`)
      } finally {
        await probe.close().catch(() => {})
      }
    },
  }),

  // The 1280 width is a desktop (mouse, no touch, desktop grid), and the
  // stubs catch Copy link and Open in browser from a card's context menu.
  dashFlow('flow-h1-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    run: async ({ page, context, fail }) => {
      const env = await page.evaluate(() => ({
        hover: matchMedia('(hover: hover)').matches,
        coarse: matchMedia('(pointer: coarse)').matches,
        touchPoints: navigator.maxTouchPoints,
        phoneShown: !!document.querySelector('main.md\\:hidden')?.getClientRects().length,
      }))
      if (!env.hover || env.coarse || env.touchPoints || env.phoneShown) fail('H1-DESKTOP', `1280 is not a desktop: ${JSON.stringify(env)}`)
      const menuButton = (name) => page.locator('#card-context-menu').getByRole('button', { name, exact: true })
      // The menu opens at the pointer and runs downwards, so right-click a
      // card that starts in the top half of the window.
      const at = await page.evaluate(() => {
        for (const a of document.querySelectorAll('article[data-card-id]')) {
          const r = a.getBoundingClientRect()
          if (r.width && r.top > 80 && r.top < innerHeight / 2) return { x: r.left + 24, y: r.top + 24 }
        }
        return null
      })
      if (!at) return fail('H1-DESKTOP', 'no desktop card starts in the top half of the window')
      await page.mouse.click(at.x, at.y, { button: 'right' })
      await menuButton('Copy link').click()
      await page.mouse.click(at.x, at.y, { button: 'right' })
      await menuButton('Open in browser').click()
      const log = await harnessLog(page)
      if (log.copied.length !== 1 || !/^https?:/.test(log.copied[0])) fail('H1-DESKTOP', `the clipboard stub holds ${JSON.stringify(log.copied)}`)
      if (log.opened.length !== 1 || log.opened[0].target !== '_blank') fail('H1-DESKTOP', `the window.open stub saw ${JSON.stringify(log.opened)}`)
      if (context.pages().length !== 1) fail('H1-DESKTOP', `${context.pages().length} pages are open; window.open was not stubbed`)
    },
  }),

  // --- PR1r: reload on a new build -------------------------------------------

  // The phone moves onto a new build when it comes back after 10 min or
  // more away: not sooner, not when it hid right after opening a story,
  // not when the user presses a key first, not onto a script that isn't
  // there, not with the Menu open or text in a field, not while a save
  // that didn't get through is still unsaved, only once the pending
  // writes are saved, and once per build. A check cut off by hiding
  // doesn't hold up the next return's, and a save the server refuses for
  // good (400, 409), or can't take at all (405: rolled back to before
  // PATCH), doesn't hold up the reload.
  dashFlow('flow-reload-on-new-build', {
    // Step 4's HEAD of the missing script and steps 6-8's refused saves
    // fail on purpose.
    expectedFailedLoads: ['/static/index-gone.js', '/api/preferences/starred_entries'],
    before: async ({ page }) => {
      await installClock(page)
      page._reload = await newBuild(page)
      // Saves (PATCH starred_entries): while `page._refuse` lists
      // statuses, each one is answered with the next of them (counted in
      // page._refused); the rest are held 500 ms and marked done just
      // before they are answered, so a reload that didn't wait for one
      // comes first. (Playwright reports a request a navigation cut short
      // as failed even when the page had its response, so
      // requestfinished can't tell.)
      page._refuse = []
      page._refused = 0
      page._saves = 0
      await page.route(
        (u) => new URL(u).pathname === '/api/preferences/starred_entries',
        async (route) => {
          if (route.request().method() !== 'PATCH') return route.fallback()
          page._saves++
          if (page._refuse.length) {
            const status = page._refuse.shift()
            await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ detail: `refused (${status})` }) })
            page._refused++
            return
          }
          await sleep(500)
          page._reload.events.push('save-done')
          return route.fallback()
        },
      )
    },
    run: async ({ page, fail }) => {
      const log = page._reload
      const quiet = async (rule, what, checks) => {
        await sleep(1200)
        if (log.checks !== checks) fail(rule, `${what}: the update check fetched / (${log.checks} checks, expected ${checks})`)
        if (log.navs) fail(rule, `${what}: the page reloaded`)
      }
      const saveButton = () => page.locator(`${MOBILE_MAIN} button[aria-label="save"]`).first()
      const reloaded = async (navs) => {
        await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible', timeout: 20000 })
        await sleep(500)
        return log.navs === navs
      }

      // 1. Nine minutes away: no check at all.
      await away(page, 9)
      await quiet('RELOAD-AWAY', 'back after 9 min', 0)

      // 2. Hid right after opening a story (its thumbnail): left alone.
      await tap(page, page.locator(`${MOBILE_MAIN} [role="button"][aria-label^="open "][aria-label$=" in new tab"]`).first())
      if ((await harnessLog(page)).opened.length !== 1) return fail('RELOAD-OPEN', 'the thumbnail opened no story')
      await away(page, 11)
      await quiet('RELOAD-OPEN', 'back 11 min after opening a story', 0)

      // 3. A key pressed while the check is under way calls it off.
      let release
      page._hold = new Promise((r) => (release = r))
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 1, 3000))) return fail('RELOAD-INPUT', `back after 11 min ran ${log.checks} checks, expected 1`)
      await page.keyboard.press('Shift')
      release()
      page._hold = null
      await sleep(1200)
      if (log.heads.length || log.navs) fail('RELOAD-INPUT', `a key during the check still went on: ${JSON.stringify(log)}`)

      // 4. The new entry script isn't served: no reload.
      page._nextScript = '/static/index-gone.js'
      await away(page, 11)
      if (!(await waitFor(() => log.heads.length === 1, 3000))) return fail('RELOAD-HEAD', `no HEAD of the new entry script: ${JSON.stringify(log)}`)
      await sleep(1200)
      if (log.navs) fail('RELOAD-HEAD', 'reloaded onto a build whose entry script is a 404')
      page._nextScript = '/static/index-next.js'

      // 5. The Menu is open: no reload (it stops before the HEAD).
      await tap(page, page.locator('button[aria-label="open menu"]'))
      await menuOpened(page)
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 3, 3000))) return fail('RELOAD-BUSY', `back with the Menu open ran ${log.checks} checks, expected 3`)
      await sleep(1200)
      if (log.heads.length !== 1 || log.navs) fail('RELOAD-BUSY', `reloading went on with the Menu open: ${JSON.stringify(log)}`)
      await tap(page, page.locator(`${MENU} button[aria-label="close menu"]`))
      await page.locator(`${MENU}[inert]`).waitFor({ state: 'attached', timeout: 5000 })

      // 5b. Text typed in the header search (no sheet): no reload (it
      // stops before the HEAD).
      await tap(page, page.locator('button[aria-label="open search"]'))
      const search = page.locator('#app-search')
      await search.waitFor({ state: 'visible', timeout: 5000 })
      await search.fill('reload')
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 4, 3000))) return fail('RELOAD-FIELD', `back with text in the search field ran ${log.checks} checks, expected 4`)
      await sleep(1200)
      if (log.heads.length !== 1 || log.navs) fail('RELOAD-FIELD', `reloading went on with text in the search field: ${JSON.stringify(log)}`)

      // 5c. A check cut off by hiding (iOS suspends the page with its
      // fetch of / on the way) doesn't keep the next return from checking.
      // The field still holds text, so neither check goes on to a reload.
      let releaseHeld
      page._hold = new Promise((r) => (releaseHeld = r))
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 5, 3000))) return fail('RELOAD-RECHECK', `back after 11 min ran ${log.checks} checks, expected 5`)
      await away(page, 11)
      const rechecked = await waitFor(() => log.checks === 6, 3000)
      releaseHeld()
      page._hold = null
      if (!rechecked) return fail('RELOAD-RECHECK', 'back again while the cut-off check was still on the way: no check')
      await sleep(1200)
      if (log.heads.length !== 1 || log.navs) fail('RELOAD-RECHECK', `a check went on with text in the search field: ${JSON.stringify(log)}`)
      await tap(page, page.locator('button[aria-label="clear search"]'))
      await page.locator('button[aria-label="open search"]').waitFor({ state: 'visible', timeout: 5000 })

      // 6. A save the server refuses for good (400: a key a reverted
      // server doesn't take; it would refuse it every time) is neither
      // retried nor kept, and the next return reloads without sending it
      // again.
      let saves = page._saves
      page._refuse = [400, 400, 400]
      await tap(page, saveButton())
      if (!(await waitFor(() => page._saves === saves + 1, 3000))) return fail('RELOAD-REFUSED', 'the save was not sent')
      await sleep(2500)
      if (page._saves !== saves + 1) return fail('RELOAD-REFUSED', `a save refused with 400 was sent again (${page._saves - saves} PATCHes)`)
      await away(page, 11)
      if (!(await waitFor(() => log.navs === 1, 6000))) return fail('RELOAD-REFUSED', `a save refused with 400 kept the page from reloading: ${JSON.stringify(log)}`)
      if (page._saves !== saves + 1) fail('RELOAD-REFUSED', `the flush sent the refused save again (${page._saves - saves} PATCHes)`)
      if (!(await reloaded(1))) return fail('RELOAD-ONCE', `${log.navs} reloads, expected 1`)
      page._refuse = []

      // Away for `minutes` once the save sent when the page hid has had
      // its answer (`refused` refusals in all), so its fate is settled
      // before the page comes back.
      const awayAfterAnswer = async (minutes, refused) => {
        await setHidden(page, true)
        if (!(await waitFor(() => page._refused >= refused, 3000))) return false
        if (!(await waitFor(() => answered(page, 'PATCH', '/api/preferences/starred_entries'), 3000))) return false
        const at = await page.evaluate(() => Date.now())
        await page.clock.setSystemTime(at + minutes * 60_000)
        await setHidden(page, false)
        return true
      }

      // 7. Another build. A save waits in its 1 s batch (the clock is
      // paused); hiding the page sends it, the server refuses it with 409
      // ("reload to keep saving"), and coming back still reloads.
      page._nextScript = '/static/index-next2.js'
      let now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      saves = page._saves
      await tap(page, saveButton())
      await sleep(400)
      if (page._saves !== saves) return fail('RELOAD-REFUSED', 'the save went out before the page hid; nothing was pending')
      page._refuse = [409, 409, 409]
      if (!(await awayAfterAnswer(11, page._refused + 1))) return fail('RELOAD-REFUSED', 'hiding the page did not send the pending save')
      if (!(await waitFor(() => log.navs === 2, 6000))) return fail('RELOAD-REFUSED', `a pending save refused with 409 kept the page from reloading: ${JSON.stringify(log)}`)
      if (page._saves !== saves + 1) fail('RELOAD-REFUSED', `${page._saves - saves} PATCHes of the save, expected 1 (refused with 409)`)
      await page.clock.resume()
      if (!(await reloaded(2))) return fail('RELOAD-ONCE', `${log.navs} reloads, expected 2`)
      page._refuse = []

      // 8. Another build, and a save waits in its batch again. Hiding
      // sends it and the server can't take it (503); coming back sends it
      // again (the return and the reload's flush) and it still fails, so
      // the page stays.
      page._nextScript = '/static/index-next3.js'
      now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      saves = page._saves
      const events = log.events.length
      await tap(page, saveButton())
      await sleep(400)
      if (page._saves !== saves) return fail('RELOAD-FLUSH', 'the save went out before the page hid; nothing was pending')
      page._refuse = Array(8).fill(503)
      if (!(await awayAfterAnswer(11, page._refused + 1))) return fail('RELOAD-FLUSH', 'hiding the page did not send the pending save')
      if (!(await waitFor(() => page._saves >= saves + 2, 3000))) return fail('RELOAD-FLUSH', `coming back did not send the unsaved save again: ${page._saves - saves} PATCHes`)
      await sleep(1200)
      if (log.navs !== 2) return fail('RELOAD-SAVED', 'reloaded although the pending save did not get through')

      // 8b. Away again: hiding sends it and it fails again, and so does
      // the return. It still keeps the page. (Each hide waits for the
      // answer: a request still unanswered on the return is given up on,
      // not sent again, sync rule 4.)
      let before = page._saves
      if (!(await awayAfterAnswer(11, page._refused + 1))) return fail('RELOAD-FLUSH', 'hiding the page did not send the unsaved save')
      if (!(await waitFor(() => page._saves >= before + 2, 3000))) return fail('RELOAD-FLUSH', `coming back did not resend the unsaved save: ${page._saves - before} PATCHes`)
      await sleep(1200)
      if (log.navs !== 2) return fail('RELOAD-SAVED', 'reloaded although the save failed again')

      // 8c. The server takes it again once the save sent by the hide has
      // failed: back once more, the save is sent, and the page reloads only
      // once it is saved.
      page._refuse = [503]
      before = page._saves
      if (!(await awayAfterAnswer(11, page._refused + 1))) return fail('RELOAD-FLUSH', 'hiding the page did not send the unsaved save')
      if (!(await waitFor(() => log.navs === 3, 6000))) return fail('RELOAD-FLUSH', `back after 11 min with a new build: no reload: ${JSON.stringify(log)}`)
      const after = log.events.slice(events)
      if (after[0] !== 'save-done') fail('RELOAD-FLUSH', `the reload started before the pending save was done: ${after.join(', ')}`)
      if (page._saves < before + 2) fail('RELOAD-FLUSH', 'the save was not sent again before the reload')
      await page.clock.resume()
      if (!(await reloaded(3))) return fail('RELOAD-ONCE', `${log.navs} reloads, expected 3`)

      // 8d. Another build, served by a server rolled back to before PATCH:
      // every save answers 405. Hiding sends the pending save; the return
      // and the reload's flush send it again and get 405 too. Only the
      // reload onto the served build can cure that, so it goes ahead.
      page._nextScript = '/static/index-next4.js'
      now = await page.evaluate(() => Date.now())
      await page.clock.pauseAt(now + 1000)
      saves = page._saves
      await tap(page, saveButton())
      await sleep(400)
      if (page._saves !== saves) return fail('RELOAD-ROLLBACK', 'the save went out before the page hid; nothing was pending')
      page._refuse = Array(12).fill(405)
      if (!(await awayAfterAnswer(11, page._refused + 1))) return fail('RELOAD-ROLLBACK', 'hiding the page did not send the pending save')
      if (!(await waitFor(() => log.navs === 4, 6000))) return fail('RELOAD-ROLLBACK', `saves answered 405 kept the page from reloading onto the served build: ${JSON.stringify(log)}`)
      if (page._saves < saves + 2) fail('RELOAD-ROLLBACK', `the save was not sent again before the reload (${page._saves - saves} PATCHes)`)
      await page.clock.resume()
      if (!(await reloaded(4))) return fail('RELOAD-ONCE', `${log.navs} reloads, expected 4`)
      page._refuse = []

      // 9. The reloaded page still runs the old build (vite preview), and
      // the server still names the new one: it checks, but doesn't reload
      // again for the same build.
      const checks = log.checks
      await away(page, 11)
      if (!(await waitFor(() => log.checks === checks + 1, 3000))) return fail('RELOAD-ONCE', 'the reloaded page ran no check')
      await sleep(1200)
      if (log.navs !== 4) fail('RELOAD-ONCE', `reloaded again for the same build (${log.navs} reloads)`)
    },
  }),

  // The desktop never reloads by itself: back after 11 min with a new
  // build it shows "New version · Reload", with or without input during
  // the check, and the button goes once the server serves the page's own
  // build again. Clicking it reloads.
  dashFlow('flow-reload-desktop-button', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await installClock(page)
      page._reload = await newBuild(page)
    },
    run: async ({ page, fail, shot }) => {
      const log = page._reload
      const button = page.getByRole('button', { name: 'New version · Reload', exact: true })
      if (await button.count()) return fail('RELOAD-DESKTOP', 'the Reload button shows before any check')

      // Back with no input at all: the button, and no reload.
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 1, 3000))) return fail('RELOAD-DESKTOP', `back after 11 min ran ${log.checks} checks, expected 1`)
      await sleep(1200)
      if (log.navs) return fail('RELOAD-DESKTOP', 'the desktop reloaded by itself')
      if (!(await button.isVisible())) return fail('RELOAD-DESKTOP', `no Reload button after 11 min away: ${JSON.stringify(log)}`)

      // The server serves the page's own build again: the button goes.
      page._nextScript = null
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 2, 3000))) return fail('RELOAD-DESKTOP', `back after 11 min ran ${log.checks} checks, expected 2`)
      if (!(await waitFor(async () => !(await button.count()), 3000))) return fail('RELOAD-DESKTOP', 'the Reload button stayed with the server serving this build')
      page._nextScript = '/static/index-next.js'

      // A new build again, with input during the check: the button comes back.
      let release
      page._hold = new Promise((r) => (release = r))
      await away(page, 11)
      if (!(await waitFor(() => log.checks === 3, 3000))) return fail('RELOAD-DESKTOP', `back after 11 min ran ${log.checks} checks, expected 3`)
      await page.mouse.wheel(0, 40)
      await page.keyboard.press('Shift')
      release()
      page._hold = null
      if (!(await waitFor(() => button.isVisible(), 3000))) return fail('RELOAD-DESKTOP', `no Reload button after 11 min away: ${JSON.stringify(log)}`)
      await shot('button')
      await sleep(1200)
      if (log.navs) return fail('RELOAD-DESKTOP', 'the desktop reloaded by itself')
      await button.click()
      if (!(await waitFor(() => log.navs === 1, 5000))) return fail('RELOAD-DESKTOP', `clicking Reload did not reload: ${JSON.stringify(log)}`)
      // The flow ends on the loaded page: closing the context while the
      // reload is still being fetched would throw in smoke's CSP route.
      await page.locator('main.grid article[data-card-id]').first().waitFor({ state: 'visible', timeout: 20000 })
      if (await button.count()) fail('RELOAD-DESKTOP', 'the Reload button is still there after the reload')
    },
  }),
  // --- PR7a: tab order and hiding ---------------------------------------------

  // Edit, ↑, Done moves a tab one place in 3 taps and one PUT; Edit, Move
  // to top, Done takes the last tab to second place (after Brief). Focus
  // stays on the pressed arrow, or goes to the opposite one when the
  // pressed one is now disabled, and the live line says where the tab is.
  dashFlow('flow-tabs-reorder', {
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      const before = await stripLabels(page)
      const at = before.indexOf('News')
      if (at < 2) return fail('TABS-REORDER', `unexpected strip ${JSON.stringify(before)}`)
      await openTabsEditor(page)
      if ((await announced(page)) !== '') fail('TABS-REORDER', `the live line reads ${JSON.stringify(await announced(page))} before any move`)
      await tap(page, editorButton(page, 'Move News up'))
      await sleep(200)
      if ((await focusedLabel(page)) !== 'Move News up') fail('TABS-REORDER', `after ↑ focus is on ${JSON.stringify(await focusedLabel(page))}`)
      const live = `News, ${at} of ${before.length}`
      if ((await announced(page)) !== live) fail('TABS-REORDER', `the live line reads ${JSON.stringify(await announced(page))}, expected ${JSON.stringify(live)}`)
      await shot('editor')
      await closeTabsEditor(page)
      taps(page, 3, 'TABS-REORDER')
      const want = [...before]
      want.splice(at - 1, 0, ...want.splice(at, 1))
      const after = await stripLabels(page)
      if (JSON.stringify(after) !== JSON.stringify(want)) fail('TABS-REORDER', `the strip reads ${JSON.stringify(after)}, expected ${JSON.stringify(want)}`)
      if (!(await waitFor(() => layoutWrites(writes).length >= 1, 3000))) return fail('TABS-REORDER', 'the move sent no category_layout write')
      await sleep(500)
      const sent = layoutWrites(writes).map((w) => w.method)
      if (sent.join() !== 'PUT') fail('TABS-REORDER', `the move sent ${JSON.stringify(sent)}, expected one PUT`)
      // A move stores the whole order (every tab but Brief).
      const order = (prefMock(page.context()).value('category_layout') || {}).order || []
      const keyOf = (l) => (l === 'Saved' ? 'meta:saved' : l === 'For You' ? 'meta:foryou' : `cat:${l.toLowerCase()}`)
      if (JSON.stringify(order) !== JSON.stringify(want.slice(1).map(keyOf))) fail('TABS-REORDER', `the stored order is ${JSON.stringify(order)}, the strip ${JSON.stringify(want)}`)
      if ((await focusedLabel(page)) !== 'Edit tabs') fail('TABS-REORDER', `after Done focus is on ${JSON.stringify(await focusedLabel(page))}, expected the Edit pill`)

      // The last tab to second place.
      page._taps = 0
      const last = after[after.length - 1]
      await openTabsEditor(page)
      await tap(page, editorButton(page, `Move ${last} to top`))
      await sleep(200)
      if ((await focusedLabel(page)) !== `Move ${last} down`) fail('TABS-REORDER', `after Move to top focus is on ${JSON.stringify(await focusedLabel(page))}, expected Move ${last} down`)
      const top = `${last}, 2 of ${after.length}`
      if ((await announced(page)) !== top) fail('TABS-REORDER', `the live line reads ${JSON.stringify(await announced(page))}, expected ${JSON.stringify(top)}`)
      if (!(await editorButton(page, `Move ${last} up`).isDisabled())) fail('TABS-REORDER', `Move ${last} up is enabled at the top`)
      await closeTabsEditor(page)
      taps(page, 3, 'TABS-REORDER-TOP')
      const moved = await stripLabels(page)
      if (moved[0] !== 'Brief' || moved[1] !== last) fail('TABS-REORDER', `after Move to top the strip reads ${JSON.stringify(moved)}`)
      await sleep(500)
      if (layoutWrites(writes).length !== 2) fail('TABS-REORDER', `${layoutWrites(writes).length} category_layout writes for two moves`)
      await shot('end')
    },
  }),

  // The desktop grid and the Jump list follow the same order. The tab
  // editor opens from the Menu there (no strip), and focus goes back to ≡.
  dashFlow('flow-tabs-reorder-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    run: async ({ page, fail, shot }) => {
      const before = await gridLabels(page)
      if (before[0] !== 'Saved' || before.includes('For You')) fail('TABS-DESK', `the grid reads ${JSON.stringify(before)}`)
      await openMenu(page)
      await page.locator(MENU).getByRole('button', { name: 'Edit tabs' }).click()
      await page.locator(TABS_EDITOR).waitFor({ state: 'visible' })
      await page.locator(TABS_EDITOR).getByRole('button', { name: 'Move News to top', exact: true }).click()
      await page.locator(TABS_EDITOR).getByRole('button', { name: 'Done', exact: true }).click()
      await page.locator(TABS_EDITOR).waitFor({ state: 'detached' })
      await sleep(300)
      if ((await focusedLabel(page)) !== 'open menu') fail('TABS-DESK', `after Done focus is on ${JSON.stringify(await focusedLabel(page))}, expected ≡`)
      const grid = await gridLabels(page)
      const want = ['News', ...before.filter((l) => l !== 'News')]
      if (JSON.stringify(grid) !== JSON.stringify(want)) fail('TABS-DESK', `the grid reads ${JSON.stringify(grid)}, expected ${JSON.stringify(want)}`)
      await openMenu(page)
      const jump = await jumpLabels(page)
      const wantJump = ['News', 'Saved', 'For You', ...before.filter((l) => l !== 'News' && l !== 'Saved')]
      if (JSON.stringify(jump) !== JSON.stringify(wantJump)) fail('TABS-DESK', `Jump to column reads ${JSON.stringify(jump)}, expected ${JSON.stringify(wantJump)}`)
      await shot('end')
    },
  }),

  // Show off hides a tab from the strip and Jump to column, keeps its
  // stories in For You, and keeps its place: Show on brings it back there.
  dashFlow('flow-tab-hide', {
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      const before = await stripLabels(page)
      await openTabsEditor(page)
      const sw = showSwitch(page, 'News')
      if ((await sw.getAttribute('aria-checked')) !== 'true') return fail('TAB-HIDE', 'News starts hidden')
      await tap(page, sw)
      if ((await sw.getAttribute('aria-checked')) !== 'false') fail('TAB-HIDE', 'the switch did not turn off')
      await closeTabsEditor(page)
      taps(page, 3, 'TAB-HIDE')
      const after = await stripLabels(page)
      if (JSON.stringify(after) !== JSON.stringify(before.filter((l) => l !== 'News'))) fail('TAB-HIDE', `the strip reads ${JSON.stringify(after)}`)
      if (!(await waitFor(() => layoutWrites(writes).length === 1, 3000))) return fail('TAB-HIDE', 'hiding sent no category_layout write')
      const stored = prefMock(page.context()).value('category_layout') || {}
      if (JSON.stringify(stored.hidden) !== '["cat:news"]') fail('TAB-HIDE', `the stored hidden list is ${JSON.stringify(stored.hidden)}`)
      // Hiding orders nothing: with no move, the order stays the default.
      if ((stored.order || []).length) fail('TAB-HIDE', `hiding stored an order: ${JSON.stringify(stored.order)}`)
      // Its stories stay in For You (101 is a News story).
      await tap(page, phoneTab(page, /^for you/i))
      if (!(await page.locator(`${MOBILE_MAIN} article[data-card-id="101"]`).count())) fail('TAB-HIDE', 'a News story left For You with its tab')
      await openMenu(page)
      const jump = await jumpLabels(page)
      if (jump.includes('News')) fail('TAB-HIDE', `Jump to column still offers News: ${JSON.stringify(jump)}`)
      if (JSON.stringify(jump) !== JSON.stringify(after.slice(1))) fail('TAB-HIDE', `Jump to column reads ${JSON.stringify(jump)}, the strip ${JSON.stringify(after)}`)
      await page.locator('button[aria-label="close menu"]').click()
      await sleep(500)
      await shot('hidden')
      // Shown again, in its old place.
      await openTabsEditor(page)
      await tap(page, showSwitch(page, 'News'))
      await closeTabsEditor(page)
      const back = await stripLabels(page)
      if (JSON.stringify(back) !== JSON.stringify(before)) fail('TAB-HIDE', `shown again, the strip reads ${JSON.stringify(back)}, expected ${JSON.stringify(before)}`)
    },
  }),

  // The arrow keys (desktop) never stop on a hidden tab, and the grid
  // leaves it out.
  dashFlow('flow-tab-hide-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ context }) => {
      prefMock(context).seed({ category_layout: { v: 1, order: [], hidden: ['cat:news'] } })
    },
    run: async ({ page, fail }) => {
      const grid = await gridLabels(page)
      if (grid.includes('News')) return fail('TAB-HIDE-DESK', `the grid still shows News: ${JSON.stringify(grid)}`)
      // One ArrowRight per grid column, each followed by ArrowDown: the
      // selected card's column, null where nothing is selected (Saved is
      // first and empty). For You, the row above the grid, is no stop.
      const visited = []
      for (let i = 0; i < grid.length + 1; i++) {
        if (i > 0) await page.keyboard.press('ArrowRight')
        await page.keyboard.press('ArrowDown')
        await sleep(150)
        visited.push(
          await page.evaluate(() => {
            const card = document.querySelector('main.grid article[data-card-id][tabindex="0"]')
            const h = card && card.closest('section') && card.closest('section').querySelector('header h2')
            return h ? h.textContent.trim() : null
          }),
        )
      }
      if (visited.includes('News')) fail('TAB-HIDE-DESK', `the arrow keys reached the hidden News: ${JSON.stringify(visited)}`)
      // The last press stays on the last column. An empty column (Saved;
      // from PR4d a category with no stories in 2 days) selects nothing.
      const withCards = await page.evaluate(() => [...document.querySelectorAll('main.grid section')].map((s) => !!s.querySelector('article[data-card-id]')))
      const stop = (i) => (withCards[i] ? grid[i] : null)
      const want = [null, ...grid.slice(1).map((_, i) => stop(i + 1)), stop(grid.length - 1)]
      if (grid[0] !== 'Saved') fail('TAB-HIDE-DESK', `the grid starts with ${JSON.stringify(grid[0])}`)
      if (JSON.stringify(visited) !== JSON.stringify(want)) fail('TAB-HIDE-DESK', `the arrow keys visited ${JSON.stringify(visited)}, expected ${JSON.stringify(want)}`)
    },
  }),

  // With a stored order, a new category goes at the end of the strip and
  // of Jump to column; the feed editor's category picker lists categories
  // in that order.
  dashFlow('flow-new-category-at-end', {
    before: async ({ page, context }) => {
      page._patches11 = await record(page, 'PATCH', '/api/sources/11')
      prefMock(context).seed({ category_layout: { v: 1, order: ['cat:video', ...DEFAULT_ORDER.filter((k) => k !== 'cat:video')], hidden: [] } })
    },
    run: async ({ page, fail }) => {
      const before = await stripLabels(page)
      if (before[1] !== 'Video') return fail('NEWCAT-END', `the stored order was not applied: ${JSON.stringify(before)}`)
      await openEditor(page, 11)
      const picked = await page.locator('#fe-category-11 optgroup[label="Your categories"] option').evaluateAll((els) => els.map((o) => o.value))
      if (picked[0] !== 'video' || picked.indexOf('deals') > picked.indexOf('news')) fail('NEWCAT-END', `the category picker lists ${JSON.stringify(picked)}, not in tab order`)
      await closeSettings(page)
      if ((await newHockeyCategory(page, fail, 'NEWCAT-END')) !== true) return
      const after = await stripLabels(page)
      if (after[after.length - 1] !== 'Hockey') fail('NEWCAT-END', `the new category is not last: ${JSON.stringify(after)}`)
      await openMenu(page)
      const jump = await jumpLabels(page)
      if (jump[jump.length - 1] !== 'Hockey') fail('NEWCAT-END', `Jump to column ends with ${JSON.stringify(jump[jump.length - 1])}`)
    },
  }),

  // With no stored order, a new category lands in label order, as it
  // always did.
  dashFlow('flow-new-category-alpha', {
    before: async ({ page }) => {
      page._patches11 = await record(page, 'PATCH', '/api/sources/11')
    },
    run: async ({ page, fail }) => {
      if ((await newHockeyCategory(page, fail, 'NEWCAT-ALPHA')) !== true) return
      const after = await stripLabels(page)
      if (after.indexOf('Hockey') !== after.indexOf('Deals') + 1) fail('NEWCAT-ALPHA', `the new category is out of label order: ${JSON.stringify(after)}`)
    },
  }),

  // Saved and For You are always tabs, right after Brief: empty, Saved
  // says "No stories yet" (no first-feed button: there are feeds).
  dashFlow('flow-saved-always', {
    run: async ({ page, fail, shot }) => {
      const tabs = await stripLabels(page)
      if (tabs[0] !== 'Brief' || tabs[1] !== 'Saved' || tabs[2] !== 'For You') return fail('SAVED-ALWAYS', `the strip reads ${JSON.stringify(tabs)}`)
      await tap(page, phoneTab(page, /^saved/i))
      if (await page.locator(`${MOBILE_MAIN} article[data-card-id]`).count()) return fail('SAVED-ALWAYS', 'Saved shows stories before anything was saved')
      if (!(await page.locator(MOBILE_MAIN).getByText('No stories yet', { exact: true }).isVisible())) fail('SAVED-ALWAYS', 'the empty Saved does not say "No stories yet"')
      if (await page.locator(`${MOBILE_MAIN} [data-first-feed]`).count()) fail('SAVED-ALWAYS', 'an empty Saved offers "Add your first feed" although there are feeds')
      if ((await storedTab(page)) !== 'meta:saved') fail('SAVED-ALWAYS', `a tap on the empty Saved stored ${JSON.stringify(await storedTab(page))}`)
      await shot('empty')
    },
  }),

  // A new device opens on For You whatever the stored order puts first.
  dashFlow('flow-fresh-device-opens-foryou', {
    before: async ({ context }) => {
      prefMock(context).seed({
        starred_entries: [103],
        category_layout: { v: 1, order: ['cat:news', 'meta:saved', 'meta:foryou'], hidden: [] },
      })
    },
    run: async ({ page, fail }) => {
      if (!(await waitFor(async () => (await stripLabels(page))[1] === 'News', 3000))) return fail('FRESH-FY', `the stored order was not applied: ${JSON.stringify(await stripLabels(page))}`)
      if (!(await waitFor(async () => (await storedTab(page)) !== null, 3000))) return fail('FRESH-FY', 'a new device stored no tab')
      const now = await selectedTab(page)
      if (!/^for you/i.test(now)) fail('FRESH-FY', `a new device opened on ${JSON.stringify(now)}`)
      if ((await storedTab(page)) !== 'meta:foryou') fail('FRESH-FY', `a new device stored ${JSON.stringify(await storedTab(page))}`)
    },
  }),

  // Only built-in feeds and no stories: For You (where a new device
  // opens) says "No stories yet" with "Add your first feed", which opens
  // Settings → Feeds.
  dashFlow('flow-first-feed-empty-meta', {
    wait: `${STRIP} [aria-selected="true"]`,
    before: async ({ page }) => {
      const builtIns = ['bbc_news', 'hn_top', 'nvd_recent', 'cisa_kev', 'rfd_hot_deals']
      const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      await page.route(
        (u) => new URL(u).pathname === '/api/sources',
        (route) => (route.request().method() === 'GET' ? route.fulfill(json(F.SOURCES.filter((src) => builtIns.includes(src.name)))) : route.fallback()),
      )
      await page.route((u) => /^\/api\/(entries|foryou)(\/|$)/.test(new URL(u).pathname), (route) => route.fulfill(json([])))
    },
    run: async ({ page, fail, shot }) => {
      const button = page.locator(`${MOBILE_MAIN} [data-first-feed]`)
      if (!(await waitFor(() => button.isVisible(), 3000))) return fail('FIRST-FEED', 'no "Add your first feed" button')
      const now = await selectedTab(page)
      if (!/^for you/i.test(now)) fail('FIRST-FEED', `a new device opened on ${JSON.stringify(now)}`)
      if (!(await page.locator(MOBILE_MAIN).getByText('No stories yet', { exact: true }).isVisible())) fail('FIRST-FEED', 'For You does not say "No stories yet"')
      const box = await button.boundingBox()
      if (!box || box.height < 43.5) fail('FIRST-FEED', `the button is ${box ? box.height : 0}px tall`)
      await shot('empty')
      await tap(page, button)
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      const tab = await page.evaluate(() => new URLSearchParams(location.search).get('tab'))
      if (tab !== 'feeds') fail('FIRST-FEED', `the button opened Settings on ${JSON.stringify(tab)}`)
      taps(page, 1, 'FIRST-FEED')
    },
  }),

  // A first load that fails says nothing about the owner's feeds: the
  // error banner shows, and an empty For You or Saved offers no "Add your
  // first feed". The button comes once a refresh brings the feed list
  // back with only built-ins.
  dashFlow('flow-first-feed-load-fails', {
    wait: `${STRIP} [aria-selected="true"]`,
    expectedFailedLoads: ['/api/sources'],
    before: async ({ page }) => {
      const builtIns = ['bbc_news', 'hn_top', 'nvd_recent', 'cisa_kev', 'rfd_hot_deals']
      const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      page._sourcesDown = true
      await page.route(
        (u) => new URL(u).pathname === '/api/sources',
        (route) => {
          if (route.request().method() !== 'GET') return route.fallback()
          if (page._sourcesDown) return route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' })
          return route.fulfill(json(F.SOURCES.filter((src) => builtIns.includes(src.name))))
        },
      )
      await page.route((u) => /^\/api\/(entries|foryou)(\/|$)/.test(new URL(u).pathname), (route) => route.fulfill(json([])))
    },
    run: async ({ page, fail, shot }) => {
      const button = page.locator(`${MOBILE_MAIN} [data-first-feed]`)
      const empty = page.locator(MOBILE_MAIN).getByText('No stories yet', { exact: true })
      if (!(await waitFor(() => page.locator('[role="alert"]').isVisible(), 3000))) return fail('FIRST-FEED-FAIL', 'no error banner after the first load failed')
      for (const re of [/^for you/i, /^saved/i]) {
        await tap(page, phoneTab(page, re))
        if (!(await waitFor(() => empty.isVisible(), 3000))) fail('FIRST-FEED-FAIL', `${re.source} does not say "No stories yet"`)
        await sleep(200)
        if (await button.count()) fail('FIRST-FEED-FAIL', `${re.source} offers "Add your first feed" after a failed load`)
      }
      await shot('failed')
      // The feeds load (only built-ins): now the button shows.
      page._sourcesDown = false
      await startPoll(page)
      if (!(await waitFor(() => button.isVisible(), 3000))) fail('FIRST-FEED-FAIL', 'no "Add your first feed" once only built-in feeds loaded')
    },
  }),

  // Desktop: the arrow-key selection follows its column, not its place.
  // Moving the selected card's column (Edit tabs) keeps that card
  // selected, and ArrowDown goes on down the same column. Hiding the
  // column drops the card, and the selection moves to the column now at
  // its place.
  dashFlow('flow-desktop-select-follows-tab', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ context }) => {
      // Saved has a story, so the column that takes News's place has one
      // to select.
      prefMock(context).seed({ starred_entries: [103] })
    },
    run: async ({ page, fail }) => {
      const SEL = 'main.grid article[data-card-id][tabindex="0"]'
      const selected = () =>
        page.evaluate((sel) => {
          const card = document.querySelector(sel)
          const h = card && card.closest('section') && card.closest('section').querySelector('header h2')
          return card ? { id: card.getAttribute('data-card-id'), column: h ? h.textContent.trim() : null } : null
        }, SEL)
      const editTabs = async (name, role = 'button') => {
        await openMenu(page)
        await page.locator(MENU).getByRole('button', { name: 'Edit tabs' }).click()
        await page.locator(TABS_EDITOR).waitFor({ state: 'visible' })
        await page.locator(TABS_EDITOR).getByRole(role, { name, exact: true }).click()
        await page.locator(TABS_EDITOR).getByRole('button', { name: 'Done', exact: true }).click()
        await page.locator(TABS_EDITOR).waitFor({ state: 'detached' })
        await sleep(300)
      }
      const grid = await gridLabels(page)
      const at = grid.indexOf('News')
      if (at < 2) return fail('SELECT-KEY', `unexpected grid ${JSON.stringify(grid)}`)
      for (let i = 0; i < at; i++) await page.keyboard.press('ArrowRight')
      await page.keyboard.press('ArrowDown')
      await page.keyboard.press('ArrowDown')
      await sleep(150)
      const first = await selected()
      if (!first || first.column !== 'News') return fail('SELECT-KEY', `the arrow keys selected ${JSON.stringify(first)}, expected a News card`)

      await editTabs('Move News to top')
      const moved = await gridLabels(page)
      if (moved[0] !== 'News') return fail('SELECT-KEY', `Move News to top left the grid as ${JSON.stringify(moved)}`)
      const kept = await selected()
      if (!kept || kept.id !== first.id || kept.column !== 'News') fail('SELECT-KEY', `after moving News the selection is ${JSON.stringify(kept)}, expected card ${first.id} in News`)
      await page.keyboard.press('ArrowDown')
      await sleep(150)
      const down = await selected()
      if (!down || down.column !== 'News' || down.id === first.id) fail('SELECT-KEY', `ArrowDown after the move selected ${JSON.stringify(down)}, expected the next News card`)

      await editTabs('Show News', 'switch')
      const hidden = await gridLabels(page)
      if (hidden.includes('News')) return fail('SELECT-KEY', `hiding News left the grid as ${JSON.stringify(hidden)}`)
      if (await page.locator(SEL).count()) fail('SELECT-KEY', `a card is still selected after its column was hidden: ${JSON.stringify(await selected())}`)
      await page.keyboard.press('ArrowDown')
      await sleep(150)
      const next = await selected()
      if (!next || next.column !== hidden[0]) fail('SELECT-KEY', `ArrowDown after hiding News selected ${JSON.stringify(next)}, expected a card in ${hidden[0]}`)
    },
  }),

  // Desktop: an empty For You still has its row (it is a shown tab), so
  // Jump to column's For You lands on it.
  dashFlow('flow-foryou-empty-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await page.route((u) => new URL(u).pathname === '/api/foryou', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
    },
    run: async ({ page, fail, shot }) => {
      // The desktop row, not the phone's (hidden) For You column.
      const row = page.locator('section').filter({ visible: true, has: page.locator('header h2', { hasText: /^For You$/ }) })
      if (!(await waitFor(async () => (await row.count()) === 1, 3000))) return fail('FY-EMPTY-DESK', 'no For You row while For You is empty')
      if (!(await row.getByText('No stories yet', { exact: true }).isVisible())) fail('FY-EMPTY-DESK', 'the empty For You row does not say "No stories yet"')
      if (await row.locator('[data-first-feed]').count()) fail('FY-EMPTY-DESK', 'the empty For You row offers "Add your first feed" although there are feeds')
      await openMenu(page)
      if (!(await jumpLabels(page)).includes('For You')) return fail('FY-EMPTY-DESK', 'Jump to column has no For You')
      await page.locator(MENU).locator('h3:text-matches("jump to column", "i")').locator('xpath=../..').getByRole('button', { name: 'For You', exact: true }).click()
      await sleep(800)
      const top = await row.evaluate((el) => el.getBoundingClientRect().top)
      if (top < 0 || top > 200) fail('FY-EMPTY-DESK', `after Jump to For You its row starts at ${top}px`)
      await shot('empty')
    },
  }),

  // Filtered to one feed with no stories, the column says how that feed
  // is doing (slice 19): cisa_kev's last check failed.
  // The source filter's column (until PR8b) loads the window too: its
  // GET /api/entries asks for the filtered feed's last 2 days, at most 25
  // per feed, active feeds only, so darknet_diaries shows 401 and 402 and
  // not 405 (4 days old). ⋯ → 7 days fetches the column's own rows with
  // the same parameters at 168 hours, and 405 joins. (The device is an
  // old one, so 405 would be unseen and drawn if it came; the clock is
  // held, so 401 and 402 aren't used up meanwhile.)
  dashFlow('flow-filter-window', {
    before: async ({ page }) => {
      await oldDevice(page)
      await installClock(page)
      page._filtered = []
      await page.route(
        (u) => new URL(u).pathname === '/api/entries' && new URL(u).searchParams.getAll('source').length > 0,
        (route) => {
          page._filtered.push(new URL(route.request().url()).search)
          return route.fallback()
        },
      )
    },
    run: async ({ page, fail }) => {
      await holdClock(page)
      await openMenu(page)
      await page.locator(MENU).getByRole('checkbox', { name: /^filter by darknet/i }).check()
      await page.keyboard.press('Escape')
      // The filter's refresh waits on a timer.
      await page.clock.runFor(1100)
      if (!(await waitFor(async () => (await tabTexts(page)).length === 2, 3000))) return fail('FILTER-WINDOW', `the filter left ${JSON.stringify(await tabTexts(page))}`)
      const want = '?source=darknet_diaries&limit=200&max_age_hours=48&per_source_cap=25&active_only=true'
      if (!(await waitFor(() => page._filtered.length > 0, 3000))) return fail('FILTER-WINDOW', 'the filter sent no filtered request')
      if (page._filtered[0] !== want) fail('FILTER-WINDOW', `the filtered request was ${page._filtered[0]}, expected ${want}`)
      if (!(await waitFor(async () => (await phoneIds(page)) === '401,402', 3000))) return fail('FILTER-WINDOW', `the filter shows ${await phoneIds(page)}, expected 401,402 (405 is 4 days old)`)
      const sent = page._filtered.length
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="7d"]`))
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      const wider = '?source=darknet_diaries&limit=200&max_age_hours=168&per_source_cap=25&active_only=true'
      if (!(await waitFor(() => page._filtered.slice(sent).includes(wider), 3000))) return fail('FILTER-WINDOW', `7 days sent ${JSON.stringify(page._filtered.slice(sent))}, expected ${wider}`)
      if (!(await waitFor(async () => (await phoneIds(page)) === '401,402,405', 3000))) fail('FILTER-WINDOW', `at 7 days the filter shows ${await phoneIds(page)}, expected 401,402,405`)
    },
  }),
  dashFlow('flow-filter-one-feed-status', {
    before: async ({ page }) => {
      await page.route(
        (u) => new URL(u).pathname === '/api/entries' && new URL(u).searchParams.getAll('source').length > 0,
        (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
      )
    },
    run: async ({ page, fail, shot }) => {
      await openMenu(page)
      await page.locator(MENU).getByRole('checkbox', { name: /^filter by cisa kev$/i }).check()
      await page.keyboard.press('Escape')
      if (!(await waitFor(async () => (await tabTexts(page)).length === 2, 3000))) return fail('FILTER-STATUS', `the filter left ${JSON.stringify(await tabTexts(page))}`)
      const status = page.locator(MOBILE_MAIN).getByText(/^last fetch failed: HTTP 503/)
      if (!(await waitFor(() => status.isVisible(), 3000))) {
        const text = ((await page.locator(MOBILE_MAIN).textContent()) || '').trim().slice(0, 200)
        return fail('FILTER-STATUS', `the empty one-feed filter does not show the feed's last error: ${JSON.stringify(text)}`)
      }
      await shot('status')
    },
  }),
  // --- PR3a: desktop single scroller and batching ----------------------------

  // The desktop scrolls as one page. With 25 For You cards (a row taller
  // than the window) the grid keeps its height instead of shrinking to
  // its padding, the document never scrolls, and the one data-scroller
  // is the wrapper around it all. A 70-story column renders 30 cards,
  // then 30 more once the wrapper scrolls to within 600px of its end.
  dashFlow('flow-desktop-layout', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await withStories(page, moreNhl(67))
      const forYou = F.ENTRIES.slice(0, 25)
      await page.route((u) => new URL(u).pathname === '/api/foryou', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(forYou) }))
    },
    run: async ({ page, fail, shot }) => {
      const row = page.locator('section').filter({ visible: true, has: page.locator('header h2', { hasText: /^For You$/ }) })
      // The row shows For You's top 6 (PR4d); a 400px window makes it
      // taller than the window, as the 25 cards did at 800px.
      if (!(await waitFor(async () => (await cardCount(row)) === 6, 5000))) return fail('DESK-LAYOUT', `the For You row has ${await cardCount(row)} cards, expected its top 6`)
      await page.setViewportSize({ width: 1280, height: 400 })
      await sleep(300)
      const scrollers = await page.evaluate(() =>
        [...document.querySelectorAll('[data-scroller]')].map((el) => ({ grid: !!el.querySelector('main.grid'), shown: el.getClientRects().length > 0 })),
      )
      if (scrollers.length !== 1 || !scrollers[0].grid || !scrollers[0].shown) fail('DESK-LAYOUT', `data-scroller is on ${JSON.stringify(scrollers)}, expected only the wrapper around the grid`)
      const box = await page.evaluate(() => {
        const sc = document.querySelector('[data-scroller]')
        const fy = [...document.querySelectorAll('section')].find((s) => s.getClientRects().length && s.querySelector('h2')?.textContent?.trim() === 'For You')
        return {
          grid: document.querySelector('main.grid').getBoundingClientRect().height,
          forYou: fy ? fy.getBoundingClientRect().height : 0,
          window: innerHeight,
          doc: document.scrollingElement.scrollHeight - innerHeight,
          scroller: sc ? sc.scrollHeight - sc.clientHeight : 0,
        }
      })
      if (box.forYou <= box.window) fail('DESK-LAYOUT', `the For You row is ${box.forYou}px tall in a ${box.window}px window; the flow needs one taller than the window`)
      if (box.grid <= 300) fail('DESK-LAYOUT', `the grid is ${box.grid}px tall under a ${box.forYou}px For You row, expected more than 300px`)
      if (box.doc > 1) fail('DESK-LAYOUT', `the document scrolls by ${box.doc}px; only the wrapper should`)
      if (box.scroller <= 0) fail('DESK-LAYOUT', 'the wrapper has nothing to scroll')

      const nhl = page.locator('main.grid section').filter({ has: page.locator('header h2', { hasText: /^nhl$/i }) })
      if ((await nhl.count()) !== 1) return fail('DESK-LAYOUT', 'no NHL column in the grid')
      await sleep(500)
      const before = await cardCount(nhl)
      if (before !== 30) fail('DESK-LAYOUT', `the 70-story NHL column renders ${before} cards before any scroll, expected 30`)
      // The sentinel 300px under the wrapper's bottom edge: the next 30
      // load before the end of the column comes into view.
      if (!(await sentinelBelow(nhl, 300))) return fail('DESK-LAYOUT', 'the NHL column has no "load more" sentinel')
      if (!(await waitFor(async () => (await cardCount(nhl)) === 60, 3000))) return fail('DESK-LAYOUT', `with the wrapper scrolled to 300px above the column's end it renders ${await cardCount(nhl)} cards, expected 60`)
      await sleep(500)
      const after = await page.evaluate(() => ({ win: scrollY, wrapper: document.querySelector('[data-scroller]').scrollTop }))
      if ((await cardCount(nhl)) !== 60) fail('DESK-LAYOUT', `the column went on to ${await cardCount(nhl)} cards without another scroll`)
      if (after.win !== 0 || after.wrapper <= 0) fail('DESK-LAYOUT', `the scroll moved the window by ${after.win}px and the wrapper by ${after.wrapper}px; only the wrapper should move`)
      await shot('scrolled')

      // PR3b: Deals's end row (in NHL's grid row) clicked with the mouse,
      // far down the page. Chromium focuses a button on mousedown, but a
      // mouse click is not a keyboard press: the focus is not handed to
      // the marker that replaces the row far above the screen, and the
      // page does not move under the pointer.
      const column = (re) => page.locator('main.grid section').filter({ has: page.locator('header h2', { hasText: re }) })
      const scrollTop = () => page.evaluate(() => document.querySelector('[data-scroller]').scrollTop)
      const deals = column(/^deals$/i)
      const dealsRow = deals.locator('button[data-caught-up]')
      if ((await dealsRow.count()) !== 1) return fail('DESK-LAYOUT', `the Deals column has no end row: ${JSON.stringify(await deals.locator('[data-caught-up]').allTextContents())}`)
      await dealsRow.evaluate((el) => el.scrollIntoView({ block: 'end' }))
      const atClick = await scrollTop()
      await dealsRow.click()
      if (!(await waitFor(async () => (await deals.locator('div[data-caught-up]').count()) === 1, 3000))) return fail('DESK-LAYOUT', `after a mouse click on Deals's end row the column still draws ${await cardCount(deals)} cards`)
      // The hand-over would run within a render or two of the marker.
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))))
      await sleep(300)
      const afterClick = await scrollTop()
      if (afterClick !== atClick) fail('DESK-LAYOUT', `a mouse click on Deals's end row moved the wrapper from ${atClick} to ${afterClick}`)
      if ((await focusedNow(page)) === 'DIV[data-caught-up]') fail('DESK-LAYOUT', "after a mouse click on Deals's end row the focus went to its marker")

      // PR3b: News's end row pressed with the keyboard, far down the
      // page. The column shrinks to its marker while NHL's 60 cards keep
      // the grid row (and the page) as tall as it was, so the marker
      // that takes the focus is far above the screen: it is brought into
      // the wrapper's view, not left out of sight.
      const news = column(/^news$/i)
      const endRow = news.locator('button[data-caught-up]')
      if ((await endRow.count()) !== 1) return fail('DESK-LAYOUT', `the News column has no end row: ${JSON.stringify(await news.locator('[data-caught-up]').allTextContents())}`)
      await endRow.evaluate((el) => el.scrollIntoView({ block: 'end' }))
      await endRow.focus()
      const deep = await page.evaluate(() => document.querySelector('[data-scroller]').scrollTop)
      await page.keyboard.press('Enter')
      if (!(await waitFor(async () => (await cardCount(news)) === 0, 3000))) return fail('DESK-LAYOUT', `after the end row's Mark as read the News column still draws ${await cardCount(news)} cards`)
      if (!(await waitFor(async () => (await focusedNow(page)) === 'DIV[data-caught-up]', 1000))) fail('DESK-LAYOUT', `after News's end row from the keyboard the focus is on ${await focusedNow(page)}, expected the marker`)
      const seen = await page.evaluate(() => {
        const a = document.activeElement
        const sc = document.querySelector('[data-scroller]')
        const r = a.getBoundingClientRect()
        const b = sc.getBoundingClientRect()
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), from: Math.round(b.top), to: Math.round(b.bottom), scrollTop: sc.scrollTop, inNews: !!a.closest('section') && a.closest('section').querySelector('header h2').textContent.trim() }
      })
      if (!/^news$/i.test(String(seen.inNews))) fail('DESK-LAYOUT', `the focused marker is in ${JSON.stringify(seen.inNews)}, expected News`)
      if (seen.top < seen.from || seen.bottom > seen.to) fail('DESK-LAYOUT', `after News's end row from the keyboard (the wrapper at ${deep}) the focused marker is at ${seen.top}..${seen.bottom}, outside the wrapper's ${seen.from}..${seen.to} (wrapper now at ${seen.scrollTop})`)
      await shot('news-marked')

      // PR3b: Security's end row pressed by a screen reader that sends
      // mouse events alone (NVDA or JAWS in Firefox): a click with detail
      // 1 on the focused button, with no pointerdown. That is not a
      // mouse's press: the focus goes on to the marker.
      const security = column(/^security$/i)
      const securityRow = security.locator('button[data-caught-up]')
      if ((await securityRow.count()) !== 1) return fail('DESK-LAYOUT', `the Security column has no end row: ${JSON.stringify(await security.locator('[data-caught-up]').allTextContents())}`)
      await securityRow.evaluate((el) => {
        el.scrollIntoView({ block: 'end' })
        el.focus({ preventScroll: true })
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1, view: window }))
      })
      if (!(await waitFor(async () => (await security.locator('div[data-caught-up]').count()) === 1, 3000))) return fail('DESK-LAYOUT', `after a screen reader's click on Security's end row the column still draws ${await cardCount(security)} cards`)
      const onSecurity = () => page.evaluate(() => { const a = document.activeElement; return !!a && a.hasAttribute('data-caught-up') && a.closest('section').querySelector('header h2').textContent.trim() })
      if (!(await waitFor(async () => /^security$/i.test(String(await onSecurity())), 1000))) fail('DESK-LAYOUT', `after a screen reader's click on Security's end row the focus is on ${await focusedNow(page)} (${JSON.stringify(await onSecurity())}), expected its marker`)

      // PR3b: ⋯ → Mark all as read on the NHL column: its 70 stories go
      // under the marker, folded, and its "70 new" goes.
      const newCount = () => nhl.evaluate((el) => { const n = el.querySelector('header [data-new-count]'); return n ? n.textContent.trim() : null })
      if ((await newCount()) !== '70 new') fail('DESK-LAYOUT', `the NHL header says ${JSON.stringify(await newCount())}, expected "70 new"`)
      await nhl.locator('button[aria-label="column preferences"]').click()
      await nhl.locator('[data-column-popover] button[data-mark-all]').click()
      if (!(await waitFor(async () => (await cardCount(nhl)) === 0, 3000))) return fail('DESK-LAYOUT', `after Mark all the NHL column still draws ${await cardCount(nhl)} cards above the marker`)
      const caught = ((await nhl.locator('[data-caught-up]').textContent()) || '').trim()
      if (!/You.re all caught up/.test(caught)) fail('DESK-LAYOUT', `after Mark all the NHL marker reads ${JSON.stringify(caught)}`)
      const earlier = nhl.locator('button[data-earlier]')
      if (((await earlier.textContent()) || '').trim() !== 'Earlier (70)' || (await earlier.getAttribute('aria-expanded')) !== 'false') {
        fail('DESK-LAYOUT', `after Mark all Earlier reads ${JSON.stringify(await earlier.textContent())}, expanded ${await earlier.getAttribute('aria-expanded')}`)
      }
      if ((await newCount()) != null) fail('DESK-LAYOUT', `after Mark all the NHL header still says ${JSON.stringify(await newCount())}`)
      if (!(await page.getByText('Marked 70 as read', { exact: true }).isVisible())) fail('DESK-LAYOUT', 'no "Marked 70 as read" toast')
      await shot('marked')
    },
  }),

  // A long tab grows as you scroll: 30 cards, then 60 once the list is
  // within 600px of its end, then all 70, with no "load more" row left.
  // The page's clock is held (PR3c): the list is flicked through, so no
  // card is read on screen and scrolled past, and none is used up.
  dashFlow('flow-auto-append', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(67))
    },
    run: async ({ page, fail, shot }) => {
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!/^nhl/i.test(await selectedTab(page))) return fail('AUTO-APPEND', 'could not select the NHL tab')
      const column = page.locator(`${MOBILE_MAIN} section`)
      const list = page.locator(`${MOBILE_MAIN} section [data-scroller]`)
      const toEnd = () => list.evaluate((el) => el.scrollTo(0, el.scrollHeight))
      if (!(await waitFor(async () => (await cardCount(column)) >= 30, 3000))) return fail('AUTO-APPEND', `the NHL tab renders ${await cardCount(column)} cards`)
      await sleep(500)
      if ((await cardCount(column)) !== 30) fail('AUTO-APPEND', `before any scroll the NHL tab renders ${await cardCount(column)} cards, expected 30`)
      // 300px short of the end: the next 30 load before the end shows.
      if (!(await sentinelBelow(column, 300))) return fail('AUTO-APPEND', 'the NHL list has no "load more" sentinel')
      if (!(await waitFor(async () => (await cardCount(column)) === 60, 3000))) return fail('AUTO-APPEND', `scrolled to 300px above the end, the list has ${await cardCount(column)} cards, expected 60`)
      await sleep(500)
      if ((await cardCount(column)) !== 60) fail('AUTO-APPEND', `the list went on to ${await cardCount(column)} cards without another scroll`)
      await toEnd()
      if (!(await waitFor(async () => (await cardCount(column)) === 70, 3000))) return fail('AUTO-APPEND', `after a second scroll to the end the list has ${await cardCount(column)} cards, expected 70`)
      if (await column.locator('[data-more]').count()) fail('AUTO-APPEND', 'the "load more" sentinel is still there with every card rendered')
      // PR3b: the list ends with the marker; nothing is seen, so it offers
      // to mark all 70.
      const end = ((await column.locator('[data-caught-up]').textContent().catch(() => '')) || '').trim()
      if (end !== '70 not seen · Mark as read') fail('AUTO-APPEND', `the list ends with ${JSON.stringify(end)}, expected "70 not seen · Mark as read"`)
      // The last batch went in below the scroll position: scroll to the
      // end again so the shot shows the last card, where the sentinel was.
      await toEnd()
      await sleep(300)
      const last = await list.evaluate((el) => {
        const cards = el.querySelectorAll('article[data-card-id]')
        const box = el.getBoundingClientRect()
        const inView = (r) => r.bottom > box.top && r.top < box.bottom
        const marker = el.querySelector('[data-caught-up]')
        return inView(cards[cards.length - 1].getBoundingClientRect()) && !!marker && inView(marker.getBoundingClientRect())
      })
      if (!last) fail('AUTO-APPEND', 'the end shot does not show the last card and the marker')
      await shot('end')
    },
  }),
  // Crossing 768px hands the scroller over without a remount: the phone
  // list carries data-scroller (and is the sentinel root and the
  // tap-to-top target) only on the phone, the wrapper only on the
  // desktop. The Column's memo holds across the flip (since PR3b no prop
  // of the phone Column changes with the width but ``scrollerRef``), so
  // only a prop the memo compares can re-render it.
  dashFlow('flow-scroller-rotate', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await withStories(page, moreNhl(67))
      await page.addInitScript(() => localStorage.setItem('popping.v1.mobileTab', 'cat:nhl'))
    },
    run: async ({ page, fail }) => {
      const scrollers = () =>
        page.evaluate(() => [...document.querySelectorAll('[data-scroller]')].map((el) => (el.querySelector('main.grid') ? 'wrapper' : el.closest('main.md\\:hidden') ? 'phone' : 'other')))
      const one = (want) => waitFor(async () => JSON.stringify(await scrollers()) === JSON.stringify([want]), 3000)
      if (!(await one('wrapper'))) fail('ROTATE', `at 1280 data-scroller is on ${JSON.stringify(await scrollers())}, expected only the wrapper`)

      await page.setViewportSize({ width: 390, height: 844 })
      if (!(await one('phone'))) return fail('ROTATE', `after 1280 → 390 data-scroller is on ${JSON.stringify(await scrollers())}, expected only the phone list`)
      if (!/^nhl/i.test(await selectedTab(page))) return fail('ROTATE', `the phone shows ${JSON.stringify(await selectedTab(page))}, expected the NHL tab`)
      const column = page.locator(`${MOBILE_MAIN} section`)
      const list = page.locator(`${MOBILE_MAIN} section [data-scroller]`)
      if (!(await sentinelBelow(column, 300))) return fail('ROTATE', 'the NHL list has no "load more" sentinel')
      if (!(await waitFor(async () => (await cardCount(column)) === 60, 3000))) fail('ROTATE', `after 1280 → 390, scrolled to 300px above the end, the list has ${await cardCount(column)} cards, expected 60`)
      const top = () => list.evaluate((el) => el.scrollTop)
      if ((await top()) <= 200) return fail('ROTATE', 'the NHL list did not scroll down')
      // A click: this context (a desktop one) has no touch.
      await phoneTab(page, /^nhl/i).click()
      if (!(await waitFor(async () => (await top()) === 0, 3000))) fail('ROTATE', `after 1280 → 390 a tap on the shown tab left the list at ${await top()}`)

      await page.setViewportSize({ width: 1280, height: 800 })
      if (!(await one('wrapper'))) fail('ROTATE', `after 390 → 1280 data-scroller is on ${JSON.stringify(await scrollers())}, expected only the wrapper`)
    },
  }),
  // ArrowDown past the first 30 cards: the selected card is rendered
  // (the batch grows to reach it), selected and scrolled into view.
  dashFlow('flow-desktop-keys-past-batch', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await withStories(page, moreNhl(67))
    },
    run: async ({ page, fail }) => {
      const grid = await gridLabels(page)
      const at = grid.findIndex((l) => /^nhl$/i.test(l))
      if (at < 0) return fail('KEYS-BATCH', `no NHL column in ${JSON.stringify(grid)}`)
      for (let i = 0; i < at; i++) await page.keyboard.press('ArrowRight')
      for (let i = 0; i < 31; i++) await page.keyboard.press('ArrowDown')
      const nhl = page.locator('main.grid section').filter({ has: page.locator('header h2', { hasText: /^nhl$/i }) })
      const sel = nhl.locator('article[data-card-id][tabindex="0"]')
      if (!(await waitFor(async () => (await sel.count()) === 1, 3000))) return fail('KEYS-BATCH', `after 31 ArrowDowns no NHL card is selected (${await cardCount(nhl)} rendered)`)
      const ids = await nhl.locator('article[data-card-id]').evaluateAll((els) => els.map((el) => el.getAttribute('data-card-id')))
      const id = await sel.getAttribute('data-card-id')
      if (ids.indexOf(id) !== 30) fail('KEYS-BATCH', `the selected card is number ${ids.indexOf(id) + 1} of the column, expected 31`)
      if (ids.length !== 60) fail('KEYS-BATCH', `the column renders ${ids.length} cards, expected 60 (the batch grown to reach the selection)`)
      if (!(await waitFor(() => sel.evaluate((el) => {
        const r = el.getBoundingClientRect()
        return r.top >= 0 && r.bottom <= innerHeight
      }), 3000))) fail('KEYS-BATCH', 'the selected card was not scrolled into view')
    },
  }),
  // --- PR3b: the read model ----------------------------------------------------

  // Opening a story uses it up (D3): from a tab (a tap, or a middle-click
  // on the headline), from search and from the desktop For You row, each
  // open sends one PATCH adding the story's id under its category's seen:
  // key, and the tab's badge drops by one while the list doesn't move. A
  // story opened just before the page hides still goes out, in the hide's
  // keepalive PATCH. The page's clock is held from the tap on News
  // (PR3c): a middle-click scrolls its link into view, and a card read on
  // screen and scrolled off the top on the way would be used up, so the
  // badge would count more than the opens. Each open drops the badge at
  // once; their PATCH goes when the clock moves.
  dashFlow('flow-open-marks-seen', {
    before: async ({ page }) => {
      await installClock(page)
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      if (before.length !== 12) return fail('OPEN-SEEN', `News shows ${before.length} stories above the marker, expected 12`)
      if ((await badge(page, /^news/i)) !== '12') fail('OPEN-SEEN', `the News badge reads ${JSON.stringify(await badge(page, /^news/i))}, expected "12"`)
      const first = before[0]
      await openStory(page, first)
      if (!(await waitFor(async () => (await badge(page, /^news/i)) === '11', 3000))) fail('OPEN-SEEN', `after an open the News badge reads ${JSON.stringify(await badge(page, /^news/i))}, expected "11"`)
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(before)) fail('OPEN-SEEN', `the list moved: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if ((await markerText(page)) !== '11 not seen · Mark as read') fail('OPEN-SEEN', `the end row reads ${JSON.stringify(await markerText(page))}`)
      const log = await harnessLog(page)
      if (!log.opened.some((o) => o.target === '_blank')) fail('OPEN-SEEN', 'the headline opened nothing')

      // A middle-click on a headline opens a background tab with no click
      // event (an auxclick): it uses the story up just the same.
      const third = before[2]
      await page.locator(`${MOBILE_MAIN} article[data-card-id="${third}"] a[aria-label="open in new tab"]`).first().click({ button: 'middle' })
      if (!(await harnessLog(page)).opened.some((o) => o.middle)) fail('OPEN-SEEN', 'the middle-click opened nothing')
      if (!(await waitFor(async () => (await badge(page, /^news/i)) === '10', 3000))) fail('OPEN-SEEN', `after a middle-click the News badge reads ${JSON.stringify(await badge(page, /^news/i))}, expected "10"`)
      // So does a middle-click on a story's Reddit thread (104 has one).
      if (before.slice(0, 3).includes(104)) return fail('OPEN-SEEN', `story 104 is among the first three: ${JSON.stringify(before)}`)
      await page.locator(`${MOBILE_MAIN} article[data-card-id="104"] a[href*="reddit.com"]`).first().click({ button: 'middle' })
      // The click scrolled 104 into view: the app's observer has seen the
      // cards above it leave the top (unread) before the clock moves.
      await observersSettled(page)
      if (!(await harnessLog(page)).opened.some((o) => o.middle && o.url.includes('reddit.com'))) fail('OPEN-SEEN', 'the middle-click on the Reddit thread opened nothing')
      if (!(await waitFor(async () => (await badge(page, /^news/i)) === '9', 3000))) fail('OPEN-SEEN', `after a middle-click on the Reddit thread the News badge reads ${JSON.stringify(await badge(page, /^news/i))}, expected "9"`)
      // prefSync's 1 s batch sends the three marks, and only those: the
      // scroll to 104 took cards off the top that hadn't been read.
      await page.clock.runFor(1100)
      await requestsSettled(page)
      if (!(await waitFor(() => seenAdded(writes, 'news').includes(first), 3000))) return fail('OPEN-SEEN', `opening story ${first} sent no PATCH adding it to seen:news: ${JSON.stringify(writes)}`)
      if (!seenAdded(writes, 'news').includes(third)) fail('OPEN-SEEN', `a middle-click on story ${third}'s headline sent no PATCH adding it to seen:news: ${JSON.stringify(writes)}`)
      if (!seenAdded(writes, 'news').includes(104)) fail('OPEN-SEEN', `a middle-click on story 104's Reddit thread sent no PATCH adding it to seen:news: ${JSON.stringify(writes)}`)
      const opened = sortedIds([first, third, 104])
      if (JSON.stringify(sortedIds(seenAdded(writes, 'news'))) !== JSON.stringify(opened)) fail('OPEN-SEEN', `seen:news got ${JSON.stringify(seenAdded(writes, 'news'))}, expected exactly the opened ${JSON.stringify(opened)}`)

      // From search: the story's own category, whatever the results show.
      // (The search waits 300 ms after the last key, and its open's PATCH
      // goes with the next 1 s batch.)
      await tap(page, page.locator('button[aria-label="open search"]'))
      await page.locator('#app-search').fill('Pettersson')
      await page.clock.runFor(350)
      const result = page.locator('main article[data-card-id="601"]').first()
      await result.waitFor({ state: 'visible' })
      await tap(page, result.locator('a[aria-label="open in new tab"]').first())
      await page.clock.runFor(1100)
      if (!(await waitFor(() => seenAdded(writes, 'nhl').includes(601), 3000))) fail('OPEN-SEEN', `opening a search result sent no PATCH adding 601 to seen:nhl: ${JSON.stringify(writes)}`)
      await tap(page, page.locator('button[aria-label="clear search"]'))
      await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible' })

      // Opened, then the page hides at once: the 1 s batch can't run (the
      // clock is still held), so only the hide's keepalive PATCH carries it.
      const second = before[1]
      if (seenAdded(writes, 'news').includes(second)) return fail('OPEN-SEEN', `story ${second} was marked before it was opened: ${JSON.stringify(writes)}`)
      await openStory(page, second)
      await setHidden(page, true)
      const keptAlive = () =>
        harnessLog(page).then((h) => h.fetches.some((f) => f.method === 'PATCH' && decodeURIComponent(f.path) === '/api/preferences/seen:news' && f.keepalive))
      if (!(await waitFor(keptAlive, 3000))) fail('OPEN-SEEN', 'hiding right after an open sent no keepalive PATCH of seen:news')
      if (!(await waitFor(() => (prefMock(page.context()).value('seen:news') || []).includes(second), 3000))) fail('OPEN-SEEN', `story ${second}, opened as the page hid, never reached the store`)
      await setHidden(page, false)

      // A middle-click on a podcast's Listen uses the episode up too. The
      // clock is still held: Podcast's few cards sit on screen with the
      // marker, and the end rule would use them up a second later, the
      // episode with them. The badge drops by one at the middle-click.
      await tap(page, phoneTab(page, /^podcast/i))
      const episode = await page.locator(`${MOBILE_MAIN} [data-group="unseen"] article[data-card-id]`).filter({ has: page.locator('a[href$=".mp3"]') }).first().getAttribute('data-card-id')
      if (!episode) return fail('OPEN-SEEN', 'no unseen podcast episode with a Listen link')
      const podcastWas = Number(await badge(page, /^podcast/i))
      const podcastNow = podcastWas > 1 ? String(podcastWas - 1) : ''
      await page.locator(`${MOBILE_MAIN} article[data-card-id="${episode}"] a[href$=".mp3"]`).first().click({ button: 'middle' })
      if (!(await waitFor(async () => (await badge(page, /^podcast/i)) === podcastNow, 3000))) fail('OPEN-SEEN', `after a middle-click on episode ${episode}'s Listen the Podcast badge reads ${JSON.stringify(await badge(page, /^podcast/i))}, expected ${JSON.stringify(podcastNow)}`)
      await page.clock.runFor(1100)
      if (!(await waitFor(() => seenAdded(writes, 'podcast').includes(Number(episode)), 3000))) fail('OPEN-SEEN', `a middle-click on episode ${episode}'s Listen sent no PATCH adding it to seen:podcast: ${JSON.stringify(writes)}`)
      if (!(await harnessLog(page)).opened.some((o) => o.middle && o.url.endsWith('.mp3'))) fail('OPEN-SEEN', 'the middle-click on Listen opened nothing')
      await page.clock.resume()

      // The desktop For You row (no marker, no dimming there: C2).
      await page.setViewportSize({ width: 1280, height: 800 })
      const row = page.locator('section').filter({ visible: true, has: page.locator('header h2', { hasText: /^For You$/ }) })
      await row.waitFor({ state: 'visible' })
      await row.locator('article[data-card-id="301"] a[aria-label="open in new tab"]').first().click()
      if (!(await waitFor(() => seenAdded(writes, 'security').includes(301), 3000))) fail('OPEN-SEEN', `opening a card in the desktop For You row sent no PATCH adding 301 to seen:security: ${JSON.stringify(writes)}`)
      await shot('desktop')
    },
  }),

  // A tab's badge: the unseen count, capped at 99+ on screen, the full
  // count in the tab's name for a screen reader ("<tab>, N new"), and
  // none at 0. Saved holds three saves no one has seen, and still has no
  // badge, and no read model at all: no marker, no C8 row, no Earlier, no
  // dimmed card and no Mark all.
  dashFlow('flow-badge-cap', {
    before: async ({ page, context }) => {
      await withStories(page, moreNhl(100))
      prefMock(context).seed({ starred_entries: [601, 602, 603] })
    },
    run: async ({ page, fail }) => {
      const nhl = phoneTab(page, /^nhl/i)
      const name = () => nhl.getAttribute('aria-label')
      if ((await badge(page, /^nhl/i)) !== '99+') fail('BADGE', `the NHL tab's badge reads ${JSON.stringify(await badge(page, /^nhl/i))} with 103 unseen, expected "99+"`)
      if (!/^nhl, 103 new$/i.test((await name()) || '')) fail('BADGE', `the NHL tab is named ${JSON.stringify(await name())}, expected "NHL, 103 new"`)
      if (!/^news, 12 new$/i.test((await phoneTab(page, /^news/i).getAttribute('aria-label')) || '')) fail('BADGE', `the News tab is named ${JSON.stringify(await phoneTab(page, /^news/i).getAttribute('aria-label'))}`)

      // Saved: three unseen saves, no badge, no read model.
      const saved = phoneTab(page, /^saved/i)
      if (!(await waitFor(() => saved.isVisible(), 3000))) return fail('BADGE', 'no Saved tab')
      if ((await saved.locator('[data-badge]').count()) || (await saved.getAttribute('aria-label'))) {
        fail('BADGE', `the Saved tab has a badge ${JSON.stringify(await badge(page, /^saved/i))}, named ${JSON.stringify(await saved.getAttribute('aria-label'))}`)
      }
      await tap(page, saved)
      if (!(await waitFor(async () => (await page.locator(`${MOBILE_MAIN} article[data-card-id]`).count()) === 3, 3000))) return fail('BADGE', `Saved shows ${await page.locator(`${MOBILE_MAIN} article[data-card-id]`).count()} cards, expected its 3 saves`)
      for (const [what, sel] of [['a marker', '[data-caught-up]'], ["C8's row", '[data-next-tab]'], ['Earlier', 'button[data-earlier]'], ['an Earlier group', '[data-group="earlier"]']]) {
        if (await page.locator(`${MOBILE_MAIN} ${sel}`).count()) fail('BADGE', `Saved has ${what}`)
      }
      const opacities = await page.locator(`${MOBILE_MAIN} article[data-card-id]`).evaluateAll((els) => els.map((el) => getComputedStyle(el).opacity))
      if (opacities.some((o) => Number(o) < 0.99)) fail('BADGE', `a card on Saved is dimmed: opacity ${JSON.stringify(opacities)}`)
      // No Mark all: Saved has no ⋯ at all (PR4d).
      if (await page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`).count()) fail('BADGE', 'Saved has a ⋯ (column preferences)')

      await tap(page, nhl)
      const first = (await groupIds(page, 'unseen'))[0]
      await openStory(page, first)
      if (!(await waitFor(async () => /, 102 new$/.test((await name()) || ''), 3000))) fail('BADGE', `after an open the NHL tab is named ${JSON.stringify(await name())}, expected "NHL, 102 new"`)
      if ((await badge(page, /^nhl/i)) !== '99+') fail('BADGE', `after an open the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}`)
      // Mark all: no badge, no count in the name.
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-mark-all]`))
      if (!(await waitFor(async () => (await badge(page, /^nhl/i)) === '' && (await name()) === null, 3000))) fail('BADGE', `after Mark all the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))} and the tab is named ${JSON.stringify(await name())}`)
    },
  }),

  // The end of a tab (D4, D24): with stories still unseen it reads "N not
  // seen · Mark as read". Flung to the end (the page's clock held: no card
  // read on the way), all 40 are unseen; once the last cards have been on
  // screen with the marker for the dwell they are used up (PR3c's end
  // rule), and the flung ones keep "You're all caught up" away: the row
  // counts them. Opening two leaves them in place and counts them out;
  // one tap on the row marks the rest, with Undo, and leaves the focus
  // alone (a finger's press). Undo puts them back above the marker.
  // Marked again from the keyboard with Earlier open, Earlier
  // folds (a new snapshot of the same Column) and the focus is on the
  // marker. Marked, the tab opens on the marker, with Earlier (N) folded
  // under it, also after leaving it and coming back.
  dashFlow('flow-caught-up', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await phoneListToEnd(page))) return fail('CAUGHT-UP', 'the NHL list never finished loading')
      const all = await groupIds(page, 'unseen')
      if (all.length !== 40) return fail('CAUGHT-UP', `the NHL tab draws ${all.length} stories, expected 40`)
      if ((await markerText(page)) !== '40 not seen · Mark as read') fail('CAUGHT-UP', `the end row reads ${JSON.stringify(await markerText(page))}`)
      if (await page.locator(PHONE_EARLIER).count()) fail('CAUGHT-UP', 'Earlier shows with nothing seen')
      if (await page.locator(`${MOBILE_MAIN} [data-next-tab]`).count()) fail('CAUGHT-UP', "C8's row shows while stories are unseen")
      const rowBox = await page.locator(PHONE_MARKER).boundingBox()
      if (!rowBox || rowBox.height < 43.5) fail('CAUGHT-UP', `the end row is ${rowBox && rowBox.height}px tall`)
      if (await page.locator(`${PHONE_LIST} [role="status"]`).count()) fail('CAUGHT-UP', 'a row in the list is role="status"')

      // The end rule (PR3c): the cards on screen with the marker for the
      // dwell are used up; every card flung past stays unseen. (The last
      // batch went in below the scroll position: to the end again.)
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, el.scrollHeight))
      await observersSettled(page)
      const last = (await listCards(page)).half
      if (!last.length || last.length > 6) return fail('CAUGHT-UP', `at the end ${JSON.stringify(last)} are on screen`)
      await dwell(page)
      await page.clock.runFor(SEEN_BATCH_MS + 50)
      const left = 40 - last.length
      const flungRow = `${left} not seen · Mark as read`
      if (!(await waitFor(async () => (await markerText(page)) === flungRow, 3000))) fail('CAUGHT-UP', `with ${JSON.stringify(last)} read at the end, the end row reads ${JSON.stringify(await markerText(page))}, expected ${JSON.stringify(flungRow)}`)
      if (/caught up/.test(await markerText(page))) fail('CAUGHT-UP', "the cards flung past didn't keep \"You're all caught up\" away")
      if ((await badge(page, /^nhl/i)) !== String(left)) fail('CAUGHT-UP', `the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "${left}"`)
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(all)) fail('CAUGHT-UP', 'the end rule moved the list')
      await shot('flung')

      // Two opened: they stay where they are and the row counts them out.
      await openStory(page, all[0])
      await openStory(page, all[1])
      const afterOpens = `${left - 2} not seen · Mark as read`
      if (!(await waitFor(async () => (await markerText(page)) === afterOpens, 3000))) fail('CAUGHT-UP', `after two opens the end row reads ${JSON.stringify(await markerText(page))}, expected ${JSON.stringify(afterOpens)}`)
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(all)) fail('CAUGHT-UP', 'opening stories moved the list')
      await shot('not-seen')

      // One tap on the row marks the rest, with Undo.
      const rest = all.slice(2).filter((id) => !last.includes(id))
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, el.scrollHeight))
      await tap(page, page.locator(PHONE_MARKER))
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 0, 3000))) return fail('CAUGHT-UP', `after Mark as read ${(await groupIds(page, 'unseen')).length} stories are still above the marker`)
      if (!/^✓\s*You.re all caught up$/.test(await markerText(page))) fail('CAUGHT-UP', `after Mark as read the marker reads ${JSON.stringify(await markerText(page))}`)
      if (!(await page.getByText(`Marked ${rest.length} as read`, { exact: true }).isVisible())) fail('CAUGHT-UP', `no "Marked ${rest.length} as read" toast`)
      // No card above the marker now: the writes can go.
      await page.clock.runFor(1100)
      const sameSet = (a, b) => JSON.stringify(sortedIds(a)) === JSON.stringify(sortedIds(b))
      if (!(await waitFor(() => sameSet(seenAdded(writes, 'nhl'), all), 3000))) return fail('CAUGHT-UP', `seen:nhl got ${JSON.stringify(seenAdded(writes, 'nhl'))}, expected all 40`)
      // A tap is a finger's press, not the keyboard's or a screen
      // reader's: Chromium focused the row on the tap (an iPhone doesn't),
      // but the focus is not handed to the marker in its place.
      if ((await focusedNow(page)) === 'DIV[data-caught-up]') fail('CAUGHT-UP', 'after a tap on Mark as read the focus went to the marker')
      await tap(page, page.getByRole('button', { name: 'Undo', exact: true }))
      // The tab starts over at its first 30 (the clock moves only to send
      // the Undo; the cards then on screen at the top are read).
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length > 0, 3000))) return fail('CAUGHT-UP', 'Undo brought nothing back above the marker')
      await page.clock.runFor(1100)
      if (!(await waitFor(() => sameSet(seenRemoved(writes, 'nhl'), rest), 3000))) fail('CAUGHT-UP', `Undo removed ${JSON.stringify(seenRemoved(writes, 'nhl'))}, expected exactly the ${rest.length} it added`)
      await phoneListToEnd(page)
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify(rest), 3000))) fail('CAUGHT-UP', `after Undo the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}, expected the ${rest.length} marked`)
      const earlierAfterUndo = await textNow(page, PHONE_EARLIER)
      if (earlierAfterUndo !== `Earlier (${2 + last.length})`) fail('CAUGHT-UP', `after Undo Earlier reads ${JSON.stringify(earlierAfterUndo)}, expected the two opened and the ${last.length} read at the end`)

      // Earlier opened, then marked again from the keyboard: Earlier folds
      // with the new snapshot (the same Column, no tab switch), and the
      // focus goes to the marker, not to <body>.
      await tap(page, page.locator(PHONE_EARLIER))
      if (!(await waitFor(async () => (await groupIds(page, 'earlier')).length === 2 + last.length, 3000))) fail('CAUGHT-UP', `Earlier (${2 + last.length}) opened shows ${(await groupIds(page, 'earlier')).length} cards`)
      await phoneListToEnd(page)
      await page.locator(PHONE_MARKER).focus()
      await page.keyboard.press('Enter')
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 0, 3000))) return fail('CAUGHT-UP', 'the second Mark as read left stories above the marker')
      if (!(await waitFor(async () => (await focusedNow(page)) === 'DIV[data-caught-up]', 1000))) fail('CAUGHT-UP', `after Mark as read from the keyboard the focus is on ${await focusedNow(page)}, expected the marker`)
      if ((await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')) !== 'false' || (await groupIds(page, 'earlier')).length) {
        fail('CAUGHT-UP', `after the second Mark as read Earlier is expanded ${await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')} with ${(await groupIds(page, 'earlier')).length} cards drawn, expected it folded`)
      }

      // Then away to News and back: the tab opens on the marker, Earlier
      // (40) folded under it.
      await tap(page, phoneTab(page, /^news/i))
      await tap(page, phoneTab(page, /^nhl/i))
      await page.locator(PHONE_MARKER).waitFor({ state: 'visible' })
      const first = await page.locator(PHONE_LIST).evaluate((el) => (el.firstElementChild && el.firstElementChild.hasAttribute('data-caught-up')) || false)
      if (!first) fail('CAUGHT-UP', 'back on the tab, the marker is not the first thing in the list')
      const earlier = page.locator(PHONE_EARLIER)
      if (((await earlier.textContent()) || '').trim() !== 'Earlier (40)' || (await earlier.getAttribute('aria-expanded')) !== 'false') {
        fail('CAUGHT-UP', `back on the tab Earlier reads ${JSON.stringify(await earlier.textContent())}, expanded ${await earlier.getAttribute('aria-expanded')}`)
      }
      if ((await badge(page, /^nhl/i)) !== '') fail('CAUGHT-UP', `the NHL badge still reads ${JSON.stringify(await badge(page, /^nhl/i))}`)
      await shot('caught-up')

      // PR3c: the end rule using up the last cards while the focus is on
      // the end row. The row turns into "You're all caught up" (a new
      // element) and the focus goes on to it, not to <body>. Podcast: its
      // two unseen episodes fit on screen with the end row.
      await tap(page, phoneTab(page, /^podcast/i))
      if (!(await waitFor(async () => /^podcast/i.test(await selectedTab(page)), 3000))) return fail('CAUGHT-UP', 'could not select the Podcast tab')
      await observersSettled(page)
      const episodes = await groupIds(page, 'unseen')
      const episodesShown = (await listCards(page)).half
      if (!episodes.length || JSON.stringify(episodesShown) !== JSON.stringify(episodes)) return fail('CAUGHT-UP', `the Podcast tab shows ${JSON.stringify(episodesShown)} of its unseen ${JSON.stringify(episodes)}`)
      if (!/not seen/.test(await markerText(page))) return fail('CAUGHT-UP', `the Podcast end row reads ${JSON.stringify(await markerText(page))}`)
      await page.locator(PHONE_MARKER).focus()
      if ((await focusedNow(page)) !== 'BUTTON[data-caught-up]') return fail('CAUGHT-UP', `the Podcast end row took no focus: ${await focusedNow(page)}`)
      await dwell(page)
      await page.clock.runFor(SEEN_BATCH_MS + 50)
      if (!(await waitFor(async () => /You.re all caught up/.test(await markerText(page)), 3000))) return fail('CAUGHT-UP', `with ${JSON.stringify(episodes)} on screen with the end row for the dwell, it reads ${JSON.stringify(await markerText(page))}, expected "You're all caught up"`)
      if ((await focusedNow(page)) !== 'DIV[data-caught-up]') fail('CAUGHT-UP', `the end rule turned the focused end row into "You're all caught up" and the focus is on ${await focusedNow(page)}, expected the marker`)
    },
  }),

  // C8: under a caught-up tab's marker, one 44px row names the next shown
  // tab with stories not seen yet and opens it (2 taps from the strip). A
  // hidden tab is never named, and with every tab at 0 the row is gone.
  // The strip without a stored order: … Deals, News, Nhl, Podcast,
  // Security, Video. Podcast holds 2 stories from the last 48 h; its
  // other 3 count as seen (D33).
  dashFlow('flow-next-tab', {
    before: async ({ context }) => {
      prefMock(context).seed({ 'seen:nhl': idsOf('nhl') })
    },
    run: async ({ page, fail, shot }) => {
      const next = page.locator(`${MOBILE_MAIN} [data-next-tab]`)
      const nextText = () => textNow(page, `${MOBILE_MAIN} [data-next-tab]`)
      // A reload with `rows` in the store; the shown tab may have no card
      // above its marker, so wait for the strip.
      const reloadWith = async (rows) => {
        prefMock(page.context()).seed(rows)
        await page.reload()
        await page.locator(`${STRIP} [aria-selected="true"]`).waitFor({ state: 'visible' })
        await waitFor(() => answered(page, 'GET', '/api/preferences'), 3000)
      }
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(() => next.isVisible(), 3000))) return fail('NEXT-TAB', 'no next-tab row under the caught-up NHL tab')
      if (!/You.re all caught up/.test(await markerText(page))) fail('NEXT-TAB', `the NHL marker reads ${JSON.stringify(await markerText(page))}`)
      const podcast = await badge(page, /^podcast/i)
      if ((await nextText()) !== `Podcast · ${podcast}` || podcast !== '2') fail('NEXT-TAB', `the row reads ${JSON.stringify(await nextText())}, expected "Podcast · 2"`)
      // Named like the tab in the strip, for VoiceOver.
      if ((await next.getAttribute('aria-label')) !== 'Podcast, 2 new') fail('NEXT-TAB', `the row is named ${JSON.stringify(await next.getAttribute('aria-label'))}, expected "Podcast, 2 new"`)
      const box = await next.boundingBox()
      if (!box || box.height < 43.5) fail('NEXT-TAB', `the row is ${box && box.height}px tall`)
      await shot('row')
      await tap(page, next)
      if (!/^podcast/i.test(await selectedTab(page))) fail('NEXT-TAB', `the row opened ${JSON.stringify(await selectedTab(page))}, expected Podcast`)
      if ((await storedTab(page)) !== 'cat:podcast') fail('NEXT-TAB', `the stored tab is ${JSON.stringify(await storedTab(page))}`)
      taps(page, 2, 'NEXT-TAB')

      // Podcast hidden: the row skips it.
      await reloadWith({ category_layout: { v: 1, order: [], hidden: ['cat:podcast'] } })
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(() => next.isVisible(), 3000))) return fail('NEXT-TAB', 'no next-tab row with Podcast hidden')
      const security = await badge(page, /^security/i)
      if ((await nextText()) !== `Security · ${security}` || !security) fail('NEXT-TAB', `with Podcast hidden the row reads ${JSON.stringify(await nextText())}, expected "Security · ${security}"`)

      // Every tab at 0: no row, so the end of the tab is the end.
      const everything = {}
      for (const cat of ['news', 'deals', 'security', 'podcast', 'video', 'nhl']) everything[`seen:${cat}`] = idsOf(cat)
      await reloadWith({ ...everything, category_layout: { v: 1, order: [], hidden: [] } })
      await tap(page, phoneTab(page, /^nhl/i))
      await page.locator(PHONE_MARKER).waitFor({ state: 'visible' })
      await sleep(300)
      if (await next.count()) fail('NEXT-TAB', `with every tab at 0 the row still reads ${JSON.stringify(await nextText())}`)
      if (await page.locator(`${STRIP} [data-badge]`).count()) fail('NEXT-TAB', 'a tab still has a badge with everything seen')
      await shot('end')
    },
  }),

  // ⋯ → Mark all as read (2 taps from the top of a tab): the tab's stories
  // go under the marker, folded, with one PATCH per seen: key and
  // "Marked N as read" with Undo, and the focus goes back to ⋯ (tapped,
  // and from the keyboard). Undo removes exactly those marks and puts the
  // stories back above the marker, in their order. At 0 the row is
  // disabled.
  dashFlow('flow-mark-all-read', {
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      const more = page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`)
      const markAll = page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-mark-all]`)
      await tap(page, more)
      await markAll.waitFor({ state: 'visible' })
      const firstRow = await page.locator(`${MOBILE_MAIN} [data-column-popover]`).evaluate((el) => !!el.firstElementChild && el.firstElementChild.hasAttribute('data-mark-all'))
      if (!firstRow) fail('MARK-ALL', "Mark all as read is not the ⋯'s first row")
      const box = await markAll.boundingBox()
      if (!box || box.height < 43.5) fail('MARK-ALL', `Mark all as read is ${box && box.height}px tall`)
      await tap(page, markAll)
      taps(page, 3, 'MARK-ALL')
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 0, 3000))) return fail('MARK-ALL', 'Mark all left stories above the marker')
      // The row held the focus and went with the popover: the focus is
      // back on ⋯, not on <body>.
      const onMore = 'BUTTON[data-column-action][aria-label="column preferences"]'
      if ((await focusedNow(page)) !== onMore) fail('MARK-ALL', `after Mark all the focus is on ${await focusedNow(page)}, expected ⋯`)
      const earlier = page.locator(PHONE_EARLIER)
      if (((await earlier.textContent()) || '').trim() !== `Earlier (${before.length})` || (await earlier.getAttribute('aria-expanded')) !== 'false') {
        fail('MARK-ALL', `after Mark all Earlier reads ${JSON.stringify(await earlier.textContent())}, expanded ${await earlier.getAttribute('aria-expanded')}`)
      }
      if ((await badge(page, /^news/i)) !== '') fail('MARK-ALL', `after Mark all the News badge reads ${JSON.stringify(await badge(page, /^news/i))}`)
      if (!(await page.getByText(`Marked ${before.length} as read`, { exact: true }).isVisible())) fail('MARK-ALL', `no "Marked ${before.length} as read" toast`)
      const sorted = (a) => JSON.stringify([...a].sort((x, y) => x - y))
      if (!(await waitFor(() => sorted(seenAdded(writes, 'news')) === sorted(before), 3000))) return fail('MARK-ALL', `seen:news got ${JSON.stringify(seenAdded(writes, 'news'))}`)

      // Undo while its toast is up (5 s): no screenshot before the tap,
      // which with --out can take longer than that under load.
      await tap(page, page.getByRole('button', { name: 'Undo', exact: true }))
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify(before), 3000))) fail('MARK-ALL', `after Undo the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}, expected ${JSON.stringify(before)}`)
      if (await page.locator(PHONE_EARLIER).count()) fail('MARK-ALL', 'after Undo Earlier is still there')
      if ((await badge(page, /^news/i)) !== String(before.length)) fail('MARK-ALL', `after Undo the News badge reads ${JSON.stringify(await badge(page, /^news/i))}`)
      if (!(await waitFor(() => sorted(seenRemoved(writes, 'news')) === sorted(before), 3000))) fail('MARK-ALL', `Undo removed ${JSON.stringify(seenRemoved(writes, 'news'))}, expected exactly what Mark all added`)
      if (await page.getByText('Column restored.').count()) fail('MARK-ALL', 'Undo showed a second toast')

      // Marked again, from the keyboard (focus back on ⋯ again); at 0 the
      // ⋯'s row is disabled.
      await more.focus()
      await page.keyboard.press('Enter')
      await markAll.waitFor({ state: 'visible' })
      await markAll.focus()
      await page.keyboard.press('Enter')
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 0, 3000))) return fail('MARK-ALL', 'the second Mark all left stories above the marker')
      if ((await focusedNow(page)) !== onMore) fail('MARK-ALL', `after Mark all from the keyboard the focus is on ${await focusedNow(page)}, expected ⋯`)
      // The marked tab and its toast; no Undo follows this one.
      await shot('marked')
      await tap(page, more)
      await markAll.waitFor({ state: 'visible' })
      if (!(await markAll.isDisabled())) fail('MARK-ALL', 'Mark all as read is enabled with nothing left to mark')
    },
  }),

  // A story that arrives with a poll goes at the end of the unseen ones
  // (not the top), and counts at once. While the marker is above the
  // screen (Earlier open and read past it) the next one waits, counted,
  // so nothing is pushed down under the reader; back at the top, it goes
  // in at the end. Meanwhile the rows under the marker stay as drawn
  // (scroll anchoring off, as on Safari): another tab's first story
  // doesn't bring C8's row in, and this tab's arrival doesn't turn
  // "You're all caught up" into "1 not seen" or take C8's row away,
  // until the marker is back on screen. The page's clock is held: the
  // unseen cards sit on screen with the marker, and on the phone they
  // would be used up a second later (PR3c's end rule).
  dashFlow('flow-new-arrival-appends', {
    before: async ({ page, context }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
      await withArrivals(page)
      // Every NHL story but the two newest is seen: Earlier holds 38.
      const ids = [...idsOf('nhl'), ...moreNhl(37).map((e) => e.id)]
      prefMock(context).seed({ 'seen:nhl': ids.filter((id) => id !== 601 && id !== 602) })
    },
    run: async ({ page, fail, shot }) => {
      const poll = async (story) => {
        await page.evaluate((st) => window.__arrivals.push(st), story)
        const landed = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries')
        await startPoll(page)
        await landed
        await sleep(400)
      }
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (JSON.stringify(await groupIds(page, 'unseen')) !== '[601,602]') return fail('ARRIVAL', `the NHL tab opens with ${JSON.stringify(await groupIds(page, 'unseen'))} above the marker`)
      await poll(arrivalNhl(7901))
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === '[601,602,7901]', 3000))) return fail('ARRIVAL', `after an arrival the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}, expected the newest at the end`)
      if ((await badge(page, /^nhl/i)) !== '3') fail('ARRIVAL', `the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "3"`)
      if ((await markerText(page)) !== '3 not seen · Mark as read') fail('ARRIVAL', `the end row reads ${JSON.stringify(await markerText(page))}`)
      await shot('arrived')

      // Earlier open, scrolled so the marker is 300px above the screen.
      await tap(page, page.locator(PHONE_EARLIER))
      if (!(await waitFor(async () => (await groupIds(page, 'earlier')).length > 0, 3000))) return fail('ARRIVAL', 'Earlier did not open')
      const list = page.locator(PHONE_LIST)
      await list.evaluate((el) => {
        const m = el.querySelector('[data-caught-up]')
        el.scrollTop += m.getBoundingClientRect().bottom - el.getBoundingClientRect().top + 300
      })
      if (!(await markerAbove(list))) return fail('ARRIVAL', 'scrolled 300px past the marker, the app never saw it above the screen')
      const anchor = await list.evaluate((el) => {
        const top = el.getBoundingClientRect().top
        const card = [...el.querySelectorAll('[data-group="earlier"] article[data-card-id]')].find((a) => a.getBoundingClientRect().bottom > top + 1)
        return card ? { id: card.getAttribute('data-card-id'), top: card.getBoundingClientRect().top } : null
      })
      if (!anchor) return fail('ARRIVAL', 'no Earlier card on screen')
      await poll(arrivalNhl(7902))
      if (JSON.stringify(await groupIds(page, 'unseen')) !== '[601,602,7901]') fail('ARRIVAL', `with the marker above the screen the arrival went in: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if ((await badge(page, /^nhl/i)) !== '4') fail('ARRIVAL', `the held arrival isn't counted: the badge reads ${JSON.stringify(await badge(page, /^nhl/i))}`)
      const moved = await list.evaluate((el, id) => el.querySelector(`article[data-card-id="${id}"]`).getBoundingClientRect().top, anchor.id)
      if (Math.abs(moved - anchor.top) > 1) fail('ARRIVAL', `the card being read moved from ${anchor.top} to ${moved}`)
      await shot('held')

      await list.evaluate((el) => el.scrollTo(0, 0))
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === '[601,602,7901,7902]', 3000))) fail('ARRIVAL', `back at the top the held arrival did not go in at the end: ${JSON.stringify(await groupIds(page, 'unseen'))}`)

      // The rows between the marker and Earlier stay put too while the
      // marker is above the screen, with scroll anchoring off as on
      // Safari. NHL caught up, every other tab at 0: no C8 row.
      const everything = { 'seen:nhl': [...idsOf('nhl'), ...moreNhl(37).map((e) => e.id)] }
      for (const cat of ['news', 'deals', 'security', 'podcast', 'video']) everything[`seen:${cat}`] = idsOf(cat)
      prefMock(page.context()).seed(everything)
      await page.reload()
      await page.locator(`${STRIP} [aria-selected="true"]`).waitFor({ state: 'visible' })
      await waitFor(() => answered(page, 'GET', '/api/preferences'), 3000)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(async () => /You.re all caught up/.test(await markerText(page)), 3000))) return fail('ARRIVAL', `the caught-up NHL tab's marker reads ${JSON.stringify(await markerText(page))}`)
      const next = page.locator(`${MOBILE_MAIN} [data-next-tab]`)
      if (await next.count()) return fail('ARRIVAL', `with every tab at 0 C8's row reads ${JSON.stringify(await next.textContent())}`)
      await tap(page, page.locator(PHONE_EARLIER))
      if (!(await waitFor(async () => (await groupIds(page, 'earlier')).length >= 30, 3000))) return fail('ARRIVAL', 'Earlier did not open on the caught-up tab')
      // The marker 300px above the screen; the first Earlier card on
      // screen is the one being read.
      const above = () =>
        list.evaluate((el) => {
          el.style.overflowAnchor = 'none'
          const m = el.querySelector('[data-caught-up]')
          el.scrollTop += m.getBoundingClientRect().bottom - el.getBoundingClientRect().top + 300
        })
      const reading = () =>
        list.evaluate((el) => {
          const top = el.getBoundingClientRect().top
          const card = [...el.querySelectorAll('[data-group="earlier"] article[data-card-id]')].find((a) => a.getBoundingClientRect().top > top + 1)
          return card ? { id: card.getAttribute('data-card-id'), top: card.getBoundingClientRect().top } : null
        })
      const stayed = async (was, what) => {
        const now = await list.evaluate((el, id) => el.querySelector(`article[data-card-id="${id}"]`).getBoundingClientRect().top, was.id)
        if (Math.abs(now - was.top) > 1) fail('ARRIVAL', `${what}: the Earlier card being read moved from ${was.top} to ${now}`)
      }
      await above()
      if (!(await markerAbove(list))) return fail('ARRIVAL', 'on the caught-up tab the app never saw the marker above the screen')
      let read = await reading()
      if (!read) return fail('ARRIVAL', 'no Earlier card on screen')
      // Another tab's first story: C8's row would come in above the reader.
      await poll(arrival(7911, 103))
      if (!(await waitFor(async () => (await badge(page, /^news/i)) === '1', 3000))) fail('ARRIVAL', `the News arrival isn't counted: the News badge reads ${JSON.stringify(await badge(page, /^news/i))}`)
      await stayed(read, 'with the marker above the screen a News story arrived')
      if (await next.count()) fail('ARRIVAL', "with the marker above the screen C8's row came in")
      await shot('end-rows-held')
      await list.evaluate((el) => el.scrollTo(0, 0))
      if (!(await waitFor(async () => (await textNow(page, `${MOBILE_MAIN} [data-next-tab]`)) === 'News · 1', 3000))) return fail('ARRIVAL', `back at the top C8's row reads ${JSON.stringify(await textNow(page, `${MOBILE_MAIN} [data-next-tab]`))}, expected "News · 1"`)

      // This tab's own arrival, held: the count goes from 0 to 1, which
      // would turn the marker into "1 not seen" and take C8's row away.
      await above()
      if (!(await markerAbove(list))) return fail('ARRIVAL', 'back past the marker, the app never saw it above the screen')
      read = await reading()
      if (!read) return fail('ARRIVAL', 'no Earlier card on screen')
      await poll(arrivalNhl(7912))
      if (!(await waitFor(async () => (await badge(page, /^nhl/i)) === '1', 3000))) fail('ARRIVAL', `the held NHL arrival isn't counted: the badge reads ${JSON.stringify(await badge(page, /^nhl/i))}`)
      if ((await groupIds(page, 'unseen')).length) fail('ARRIVAL', `with the marker above the screen the arrival went in: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      await stayed(read, 'with the marker above the screen an NHL story arrived')
      if (!(await next.count())) fail('ARRIVAL', "with the marker above the screen C8's row went")
      await list.evaluate((el) => el.scrollTo(0, 0))
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === '[7912]', 3000))) fail('ARRIVAL', `back at the top the held arrival did not go in: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if (!(await waitFor(async () => (await markerText(page)) === '1 not seen · Mark as read' && !(await next.count()), 3000))) fail('ARRIVAL', `back at the top the end reads ${JSON.stringify(await markerText(page))} with C8's row ${(await next.count()) ? 'still there' : 'gone'}`)
    },
  }),

  // A story that arrives when the unseen cards drawn fill whole batches
  // (30 of 30): once every unseen card has been drawn the list grows with
  // the arrival at once, so the marker and an open Earlier never leave
  // the page for a frame while a batch waits for the "load more"
  // sentinel (and the list never jumps when they come back). The page's
  // clock is held: the last cards sit on screen with the marker (PR3c's
  // end rule would use them up a second later).
  dashFlow('flow-arrival-batch-edge', {
    before: async ({ page, context }) => {
      await installClock(page)
      await withStories(page, moreNhl(30))
      await withArrivals(page)
      // 33 NHL stories, the three oldest seen: 30 unseen, Earlier (3).
      prefMock(context).seed({ 'seen:nhl': moreNhl(30).slice(-3).map((e) => e.id) })
    },
    run: async ({ page, fail, shot }) => {
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 30 && (await markerText(page)) === '30 not seen · Mark as read', 3000))) {
        return fail('BATCH-EDGE', `the NHL tab draws ${(await groupIds(page, 'unseen')).length} unseen cards and ends with ${JSON.stringify(await markerText(page))}, expected 30 and the marker`)
      }
      await tap(page, page.locator(PHONE_EARLIER))
      if (!(await waitFor(async () => (await groupIds(page, 'earlier')).length === 3, 3000))) return fail('BATCH-EDGE', 'Earlier (3) did not open')
      // The marker on screen, so the arrival isn't held.
      await page.locator(PHONE_MARKER).evaluate((el) => el.scrollIntoView({ block: 'center' }))
      await sleep(400)
      await page.locator(PHONE_LIST).evaluate((list) => {
        window.__lost = []
        new MutationObserver(() => {
          if (!list.querySelector('[data-caught-up]')) window.__lost.push('the marker')
          if (!list.querySelector('[data-group="earlier"]')) window.__lost.push('Earlier')
        }).observe(list, { childList: true, subtree: true })
      })
      await page.evaluate((st) => window.__arrivals.push(st), arrivalNhl(7921))
      const landed = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries')
      await startPoll(page)
      await landed
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 31, 3000))) return fail('BATCH-EDGE', `after an arrival the tab draws ${(await groupIds(page, 'unseen')).length} unseen cards, expected 31`)
      await sleep(400)
      const ids = await groupIds(page, 'unseen')
      if (ids[ids.length - 1] !== 7921) fail('BATCH-EDGE', `the arrival is not drawn last: ${JSON.stringify(ids.slice(-3))}`)
      const lost = await page.evaluate(() => [...new Set(window.__lost)])
      if (lost.length) fail('BATCH-EDGE', `${lost.join(' and ')} left the page when the 31st story arrived`)
      if ((await markerText(page)) !== '31 not seen · Mark as read') fail('BATCH-EDGE', `the end row reads ${JSON.stringify(await markerText(page))}`)
      if (await page.locator(`${MOBILE_MAIN} [data-more]`).count()) fail('BATCH-EDGE', 'a "load more" sentinel came back for the arrival')
      await shot('arrived')
    },
  }),

  // Earlier opens when the keyboard selection moves into it. A new
  // snapshot (`r`) folds it again with the selection inside, and so does
  // its button; either way the selection goes with it, so `h`, `b`, `s`
  // and Enter can't act on a card that isn't drawn (Enter reaches the
  // focused Earlier button instead), and the next ArrowDown starts at
  // the column's first card.
  dashFlow('flow-earlier-keys-fold', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page, context }) => {
      prefMock(context).seed({ 'seen:nhl': [602, 603] })
      await recordInteractions(page)
    },
    run: async ({ page, fail }) => {
      const grid = await gridLabels(page)
      const at = grid.findIndex((l) => /^nhl$/i.test(l))
      if (at < 0) return fail('EARLIER-KEYS', `no NHL column in ${JSON.stringify(grid)}`)
      const nhl = page.locator('main.grid section').filter({ has: page.locator('header h2', { hasText: /^nhl$/i }) })
      const button = nhl.locator('button[data-earlier]')
      const expanded = () => button.getAttribute('aria-expanded')
      const drawn = () => nhl.locator('[data-group="earlier"] article[data-card-id]').count()
      const selected = () => nhl.locator('article[data-card-id][tabindex="0"]').evaluateAll((els) => els.map((el) => ({ id: el.getAttribute('data-card-id'), earlier: !!el.closest('[data-group="earlier"]') })))
      const intoEarlier = async (what) => {
        if (!(await waitFor(async () => { const s = await selected(); return s.length === 1 && s[0].earlier }, 3000))) {
          fail('EARLIER-KEYS', `${what} selected ${JSON.stringify(await selected())}, expected Earlier's first card`)
          return false
        }
        if ((await expanded()) !== 'true') fail('EARLIER-KEYS', `${what}: the selection moved into Earlier and it stayed folded`)
        return true
      }
      // Folded, nothing of Earlier is drawn, so whether the selection went
      // with it can't be read off the page (a selected card that isn't
      // drawn has no tabindex either): the steps after each fold show it
      // by what the keys do.
      const folded = async (what) => {
        if (!(await waitFor(async () => (await expanded()) === 'false' && (await drawn()) === 0, 3000))) {
          fail('EARLIER-KEYS', `${what} with the selection inside left Earlier expanded ${await expanded()} with ${await drawn()} cards drawn, expected it folded`)
        }
      }
      // With no selection the next ArrowDown starts at the column's first
      // card (still on the card in Earlier, it would go on to 603).
      const downTo601 = async (what) => {
        await page.keyboard.press('ArrowDown')
        if (!(await waitFor(async () => JSON.stringify(await selected()) === '[{"id":"601","earlier":false}]', 3000))) fail('EARLIER-KEYS', `${what} ArrowDown selected ${JSON.stringify(await selected())}, expected the column's first card, 601`)
      }
      await button.waitFor({ state: 'visible' })
      if ((await expanded()) !== 'false') fail('EARLIER-KEYS', 'Earlier (2) starts open')
      for (let i = 0; i < at; i++) await page.keyboard.press('ArrowRight')
      // The one unseen card, then Earlier's first.
      await page.keyboard.press('ArrowDown')
      await page.keyboard.press('ArrowDown')
      if (!(await intoEarlier('two ArrowDowns'))) return

      // Refresh with nothing new: a new snapshot, which folds Earlier
      // (the selection inside didn't move, so it doesn't open it again).
      await page.keyboard.press('r')
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('Nothing new', { exact: true }).isVisible(), 3000))) fail('EARLIER-KEYS', 'r showed no "Nothing new"')
      await folded('r')
      await downTo601('after the fold')
      if ((await expanded()) !== 'false') fail('EARLIER-KEYS', 'ArrowDown onto the unseen card opened Earlier')

      // Into Earlier again, then its button folds it.
      await page.keyboard.press('ArrowDown')
      if (!(await intoEarlier('ArrowDown from 601'))) return
      // No shortcut reaches the card that went with the fold, not even an
      // `h` sent in the same task as the click (straight after React's
      // own flush of it): only a drop made in that flush, from a layout
      // effect, is in time for it.
      const sent = page._events.length
      await button.evaluate((b) => {
        b.click()
        queueMicrotask(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'h', bubbles: true })))
      })
      await folded('the Earlier button')
      for (const key of ['b', 's']) await page.keyboard.press(key)
      await sleep(400)
      const acted = page._events.slice(sent)
      if (acted.length) fail('EARLIER-KEYS', `with Earlier folded, h, b and s sent ${JSON.stringify(acted)}`)
      const label = ((await button.textContent()) || '').trim()
      if (label !== 'Earlier (2)') fail('EARLIER-KEYS', `with Earlier folded, h made it ${JSON.stringify(label)}`)
      if (await page.locator('[role="status"]').getByText(/^(Hidden|Removed from Saved)/).count()) fail('EARLIER-KEYS', 'with Earlier folded, h or b showed a toast')
      // Enter is the focused Earlier button's own again.
      await button.focus()
      await page.keyboard.press('Enter')
      if (!(await waitFor(async () => (await expanded()) === 'true' && (await drawn()) === 2, 3000))) return fail('EARLIER-KEYS', `Enter on the focused Earlier button left it expanded ${await expanded()} with ${await drawn()} cards drawn`)
      // s didn't open the summary of the card that went with the fold.
      if (await nhl.locator('[data-group="earlier"] button[aria-label="hide summary"]').count()) fail('EARLIER-KEYS', "with Earlier folded, s opened a story's summary")
      // And the button's fold dropped the selection.
      await downTo601('after the Earlier button and Enter')
    },
  }),

  // C1: back after 10 minutes or more away, every tab starts over (what
  // was opened goes under the marker, Earlier folded, and a story that
  // came in while away is sorted in first) and the list is at its top.
  // Under 10 minutes nothing moves, and nothing moves either when the
  // page hid right after opening a story, however long it was away. When
  // the return's refresh is slow, a story opened, a scroll, a finger on
  // the glass, a click alone, a key or the wheel before it lands keeps
  // the list as it is: the late stories go at the end.
  dashFlow('flow-return-after-10-min', {
    before: async ({ page }) => {
      await installClock(page)
      await withArrivals(page)
    },
    run: async ({ page, fail, shot }) => {
      // The entries request after `gate()` waits until `gate.open()`, as
      // a resumed iPhone's does: its refresh always takes longer than a
      // frame.
      let held = null
      let waiting = false
      const gate = () => {
        waiting = false
        held = new Promise((resolve) => {
          gate.open = resolve
        })
      }
      await page.route(
        (u) => new URL(u).pathname === '/api/entries',
        async (route) => {
          const wait = held
          held = null
          if (wait) {
            waiting = true
            await wait
          }
          return route.fallback()
        },
      )
      const land = async () => {
        const landed = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries')
        gate.open()
        await landed
        await sleep(600)
      }

      await tap(page, phoneTab(page, /^news/i))
      const list = page.locator(PHONE_LIST)
      const scrollTop = () => list.evaluate((el) => el.scrollTop)
      const before = await groupIds(page, 'unseen')
      const first = before[0]
      await openStory(page, first)
      // Past the 2 s open guard (the 1 s batch sends the mark meanwhile).
      await page.clock.runFor(3000)
      if (!(await waitFor(() => (prefMock(page.context()).value('seen:news') || []).includes(first), 3000))) return fail('RETURN', 'the open never reached the store')
      await list.evaluate((el) => el.scrollTo(0, 400))
      // Every card on screen was read for those 3 s, so the ones this
      // takes off the top are used up (PR3c): the reset below puts them
      // under Earlier with the opened one.
      await observersSettled(page)
      const readOff = (await listCards(page)).above.filter((id) => id !== first)

      await away(page, 9)
      await sleep(800)
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(before)) fail('RETURN', 'back after 9 min the list moved')
      if (Math.abs((await scrollTop()) - 400) > 2) fail('RETURN', `back after 9 min the list is at ${await scrollTop()}, expected 400`)

      // A News story arrives while the app is away: the return's refresh
      // brings it, and the tab, started over again when it lands, has it
      // first (newest first), not appended at the end.
      // The return's own jump to the top (from 400) sends a scroll event
      // before its refresh lands: that isn't the user, so it calls nothing
      // off.
      await page.evaluate((st) => window.__arrivals.push(st), arrival(7951, 103))
      await list.evaluate((el) => {
        window.__atTop = false
        el.addEventListener('scroll', () => {
          if (el.scrollTop === 0) window.__atTop = true
        })
      })
      gate()
      await away(page, 11)
      if (!(await waitFor(() => waiting, 3000))) return fail('RETURN', "back after 11 min the return's refresh never started")
      if (!(await waitFor(() => page.evaluate(() => window.__atTop), 3000))) fail('RETURN', "back after 11 min the return's scroll to the top sent no scroll event")
      await land()
      const rest = [7951, ...before.filter((id) => id !== first && !readOff.includes(id))]
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify(rest), 3000))) fail('RETURN', `back after 11 min the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}, expected the arrival first, then all but the opened one${readOff.length ? ` and the ${JSON.stringify(readOff)} read off the top` : ''}`)
      const earlier = page.locator(PHONE_EARLIER)
      const earlierWant = `Earlier (${1 + readOff.length})`
      if ((await textNow(page, PHONE_EARLIER)) !== earlierWant || (await earlier.getAttribute('aria-expanded')) !== 'false') fail('RETURN', `back after 11 min Earlier reads ${JSON.stringify(await textNow(page, PHONE_EARLIER))}, expanded ${await earlier.getAttribute('aria-expanded')}: expected the opened story${readOff.length ? ' and the ones read off the top' : ''} folded under ${JSON.stringify(earlierWant)}`)
      if (!(await waitFor(async () => (await scrollTop()) === 0, 3000))) fail('RETURN', `back after 11 min the list is at ${await scrollTop()}, expected its top`)
      await shot('reset')

      // Opened (a card in view, so the tap scrolls nothing), then away at
      // once: left alone, however long.
      const now = await groupIds(page, 'unseen')
      await list.evaluate((el) => el.scrollTo(0, 400))
      const inView = await list.evaluate((el) => {
        const box = el.getBoundingClientRect()
        const card = [...el.querySelectorAll('[data-group="unseen"] article[data-card-id]')].find((a) => {
          const r = a.getBoundingClientRect()
          return r.top >= box.top && r.top < box.bottom - 120
        })
        return card ? Number(card.getAttribute('data-card-id')) : null
      })
      if (inView == null) return fail('RETURN', 'no card in view at 400px')
      await openStory(page, inView)
      const at = await scrollTop()
      await away(page, 11)
      await sleep(1000)
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(now)) fail('RETURN', `back 11 min after opening a story the list moved: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if (Math.abs((await scrollTop()) - at) > 2) fail('RETURN', `back 11 min after opening a story the list is at ${await scrollTop()}, expected ${at}`)

      // Back after 11 min, the tab starts over at its top, and the return's
      // refresh is slow (a resumed iPhone). Whatever the user does before
      // it lands calls off its second reset: what it brings goes at the
      // end, as a poll's would, and nothing moves under them. The list is
      // off its top when the page hides, so being at 0 on the way back
      // shows the return started the tab over.
      const slowReturn = async (story) => {
        await page.clock.runFor(3000)
        await page.evaluate((st) => window.__arrivals.push(st), story)
        await list.evaluate((el) => el.scrollTo(0, 200))
        await sleep(300)
        if ((await scrollTop()) === 0) return false
        gate()
        await away(page, 11)
        if (!(await waitFor(() => waiting, 3000))) return false
        const reset = await groupIds(page, 'unseen')
        return reset[0] !== story.id && (await scrollTop()) === 0 ? reset : false
      }

      // The top story opened before the refresh lands: it is still first,
      // not under Earlier. Opened with a dispatched middle-click alone, an
      // auxclick that comes with no pointer, touch, click, key or wheel
      // event, so none of the input listeners calls the reset off: the
      // open guards do (openEntry's call-off and the landing's check of
      // the last open).
      const shown = await slowReturn(arrival(7952, 103))
      if (!shown) return fail('RETURN', 'back after 11 min with a slow refresh, the tab did not start over at its top')
      const earlierWas = await textNow(page, PHONE_EARLIER)
      const openedWas = (await harnessLog(page)).opened.length
      await page.locator(`${MOBILE_MAIN} article[data-card-id="${shown[0]}"] a[aria-label="open in new tab"]`).first().dispatchEvent('auxclick', { button: 1 })
      if ((await harnessLog(page)).opened.length !== openedWas + 1) fail('RETURN', `the middle-click on story ${shown[0]} opened nothing`)
      await land()
      const afterOpen = await groupIds(page, 'unseen')
      if (afterOpen[0] !== shown[0]) fail('RETURN', `story ${shown[0]}, opened before the return's refresh landed, moved: the list starts ${JSON.stringify(afterOpen.slice(0, 3))}`)
      if ((await textNow(page, PHONE_EARLIER)) !== earlierWas) fail('RETURN', `the opened story went under Earlier: ${JSON.stringify(await textNow(page, PHONE_EARLIER))}, was ${JSON.stringify(earlierWas)}`)
      if (afterOpen[afterOpen.length - 1] !== 7952) fail('RETURN', `the story the late refresh brought is not at the end: ${JSON.stringify(afterOpen.slice(-3))}`)

      // The list scrolled (a finger, or VoiceOver's scroll, which sends no
      // touch) before the refresh lands: it stays as it is.
      const before2 = await slowReturn(arrival(7953, 103))
      if (!before2) return fail('RETURN', 'the second slow return did not start the tab over at its top')
      if (!(await scrolledTo(list, 300))) return fail('RETURN', 'scrolled to 300 after the second slow return, the list sent no scroll event')
      await land()
      const afterScroll = await groupIds(page, 'unseen')
      if (JSON.stringify(afterScroll) !== JSON.stringify([...before2, 7953])) fail('RETURN', `scrolled before the return's refresh landed, the list became ${JSON.stringify(afterScroll)}, expected ${JSON.stringify([...before2, 7953])}`)
      if (Math.abs((await scrollTop()) - 300) > 2) fail('RETURN', `scrolled to 300 before the refresh landed, the list is at ${await scrollTop()}`)

      // A finger resting on the top card when the refresh lands (no move,
      // no open, no scroll): nothing moves under it.
      const before3 = await slowReturn(arrival(7954, 103))
      if (!before3) return fail('RETURN', 'the third slow return did not start the tab over at its top')
      const foldWas = await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')
      const release = await restFinger(page, page.locator(`${MOBILE_MAIN} article[data-card-id="${before3[0]}"]`))
      await land()
      const afterTouch = await groupIds(page, 'unseen')
      await release()
      if (JSON.stringify(afterTouch) !== JSON.stringify([...before3, 7954])) fail('RETURN', `with a finger on the list when the return's refresh landed, the list became ${JSON.stringify(afterTouch)}, expected ${JSON.stringify([...before3, 7954])}`)
      if ((await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')) !== foldWas) fail('RETURN', `with a finger on the list when the refresh landed, Earlier went from expanded ${foldWas} to ${await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')}`)
      if ((await scrollTop()) !== 0) fail('RETURN', `with a finger on the list when the refresh landed, the list moved to ${await scrollTop()}`)

      // The rest of the inputs, each alone before the refresh lands: the
      // list stays as it is, the late story goes at the end, and the list
      // stays at its top. A click with no pointer, touch or key (a desktop
      // screen reader in Firefox opening Earlier sends only mouse events):
      // Earlier stays open. A bare key (Shift, with the focus on the
      // page). The wheel turned up with the list at its top, which
      // scrolls nothing.
      const stays = async (what, story, before, extra) => {
        await land()
        const after = await groupIds(page, 'unseen')
        if (JSON.stringify(after) !== JSON.stringify([...before, story])) fail('RETURN', `${what} before the return's refresh landed, the list became ${JSON.stringify(after)}, expected ${JSON.stringify([...before, story])}`)
        if ((await scrollTop()) !== 0) fail('RETURN', `${what} before the refresh landed, the list moved to ${await scrollTop()}`)
        if (extra) await extra()
      }
      const before4 = await slowReturn(arrival(7955, 103))
      if (!before4) return fail('RETURN', 'the fourth slow return did not start the tab over at its top')
      const earlierButton = page.locator(PHONE_EARLIER)
      if ((await earlierButton.getAttribute('aria-expanded')) !== 'false') return fail('RETURN', 'back after the fourth slow return Earlier is not folded')
      await earlierButton.dispatchEvent('click')
      if (!(await waitFor(async () => (await earlierButton.getAttribute('aria-expanded')) === 'true' && (await groupIds(page, 'earlier')).length > 0, 3000))) return fail('RETURN', 'a click on "Earlier" did not open it')
      await stays('with Earlier opened by a click alone', 7955, before4, async () => {
        if ((await earlierButton.getAttribute('aria-expanded')) !== 'true' || !(await groupIds(page, 'earlier')).length) fail('RETURN', `Earlier, opened by a click before the return's refresh landed, folded: expanded ${await earlierButton.getAttribute('aria-expanded')} with ${(await groupIds(page, 'earlier')).length} cards drawn`)
      })

      const before5 = await slowReturn(arrival(7956, 103))
      if (!before5) return fail('RETURN', 'the fifth slow return did not start the tab over at its top')
      await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
      await page.keyboard.press('Shift')
      await stays('with a key pressed', 7956, before5)

      const before6 = await slowReturn(arrival(7957, 103))
      if (!before6) return fail('RETURN', 'the sixth slow return did not start the tab over at its top')
      const box = await list.boundingBox()
      await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 200))
      await page.evaluate(() => {
        window.__wheeled = false
        window.addEventListener('wheel', () => { window.__wheeled = true }, { once: true, capture: true })
      })
      await page.mouse.wheel(0, -100)
      if (!(await waitFor(() => page.evaluate(() => window.__wheeled), 3000))) return fail('RETURN', 'the wheel over the list sent no wheel event')
      await stays('with the wheel turned', 7957, before6)

      // PR3c: stories used up on screen call the second reset off too, as
      // an open does. The end rule uses up the cards on screen with the end
      // row with no touch, scroll or key, so a slow refresh landing after
      // it would otherwise move them under a folded Earlier. NHL's stories
      // and its end row fit on one screen; the tab is left at once (the
      // clock held from the tap, so nothing is read before), and back
      // after 11 min, with the refresh slow, the user reads for the dwell
      // and touches nothing.
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(async () => /^nhl/i.test(await selectedTab(page)), 3000))) return fail('RETURN', 'could not select the NHL tab')
      await observersSettled(page)
      const few = await groupIds(page, 'unseen')
      const fewShown = (await listCards(page)).half
      const endRowShown = await list.evaluate((el) => {
        const m = el.querySelector('[data-caught-up]')
        return !!m && m.getBoundingClientRect().bottom <= el.getBoundingClientRect().bottom
      })
      if (!few.length || JSON.stringify(fewShown) !== JSON.stringify(few) || !endRowShown) return fail('RETURN', `the NHL tab shows ${JSON.stringify(fewShown)} of ${JSON.stringify(few)}, end row on screen ${endRowShown}: expected every unseen card and the end row on one screen`)
      gate()
      await away(page, 11)
      if (!(await waitFor(() => waiting, 3000))) return fail('RETURN', "back on NHL after 11 min the return's refresh never started")
      await observersSettled(page)
      await dwell(page)
      await page.clock.runFor(SEEN_BATCH_MS + 50)
      if (!(await waitFor(async () => /You.re all caught up/.test(await markerText(page)), 3000))) return fail('RETURN', `back after 11 min with ${JSON.stringify(few)} on screen with the end row for the dwell, the end row reads ${JSON.stringify(await markerText(page))}, expected "You're all caught up"`)
      await land()
      const afterEndRule = await groupIds(page, 'unseen')
      if (JSON.stringify(afterEndRule) !== JSON.stringify(few)) fail('RETURN', `${JSON.stringify(few)}, used up on screen by the end rule before the return's refresh landed, moved when it landed: above the marker now ${JSON.stringify(afterEndRule)}, Earlier ${JSON.stringify(await textNow(page, PHONE_EARLIER))}`)
      if (await page.locator(PHONE_EARLIER).count()) fail('RETURN', `after the end rule and the landing the NHL tab has ${JSON.stringify(await textNow(page, PHONE_EARLIER))}, expected none`)
    },
  }),
  // C1 from deep in Earlier: the marker was far above the screen (the
  // hold on) when the page hid. Back after 11 min the tab starts over at
  // its top with more unseen stories than fit, so the marker is now below
  // the screen, and the hold must be let go although the marker never
  // crossed the screen's edge: the end row follows the opens, an arrival
  // goes in at once, and the row doesn't jump off the screen when it is
  // scrolled to. The page's clock is held from the tap on the tab
  // (PR3c): the six unseen cards sit on screen with the marker at first,
  // and the last of them with it again at the end, where the end rule
  // would use them up a second later.
  dashFlow('flow-return-from-earlier', {
    before: async ({ page, context }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
      await withArrivals(page)
      // 40 NHL stories: 601, 602 and the four newest of the rest unseen,
      // the other 34 seen.
      const unseen = [601, 602, 7001, 7002, 7003, 7004]
      prefMock(context).seed({ 'seen:nhl': [...idsOf('nhl'), ...moreNhl(37).map((e) => e.id)].filter((id) => !unseen.includes(id)) })
    },
    run: async ({ page, fail, shot }) => {
      const list = page.locator(PHONE_LIST)
      const markerTop = () => page.locator(PHONE_MARKER).evaluate((m) => m.getBoundingClientRect().top - m.closest('[data-scroller]').getBoundingClientRect().top)
      const listHeight = () => list.evaluate((el) => el.clientHeight)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      const first = await groupIds(page, 'unseen')
      if (JSON.stringify(first) !== '[601,602,7001,7002,7003,7004]') return fail('RETURN-EARLIER', `the NHL tab opens with ${JSON.stringify(first)} above the marker`)
      await tap(page, page.locator(PHONE_EARLIER))
      if (!(await waitFor(async () => (await groupIds(page, 'earlier')).length >= 30, 3000))) return fail('RETURN-EARLIER', 'Earlier did not open')
      // Deep in Earlier: the marker 600px above the screen, and the app's
      // observer has seen it there, so the hold is on when the page hides
      // (with it still off, the return has no hold to let go, and the
      // flow can't tell the fix from its absence).
      await list.evaluate((el) => {
        const m = el.querySelector('[data-caught-up]')
        el.scrollTop += m.getBoundingClientRect().bottom - el.getBoundingClientRect().top + 600
      })
      if (!(await markerAbove(list))) return fail('RETURN-EARLIER', 'deep in Earlier, the app never saw the marker above the screen')
      if ((await markerTop()) > -600) return fail('RETURN-EARLIER', `the marker is at ${await markerTop()}, expected 600px above the screen`)
      await page.clock.runFor(3000)

      // A story arrives while away; back after 11 min the tab starts over
      // at its top: 7 unseen, and the marker below the screen.
      await page.evaluate((st) => window.__arrivals.push(st), arrivalNhl(7991))
      await away(page, 11)
      const reset = [7991, ...first]
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify(reset), 3000))) return fail('RETURN-EARLIER', `back after 11 min the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}, expected ${JSON.stringify(reset)}`)
      if (!(await waitFor(async () => (await list.evaluate((el) => el.scrollTop)) === 0, 3000))) fail('RETURN-EARLIER', `back after 11 min the list is at ${await list.evaluate((el) => el.scrollTop)}, expected its top`)
      await sleep(400)
      if ((await markerTop()) < (await listHeight())) return fail('RETURN-EARLIER', `back after 11 min the marker is at ${await markerTop()}, expected below the ${await listHeight()}px list`)

      // Two opens: the badge and the end row (below the screen) both say 5.
      await openStory(page, 7991)
      await openStory(page, 601)
      if (!(await waitFor(async () => (await badge(page, /^nhl/i)) === '5', 3000))) fail('RETURN-EARLIER', `after two opens the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "5"`)
      if (!(await waitFor(async () => (await markerText(page)) === '5 not seen · Mark as read', 3000))) fail('RETURN-EARLIER', `after two opens the end row reads ${JSON.stringify(await markerText(page))}, expected "5 not seen · Mark as read"`)

      // A poll brings another: the marker is below the screen, so it goes
      // in at once, at the end of the unseen ones.
      await page.evaluate((st) => window.__arrivals.push(st), arrivalNhl(7992))
      const landed = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries')
      await startPoll(page)
      await landed
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify([...reset, 7992]), 3000))) fail('RETURN-EARLIER', `with the marker below the screen the arrival waits: the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if (!(await waitFor(async () => (await markerText(page)) === '6 not seen · Mark as read', 3000))) fail('RETURN-EARLIER', `after the arrival the end row reads ${JSON.stringify(await markerText(page))}, expected "6 not seen · Mark as read"`)
      await shot('arrived')

      // Scrolled to, the end row stays where it was put.
      await page.locator(PHONE_MARKER).evaluate((m) => m.scrollIntoView({ block: 'center' }))
      const put = await markerTop()
      await sleep(600)
      const now = await markerTop()
      if (Math.abs(now - put) > 1) fail('RETURN-EARLIER', `the end row, scrolled to at ${put}, moved to ${now}`)
      if ((await markerText(page)) !== '6 not seen · Mark as read') fail('RETURN-EARLIER', `on screen the end row reads ${JSON.stringify(await markerText(page))}`)
    },
  }),
  // Refresh (the header button; `r` on a keyboard) starts every tab over
  // once its stories land: an opened story goes under the marker, a story
  // that came in is sorted in first, Earlier folds again, and the toast
  // counts what came in ("1 new"), or says "Nothing new". A poll never
  // does any of this. The page's clock is held from the tap on News
  // (PR3c): opening Earlier scrolls the list to its end, where the last
  // cards sit on screen with the marker, and the NHL part scrolls read
  // cards off the top; either would use stories up, and the Refreshes
  // count what is unseen.
  dashFlow('flow-refresh-resets', {
    before: async ({ page }) => {
      await installClock(page)
      // 61 NHL stories, every one unseen, for the last part.
      await withStories(page, moreNhl(58))
      await withArrivals(page)
    },
    run: async ({ page, fail, shot }) => {
      await holdClock(page)
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      await openStory(page, before[0])
      await page.evaluate((st) => window.__arrivals.push(st), arrival(7961, 103))
      // A poll first: the arrival goes at the end, the opened story stays.
      const polled = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries')
      await startPoll(page)
      await polled
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify([...before, 7961]), 3000))) fail('REFRESH', `after a poll the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if (await page.locator('[role="status"]').getByText(/^(\d+ new|Nothing new)$/).count()) fail('REFRESH', 'a poll showed a refresh toast')
      // Refresh: the tab starts over.
      await page.evaluate((st) => window.__arrivals.push(st), arrival(7962, 103))
      await tap(page, page.locator('header[data-app-header] button[aria-label="refresh"]'))
      const want = [7962, 7961, ...before.slice(1)]
      if (!(await waitFor(async () => JSON.stringify(await groupIds(page, 'unseen')) === JSON.stringify(want), 3000))) fail('REFRESH', `after Refresh the stories above the marker are ${JSON.stringify(await groupIds(page, 'unseen'))}, expected ${JSON.stringify(want)}`)
      if ((await textNow(page, PHONE_EARLIER)) !== 'Earlier (1)') fail('REFRESH', `after Refresh Earlier reads ${JSON.stringify(await textNow(page, PHONE_EARLIER))}, expected the opened story`)
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('1 new', { exact: true }).isVisible(), 3000))) fail('REFRESH', 'no "1 new" toast')
      await shot('refreshed')
      // Earlier opened, then Refresh again with nothing new: the same
      // Column (no tab switch) starts over, so Earlier folds again. The
      // "1 new" toast stays up while the clock is held, over the middle
      // of Earlier at the end of the list: the tap goes to its left end.
      await tap(page, page.locator(PHONE_EARLIER), { position: { x: 16, y: 22 } })
      if (!(await waitFor(async () => (await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')) === 'true' && (await groupIds(page, 'earlier')).length === 1, 3000))) fail('REFRESH', 'Earlier (1) did not open')
      await tap(page, page.locator('header[data-app-header] button[aria-label="refresh"]'))
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('Nothing new', { exact: true }).isVisible(), 3000))) fail('REFRESH', 'no "Nothing new" toast')
      if (!(await waitFor(async () => (await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')) === 'false', 3000)) || (await groupIds(page, 'earlier')).length) {
        fail('REFRESH', `after the second Refresh Earlier is expanded ${await page.locator(PHONE_EARLIER).getAttribute('aria-expanded')} with ${(await groupIds(page, 'earlier')).length} cards drawn, expected it folded`)
      }
      // Every unseen card is drawn (13, one batch), so arrivals would be
      // drawn at once. A Refresh that brings 20 starts the tab over at its
      // first 30 instead: the rest wait for the "load more" sentinel.
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, 0))
      const unseenWas = (await groupIds(page, 'unseen')).length
      if (unseenWas !== 13) fail('REFRESH', `before the third Refresh the tab draws ${unseenWas} unseen cards, expected 13`)
      await page.evaluate((rows) => window.__arrivals.push(...rows), Array.from({ length: 20 }, (_, i) => arrival(7970 + i, 103)))
      await tap(page, page.locator('header[data-app-header] button[aria-label="refresh"]'))
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('20 new', { exact: true }).isVisible(), 3000))) fail('REFRESH', 'no "20 new" toast')
      await sleep(400)
      const drawn = (await groupIds(page, 'unseen')).length
      const sentinel = await page.locator(`${MOBILE_MAIN} [data-more]`).count()
      if (drawn !== 30 || sentinel !== 1) fail('REFRESH', `after a Refresh that brought 20 to 13 unseen the tab draws ${drawn} unseen cards ${sentinel ? 'with' : 'without'} a "load more" sentinel, expected its first 30 and the sentinel`)

      // A tab that had drawn two batches of more unseen stories (60 of
      // 61), one of them opened: the Refresh's snapshot has 60 unseen,
      // which the old limit already reaches, and still draws its first 30
      // and the sentinel.
      await tap(page, phoneTab(page, /^nhl/i))
      const nhlMore = page.locator(`${MOBILE_MAIN} [data-more]`)
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 30 && (await nhlMore.count()) === 1, 3000))) return fail('REFRESH', `the 61-story NHL tab draws ${(await groupIds(page, 'unseen')).length} unseen cards at first, expected 30 and the sentinel`)
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, el.scrollHeight))
      if (!(await waitFor(async () => (await groupIds(page, 'unseen')).length === 60, 3000))) return fail('REFRESH', `scrolled to its end the NHL tab draws ${(await groupIds(page, 'unseen')).length} unseen cards, expected 60`)
      await sleep(400)
      if ((await groupIds(page, 'unseen')).length !== 60 || (await nhlMore.count()) !== 1) return fail('REFRESH', `the NHL tab went on to ${(await groupIds(page, 'unseen')).length} unseen cards without another scroll`)
      await openStory(page, 601)
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, 0))
      await tap(page, page.locator('header[data-app-header] button[aria-label="refresh"]'))
      // The opened story goes under the marker (not drawn yet: the marker
      // and Earlier wait for the unseen cards).
      if (!(await waitFor(async () => !(await groupIds(page, 'unseen')).includes(601) && (await page.locator('[role="status"]').getByText('Nothing new', { exact: true }).isVisible()), 3000))) return fail('REFRESH', `after the NHL Refresh the stories above the marker start ${JSON.stringify((await groupIds(page, 'unseen')).slice(0, 3))}, expected the opened 601 gone from them, and "Nothing new"`)
      await sleep(400)
      const nhlDrawn = (await groupIds(page, 'unseen')).length
      const nhlSentinel = await nhlMore.count()
      if (nhlDrawn !== 30 || nhlSentinel !== 1) fail('REFRESH', `after a Refresh to 60 unseen with 60 drawn before it, the NHL tab draws ${nhlDrawn} unseen cards ${nhlSentinel ? 'with' : 'without'} a "load more" sentinel, expected its first 30 and the sentinel`)
    },
  }),

  // --- PR3c: seen on scroll ----------------------------------------------------

  // Seen on scroll (D3): on the phone, a card at least half on screen for
  // SEEN_DWELL_MS and then scrolled off the top is used up. The tab is
  // opened with a tap (a new list, so the observer rebinds to it) and the
  // Menu opened and closed: while it is open the dwell doesn't count, and
  // once closed it pauses nothing, though it stays mounted with
  // aria-modal. The seen: PATCHes add exactly the qualified cards that
  // left the top, the badge drops by as many, and no card moves. In a
  // fling, the cards on screen when it starts are seen and the cards first
  // shown during it stay unseen. A card seen on scroll just before ⋯ →
  // Mark all stays seen after Mark all's Undo.
  dashFlow('flow-scroll-marks-seen', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!/^nhl/i.test(await selectedTab(page))) return fail('SCROLL-SEEN', 'could not select the NHL tab')
      await observersSettled(page)
      const order = await groupIds(page, 'unseen')
      if (order.length !== 30) return fail('SCROLL-SEEN', `the NHL tab draws ${order.length} cards, expected its first 30`)
      if ((await badge(page, /^nhl/i)) !== '40') fail('SCROLL-SEEN', `the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "40"`)
      const added = () => seenAdded(writes, 'nhl')
      const sameOrder = async () => {
        const now = await groupIds(page, 'unseen')
        return JSON.stringify(now.slice(0, order.length)) === JSON.stringify(order)
      }

      // The Menu open for longer than the dwell, closed, then the top card
      // scrolled off at once: it never qualified, so it stays unseen.
      await tap(page, page.locator('button[aria-label="open menu"]'))
      await menuOpened(page)
      await dwell(page)
      await tap(page, page.locator('button[aria-label="close menu"]'))
      await page.waitForFunction(() => document.querySelector('aside[aria-label="menu"]').hasAttribute('inert'), null, { timeout: 3000 })
      await scrollCardToTop(page, order[1])
      await writeSeen(page)
      if (added().length) fail('SCROLL-SEEN', `a card scrolled off straight after the Menu, which was open through the dwell, was marked: ${JSON.stringify(added())}`)

      // Read, then scrolled off the top: those cards, and only those.
      await dwell(page)
      const before = await listCards(page)
      const firstQualified = before.half
      const next = order.find((id) => !firstQualified.includes(id) && !before.above.includes(id))
      await scrollCardToTop(page, next)
      const leftTop = (await listCards(page)).above.filter((id) => firstQualified.includes(id))
      if (leftTop.length !== firstQualified.length) return fail('SCROLL-SEEN', `the scroll took ${JSON.stringify(leftTop)} off the top, expected ${JSON.stringify(firstQualified)}`)
      if (added().length) fail('SCROLL-SEEN', `a seen: PATCH went before its batches: ${JSON.stringify(added())}`)
      // The collector holds the marks for SEEN_BATCH_MS: nothing is written
      // before it runs, so nothing is drawn. The wait is real time: React
      // renders through MessageChannel, which the held clock doesn't hold
      // (a frame wouldn't do: rAF is held), so a mark written at once would
      // be on the badge by then.
      await sleep(200)
      if ((await badge(page, /^nhl/i)) !== '40') fail('SCROLL-SEEN', `the badge dropped before the seen batch ran: it reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "40"`)
      // The batch runs: one write, one render, before prefSync's own PATCH
      // batch has gone.
      await page.clock.runFor(SEEN_BATCH_MS + 50)
      const afterRead = String(40 - leftTop.length)
      if (!(await waitFor(async () => (await badge(page, /^nhl/i)) === afterRead, 3000))) fail('SCROLL-SEEN', `after the seen batch the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "${afterRead}"`)
      if (added().length) fail('SCROLL-SEEN', `a seen: PATCH went before prefSync's batch: ${JSON.stringify(added())}`)
      await page.clock.runFor(1100)
      await requestsSettled(page)
      if (!(await waitFor(() => added().length >= leftTop.length, 3000))) return fail('SCROLL-SEEN', `scrolling ${JSON.stringify(leftTop)} off the top sent ${JSON.stringify(added())}`)
      if (JSON.stringify(sortedIds(added())) !== JSON.stringify(sortedIds(leftTop))) fail('SCROLL-SEEN', `seen:nhl got ${JSON.stringify(added())}, expected exactly ${JSON.stringify(leftTop)}`)
      const nhlPatches = writes.filter((w) => w.method === 'PATCH' && w.key === 'seen:nhl').length
      if (nhlPatches !== 1) fail('SCROLL-SEEN', `${nhlPatches} seen:nhl PATCHes for one batch, expected 1`)
      if (!(await sameOrder())) fail('SCROLL-SEEN', `the cards moved: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      if (added().includes(order[0])) fail('SCROLL-SEEN', `story ${order[0]}, scrolled off before it qualified, was marked`)

      // A fling: the cards on screen when it starts were read (a dwell
      // with them there); the ones it shows on the way never are. It
      // stops short of the end.
      await dwell(page)
      const atFling = (await listCards(page)).half
      for (let i = 0; i < 5; i++) await scrollListBy(page, 350)
      const flung = await listCards(page)
      if (await page.locator(PHONE_MARKER).count()) {
        const marker = await page.locator(PHONE_MARKER).boundingBox()
        if (marker && marker.y < 844) return fail('SCROLL-SEEN', 'the fling reached the marker; the flow needs a longer tab')
      }
      const readBefore = atFling.filter((id) => flung.above.includes(id))
      const passed = flung.above.filter((id) => !atFling.includes(id) && !firstQualified.includes(id) && id !== order[0])
      if (!passed.length) return fail('SCROLL-SEEN', 'the fling passed no card it first showed')
      await writeSeen(page)
      const want = sortedIds([...leftTop, ...readBefore])
      if (!(await waitFor(() => added().length >= want.length, 3000))) return fail('SCROLL-SEEN', `after the fling seen:nhl got ${JSON.stringify(added())}, expected ${JSON.stringify(want)}`)
      if (JSON.stringify(sortedIds(added())) !== JSON.stringify(want)) fail('SCROLL-SEEN', `after the fling seen:nhl got ${JSON.stringify(sortedIds(added()))}, expected exactly ${JSON.stringify(want)} (cards first shown in the fling: ${JSON.stringify(passed)})`)
      const afterFling = String(40 - want.length)
      if (!(await waitFor(async () => (await badge(page, /^nhl/i)) === afterFling, 3000))) fail('SCROLL-SEEN', `after the fling the NHL badge reads ${JSON.stringify(await badge(page, /^nhl/i))}, expected "${afterFling}"`)
      if (!(await sameOrder())) fail('SCROLL-SEEN', `the cards moved after the fling: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      await shot('after-fling')

      // A card seen on scroll and still being collected when ⋯ → Mark all
      // runs is written as seen first: Mark all counts and undoes only
      // what it marked.
      await dwell(page)
      const now = await listCards(page)
      const pending = now.half[0]
      await scrollCardToTop(page, now.half[1])
      const toMark = 40 - want.length - 1
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-mark-all]`))
      if (!(await waitFor(() => page.getByText(`Marked ${toMark} as read`, { exact: true }).isVisible(), 3000))) fail('SCROLL-SEEN', `no "Marked ${toMark} as read" toast with story ${pending} seen on scroll just before`)
      // Mark all's PATCH goes first (no card above the marker now), or the
      // Undo would fold into it and send nothing.
      await page.clock.runFor(1100)
      await tap(page, page.getByRole('button', { name: 'Undo', exact: true }))
      await page.clock.runFor(1100)
      if (!(await waitFor(() => seenRemoved(writes, 'nhl').length > 0, 3000))) return fail('SCROLL-SEEN', 'Undo sent no removal')
      if (seenRemoved(writes, 'nhl').includes(pending)) fail('SCROLL-SEEN', `Mark all's Undo took back story ${pending}, seen on scroll before it`)
      if (seenRemoved(writes, 'nhl').length !== toMark) fail('SCROLL-SEEN', `Undo removed ${seenRemoved(writes, 'nhl').length}, expected the ${toMark} Mark all marked`)
      if (!added().includes(pending)) fail('SCROLL-SEEN', `story ${pending}, seen on scroll, was never written`)
    },
  }),

  // A snapshot started while the seen collector still holds a story
  // places it as seen. For You's top card, read and scrolled off the top,
  // is still being collected (SEEN_BATCH_MS) when its own category's tab
  // is tapped: the tab opens with it under the marker. Placed as unseen,
  // it would stay above the marker once its mark landed a moment later
  // (ids in a snapshot keep their group): a new-looking story the counts
  // leave out. The same for a card read off the top just before a Refresh
  // lands (every tab starts over) and just before a new Sort (the tab's
  // order). The page's clock is held, so the collector's timer can't run
  // first.
  dashFlow('flow-seen-tab-switch', {
    before: async ({ page }) => {
      await installClock(page)
      // News (For You's top story, 101, is one) long enough for three
      // reads off the top with its end row below the screen.
      await withStories(page, moreNhl(20).map((e) => ({ ...e, id: e.id + 100, source_id: 3, url: e.url.replace('/16/', '/3/') })))
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      if (!/^for you/i.test(await selectedTab(page))) return fail('SEEN-SWITCH', `a new device opens on ${JSON.stringify(await selectedTab(page))}, expected For You`)
      // The end rule must not use the cards up instead: the marker is
      // below the screen.
      const markerBelow = () =>
        page.locator(PHONE_LIST).evaluate((el) => {
          const m = el.querySelector('[data-caught-up]')
          return !m || m.getBoundingClientRect().top >= el.getBoundingClientRect().bottom
        })
      // The top card read (a dwell), then scrolled off the top: it is in
      // the collector now. Its id, or null. A story an earlier step used
      // is passed over (one left above the marker by a broken step would
      // otherwise be read again, already marked, and hide the next step's
      // result).
      const used = []
      const readOff = async (where) => {
        await observersSettled(page)
        if (!(await markerBelow())) {
          fail('SEEN-SWITCH', `${where} ends on the first screen; the flow needs a longer tab`)
          return null
        }
        await dwell(page)
        const { half } = await listCards(page)
        const id = half.find((x) => !used.includes(x))
        const order = await groupIds(page, 'unseen')
        const after = order[order.indexOf(id) + 1]
        if (id == null || after == null) {
          fail('SEEN-SWITCH', `only ${JSON.stringify(half)} are on ${where}'s screen`)
          return null
        }
        await scrollCardToTop(page, after)
        if (!(await listCards(page)).above.includes(id)) {
          fail('SEEN-SWITCH', `story ${id} did not leave the top of ${where}`)
          return null
        }
        used.push(id)
        return id
      }
      // After the reset, the batches run: the story is under the marker,
      // and its mark went once.
      const underMarker = async (id, what) => {
        await page.clock.runFor(SEEN_BATCH_MS + 50)
        await page.clock.runFor(1100)
        await requestsSettled(page)
        if ((await groupIds(page, 'unseen')).includes(id)) fail('SEEN-SWITCH', `story ${id}, seen on scroll just before ${what}, is above the marker`)
        const sent = seenAdded(writes, category).filter((x) => x === id).length
        if (sent !== 1) fail('SEEN-SWITCH', `seen:${category} added story ${id} ${sent} times, expected once`)
      }

      // For You's top story, then its category's tab tapped.
      const story = await readOff('For You')
      if (story == null) return
      const entry = F.ENTRIES.find((e) => e.id === story)
      const category = entry && (F.SOURCES.find((s) => s.id === entry.source_id) || {}).category
      if (!category || !/^[a-z]+$/.test(category)) return fail('SEEN-SWITCH', `For You's top story ${story} has no plain category: ${JSON.stringify(category)}`)
      const tabRe = new RegExp(`^${category}`, 'i')
      await tap(page, phoneTab(page, tabRe))
      if (!(await waitFor(async () => tabRe.test(await selectedTab(page)), 3000))) return fail('SEEN-SWITCH', `could not select the ${category} tab`)
      await underMarker(story, 'the tab switch')

      // A Refresh that lands inside the collector's window.
      const beforeRefresh = await readOff(`the ${category} tab`)
      if (beforeRefresh == null) return
      await tap(page, page.locator('header[data-app-header] button[aria-label="refresh"]'))
      if (!(await waitFor(() => page.locator('[role="status"]').getByText(/^(\d+ new|Nothing new)$/).isVisible(), 3000))) return fail('SEEN-SWITCH', 'the Refresh never landed')
      await underMarker(beforeRefresh, 'a Refresh')

      // A new Sort, from the tab's ⋯.
      const beforeSort = await readOff(`the ${category} tab`)
      if (beforeSort == null) return
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-sort="best"]`))
      if (!(await waitFor(async () => (await page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-sort="best"]`).getAttribute('aria-pressed')) === 'true', 3000))) return fail('SEEN-SWITCH', 'Best did not take')
      await underMarker(beforeSort, 'a new Sort')

      // All three are in Earlier.
      await page.keyboard.press('Escape')
      if (!(await page.locator(PHONE_EARLIER).count())) return fail('SEEN-SWITCH', `the ${category} tab has no Earlier`)
      await tap(page, page.locator(PHONE_EARLIER))
      const want = [story, beforeRefresh, beforeSort]
      if (!(await waitFor(async () => (await groupIds(page, 'earlier')).filter((id) => want.includes(id)).length === 3, 3000))) {
        fail('SEEN-SWITCH', `the ${category} tab's Earlier holds ${JSON.stringify(await groupIds(page, 'earlier'))}, expected ${JSON.stringify(want)} among them`)
      }
      await shot('earlier')
    },
  }),

  // A tab that starts over where it is (a Refresh landing, a new Sort)
  // starts its list at its top. Left mid-list, the reset would take the
  // stories read off the top out from above the screen, and the cards
  // being read would jump up past its top: the tracker can't tell that
  // from a scroll past them, so they would be marked, and the reader's
  // place lost. The clock held; NHL with 40 stories, read a few screens
  // down each time.
  dashFlow('flow-seen-reset-in-place', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!/^nhl/i.test(await selectedTab(page))) return fail('SEEN-RESET', 'could not select the NHL tab')
      await observersSettled(page)
      const list = page.locator(PHONE_LIST)
      const scrollTop = () => list.evaluate((el) => el.scrollTop)
      // `screens` screens read and scrolled off the top (a dwell, then the
      // last card on screen to the top), their marks written; then the
      // screen it ends on read too (a dwell): those cards, or null.
      const readDown = async (screens, where) => {
        for (let i = 0; i < screens; i++) {
          await dwell(page)
          const { half } = await listCards(page)
          if (half.length < 2) {
            fail('SEEN-RESET', `${where}: only ${JSON.stringify(half)} on screen`)
            return null
          }
          await scrollCardToTop(page, half[half.length - 1])
        }
        await writeSeen(page)
        await dwell(page)
        const { half } = await listCards(page)
        if (half.length < 2 || (await scrollTop()) === 0) {
          fail('SEEN-RESET', `${where}: reading ${JSON.stringify(half)} at ${await scrollTop()}, expected a few cards well down the list`)
          return null
        }
        return half
      }
      // After the reset: none of the cards being read was marked, and the
      // list is at its top.
      const nothingMarked = async (reading, what) => {
        await observersSettled(page)
        await writeSeen(page)
        const marked = seenAdded(writes, 'nhl').filter((id) => reading.includes(id))
        if (marked.length) fail('SEEN-RESET', `${what} marked ${JSON.stringify(marked)}, on screen being read and never scrolled past`)
        if ((await scrollTop()) !== 0) fail('SEEN-RESET', `after ${what} the list is at ${await scrollTop()}, expected its top`)
      }

      // A Refresh: the tab starts over at its top, the stories read off
      // the top are under the marker, and the cards being read lead the
      // unseen ones.
      const reading = await readDown(3, 'before the Refresh')
      if (!reading) return
      const readOff = seenAdded(writes, 'nhl')
      if (readOff.length < 6) return fail('SEEN-RESET', `three screens read off the top marked only ${JSON.stringify(readOff)}`)
      await tap(page, page.locator('header[data-app-header] button[aria-label="refresh"]'))
      if (!(await waitFor(() => page.locator('[role="status"]').getByText(/^(\d+ new|Nothing new)$/).isVisible(), 3000))) return fail('SEEN-RESET', 'the Refresh never landed')
      await nothingMarked(reading, 'a Refresh')
      const unseen = await groupIds(page, 'unseen')
      if (readOff.some((id) => unseen.includes(id))) fail('SEEN-RESET', `after a Refresh stories read off the top are above the marker: ${JSON.stringify(readOff.filter((id) => unseen.includes(id)))}`)
      if (JSON.stringify(unseen.slice(0, reading.length)) !== JSON.stringify(reading)) fail('SEEN-RESET', `after a Refresh the unseen stories start ${JSON.stringify(unseen.slice(0, reading.length + 2))}, expected the cards being read, ${JSON.stringify(reading)}, first`)
      const shown = (await listCards(page)).half
      if (!reading.every((id) => shown.includes(id))) fail('SEEN-RESET', `after a Refresh ${JSON.stringify(shown)} are on screen, expected the cards being read, ${JSON.stringify(reading)}`)
      await shot('refreshed')

      // A new Sort from the tab's ⋯ (above the list): the same.
      const reading2 = await readDown(2, 'before Best')
      if (!reading2) return
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-sort="best"]`))
      if (!(await waitFor(async () => (await page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-sort="best"]`).getAttribute('aria-pressed')) === 'true', 3000))) return fail('SEEN-RESET', 'Best did not take')
      await page.keyboard.press('Escape')
      await nothingMarked(reading2, 'a new Sort')
    },
  }),

  // The phone turned past 768px inside the seen batch's 300 ms: the grid's
  // columns weren't drawn, so they start their snapshots in the render the
  // turn causes, and a story read off the top of For You a moment before
  // is still in the collector. It is placed as seen: under its column's
  // marker, not drawn as new and counted out. The clock held, so the
  // collector's timer can't run first.
  dashFlow('flow-seen-turn-mid-batch', {
    before: async ({ page }) => {
      await installClock(page)
    },
    run: async ({ page, fail }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      if (!/^for you/i.test(await selectedTab(page))) return fail('SEEN-TURN', `a new device opens on ${JSON.stringify(await selectedTab(page))}, expected For You`)
      await observersSettled(page)
      await dwell(page)
      const { half } = await listCards(page)
      if (half.length < 2) return fail('SEEN-TURN', `only ${JSON.stringify(half)} are on For You's screen`)
      const story = half[0]
      await scrollCardToTop(page, half[1])
      if (!(await listCards(page)).above.includes(story)) return fail('SEEN-TURN', `story ${story} did not leave the top of For You`)
      const entry = F.ENTRIES.find((e) => e.id === story)
      const category = entry && (F.SOURCES.find((s) => s.id === entry.source_id) || {}).category
      if (!category || !/^[a-z]+$/.test(category)) return fail('SEEN-TURN', `For You's top story ${story} has no plain category: ${JSON.stringify(category)}`)
      if (seenAdded(writes, category).length) return fail('SEEN-TURN', `the mark went before the turn: ${JSON.stringify(seenAdded(writes, category))}`)

      await page.setViewportSize({ width: 844, height: 390 })
      if (!(await waitFor(() => page.locator('main.grid article[data-card-id]').first().isVisible(), 3000))) return fail('SEEN-TURN', 'at 844 wide the desktop grid did not show')
      await writeSeen(page)
      // The grid's column of the story's category, and where the story is
      // drawn in it: above the marker, in Earlier (once opened), or not
      // at all (folded, or past the first batch).
      const column = page.locator('main.grid section').filter({ has: page.locator('header h2', { hasText: new RegExp(`^${category}$`, 'i') }) })
      if ((await column.count()) !== 1) return fail('SEEN-TURN', `the grid has ${await column.count()} ${category} columns`)
      const drawnIn = () => column.evaluate((sec, id) => {
        const card = sec.querySelector(`article[data-card-id="${id}"]`)
        const group = card && card.closest('[data-group]')
        return group ? group.getAttribute('data-group') : null
      }, story)
      if ((await drawnIn()) === 'unseen') fail('SEEN-TURN', `story ${story}, read off the top of For You just before the turn, is drawn above the ${category} column's marker`)
      // Under the marker: in the column's Earlier.
      const earlier = column.locator('button[data-earlier]')
      if (!(await earlier.count())) fail('SEEN-TURN', `the ${category} column has no Earlier`)
      else {
        await tap(page, earlier)
        if (!(await waitFor(async () => (await drawnIn()) === 'earlier', 3000))) fail('SEEN-TURN', `story ${story} is not in the ${category} column's Earlier (drawn: ${JSON.stringify(await drawnIn())})`)
      }
      const sent = seenAdded(writes, category).filter((x) => x === story).length
      if (sent !== 1) fail('SEEN-TURN', `seen:${category} added story ${story} ${sent} times, expected once`)
    },
  }),

  // The ranking's `view` is sent when a story is used up (D3), not when its
  // card is drawn: a tab opened and read for a while sends none. The top
  // card opened without a scroll sends its `click` and one view; a card
  // scrolled off the top sends one; the opened one, scrolled off too,
  // sends no second view.
  dashFlow('flow-view-on-seen', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
    },
    run: async ({ page, fail }) => {
      await recordInteractions(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      await observersSettled(page)
      const views = () => page._events.filter((e) => e.type === 'view').map((e) => e.entry_id)
      // Drawn and on screen through the dwell, then long enough for the
      // interaction batch to go (its idle callback waits at most 1 s).
      await dwell(page)
      await page.clock.runFor(1500)
      await requestsSettled(page)
      if (views().length) fail('VIEW-ON-SEEN', `views sent for cards only drawn: ${JSON.stringify(views())}`)
      const { half } = await listCards(page)
      if (half.length < 3) return fail('VIEW-ON-SEEN', `only ${JSON.stringify(half)} are on screen`)

      // The top card opened where it is, nothing scrolled.
      await openStory(page, half[0])
      await page.clock.runFor(1500)
      await requestsSettled(page)
      if (!page._events.some((e) => e.type === 'click' && e.entry_id === half[0])) fail('VIEW-ON-SEEN', `opening story ${half[0]} sent no click`)
      if (!(await waitFor(() => views().length > 0, 3000))) return fail('VIEW-ON-SEEN', `story ${half[0]} was opened and no view was sent`)
      if (JSON.stringify(views()) !== JSON.stringify([half[0]])) fail('VIEW-ON-SEEN', `after the open, views ${JSON.stringify(views())}, expected only [${half[0]}]`)

      // Scrolled off the top: the card read there sends one; the opened
      // card (used up already) sends no second one.
      await scrollCardToTop(page, half[2])
      await page.clock.runFor(SEEN_BATCH_MS + 50)
      await page.clock.runFor(1500)
      await requestsSettled(page)
      if (!(await waitFor(() => views().length > 1, 3000))) return fail('VIEW-ON-SEEN', `story ${half[1]} was seen on scroll and no view was sent`)
      if (JSON.stringify(views()) !== JSON.stringify([half[0], half[1]])) fail('VIEW-ON-SEEN', `views ${JSON.stringify(views())}, expected [${half[0]},${half[1]}]: one per story used up`)
    },
  }),

  // A card that leaves the top just before the page hides (within the
  // 300 ms the seen batch waits) goes out in the hide's keepalive PATCH.
  // A dwell the hide cuts in two starts over when the page is back.
  dashFlow('flow-seen-flush-on-hide', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
    },
    run: async ({ page, fail }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      await observersSettled(page)
      await dwell(page)
      const { half } = await listCards(page)
      const gone = half[0]
      await scrollCardToTop(page, half[1])
      if (seenAdded(writes, 'nhl').length) fail('SEEN-HIDE', `the mark went before the page hid: ${JSON.stringify(writes)}`)
      // The clock is held: neither the 300 ms batch nor the 1 s PATCH batch
      // can run, so only the hide's flush can send it.
      await setHidden(page, true)
      const keptAlive = () =>
        harnessLog(page).then((h) => h.fetches.some((f) => f.method === 'PATCH' && decodeURIComponent(f.path) === '/api/preferences/seen:nhl' && f.keepalive))
      if (!(await waitFor(keptAlive, 3000))) fail('SEEN-HIDE', 'hiding right after a card left the top sent no keepalive PATCH of seen:nhl')
      if (!(await waitFor(() => (prefMock(page.context()).value('seen:nhl') || []).includes(gone), 3000))) fail('SEEN-HIDE', `story ${gone}, seen as the page hid, never reached the store`)
      if (JSON.stringify(seenAdded(writes, 'nhl')) !== JSON.stringify([gone])) fail('SEEN-HIDE', `the hide sent ${JSON.stringify(seenAdded(writes, 'nhl'))}, expected [${gone}]`)
      await setHidden(page, false)

      // Hidden 60% into a dwell and back at once (iOS freezes a hidden
      // page's timers and fires them late, just after it is back): the
      // dwell starts over, so half a dwell after the return, past where
      // the first timer was due, the cards haven't qualified. (600 ms and
      // 500 ms at SEEN_DWELL_MS 1000; scaled, so a retune keeps the step.)
      const intoDwell = Math.round(SEEN_DWELL_MS * 0.6)
      const afterReturn = Math.round(SEEN_DWELL_MS * 0.5)
      const ids = await groupIds(page, 'unseen')
      const shown = (await listCards(page)).half
      const fresh = ids[Math.max(...shown.map((id) => ids.indexOf(id))) + 2]
      await scrollCardToTop(page, fresh)
      const freshShown = (await listCards(page)).half
      if (!freshShown.length || freshShown.some((id) => shown.includes(id))) return fail('SEEN-HIDE', `after the jump ${JSON.stringify(freshShown)} are on screen, expected new cards`)
      await page.clock.runFor(intoDwell)
      await setHidden(page, true)
      await setHidden(page, false)
      await page.clock.runFor(afterReturn)
      const after = ids[Math.max(...freshShown.map((id) => ids.indexOf(id))) + 2]
      await scrollCardToTop(page, after)
      await writeSeen(page)
      const early = seenAdded(writes, 'nhl').filter((id) => freshShown.includes(id))
      if (early.length) fail('SEEN-HIDE', `${JSON.stringify(early)} counted on a dwell the hide cut in two`)
      // Read for a full dwell after that, the next cards do count.
      const next = (await listCards(page)).half
      await dwell(page)
      await scrollCardToTop(page, ids[Math.max(...next.map((id) => ids.indexOf(id))) + 1])
      await writeSeen(page)
      if (!(await waitFor(() => next.every((id) => seenAdded(writes, 'nhl').includes(id)), 3000))) fail('SEEN-HIDE', `after a full dwell ${JSON.stringify(next)} went off the top and seen:nhl got ${JSON.stringify(seenAdded(writes, 'nhl'))}`)
    },
  }),

  // Saved is open only: its cards read on screen and scrolled off the top
  // use nothing up (no seen: PATCH, no view). Neither do the cards on
  // screen at its end (it has no marker).
  dashFlow('flow-saved-open-only', {
    before: async ({ page, context }) => {
      await installClock(page)
      await withStories(page, moreNhl(12))
      prefMock(context).seed({ starred_entries: moreNhl(12).map((e) => e.id) })
    },
    run: async ({ page, fail }) => {
      const writes = await prefWrites(page)
      await recordInteractions(page)
      await holdClock(page)
      const saved = phoneTab(page, /^saved/i)
      if (!(await waitFor(() => saved.isVisible(), 3000))) return fail('SAVED-OPEN-ONLY', 'no Saved tab')
      await tap(page, saved)
      if (!(await waitFor(async () => (await page.locator(`${MOBILE_MAIN} article[data-card-id]`).count()) === 12, 3000))) return fail('SAVED-OPEN-ONLY', `Saved shows ${await page.locator(`${MOBILE_MAIN} article[data-card-id]`).count()} cards, expected its 12 saves`)
      await observersSettled(page)
      for (let i = 0; i < 3; i++) {
        await dwell(page)
        await scrollListBy(page, 500)
      }
      await page.locator(PHONE_LIST).evaluate((el) => el.scrollTo(0, el.scrollHeight))
      await observersSettled(page)
      await dwell(page)
      await writeSeen(page)
      await page.clock.runFor(1500)
      await requestsSettled(page)
      const above = await page.locator(PHONE_LIST).evaluate((el) => [...el.querySelectorAll('article[data-card-id]')].filter((a) => a.getBoundingClientRect().bottom <= el.getBoundingClientRect().top).length)
      if (above < 3) return fail('SAVED-OPEN-ONLY', `only ${above} saved cards went off the top`)
      const marks = writes.filter((w) => w.method === 'PATCH' && w.key.startsWith('seen:'))
      if (marks.length) fail('SAVED-OPEN-ONLY', `scrolling Saved marked stories: ${JSON.stringify(marks)}`)
      const views = page._events.filter((e) => e.type === 'view')
      if (views.length) fail('SAVED-OPEN-ONLY', `scrolling Saved sent views: ${JSON.stringify(views)}`)
    },
  }),

  // The phone turned sideways (844 wide, past 768px): the phone layout is
  // hidden and nothing is tracked, so no PATCH, however long. Back upright,
  // the same cards (nothing remounted) are watched again: two read and
  // scrolled off the top send exactly those two.
  dashFlow('flow-landscape-no-marks', {
    before: async ({ page }) => {
      await installClock(page)
      await withStories(page, moreNhl(37))
    },
    run: async ({ page, fail }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      await tap(page, phoneTab(page, /^nhl/i))
      await observersSettled(page)
      await dwell(page)
      const nhlCard = await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().elementHandle()
      await page.setViewportSize({ width: 844, height: 390 })
      if (!(await waitFor(() => page.locator('main.grid article[data-card-id]').first().isVisible(), 3000))) return fail('LANDSCAPE', 'at 844 wide the desktop grid did not show')
      await observersSettled(page)
      await page.evaluate(() => {
        const w = document.querySelector('[data-scroller]')
        if (w) w.scrollTop += 600
      })
      await observersSettled(page)
      await dwell(page)
      await writeSeen(page)
      await page.clock.runFor(5000)
      await requestsSettled(page)
      if (writes.some((w) => w.method === 'PATCH' && w.key.startsWith('seen:'))) fail('LANDSCAPE', `sideways, a seen: PATCH went: ${JSON.stringify(writes)}`)

      await page.setViewportSize({ width: 390, height: 844 })
      if (!(await waitFor(() => page.locator(PHONE_LIST).isVisible(), 3000))) return fail('LANDSCAPE', 'back at 390 the phone list did not show')
      const same = await page.evaluate((el) => el.isConnected, nhlCard)
      if (!same) fail('LANDSCAPE', 'the NHL cards remounted across the turn')
      await observersSettled(page)
      await dwell(page)
      const { half, above } = await listCards(page)
      if (half.length < 3) return fail('LANDSCAPE', `back at 390 only ${JSON.stringify(half)} are on screen`)
      await scrollCardToTop(page, half[2])
      await writeSeen(page)
      const want = sortedIds(half.slice(0, 2))
      if (!(await waitFor(() => seenAdded(writes, 'nhl').length >= 2, 3000))) return fail('LANDSCAPE', `back at 390, two cards scrolled off the top sent ${JSON.stringify(writes)}`)
      if (JSON.stringify(sortedIds(seenAdded(writes, 'nhl'))) !== JSON.stringify(want)) fail('LANDSCAPE', `seen:nhl got ${JSON.stringify(seenAdded(writes, 'nhl'))}, expected exactly ${JSON.stringify(want)} (above the top before: ${JSON.stringify(above)})`)
    },
  }),

  // --- PR5a: the Reddit card --------------------------------------------------

  // ▲ and ▼ do what they do on Reddit (D2): the arrow colours and the
  // card stays; the same arrow again takes the vote back; no toast.
  dashFlow('flow-card-votes', {
    before: async ({ page }) => {
      await recordInteractions(page)
    },
    run: async ({ page, fail }) => {
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      const id = before[0]
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`)
      const up = () => card.locator('button[aria-label="upvote"], button[aria-label="remove upvote"]')
      const down = () => card.locator('button[aria-label="downvote"], button[aria-label="remove downvote"]')
      const pressed = (b) => b.getAttribute('aria-pressed')
      const noToast = async (what) => {
        await sleep(400)
        if (await page.locator('[role="status"]').count()) fail('VOTES', `${what} showed a toast: ${JSON.stringify(await page.locator('[role="status"]').textContent())}`)
      }
      await tap(page, up())
      if (!(await waitFor(async () => (await pressed(up())) === 'true', 2000))) return fail('VOTES', '▲ did not turn on')
      if (!(await waitFor(() => eventsFor(page, id, 'thumb_up') === 1, 3000))) fail('VOTES', `▲ sent ${eventsFor(page, id, 'thumb_up')} thumb_up events, expected 1`)
      if (!/text-orange-500/.test((await up().getAttribute('class')) || '')) fail('VOTES', '▲ on is not orange')
      await noToast('▲')
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(before)) fail('VOTES', `▲ moved the list: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
      // Again: off (a thumb_down cancels the thumb_up).
      await tap(page, up())
      if (!(await waitFor(async () => (await pressed(up())) === 'false', 2000))) return fail('VOTES', '▲ again did not turn off')
      if (!(await waitFor(() => eventsFor(page, id, 'thumb_down') === 1, 3000))) fail('VOTES', `▲ off sent ${eventsFor(page, id, 'thumb_down')} thumb_down events, expected 1`)
      await noToast('▲ off')
      // ▼ on, blue; ▲ then switches (two thumb_ups).
      await tap(page, down())
      if (!(await waitFor(async () => (await pressed(down())) === 'true', 2000))) return fail('VOTES', '▼ did not turn on')
      if (!/text-indigo-400/.test((await down().getAttribute('class')) || '')) fail('VOTES', '▼ on is not blue')
      if (!(await waitFor(() => eventsFor(page, id, 'thumb_down') === 2, 3000))) fail('VOTES', `▼ sent ${eventsFor(page, id, 'thumb_down') - 1} thumb_down events, expected 1`)
      await noToast('▼')
      await tap(page, up())
      if (!(await waitFor(async () => (await pressed(up())) === 'true' && (await pressed(down())) === 'false', 2000))) return fail('VOTES', '▲ after ▼ did not switch')
      if (!(await waitFor(() => eventsFor(page, id, 'thumb_up') === 3, 3000))) fail('VOTES', `the switch sent ${eventsFor(page, id, 'thumb_up') - 1} thumb_up events, expected 2`)
      await noToast('the switch')
      if (!(await card.isVisible())) fail('VOTES', 'the card went')
      // Stored as a vote, never a whole list (C17 would fail the run).
      if (!(await waitFor(() => (prefMock(page.context()).value('voted_entries') || {})[id] === 'up', 3000))) fail('VOTES', `the store holds ${JSON.stringify(prefMock(page.context()).value('voted_entries'))}`)
    },
  }),

  // Save is silent and records one `bookmark`; unsave records none and
  // shows "Removed from Saved · Undo", whose Undo saves it again.
  dashFlow('flow-card-save', {
    before: async ({ page }) => {
      await installClock(page)
      await recordInteractions(page)
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await tap(page, phoneTab(page, /^news/i))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id]`).first()
      const id = Number(await card.getAttribute('data-card-id'))
      const save = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="save"]`)
      const unsave = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="unsave"]`)
      // The clock is held from here (holdClock): each 1 s batch goes only
      // on runFor, and the unsave's toast stays up until the Undo tap
      // however slow the steps between.
      await holdClock(page)
      await tap(page, save())
      if (!(await waitFor(async () => (await unsave().count()) === 1, 2000))) return fail('SAVE', 'the bookmark did not fill')
      if ((await unsave().getAttribute('aria-pressed')) !== 'true') fail('SAVE', 'the saved bookmark is not aria-pressed')
      if (!(await waitFor(() => bookmarksFor(page, id) === 1, 3000))) fail('SAVE', `a save sent ${bookmarksFor(page, id)} bookmark events, expected 1`)
      await sleep(400)
      if (await page.locator('[role="status"]').count()) fail('SAVE', `saving showed a toast: ${JSON.stringify(await page.locator('[role="status"]').textContent())}`)
      // The save's PATCH goes with the next 1 s batch; unsave after it
      // (an unsave inside the batch would fold the two into nothing).
      await page.clock.runFor(1000)
      if (!(await waitFor(() => patched(writes, 'starred_entries', 'add').includes(id), 3000))) return fail('SAVE', `the save sent no PATCH adding ${id}: ${JSON.stringify(writes)}`)
      await tap(page, unsave())
      if (!(await waitFor(async () => (await save().count()) === 1, 2000))) return fail('SAVE', 'unsaving did not empty the bookmark')
      const toastText = page.locator('[role="status"]').getByText('Removed from Saved', { exact: true })
      if (!(await waitFor(() => toastText.isVisible(), 2000))) return fail('SAVE', 'no "Removed from Saved" toast')
      await shot('unsaved')
      // The unsave's PATCH goes with the next 1 s batch (an Undo inside
      // the batch would fold the two into nothing).
      await page.clock.runFor(1000)
      if (!(await waitFor(() => patched(writes, 'starred_entries', 'remove').includes(id), 3000))) return fail('SAVE', `the unsave sent no PATCH removing ${id}: ${JSON.stringify(writes)}`)
      if (!(await waitFor(() => !(prefMock(page.context()).value('starred_entries') || []).includes(id), 3000))) fail('SAVE', 'the story is still in the store after the unsave')
      if (bookmarksFor(page, id) !== 1) fail('SAVE', `an unsave sent a bookmark (${bookmarksFor(page, id)} in all)`)
      await tap(page, undoButton(page))
      if (!(await waitFor(async () => (await unsave().count()) === 1, 2000))) return fail('SAVE', 'Undo did not save the story again')
      await sleep(400)
      if (bookmarksFor(page, id) !== 1) fail('SAVE', `Undo sent a bookmark (${bookmarksFor(page, id)} in all)`)
      await page.clock.runFor(1000)
      if (!(await waitFor(() => (prefMock(page.context()).value('starred_entries') || []).includes(id), 3000))) fail('SAVE', 'the story is not in the store after Undo')
      const ops = writes.filter((w) => w.key === 'starred_entries').map((w) => ((w.body.add || []).length ? 'add' : 'remove'))
      if (JSON.stringify(ops) !== JSON.stringify(['add', 'remove', 'add'])) fail('SAVE', `starred_entries saw ${JSON.stringify(ops)}, expected add, remove, add`)
    },
  }),

  // Share hands the story's own address to the system sheet and records
  // `share` once it is done; without a sheet it copies the address; a
  // story whose address isn't http(s) is neither shared nor copied.
  dashFlow('flow-card-share', {
    before: async ({ page }) => {
      await recordInteractions(page)
      await withStories(page, [
        { ...F.ENTRIES.find((e) => e.id === 103), id: 9901, title: 'A story with a script address', url: 'javascript:alert(1)', published_at: new Date(F.NOW - 60000).toISOString() },
      ])
    },
    run: async ({ page, fail }) => {
      await tap(page, phoneTab(page, /^news/i))
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id="103"]`)
      const share = () => card.locator('button[aria-label="share"]')
      await tap(page, share())
      const shared = () => harnessLog(page).then((h) => h.shared)
      if (!(await waitFor(async () => (await shared()).length === 1, 2000))) return fail('SHARE', 'Share called nothing')
      const [s] = await shared()
      const url = F.ENTRIES.find((e) => e.id === 103).url
      if (s.url !== url || !s.title) fail('SHARE', `navigator.share got ${JSON.stringify(s)}, expected the story's title and ${url}`)
      if (!(await waitFor(() => eventsFor(page, 103, 'share') === 1, 3000))) fail('SHARE', `a share sent ${eventsFor(page, 103, 'share')} share events, expected 1`)
      if ((await harnessLog(page)).copied.length) fail('SHARE', 'a share also copied the address')
      // The sheet dismissed (the promise rejects with AbortError): no
      // `share`, nothing copied, no toast.
      await page.evaluate(() => Object.defineProperty(navigator, 'share', { configurable: true, value: () => Promise.reject(new DOMException('dismissed', 'AbortError')) }))
      await tap(page, share())
      await sleep(400)
      if (eventsFor(page, 103, 'share') !== 1) fail('SHARE', `a dismissed share sent a share event (${eventsFor(page, 103, 'share')} in all)`)
      if ((await harnessLog(page)).copied.length) fail('SHARE', 'a dismissed share copied the address')
      if (await page.locator('[role="status"]').count()) fail('SHARE', `a dismissed share showed a toast: ${JSON.stringify(await page.locator('[role="status"]').textContent())}`)
      // No system sheet: the address is copied instead.
      await page.evaluate(() => Object.defineProperty(navigator, 'share', { configurable: true, value: undefined }))
      await tap(page, share())
      const copied = () => harnessLog(page).then((h) => h.copied)
      if (!(await waitFor(async () => (await copied()).length === 1, 2000))) return fail('SHARE', 'without navigator.share, Share copied nothing')
      if ((await copied())[0] !== url) fail('SHARE', `the clipboard holds ${JSON.stringify(await copied())}`)
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('Link copied', { exact: true }).isVisible(), 2000))) fail('SHARE', 'no "Link copied" toast')
      await sleep(400)
      if (eventsFor(page, 103, 'share') !== 1) fail('SHARE', `the copy sent a share event (${eventsFor(page, 103, 'share')} in all)`)
      // A javascript: address: Share is disabled, and its sheet's Copy
      // link and Open in browser too.
      const js = page.locator(`${MOBILE_MAIN} article[data-card-id="9901"]`)
      await js.scrollIntoViewIfNeeded()
      if (!(await js.locator('button[aria-label="share"]').isDisabled())) fail('SHARE', 'Share is enabled for a javascript: address')
      await js.locator('button[aria-label="share"]').evaluate((b) => b.click())
      await sleep(300)
      if ((await shared()).length !== 1 || (await copied()).length !== 1) fail('SHARE', `the javascript: story was shared or copied: ${JSON.stringify(await harnessLog(page))}`)
      await openSheet(page, js)
      for (const row of ['Copy link', 'Open in browser']) {
        if (!(await sheetRow(page, row).isDisabled())) fail('SHARE', `${row} is enabled for a javascript: address`)
      }
      await page.keyboard.press('Escape')
      if (!(await waitFor(async () => (await page.locator(SHEET).count()) === 0, 2000))) fail('SHARE', 'Esc did not close the sheet')
    },
  }),

  // The ⋯ sheet: four 44px rows, the focus trap, Esc (which keeps a
  // search's query), and after Hide the focus on the next card's headline.
  dashFlow('flow-card-sheet', {
    run: async ({ page, fail, shot }) => {
      await tap(page, phoneTab(page, /^news/i))
      const ids = await groupIds(page, 'unseen')
      const card = (id) => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`)
      await openSheet(page, card(ids[0]))
      await shot('sheet')
      const rows = await page.$$eval(`${SHEET} button[data-sheet-action]`, (els) => els.map((b) => ({ text: b.textContent.trim(), h: b.getBoundingClientRect().height, disabled: b.disabled })))
      if (JSON.stringify(rows.map((r) => r.text)) !== JSON.stringify(['Hide', 'Edit feed', 'Copy link', 'Open in browser'])) fail('SHEET', `the rows are ${JSON.stringify(rows.map((r) => r.text))}`)
      for (const r of rows) {
        if (r.h < 43.5) fail('SHEET', `"${r.text}" is ${r.h}px tall`)
        if (r.disabled) fail('SHEET', `"${r.text}" is disabled`)
      }
      const cancel = page.locator(`${SHEET} button[data-sheet-cancel]`)
      if ((await cancel.boundingBox())?.height < 43.5) fail('SHEET', 'Cancel is under 44px')
      // No shortcut acts on the page behind the sheet: `/` opens no search.
      await page.keyboard.press('/')
      await sleep(300)
      if (await page.locator('#app-search').count()) fail('SHEET', '`/` opened the search behind the sheet')
      // Focus starts on Cancel and the trap cycles: Tab wraps to Hide,
      // Shift+Tab back to Cancel.
      if ((await focusedNow(page)) !== 'BUTTON[data-sheet-cancel]') fail('SHEET', `focus starts on ${await focusedNow(page)}, expected Cancel`)
      await page.keyboard.press('Tab')
      if (!(await waitFor(() => page.evaluate(() => document.activeElement?.textContent?.trim() === 'Hide' && !!document.activeElement.closest('[aria-label="story actions"]')), 1000))) fail('SHEET', `Tab from Cancel went to ${await focusedNow(page)}, expected Hide`)
      await page.keyboard.press('Shift+Tab')
      if (!(await waitFor(async () => (await focusedNow(page)) === 'BUTTON[data-sheet-cancel]', 1000))) fail('SHEET', `Shift+Tab from Hide went to ${await focusedNow(page)}, expected Cancel`)
      // Esc closes and puts the focus back on ⋯.
      await page.keyboard.press('Escape')
      if (!(await waitFor(async () => (await page.locator(SHEET).count()) === 0, 2000))) return fail('SHEET', 'Esc did not close the sheet')
      if (!(await waitFor(() => page.evaluate((id) => document.activeElement?.getAttribute('aria-label') === 'more actions' && document.activeElement.closest(`article[data-card-id="${id}"]`) !== null, ids[0]), 2000))) fail('SHEET', `after Esc the focus is on ${await focusedNow(page)}, expected the card's ⋯`)
      // A tap outside closes it too.
      await openSheet(page, card(ids[0]))
      await page.mouse.click(200, 40)
      if (!(await waitFor(async () => (await page.locator(SHEET).count()) === 0, 2000))) fail('SHEET', 'a tap outside did not close the sheet')
      // Edit feed opens that feed in Settings, which has the page now:
      // the focus doesn't go back to the card's ⋯ under it.
      await openSheet(page, card(ids[0]))
      await tap(page, sheetRow(page, 'Edit feed'))
      await page.locator(SETTINGS).waitFor({ state: 'visible' })
      if (!/[?&]source=\d+/.test(page.url())) fail('SHEET', `Edit feed opened ${page.url()}, not a feed's editor`)
      await sleep(300)
      if (/\[data-card-more\]/.test(await focusedNow(page))) fail('SHEET', 'after Edit feed the focus went back to the card\'s ⋯ under Settings')
      await closeSettings(page)
      await page.locator(SETTINGS).waitFor({ state: 'hidden' })
      // From a search result, Esc keeps the query.
      await tap(page, page.locator('button[aria-label="open search"]'))
      await page.locator('#app-search').fill('Pettersson')
      const result = page.locator('main article[data-card-id="601"]').first()
      await result.waitFor({ state: 'visible' })
      await openSheet(page, result)
      await page.keyboard.press('Escape')
      if (!(await waitFor(async () => (await page.locator(SHEET).count()) === 0, 2000))) return fail('SHEET', "Esc did not close a search result's sheet")
      await sleep(300)
      if ((await page.locator('#app-search').inputValue()) !== 'Pettersson') fail('SHEET', `Esc on the sheet cleared the search: ${JSON.stringify(await page.locator('#app-search').inputValue())}`)
      if (!(await result.isVisible())) fail('SHEET', 'Esc on the sheet took the search results away')
      // Hide on the only result: no card is left to take the focus and
      // the tab strip is away, so it lands on the search field.
      await hideViaSheet(page, result)
      if (!(await waitFor(async () => (await result.count()) === 0, 2000))) return fail('SHEET', 'Hide left the search result in place')
      if (!(await waitFor(() => page.evaluate(() => document.activeElement?.id === 'app-search'), 2000))) fail('SHEET', `after hiding the only search result the focus is on ${await focusedNow(page)}, expected the search field`)
      await tap(page, page.locator('button[aria-label="clear search"]'))
      await page.locator(`${MOBILE_MAIN} article[data-card-id]`).first().waitFor({ state: 'visible' })
      // Hide from the sheet: the card goes and the focus lands on the
      // next card's headline.
      await hideViaSheet(page, card(ids[0]))
      if (!(await waitFor(async () => (await card(ids[0]).count()) === 0, 2000))) return fail('SHEET', 'Hide left the card in place')
      if (!(await waitFor(() => page.evaluate((id) => document.activeElement?.getAttribute('aria-label') === 'open in new tab' && document.activeElement.closest(`article[data-card-id="${id}"]`) !== null, ids[1]), 2000))) {
        fail('SHEET', `after Hide the focus is on ${await focusedNow(page)}, expected the next card's headline`)
      }
      await shot('hidden')
    },
  }),

  // A hide's `never` is held on the device (D25): Undo cancels it and
  // puts the story back in its place; the toast expiring sends it once.
  dashFlow('flow-hide-undo', {
    before: async ({ page }) => {
      await installClock(page)
      await recordInteractions(page)
      // The beacon is refused, so the page hiding sends a held `never`
      // with fetch and keepalive, which the harness's fetch log shows.
      await page.addInitScript(() => {
        Object.defineProperty(navigator, 'sendBeacon', { configurable: true, value: () => false })
      })
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      const [first, second] = before
      const card = (id) => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`)
      // The clock is held from here (holdClock): the toast's 5 s and each
      // 1 s batch run only on runFor, so a slow step on a loaded machine
      // can't let the toast go before the flow taps Undo.
      await holdClock(page)
      await hideViaSheetHeld(page, card(first))
      if (!(await waitFor(async () => (await card(first).count()) === 0, 2000))) return fail('HIDE-UNDO', 'Hide left the card in place')
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('Hidden', { exact: true }).isVisible(), 2000))) return fail('HIDE-UNDO', 'no "Hidden" toast')
      await shot('hidden')
      // The hide's PATCH goes with the next 1 s batch; Undo after it, so
      // its own PATCH (a remove) is seen too.
      await page.clock.runFor(1000)
      if (!(await waitFor(() => patched(writes, 'hidden_entries', 'add').includes(first), 3000))) return fail('HIDE-UNDO', `the hide sent no PATCH adding ${first}: ${JSON.stringify(writes)}`)
      await tap(page, undoButton(page))
      if (!(await waitFor(async () => (await card(first).count()) === 1, 2000))) return fail('HIDE-UNDO', 'Undo did not bring the story back')
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(before)) fail('HIDE-UNDO', `after Undo the list is ${JSON.stringify(await groupIds(page, 'unseen'))}, expected ${JSON.stringify(before)}`)
      // Past the toast's window: the `never` was cancelled, not sent.
      await page.clock.runFor(6000)
      await requestsSettled(page)
      if (neversFor(page, first) !== 0) fail('HIDE-UNDO', `Hide then Undo sent ${neversFor(page, first)} never events`)
      if (!(await waitFor(() => patched(writes, 'hidden_entries', 'add').includes(first) && patched(writes, 'hidden_entries', 'remove').includes(first), 3000))) fail('HIDE-UNDO', `hidden_entries saw ${JSON.stringify(writes.filter((w) => w.key === 'hidden_entries'))}`)
      if ((prefMock(page.context()).value('hidden_entries') || []).includes(first)) fail('HIDE-UNDO', 'the story is still hidden in the store after Undo')
      // Hide and let the toast go: exactly one `never`.
      await hideViaSheetHeld(page, card(second))
      if (!(await waitFor(async () => (await card(second).count()) === 0, 2000))) return fail('HIDE-UNDO', 'the second Hide left the card in place')
      await page.clock.runFor(2000)
      await requestsSettled(page)
      if (neversFor(page, second) !== 0) fail('HIDE-UNDO', `the never was sent while the toast still offered Undo (${neversFor(page, second)})`)
      await page.clock.runFor(3500)
      if (!(await waitFor(async () => (await page.locator('[role="status"]').count()) === 0, 2000))) fail('HIDE-UNDO', 'the toast is still up after 5 s')
      await requestsSettled(page)
      if (!(await waitFor(() => neversFor(page, second) === 1, 3000))) fail('HIDE-UNDO', `after the toast, ${neversFor(page, second)} never events, expected 1`)
      await page.clock.runFor(3000)
      await requestsSettled(page)
      if (neversFor(page, second) !== 1) fail('HIDE-UNDO', `a second never went later (${neversFor(page, second)})`)
      if (!(await waitFor(() => (prefMock(page.context()).value('hidden_entries') || []).includes(second), 3000))) fail('HIDE-UNDO', 'the hidden story never reached the store')
      // A hide, then the page hidden at once: the held `never` goes with
      // the hide's keepalive POST, ahead of the toast.
      const third = before[2]
      await hideViaSheetHeld(page, card(third))
      if (!(await waitFor(async () => (await card(third).count()) === 0, 2000))) return fail('HIDE-UNDO', 'the third Hide left the card in place')
      await setHidden(page, true)
      const keptAlive = () => harnessLog(page).then((h) => h.fetches.some((f) => f.method === 'POST' && f.path === '/api/interactions/batch' && f.keepalive))
      if (!(await waitFor(keptAlive, 3000))) fail('HIDE-UNDO', 'hiding the page right after a Hide sent no keepalive POST /api/interactions/batch')
      if (!(await waitFor(() => neversFor(page, third) === 1, 3000))) fail('HIDE-UNDO', `the page hiding sent ${neversFor(page, third)} never events for the third story, expected 1`)
      await setHidden(page, false)
      await page.clock.runFor(6000)
      await requestsSettled(page)
      if (neversFor(page, third) !== 1) fail('HIDE-UNDO', `the toast expiring sent the never again (${neversFor(page, third)})`)
    },
  }),

  // Keyboard `h` on the desktop: the selection goes with the hidden
  // card, so `h` again (or a held key's repeat), `b` and `a` act on
  // nothing; one "Hidden" toast and one `never` per hide, none after
  // Undo. A held `b` or `a` fires once.
  dashFlow('flow-hide-keys-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    before: async ({ page }) => {
      await installClock(page)
      await recordInteractions(page)
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      const sel = 'article[data-card-id][tabindex="0"]'
      const selected = async () => ((await page.locator(sel).count()) ? Number(await page.locator(sel).first().getAttribute('data-card-id')) : null)
      const toasts = () => page.locator('[role="status"]').count()
      // The first column is Saved, empty: step to the next, then down.
      await page.keyboard.press('ArrowRight')
      await page.keyboard.press('ArrowDown')
      if (!(await waitFor(async () => (await selected()) !== null, 3000))) return fail('HIDE-KEYS', 'ArrowDown selected no card')
      const id = await selected()
      const card = () => page.locator(`main.grid article[data-card-id="${id}"]`)
      await page.keyboard.press('h')
      if (!(await waitFor(async () => (await card().count()) === 0, 2000))) return fail('HIDE-KEYS', 'h did not hide the selected card')
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('Hidden', { exact: true }).isVisible(), 2000))) return fail('HIDE-KEYS', 'no "Hidden" toast')
      await shot('hidden')
      if ((await selected()) !== null) fail('HIDE-KEYS', `after h the selection is still on card ${await selected()}`)
      // h again, then b and a: nothing is drawn to act on.
      await page.keyboard.press('h')
      await page.keyboard.press('b')
      await page.keyboard.press('a')
      await sleep(800)
      if ((await toasts()) !== 1) fail('HIDE-KEYS', `a second h left ${await toasts()} toasts up, expected the one`)
      if (!(await page.locator('[role="status"]').getByText('Hidden', { exact: true }).isVisible())) fail('HIDE-KEYS', 'the toast after a second h is not the first "Hidden"')
      if (eventsFor(page, id, 'bookmark') !== 0) fail('HIDE-KEYS', `b after a hide sent ${eventsFor(page, id, 'bookmark')} bookmark events`)
      if (eventsFor(page, id, 'thumb_up') !== 0) fail('HIDE-KEYS', `a after a hide sent ${eventsFor(page, id, 'thumb_up')} thumb_up events`)
      // The hide's PATCH goes with the 1 s batch; one add, nothing else.
      await page.clock.runFor(1500)
      await requestsSettled(page)
      if (writes.some((w) => w.key === 'starred_entries' || w.key === 'voted_entries')) fail('HIDE-KEYS', `b / a after a hide wrote ${JSON.stringify(writes.filter((w) => w.key !== 'hidden_entries'))}`)
      const adds = patched(writes, 'hidden_entries', 'add').filter((x) => x === id)
      if (adds.length !== 1) fail('HIDE-KEYS', `hidden_entries saw ${adds.length} adds of ${id}, expected 1`)
      // Past the toast's window: exactly one `never`, not two.
      await page.clock.runFor(6000)
      if (!(await waitFor(async () => (await toasts()) === 0, 2000))) fail('HIDE-KEYS', 'the toast is still up after 5 s')
      await requestsSettled(page)
      if (!(await waitFor(() => neversFor(page, id) === 1, 3000))) fail('HIDE-KEYS', `after the toast, ${neversFor(page, id)} never events for ${id}, expected 1`)
      await page.clock.runFor(3000)
      await requestsSettled(page)
      if (neversFor(page, id) !== 1) fail('HIDE-KEYS', `a second never went later (${neversFor(page, id)})`)
      // Select the next card, h twice, then Undo: the story is back, the
      // selection stays off it, and no `never` goes.
      await page.keyboard.press('ArrowDown')
      if (!(await waitFor(async () => (await selected()) !== null, 3000))) return fail('HIDE-KEYS', 'ArrowDown after the hide selected no card')
      const second = await selected()
      const card2 = () => page.locator(`main.grid article[data-card-id="${second}"]`)
      await page.keyboard.press('h')
      if (!(await waitFor(async () => (await card2().count()) === 0, 2000))) return fail('HIDE-KEYS', 'h did not hide the second card')
      await page.keyboard.press('h')
      await sleep(300)
      if ((await toasts()) !== 1) fail('HIDE-KEYS', `h twice left ${await toasts()} toasts up`)
      await undoButton(page).click()
      if (!(await waitFor(async () => (await card2().count()) === 1, 2000))) return fail('HIDE-KEYS', 'Undo did not bring the second story back')
      if ((await selected()) !== null) fail('HIDE-KEYS', `after Undo the selection is on card ${await selected()}`)
      await page.clock.runFor(6000)
      await requestsSettled(page)
      if (neversFor(page, second) !== 0) fail('HIDE-KEYS', `h twice then Undo sent ${neversFor(page, second)} never events`)
      if ((prefMock(page.context()).value('hidden_entries') || []).includes(second)) fail('HIDE-KEYS', 'the second story is still hidden in the store after Undo')
      // A held b (keydown, then its repeat) saves once; a held a votes
      // once. A saved card is drawn in Saved too, so every copy is read.
      await page.keyboard.press('ArrowDown')
      if (!(await waitFor(async () => (await selected()) !== null, 3000))) return fail('HIDE-KEYS', 'ArrowDown after Undo selected no card')
      const third = await selected()
      const labels = (sel2) => page.locator(`main.grid article[data-card-id="${third}"] ${sel2}`).evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')))
      const allAre = async (sel2, label) => {
        const got = await labels(sel2)
        return got.length > 0 && got.every((l) => l === label)
      }
      await page.keyboard.down('b')
      await page.keyboard.down('b')
      await page.keyboard.up('b')
      if (!(await waitFor(() => allAre('button[data-star]', 'unsave'), 2000))) return fail('HIDE-KEYS', `a held b did not save the card (or its repeat unsaved it): ${JSON.stringify(await labels('button[data-star]'))}`)
      await sleep(400)
      if (!(await allAre('button[data-star]', 'unsave'))) fail('HIDE-KEYS', `the repeat of a held b unsaved the card: ${JSON.stringify(await labels('button[data-star]'))}`)
      if (eventsFor(page, third, 'bookmark') !== 1) fail('HIDE-KEYS', `a held b sent ${eventsFor(page, third, 'bookmark')} bookmark events, expected 1`)
      const UP = 'button[aria-label="upvote"], button[aria-label="remove upvote"]'
      await page.keyboard.down('a')
      await page.keyboard.down('a')
      await page.keyboard.up('a')
      if (!(await waitFor(() => allAre(UP, 'remove upvote'), 2000))) return fail('HIDE-KEYS', `a held a did not vote the card up (or its repeat took it back): ${JSON.stringify(await labels(UP))}`)
      await sleep(400)
      if (!(await allAre(UP, 'remove upvote'))) fail('HIDE-KEYS', `the repeat of a held a took the vote back: ${JSON.stringify(await labels(UP))}`)
      if (eventsFor(page, third, 'thumb_up') !== 1 || eventsFor(page, third, 'thumb_down') !== 0) fail('HIDE-KEYS', `a held a sent ${eventsFor(page, third, 'thumb_up')} thumb_up and ${eventsFor(page, third, 'thumb_down')} thumb_down events, expected 1 and 0`)
    },
  }),

  // Swipe left hides, with Undo (CDP touches; Chromium only).
  dashFlow('flow-swipe-left-hide', {
    before: async ({ page }) => {
      await installClock(page)
    },
    run: async ({ page, fail, shot }) => {
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      const id = before[0]
      const card = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`)
      // Held from here (holdClock), so the toast stays up until the Undo
      // tap however slow the steps between (a swipe commits on touchend,
      // with no timer).
      await holdClock(page)
      if (!(await swipeCard(page, card(), -160))) return
      if (!(await waitFor(async () => (await card().count()) === 0, 2000))) return fail('SWIPE-LEFT', 'a swipe left did not hide the story')
      if (!(await waitFor(() => page.locator('[role="status"]').getByText('Hidden', { exact: true }).isVisible(), 2000))) return fail('SWIPE-LEFT', 'no "Hidden" toast')
      await shot('hidden')
      if ((await harnessLog(page)).opened.length) fail('SWIPE-LEFT', 'the swipe opened the story')
      await tap(page, undoButton(page))
      if (!(await waitFor(async () => (await card().count()) === 1, 2000))) return fail('SWIPE-LEFT', 'Undo did not bring the story back')
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(before)) fail('SWIPE-LEFT', `after Undo the list is ${JSON.stringify(await groupIds(page, 'unseen'))}`)
    },
  }),

  // Swipe right does nothing: the card doesn't move and stays.
  dashFlow('flow-swipe-right-dead', {
    run: async ({ page, fail }) => {
      const writes = await prefWrites(page)
      await tap(page, phoneTab(page, /^news/i))
      const before = await groupIds(page, 'unseen')
      const id = before[0]
      const card = () => page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"]`)
      // Any transform during the drag is a failure: the card never
      // follows a finger going right.
      await card().evaluate((el) => {
        el.__moved = []
        const mo = new MutationObserver(() => {
          if (el.style.transform) el.__moved.push(el.style.transform)
        })
        mo.observe(el, { attributes: true, attributeFilter: ['style'] })
      })
      if (!(await swipeCard(page, card(), 160))) return
      await sleep(500)
      if ((await card().count()) !== 1) return fail('SWIPE-RIGHT', 'a swipe right took the card away')
      const moved = await card().evaluate((el) => el.__moved)
      if (moved.length) fail('SWIPE-RIGHT', `the card moved with a swipe right: ${JSON.stringify(moved)}`)
      if (await page.locator('[role="status"]').count()) fail('SWIPE-RIGHT', `a swipe right showed a toast: ${JSON.stringify(await page.locator('[role="status"]').textContent())}`)
      if (writes.some((w) => w.key === 'hidden_entries' || w.key.startsWith('seen:'))) fail('SWIPE-RIGHT', `a swipe right wrote ${JSON.stringify(writes)}`)
      if ((await harnessLog(page)).opened.length) fail('SWIPE-RIGHT', 'the swipe opened the story')
      if (JSON.stringify(await groupIds(page, 'unseen')) !== JSON.stringify(before)) fail('SWIPE-RIGHT', `the list changed: ${JSON.stringify(await groupIds(page, 'unseen'))}`)
    },
  }),

  // The preview under the headline (D34): clamped to 2 lines on the
  // phone, gone while the Summary panel is open, and one open target
  // with the title: a tap on it opens the story and marks it seen.
  dashFlow('flow-card-preview', {
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      // For You, the first tab: story 101 (news) has a 300-character blurb.
      const card = page.locator(`${MOBILE_MAIN} article[data-card-id="101"]`)
      const preview = card.locator('[data-preview]')
      if (!(await preview.count())) return fail('PREVIEW', 'story 101 shows no preview')
      const box = await preview.evaluate((el) => {
        const cs = getComputedStyle(el)
        return { clamp: cs.webkitLineClamp, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, lineHeight: parseFloat(cs.lineHeight), inLink: !!el.closest('a[aria-label="open in new tab"]'), text: el.textContent }
      })
      if (box.clamp !== '2') fail('PREVIEW', `the preview's line clamp is ${JSON.stringify(box.clamp)}, expected 2 on a phone`)
      if (box.clientHeight > box.lineHeight * 2 + 1) fail('PREVIEW', `the preview is ${box.clientHeight}px tall, more than 2 lines of ${box.lineHeight}px`)
      if (box.scrollHeight <= box.clientHeight) fail('PREVIEW', 'a 300-character blurb was not cut by the clamp')
      if (!box.inLink) fail('PREVIEW', 'the preview is not inside the headline link')
      if (!box.text.startsWith('The federal infrastructure minister')) fail('PREVIEW', `the preview reads ${JSON.stringify(box.text.slice(0, 60))}`)
      // A story the server sent no preview for shows none.
      await tap(page, phoneTab(page, /^news/i))
      if (await page.locator(`${MOBILE_MAIN} article[data-card-id="103"] [data-preview]`).count()) fail('PREVIEW', 'story 103 shows a preview although the server sent none')
      await tap(page, phoneTab(page, /^for you/i))
      await shot('preview')
      // The Summary panel takes its place while open.
      await tap(page, card.locator('button[aria-label="show summary"]'))
      if (!(await waitFor(async () => (await preview.count()) === 0, 2000))) fail('PREVIEW', 'the preview stays while the Summary panel is open')
      await tap(page, card.locator('button[aria-label="hide summary"]'))
      if (!(await waitFor(async () => (await preview.count()) === 1, 2000))) fail('PREVIEW', 'the preview did not come back once the Summary panel closed')
      // A tap on the blurb opens the story and marks it seen.
      await tap(page, preview)
      const log = await harnessLog(page)
      if (!log.opened.some((o) => o.target === '_blank' && o.url.endsWith('/3/article-101'))) fail('PREVIEW', `a tap on the preview opened ${JSON.stringify(log.opened)}`)
      if (!(await waitFor(() => seenAdded(writes, 'news').includes(101), 3000))) fail('PREVIEW', `the open sent no PATCH adding 101 to seen:news: ${JSON.stringify(writes)}`)
    },
  }),

  // --- PR4d: the window ---------------------------------------------------------
  // (``oldDevice``, above: a device that loaded the read model a month
  // ago, so D33's baseline doesn't count the 3- and 10-day fixture
  // stories as seen.)

  // Every tab loads the last 2 days (D5), the 25 best stories per feed in
  // them (D28), from the feeds that aren't paused; For You loads 50 of
  // the same 2 days. So Podcast is 401 and 402 (403 and 404 are the
  // paused 12's, 405 is 4 days old), Video 501 to 504 (505 and 506 are
  // older), Security has no 304 or 305 (10 is paused). The ⋯ offers Show
  // stories from (1 day · 2 days · 7 days · All) with 2 days chosen, and
  // no "Hide if older than"; Saved's ⋯ offers no window.
  dashFlow('flow-window-default', {
    before: async ({ page }) => {
      await installClock(page)
      page._gets = []
      await page.route(
        (u) => /^\/api\/(entries|foryou)$/.test(new URL(u).pathname),
        (route) => {
          const u = new URL(route.request().url())
          page._gets.push(u.pathname + u.search)
          return route.fallback()
        },
      )
    },
    run: async ({ page, fail, shot }) => {
      await holdClock(page)
      const first = page._gets.slice(0, 2).sort()
      const want = ['/api/entries?per_category_limit=100&max_age_hours=48&per_source_cap=25&active_only=true', '/api/foryou?limit=50&max_age_hours=48']
      if (JSON.stringify(first) !== JSON.stringify(want)) fail('WINDOW', `the first load asked ${JSON.stringify(first)}, expected ${JSON.stringify(want)}`)
      const podcast = await openedIds(page, /^podcast/i)
      if (JSON.stringify(podcast) !== JSON.stringify([401, 402])) fail('WINDOW', `Podcast shows ${podcast}, expected 401,402 (2 days, no paused feed)`)
      const video = await openedIds(page, /^video/i)
      if (JSON.stringify(video) !== JSON.stringify([501, 502, 503, 504])) fail('WINDOW', `Video shows ${video}, expected 501,502,503,504`)
      const security = await openedIds(page, /^security/i)
      if (security.some((id) => id === 304 || id === 305)) fail('WINDOW', `Security shows the paused feed's ${security.filter((id) => id === 304 || id === 305)}`)
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      const windows = await page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window]`).evaluateAll((els) => els.map((el) => [el.getAttribute('data-window'), el.getAttribute('aria-pressed')]))
      if (JSON.stringify(windows) !== JSON.stringify([['1d', 'false'], ['2d', 'true'], ['7d', 'false'], ['all', 'false']])) fail('WINDOW', `Show stories from reads ${JSON.stringify(windows)}`)
      if (await page.locator(`${MOBILE_MAIN} [data-column-popover] select`).count()) fail('WINDOW', 'the "Hide if older than" select is still there')
      await shot('popover')
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      // Saved offers no window, and no ⋯ at all: nothing in it would apply.
      await tap(page, phoneTab(page, /^saved/i))
      if (!(await waitFor(async () => /^saved/i.test(await selectedTab(page)), 3000))) return fail('WINDOW', 'Saved did not open')
      if (await page.locator(`${MOBILE_MAIN} [data-column-action="prefs"]`).count()) fail('WINDOW', 'Saved has a ⋯ (column preferences) button')
    },
  }),

  // The owner's bug, which the window fixes (D5, D28): the dashboard load
  // used to ask per_category_limit=100 alone, cut by all-time score, so a
  // quiet feed in a busy category (BNN Bloomberg in News) never reached
  // its tab: the busy feeds' older, higher-scoring stories took all 100
  // places. Seeded in the mock's store (``dbMock``, so the server's cuts
  // apply, as they don't to ``withStories``): hn_top (News) gets 30
  // stories in the last 2 days scoring 99 to 90.3 (7001 to 7030) and 90
  // older ones, 3 to 20 days, scoring 89.9 to 81 (7101 to 7190); a new
  // News feed, bnn_bloomberg (30), brings 3 in the last half hour scoring
  // 8 to 12 (7201 to 7203). The load cuts hn_top to its 25 best of the 2
  // days (7001 to 7025; the fixtures' 102, 107 and 108 go too), then News
  // to its newest 100, which hold everything left: News shows bnn's 3
  // and every other News feed's 2-day stories, and 25 of hn_top's. Cut
  // by score alone, hn_top's 120 (and 101) take the 100 places, bnn is
  // out, and the device's 2-day cut shows 30 of hn_top's. The clock is
  // held: no card is used up and no poll lands while the list is read to
  // its end.
  dashFlow('flow-busy-feed-no-crowding', {
    before: async ({ page, context }) => {
      const db = dbMock(context)
      db.addSources([{ ...F.SOURCES.find((s) => s.id === 1), id: 30, name: 'bnn_bloomberg', url: 'https://www.bnnbloomberg.ca/arc/outboundfeeds/rss/', favicon_path: null, favicon_url: null, net_vote_score: 0 }])
      db.addEntries([
        ...Array.from({ length: 30 }, (_, i) => F.mkEntry(7001 + i, 2, `Busy feed, today, story ${i + 1}`, { score: 99 - i * 0.3, ago: 60 + i * 80, preview: null })),
        ...Array.from({ length: 90 }, (_, i) => F.mkEntry(7101 + i, 2, `Busy feed, ${3 + Math.floor(i / 5)} days ago, story ${i + 1}`, { score: 89.9 - i * 0.1, ago: 3 * 24 * 60 + i * 270, preview: null })),
        ...[0, 1, 2].map((i) => F.mkEntry(7201 + i, 30, `Markets close mixed, update ${i + 1}`, { score: 8 + i * 2, ago: 5 + i * 10, preview: null })),
      ])
      await installClock(page)
    },
    run: async ({ page, fail, shot }) => {
      await holdClock(page)
      const first = await openedIds(page, /^news/i)
      if (!first.length) return fail('CROWDING', 'News drew no card')
      if (!(await phoneListToEnd(page))) return fail('CROWDING', 'the News list never loaded its last batch')
      const ids = (await phoneIds(page)).split(',').map(Number)
      await shot('news')
      const quiet = [7201, 7202, 7203]
      const missing = quiet.filter((id) => !ids.includes(id))
      if (missing.length) fail('CROWDING', `News lacks bnn_bloomberg's ${JSON.stringify(missing)} (it shows ${ids.length} stories: ${JSON.stringify(ids)})`)
      const hnIds = new Set([...F.ENTRIES.filter((e) => e.source_id === 2).map((e) => e.id), ...Array.from({ length: 30 }, (_, i) => 7001 + i), ...Array.from({ length: 90 }, (_, i) => 7101 + i)])
      const busy = sortedIds(ids.filter((id) => hnIds.has(id)))
      const want = Array.from({ length: 25 }, (_, i) => 7001 + i)
      if (busy.length > 25) fail('CROWDING', `News shows ${busy.length} of hn_top's stories, more than the 25 the per-feed cap keeps`)
      else if (JSON.stringify(busy) !== JSON.stringify(want)) fail('CROWDING', `News shows hn_top's ${JSON.stringify(busy)}, expected its 25 best of the 2 days, 7001 to 7025`)
      // Every other News feed's 2-day stories are there too.
      const others = F.ENTRIES.filter((e) => e.source_id !== 2 && F.SOURCES.find((s) => s.id === e.source_id).category === 'news' && Date.parse(e.fetched_at) >= F.NOW - 48 * 3600000).map((e) => e.id)
      const lost = others.filter((id) => !ids.includes(id))
      if (lost.length) fail('CROWDING', `News lacks the other feeds' 2-day stories ${JSON.stringify(lost)}`)
      const stray = ids.filter((id) => !quiet.includes(id) && !hnIds.has(id) && !others.includes(id))
      if (stray.length) fail('CROWDING', `News shows ${JSON.stringify(stray)}, which are no 2-day News stories`)
    },
  }),

  // ⋯ → 7 days on Nhl fetches the tab's own rows (category=nhl, 7 days,
  // the per-feed cap, active feeds) and stores window: '7d' with
  // maxAgeHours null (so an older build keeps the window); 604 (3 days old) joins at its place (Newest: last),
  // 605 (10 days) doesn't. Away on News nothing is fetched for Nhl; back
  // on it the rows draw at once, in order, and one more fetch goes. A
  // cold load with the window stored opens Nhl in the same order, from
  // its first wider answer (the snapshot starts over: the list is at its
  // top). The clock is held so no card is used up meanwhile.
  dashFlow('flow-tab-window-7d', {
    before: async ({ page }) => {
      await oldDevice(page)
      await installClock(page)
      page._wider = []
      await page.route(
        (u) => new URL(u).pathname === '/api/entries' && new URL(u).searchParams.get('category') === 'nhl',
        (route) => {
          const u = new URL(route.request().url())
          page._wider.push(u.search)
          return route.fallback()
        },
      )
    },
    run: async ({ page, fail, shot }) => {
      const writes = await prefWrites(page)
      await holdClock(page)
      const before = await openedIds(page, /^nhl/i)
      if (JSON.stringify(before) !== JSON.stringify([601, 602, 603])) return fail('WINDOW-7D', `Nhl opens as ${before}`)
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="7d"]`))
      if (!(await waitFor(() => page._wider.length === 1, 3000))) return fail('WINDOW-7D', `7 days sent ${page._wider.length} requests for Nhl: ${JSON.stringify(page._wider)}`)
      const want = '?category=nhl&limit=200&max_age_hours=168&per_source_cap=25&active_only=true'
      if (page._wider[0] !== want) fail('WINDOW-7D', `the wider request was ${page._wider[0]}, expected ${want}`)
      if (!(await waitFor(async () => (await phoneIds(page)) === '601,602,603,604', 3000))) return fail('WINDOW-7D', `after 7 days Nhl shows ${await phoneIds(page)}, expected 601,602,603,604`)
      if ((await page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="7d"]`).getAttribute('aria-pressed')) !== 'true') fail('WINDOW-7D', '7 days is not shown as chosen')
      await shot('7d')
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      // The write goes in its 1 s batch.
      await page.clock.runFor(1100)
      if (!(await waitFor(() => writes.some((w) => w.key === 'column_prefs:nhl'), 3000))) return fail('WINDOW-7D', '7 days sent no column_prefs PUT')
      const row = writes.find((w) => w.key === 'column_prefs:nhl')
      if (row.method !== 'PUT' || !row.value || row.value.window !== '7d' || row.value.maxAgeHours !== null) fail('WINDOW-7D', `the Nhl row was written as ${JSON.stringify(row)}`)
      // Away: a poll fetches nothing for Nhl.
      await tap(page, phoneTab(page, /^news/i))
      await pollDone(page)
      if (page._wider.length !== 1) fail('WINDOW-7D', `away on News, Nhl was fetched again: ${JSON.stringify(page._wider)}`)
      // Back: the rows draw at once, in order; one more fetch goes.
      const back = await openedIds(page, /^nhl/i)
      if (JSON.stringify(back) !== JSON.stringify([601, 602, 603, 604])) fail('WINDOW-7D', `back on Nhl it shows ${back}, expected 601,602,603,604 at once`)
      if (!(await waitFor(() => page._wider.length === 2, 3000))) fail('WINDOW-7D', `back on Nhl ${page._wider.length - 1} more fetches went, expected 1`)
      // A cold load with the window stored.
      await page.clock.runFor(1100)
      await requestsSettled(page)
      page._wider.length = 0
      // (The page reloads under the held clock, as flow-new-arrival-appends does.)
      await page.reload()
      if (!(await waitFor(async () => /^nhl/i.test(await selectedTab(page)) && (await phoneIds(page)) === '601,602,603,604', 5000))) {
        return fail('WINDOW-7D', `after a reload Nhl shows ${await phoneIds(page)} (tab ${JSON.stringify(await selectedTab(page))}), expected 601,602,603,604`)
      }
      if (page._wider.length !== 1) fail('WINDOW-7D', `the reload fetched Nhl ${page._wider.length} times`)
      const top = await page.locator(PHONE_LIST).evaluate((el) => el.scrollTop)
      if (top !== 0) fail('WINDOW-7D', `after the reload the list is at ${top}px`)
    },
  }),

  // A wider tab's rows go in at their place in the tab's sort, never at
  // the end like an arrival. Nhl on Best: 601, 602, 603 at 2 days. 7 days
  // with the list untouched, its answer held while 602 is marked seen on
  // another device (a poll's resync brings the mark; a poll never starts
  // a snapshot over, so 602 stays above the marker): the first answer
  // starts the snapshot over, so 602 goes under Earlier and 604 in at its
  // place, 601, 604, 603. Without the new snapshot 602 would stay, 601,
  // 602, 604, 603. Then All, its answer held while 601 is opened (the
  // list is touched, so no new snapshot: 601 stays where it is, read):
  // 605 goes in at its place, 601, 605, 604, 603. Appended, it would be
  // last; a new snapshot would fold the read 601 under Earlier.
  dashFlow('flow-tab-window-in-order', {
    before: async ({ page }) => {
      await oldDevice(page)
      await installClock(page)
      page._wider = []
      page._widerHold = null
      await page.route(
        (u) => new URL(u).pathname === '/api/entries' && new URL(u).searchParams.get('category') === 'nhl',
        async (route) => {
          page._wider.push(new URL(route.request().url()).search)
          if (page._widerHold) await page._widerHold
          return route.fallback()
        },
      )
    },
    run: async ({ page, context, fail, shot }) => {
      await holdClock(page)
      await openedIds(page, /^nhl/i)
      const more = page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`)
      const choose = (sel) => tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[${sel}]`))
      const holdWider = () => {
        let release
        page._widerHold = new Promise((r) => {
          release = r
        })
        return () => {
          page._widerHold = null
          release()
        }
      }
      await tap(page, more)
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      await choose('data-sort="best"')
      if (!(await waitFor(async () => (await phoneIds(page)) === '601,602,603', 3000))) return fail('WINDOW-ORDER', `Nhl on Best shows ${await phoneIds(page)}, expected 601,602,603`)
      // 7 days, its answer held while 602 is seen elsewhere.
      let release = holdWider()
      await choose('data-window="7d"')
      if (!(await waitFor(() => page._wider.length === 1, 3000))) return fail('WINDOW-ORDER', `7 days sent ${page._wider.length} requests for Nhl`)
      await tap(page, more)
      prefMock(context).seed({ 'seen:nhl': [602] })
      await pollDone(page)
      if (!(await waitFor(async () => (await phoneIds(page)) === '601,602,603', 3000))) return fail('WINDOW-ORDER', `before the 7-day answer Nhl shows ${await phoneIds(page)}, expected 601,602,603 (602 seen, in its place)`)
      if (await page.locator(`${MOBILE_MAIN} button[data-earlier]`).count()) return fail('WINDOW-ORDER', 'before the 7-day answer Nhl already has an Earlier row')
      release()
      if (!(await waitFor(async () => (await phoneIds(page)) === '601,604,603', 3000))) {
        return fail('WINDOW-ORDER', `at 7 days Nhl on Best shows ${await phoneIds(page)}, expected 601,604,603 (the first answer starts the snapshot over: 602, seen, under Earlier)`)
      }
      if (!(await page.locator(`${MOBILE_MAIN} button[data-earlier]`).count())) fail('WINDOW-ORDER', 'at 7 days Nhl has no Earlier row for the seen 602')
      await requestsSettled(page)
      // All, its answer held until 601 has been opened.
      const sent = page._wider.length
      release = holdWider()
      await tap(page, more)
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      await choose('data-window="all"')
      if (!(await waitFor(() => page._wider.length === sent + 1, 3000))) return fail('WINDOW-ORDER', `All sent ${page._wider.length - sent} requests for Nhl`)
      await tap(page, more)
      await openStory(page, 601)
      release()
      if (!(await waitFor(async () => (await phoneIds(page)) === '601,605,604,603', 3000))) {
        return fail('WINDOW-ORDER', `at All, after 601 was opened, Nhl on Best shows ${await phoneIds(page)}, expected 601,605,604,603 (605 at its place, 601 still on top)`)
      }
      await shot('in-order')
    },
  }),

  // For You's Best is the server's ranking, which moves from poll to
  // poll. For You at 7 days: the first answer ranks 101, 301, 102, 201,
  // 401, 104, 303, 502, 604, 505 (the snapshot starts over). The next
  // poll's 7-day answer swaps the top two and brings 506 at rank 9: 506
  // goes in between 502 and 604, where the ranking puts it among the
  // stories on screen, not second because the first two moved; the
  // stories on screen keep their places. (An old device, the clock held:
  // every story is unseen.)
  dashFlow('flow-foryou-window-rerank', {
    before: async ({ page }) => {
      await oldDevice(page)
      await installClock(page)
      page._fy7 = [101, 301, 102, 201, 401, 104, 303, 502, 604, 505]
      page._fy7Asked = 0
      await page.route(
        (u) => new URL(u).pathname === '/api/foryou' && new URL(u).searchParams.get('max_age_hours') === '168',
        (route) => {
          page._fy7Asked++
          const rows = page._fy7.map((id) => F.ENTRIES.find((e) => e.id === id))
          return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) })
        },
      )
    },
    run: async ({ page, fail, shot }) => {
      await holdClock(page)
      await openedIds(page, /^for you/i)
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="7d"]`))
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      const first = '101,301,102,201,401,104,303,502,604,505'
      if (!(await waitFor(async () => (await phoneIds(page)) === first, 3000))) return fail('FY-RERANK', `at 7 days For You shows ${await phoneIds(page)}, expected ${first}`)
      await requestsSettled(page)
      const asked = page._fy7Asked
      page._fy7 = [301, 101, 102, 201, 401, 104, 303, 502, 506, 604, 505]
      await pollDone(page)
      if (!(await waitFor(() => page._fy7Asked > asked, 3000))) return fail('FY-RERANK', 'the poll fetched no 7-day For You')
      const want = '101,301,102,201,401,104,303,502,506,604,505'
      if (!(await waitFor(async () => (await phoneIds(page)) === want, 3000))) {
        return fail('FY-RERANK', `after the poll For You shows ${await phoneIds(page)}, expected ${want} (506 at its rank among the stories on screen)`)
      }
      await shot('rerank')
    },
  }),

  // The badge counts inside the tab's window: Video at 1 day is 501 and
  // 502, at 2 days 501 to 504, at 7 days 505 and 506 too (fetched for the
  // tab). A wider tab that isn't on screen counts from the 2-day data.
  dashFlow('flow-badge-in-window', {
    before: async ({ page }) => {
      await oldDevice(page)
      await installClock(page)
    },
    run: async ({ page, fail }) => {
      await holdClock(page)
      const video = await openedIds(page, /^video/i)
      if (JSON.stringify(video) !== JSON.stringify([501, 502, 503, 504])) return fail('BADGE-WINDOW', `Video opens as ${video}`)
      if ((await badge(page, /^video/i)) !== '4') fail('BADGE-WINDOW', `Video's badge reads ${JSON.stringify(await badge(page, /^video/i))} at 2 days, expected 4`)
      const pick = async (w) => {
        await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
        await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
        await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="${w}"]`))
        await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      }
      await pick('1d')
      if (!(await waitFor(async () => (await phoneIds(page)) === '501,502', 3000))) return fail('BADGE-WINDOW', `at 1 day Video shows ${await phoneIds(page)}`)
      if ((await badge(page, /^video/i)) !== '2') fail('BADGE-WINDOW', `Video's badge reads ${JSON.stringify(await badge(page, /^video/i))} at 1 day, expected 2`)
      await pick('7d')
      if (!(await waitFor(async () => (await phoneIds(page)) === '501,502,503,504,505,506', 3000))) return fail('BADGE-WINDOW', `at 7 days Video shows ${await phoneIds(page)}`)
      if ((await badge(page, /^video/i)) !== '6') fail('BADGE-WINDOW', `Video's badge reads ${JSON.stringify(await badge(page, /^video/i))} at 7 days, expected 6`)
      await tap(page, phoneTab(page, /^news/i))
      await sleep(300)
      if ((await badge(page, /^video/i)) !== '4') fail('BADGE-WINDOW', `off screen at 7 days, Video's badge reads ${JSON.stringify(await badge(page, /^video/i))}, expected 4 (the 2-day data)`)
    },
  }),

  // The paused feed (krebs_on_security, 10) is in no tab: not in
  // Security, not in For You (304 is in F.FOR_YOU_RANKED, the mock's list,
  // as it would be in the server's ranking before PR4b's active filter).
  dashFlow('flow-paused-hidden', {
    run: async ({ page, fail }) => {
      const fy = await openedIds(page, /^for you/i)
      if (!fy.length) return fail('PAUSED', 'For You is empty')
      if (fy.includes(304)) fail('PAUSED', `For You shows the paused feed's 304: ${fy}`)
      const security = await openedIds(page, /^security/i)
      if (!security.length) return fail('PAUSED', 'Security is empty')
      if (security.some((id) => id === 304 || id === 305)) fail('PAUSED', `Security shows the paused feed's stories: ${security}`)
    },
  }),

  // The same on the desktop: the For You row (its top 6) and the
  // Security column have none of the paused feed's stories.
  dashFlow('flow-paused-hidden-desktop', {
    widths: [1280],
    wait: 'main.grid article[data-card-id]',
    run: async ({ page, fail }) => {
      const row = page.locator('section').filter({ visible: true, has: page.locator('header h2', { hasText: /^For You$/ }) })
      if (!(await waitFor(async () => (await row.count()) === 1 && (await row.locator('article[data-card-id]').count()) > 0, 3000))) return fail('PAUSED-DESK', 'no For You row with stories')
      const fy = await row.locator('article[data-card-id]').evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-card-id'))))
      if (fy.includes(304)) fail('PAUSED-DESK', `the For You row shows the paused feed's 304: ${fy}`)
      if (fy.length !== 6) fail('PAUSED-DESK', `the For You row shows ${fy.length} stories, expected its top 6`)
      const security = page.locator('main.grid section').filter({ has: page.locator('header h2', { hasText: /^Security$/ }) })
      const ids = await security.locator('article[data-card-id]').evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-card-id'))))
      if (!ids.length) return fail('PAUSED-DESK', 'the Security column is empty')
      if (ids.some((id) => id === 304 || id === 305)) fail('PAUSED-DESK', `the Security column shows the paused feed's stories: ${ids}`)
    },
  }),

  // A category whose only stories are older than 2 days keeps its tab
  // (nhl: the page's fetch drops 601 to 603, so only 604, 3 days old, and
  // 605 are left): it says "Nothing in 2 days · Show 7 days"; ⋯ → 7 days
  // shows 604 within 3 taps. Back at 2 days, the line's "Show 7 days"
  // does the same. The category stays in the feed editor's picker. A
  // category whose feeds are all paused leaves the strip.
  // A quiet tab says nothing of a wider window until that window's fetch
  // has answered. Nhl is quiet at 2 days (601 to 603 kept out); its
  // "Show 7 days" sends the 7-day request, held: no "Nothing in 7 days"
  // line (nor its "Show all") while it is on its way, nor once it fails
  // (503: the banner says so). The next poll's fetch answers, and 604
  // shows, with no line ever drawn for 7 days.
  dashFlow('flow-quiet-tab-pending', {
    expectedFailedLoads: ['/api/entries'],
    before: async ({ page }) => {
      await oldDevice(page)
      await keepOutOfTabs(page, [601, 602, 603])
      page._wider = []
      page._widerHold = null
      page._widerFail = true
      await page.route(
        (u) => new URL(u).pathname === '/api/entries' && new URL(u).searchParams.get('category') === 'nhl',
        async (route) => {
          page._wider.push(new URL(route.request().url()).search)
          if (page._widerHold) await page._widerHold
          if (page._widerFail) return route.fulfill({ status: 503, contentType: 'text/plain', body: 'unavailable' })
          return route.fallback()
        },
      )
    },
    run: async ({ page, fail }) => {
      const empty = page.locator(`${MOBILE_MAIN} [data-window-empty]`)
      const said = () => page.locator(MOBILE_MAIN).getByText(/Nothing in 7 days/).count()
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(() => empty.isVisible(), 3000))) return fail('QUIET-PENDING', 'the empty Nhl tab has no window line')
      let release
      page._widerHold = new Promise((r) => {
        release = r
      })
      await tap(page, empty.locator('button[data-show-wider]'))
      if (!(await waitFor(() => page._wider.length === 1, 3000))) return fail('QUIET-PENDING', `Show 7 days sent ${page._wider.length} requests for Nhl`)
      await sleep(300)
      if ((await empty.count()) || (await said())) return fail('QUIET-PENDING', `with the 7-day answer on its way, Nhl reads ${JSON.stringify(((await page.locator(MOBILE_MAIN).textContent()) || '').trim().slice(0, 120))}`)
      // It fails: still no claim, the banner says why.
      const failed = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/entries' && new URL(r.url()).searchParams.get('category') === 'nhl')
      page._widerHold = null
      release()
      await failed
      if (!(await waitFor(() => page.locator('[role="alert"]').isVisible(), 3000))) fail('QUIET-PENDING', 'the failed 7-day fetch shows no banner')
      await sleep(300)
      if ((await empty.count()) || (await said())) fail('QUIET-PENDING', 'after the 7-day fetch failed, Nhl says "Nothing in 7 days"')
      // The next poll's fetch answers: 604.
      page._widerFail = false
      await pollDone(page)
      if (!(await waitFor(async () => (await phoneIds(page)) === '604', 3000))) return fail('QUIET-PENDING', `once the 7-day fetch answered Nhl shows ${JSON.stringify(await phoneIds(page))}, expected 604`)
    },
  }),

  dashFlow('flow-quiet-tab-kept', {
    before: async ({ page }) => {
      await oldDevice(page)
      await keepOutOfTabs(page, [601, 602, 603])
    },
    run: async ({ page, fail, shot }) => {
      if (!(await tabTexts(page)).some((t) => /^nhl/i.test(t))) return fail('QUIET', `no Nhl tab: ${JSON.stringify(await tabTexts(page))}`)
      const empty = page.locator(`${MOBILE_MAIN} [data-window-empty]`)
      await tap(page, phoneTab(page, /^nhl/i))
      if (!(await waitFor(() => empty.isVisible(), 3000))) return fail('QUIET', 'the empty Nhl tab has no window line')
      const line = ((await empty.textContent()) || '').replace(/\s*·\s*/g, ' · ').replace(/\s+/g, ' ').trim()
      if (line !== 'Nothing in 2 days · Show 7 days') fail('QUIET', `the line reads ${JSON.stringify(line)}`)
      await shot('quiet')
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="7d"]`))
      taps(page, 3, 'QUIET')
      if (!(await waitFor(async () => (await phoneIds(page)) === '604', 3000))) return fail('QUIET', `at 7 days Nhl shows ${JSON.stringify(await phoneIds(page))}, expected 604`)
      await tap(page, page.locator(`${MOBILE_MAIN} [data-column-popover] button[data-window="2d"]`))
      if (!(await waitFor(() => empty.isVisible(), 3000))) return fail('QUIET', 'back at 2 days the window line is gone')
      await tap(page, page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`))
      await tap(page, empty.locator('button[data-show-wider]'))
      if (!(await waitFor(async () => (await phoneIds(page)) === '604', 3000))) return fail('QUIET', `after Show 7 days Nhl shows ${JSON.stringify(await phoneIds(page))}, expected 604`)
      // The picker still offers nhl.
      await openEditor(page, 16)
      const options = await page.locator('#fe-category-16').evaluate((el) => [...el.options].map((o) => o.value))
      if (!options.includes('nhl')) fail('QUIET', `the category picker lost nhl: ${JSON.stringify(options)}`)
      await closeSettings(page)
      // Every feed of the category paused (on another device): the tab goes.
      const status = await page.evaluate(() => fetch('/api/sources/16', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active: false }) }).then((r) => r.status))
      if (status !== 200) return fail('QUIET', `pausing nhl_news answered ${status}`)
      await page.reload()
      // The tabs are in once News is (the sources and the stories land in
      // one render).
      if (!(await waitFor(async () => (await tabTexts(page)).some((t) => /^news/i.test(t)), 5000))) return fail('QUIET', `after the reload the strip is ${JSON.stringify(await tabTexts(page))}`)
      if ((await tabTexts(page)).some((t) => /^nhl/i.test(t))) fail('QUIET', `with its only feed paused, Nhl is still a tab: ${JSON.stringify(await tabTexts(page))}`)
    },
  }),
]
