#!/usr/bin/env node
// Popping iPhone layout guard (REDESIGN_PLAN.md section 4, "The guard").
//
//   node smoke.cjs <base> [--csp] [--strict] [--browser chromium|webkit]
//                         [--out <dir>] [--only <state>[,<state>...]]
//                         [--states-only]
//
// Opens the built app (vite preview) at 390x844@3 and 320x568@2 with touch
// emulation and a fully mocked API, drives it into each state in STATES, and
// checks the layout rules C1-C16 in every state (and C17, no whole-list PUT
// of a preference set key, in every state and flow). Exit 1 on any failure.
// --states-only skips the behaviour flows (the WebKit report-only run).
//
// The caller sets PLAYWRIGHT_BROWSERS_PATH (locally /opt/pw-browsers; CI uses
// Playwright's default). This script never sets it. Keep it CommonJS:
// frontend/package.json is "type": "module".
'use strict'

const fs = require('fs')
const path = require('path')
const playwright = require('playwright')
const { mockApi, prefMock, IOS_UA } = require('./capture.cjs')
const F = require('./fixtures.cjs')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------------------
// Widths. Each state lists the widths it runs at (default: all of these).
// ---------------------------------------------------------------------------
const WIDTHS = {
  390: { width: 390, height: 844, deviceScaleFactor: 3 },
  320: { width: 320, height: 568, deviceScaleFactor: 2 },
  // A 390 iPhone with Safari's Page Zoom at 125%. Only the states that
  // list it run here.
  312: { width: 312, height: 675, deviceScaleFactor: 3.75 },
  // A desktop browser: no touch, no iOS user agent, and no C1. Only the
  // states and flows that list it run here; they wait for a desktop card
  // (wait: 'main.grid article[data-card-id]').
  1280: { width: 1280, height: 800, deviceScaleFactor: 1, desktop: true },
}
const DEFAULT_WIDTHS = [390, 320]

// ---------------------------------------------------------------------------
// Rules. In-page rules are implemented in pageChecks() below; C8 and C9 are
// collected from page events. `warn` rules only fail the run with --strict.
// ---------------------------------------------------------------------------
const RULES = {
  C1: { title: 'harness: (pointer: coarse) matches' },
  C2: { title: 'page width: body and #root scrollWidth <= innerWidth' },
  C2b: { title: 'page height: no document scroll with a 34px home-indicator inset' },
  C3: { title: 'viewport escape: every visible box inside [-1, innerWidth + 1]' },
  C4: { title: 'vertical scrollers: no sideways overflow, overscroll-behavior-y contain' },
  C5: { title: 'clipped text: text stays inside its clipping box' },
  C6: { title: 'field font: inputs, selects, textareas >= 16px without the coarse-pointer rule', warn: true },
  C7: { title: 'tap targets: >= 43.5px tall (and wide when there is no text)', warn: true },
  C8: { title: 'runtime: no page errors, console errors, unmocked requests or dialogs' },
  C9: { title: 'CSP: no "Refused to" console messages under the production policy' },
  C10: { title: 'no Save: Settings has no button named "Save" (every field saves on its own)' },
  C11: { title: 'one line: button, tab, time, [data-one-line] and nowrap link text on one line' },
  C12: { title: 'gutter: text and controls at least 8px from the screen edges' },
  C13: { title: 'measure: headline >= 55% of the card, no text block > 8 lines and < 160px wide' },
  C14: { title: 'ellipsis floor: a truncated box keeps >= min(96px, its content)' },
  C15: { title: 'no native search field (input[type=search])' },
  C16: { title: 'title fits: the header h1 is not cut off' },
  C17: { title: 'preferences: no whole-list PUT of a set key (saves, hides, votes, read and seen marks go as PATCH)' },
}
// Rules every state runs in the page. Per-state additions go in `extra`.
const PAGE_RULES = ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C10', 'C11', 'C12', 'C13', 'C14', 'C15']
// Rules that change the page (CSSOM or inline style) and restore it; they run
// after the screenshot.
const MUTATING_RULES = ['C2b', 'C6']

// C7 scopes: controls inside these are measured (the phone's cards from PR7).
const C7_SCOPES = ['[role="dialog"]', 'aside[aria-label="menu"]', 'header', '[role="tablist"]', 'main.md\\:hidden article']
// C12 scopes: the phone's content surfaces.
const C12_ROOTS = ['main.md\\:hidden', '[role="dialog"][aria-label="settings"]', 'aside[aria-label="menu"]', '[role="dialog"][aria-label="edit tabs"]', '[role="dialog"][aria-label="story actions"]']

// ---------------------------------------------------------------------------
// States. Each PR adds its own. Fields:
//   name     unique id (used by --only and the screenshot file name)
//   url      path + query to open (default '/')
//   widths   widths to run at (default DEFAULT_WIDTHS)
//   wait     selector that means "the app has rendered" (default: a phone card)
//   before   async ({ page, context }) before navigation (extra routes)
//   setup    async ({ page, width, settle }) after the app rendered
//   extra    rule ids added to PAGE_RULES for this state (e.g. 'C2b')
//   checks   false to skip the layout rules (behaviour flows)
//   run      async ({ page, context, width, fail, settle, shot }) assertions
//            after the checks; call fail(rule, detail) for each problem, and
//            shot(label) to save <width>-<name>-<label>.png under --out
//   expectedFailedLoads  pathnames whose failed requests the state causes
//            on purpose (a refused or dropped save); the browser's
//            "Failed to load resource" line for them is not C8
// ---------------------------------------------------------------------------
const MOBILE_MAIN = 'main.md\\:hidden'
const PHONE_CARD = `${MOBILE_MAIN} article[data-card-id]`
const SETTINGS = '[role="dialog"][aria-label="settings"]'
const FEED_TABS = `${SETTINGS} [role="tablist"][aria-label="feed manager tabs"]`

async function openSearch(page, query) {
  await page.locator('button[aria-label="open search"]').click()
  const input = page.locator('#app-search')
  await input.waitFor({ state: 'visible' })
  await input.fill(query)
  // The results header repeats the settled (debounced) query.
  await page.waitForFunction(
    (q) => [...document.querySelectorAll('header')].some((h) => (h.textContent || '').includes(q)),
    query,
    { timeout: 10000 },
  )
}

async function waitForSettingsTab(page) {
  await page.locator(SETTINGS).waitFor({ state: 'visible' })
  await page.locator(`${SETTINGS} [role="tab"][aria-selected="true"]`).first().waitFor({ state: 'visible' })
}

// Settings, Feeds, then the feed-manager tab at `index`
// (0 "My feeds", 1 "Suggested", 2 "Add").
async function openFeedTab(page, index) {
  await waitForSettingsTab(page)
  await page.locator(`${FEED_TABS} [role="tab"]`).nth(index).click()
}

// A feed's editor, opened through its ?source= link (PR6).
async function waitForEditor(page, id) {
  await waitForSettingsTab(page)
  await page.locator(`#feed-${id} button[aria-expanded="true"]`).waitFor({ state: 'visible' })
}
const editorState = (suffix, id, fields = {}) => ({
  name: `settings-editor-${suffix}`,
  url: `/?view=settings&tab=feeds&source=${id}`,
  ...fields,
  setup: async (ctx) => {
    await waitForEditor(ctx.page, id)
    if (fields.setup) await fields.setup(ctx)
  },
})

const settingsState = (tab, name = `settings-${tab}`) => ({
  name,
  url: `/?view=settings&tab=${tab}`,
  setup: async ({ page }) => waitForSettingsTab(page),
})

function briefNeedsAiState(name, { brief, collapse }) {
  return {
    name,
    widths: [390, 320, 312],
    before: async ({ page }) => {
      const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      await page.route((u) => new URL(u).pathname === '/api/llm/status', (route) =>
        route.fulfill(json({ configured: false, backend: null, model: null })),
      )
      const rows = brief
        ? [{ id: 1, generated_at: new Date(F.NOW - 2 * 3600000).toISOString(), tone: 'terse', content: 'TODAY IN ONE SENTENCE\nA quiet day.\nHIGHLIGHTS\n- Nothing much happened.', delivered_at: null, meta: null }]
        : []
      await page.route((u) => new URL(u).pathname === '/api/brief/latest', (route) => route.fulfill(json(rows)))
    },
    setup: async ({ page }) => {
      await page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"]').first().click()
      const setUp = page.locator(`${MOBILE_MAIN} [data-brief-action="set-up-ai"]`)
      if (brief) await page.locator(`${MOBILE_MAIN} header[role="button"]`).waitFor({ state: 'visible' })
      await setUp.waitFor({ state: 'visible' })
      if (collapse) {
        await page.locator(`${MOBILE_MAIN} [aria-expanded="true"][role="button"] > span[aria-hidden="true"]`).first().click()
        await page.locator(`${MOBILE_MAIN} [aria-expanded="false"][role="button"]`).waitFor({ state: 'visible' })
        await setUp.waitFor({ state: 'visible' })
      }
    },
  }
}

// PR3b: the NHL tab with `seen` stored as seen, Earlier opened or not.
// The rows under the last unseen card are 44px, never role="status", and
// never a <section>. The page's timers are held from the tap on the tab:
// the tab's few cards are on screen with the marker, and on the phone
// they would be used up a second later (PR3c's end rule), so the state
// is the tab as it opens.
function readModelState(name, seen, openEarlier) {
  return {
    name,
    before: async ({ page, context }) => {
      if (seen.length) prefMock(context).seed({ 'seen:nhl': seen })
      await page.clock.install({ time: F.NOW })
    },
    setup: async ({ page }) => {
      await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000)
      await page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"]').filter({ hasText: /^Nhl/ }).first().click()
      await page.locator(`${MOBILE_MAIN} [data-caught-up]`).waitFor({ state: 'visible' })
      if (openEarlier) {
        await page.locator(`${MOBILE_MAIN} button[data-earlier]`).click()
        await page.locator(`${MOBILE_MAIN} [data-group="earlier"] article[data-card-id]`).first().waitFor({ state: 'visible' })
        await page.locator(`${MOBILE_MAIN} [data-caught-up]`).scrollIntoViewIfNeeded()
      }
    },
    run: async ({ page, fail }) => {
      const rows = await page.$$eval(`${MOBILE_MAIN} [data-caught-up], ${MOBILE_MAIN} [data-next-tab], ${MOBILE_MAIN} [data-earlier]`, (els) =>
        els.map((el) => ({ text: el.textContent.trim(), h: el.getBoundingClientRect().height, status: !!el.closest('[role="status"]') || el.getAttribute('role') === 'status', section: !!el.closest('main section [data-scroller] section') })),
      )
      for (const r of rows) {
        if (r.h < 43.5) fail('READ-MODEL', `"${r.text}" is ${r.h}px tall`)
        if (r.status) fail('READ-MODEL', `"${r.text}" is a status region`)
        if (r.section) fail('READ-MODEL', `"${r.text}" is inside a <section> of its own`)
      }
      const unseen = await page.locator(`${MOBILE_MAIN} [data-group="unseen"] article[data-card-id]`).count()
      const marker = rows.find((r) => /caught up|not seen/.test(r.text))
      const want = { 'caught-up': [0, /^✓\s*You.re all caught up$/], 'not-seen-row': [3, /^3 not seen · Mark as read$/], 'earlier-open': [1, /^1 not seen · Mark as read$/] }[name]
      if (unseen !== want[0]) fail('READ-MODEL', `${unseen} cards above the marker, expected ${want[0]}`)
      if (!marker || !want[1].test(marker.text)) fail('READ-MODEL', `the marker reads ${JSON.stringify(marker && marker.text)}`)
      // C8: the next tab in the strip after Nhl with new stories.
      if (name === 'caught-up' && !rows.some((r) => /^Podcast · \d+/.test(r.text))) fail('READ-MODEL', "no C8 row naming Podcast under the caught-up tab")
      if (name === 'earlier-open') {
        const dim = await page.$$eval(`${MOBILE_MAIN} [data-group="earlier"] article[data-card-id]`, (els) => els.map((el) => getComputedStyle(el).opacity))
        if (dim.length !== 2 || dim.some((o) => Number(o) > 0.61)) fail('READ-MODEL', `Earlier's cards have opacity ${JSON.stringify(dim)}, expected two at 0.6`)
      }
    },
  }
}

const STATES = [
  {
    name: 'dashboard',
    widths: [390, 320, 312],
    extra: ['C2b', 'C16'],
    // The App's top bar holds exactly: title, search, refresh (phones)
    // and the one Menu button. Column headers are not counted.
    run: async ({ page, fail }) => {
      const labels = await page.$$eval('header[data-app-header] button', (els) =>
        els.filter((b) => b.getClientRects().length).map((b) => b.getAttribute('aria-label') || b.textContent.trim()),
      )
      const want = ['open search', 'refresh', 'open menu']
      if (JSON.stringify(labels) !== JSON.stringify(want)) fail('HEADER', `header buttons are ${JSON.stringify(labels)}, expected ${JSON.stringify(want)}`)
      if (await page.locator('[aria-label="open settings"]').count()) fail('HEADER', 'the old settings gear is still in the page')
      // PR10: a phone renders only the phone layout, one Column, not the
      // hidden desktop grid of every column.
      const columns = await page.locator('main section').count()
      if (columns !== 1) fail('PHONE', `${columns} Column sections under <main>, expected 1 (the phone's)`)
      // PR7a: the Edit pill is pinned at the strip's trailing edge, outside
      // the tablist, in full view at every width (312 included).
      const pill = await page.evaluate(() => {
        const b = document.querySelector('button[data-edit-tabs]')
        if (!b) return null
        const r = b.getBoundingClientRect()
        const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return { left: r.left, right: r.right, height: r.height, inTablist: !!b.closest('[role="tablist"]'), onTop: !!at && b.contains(at) }
      })
      if (!pill) fail('EDIT-PILL', 'no Edit pill')
      else {
        if (pill.inTablist) fail('EDIT-PILL', 'the Edit pill is inside the tablist')
        if (pill.left < 0 || pill.right > (await page.evaluate(() => innerWidth))) fail('EDIT-PILL', `the Edit pill is off screen (${pill.left}-${pill.right})`)
        if (pill.height < 43.5) fail('EDIT-PILL', `the Edit pill is ${pill.height}px tall`)
        if (!pill.onTop) fail('EDIT-PILL', 'the Edit pill is covered')
      }
    },
  },
  {
    // PR7a: the tab editor, opened from the Edit pill.
    name: 'tabs-editor',
    setup: async ({ page }) => {
      await page.locator('button[data-edit-tabs]').click()
      const editor = '[role="dialog"][aria-label="edit tabs"]'
      await page.locator(editor).waitFor({ state: 'visible' })
      // Wait for the slide-up to finish.
      await page.waitForFunction((sel) => {
        const d = document.querySelector(sel)
        return !!d && d.getAnimations().every((a) => a.playState !== 'running') && Math.abs(d.getBoundingClientRect().bottom - innerHeight) < 1
      }, editor, { timeout: 5000 })
    },
  },
  {
    // PR5a: a card's ⋯ sheet (Hide, Edit feed, Copy link, Open in
    // browser, Cancel) after its slide-up.
    name: 'card-action-sheet',
    widths: [390, 320],
    setup: async ({ page }) => {
      await page.locator(`${PHONE_CARD} button[aria-label="more actions"]`).first().click()
      const sheet = '[role="dialog"][aria-label="story actions"]'
      await page.locator(sheet).waitFor({ state: 'visible' })
      await page.waitForFunction((sel) => {
        const d = document.querySelector(sel)
        return !!d && d.getAnimations().every((a) => a.playState !== 'running') && Math.abs(d.getBoundingClientRect().bottom - innerHeight) < 1
      }, sheet, { timeout: 5000 })
    },
    run: async ({ page, fail }) => {
      const rows = await page.$$eval('[role="dialog"][aria-label="story actions"] button', (els) => els.map((b) => ({ text: b.textContent.trim(), h: b.getBoundingClientRect().height })))
      const want = ['Hide', 'Edit feed', 'Copy link', 'Open in browser', 'Cancel']
      if (JSON.stringify(rows.map((r) => r.text)) !== JSON.stringify(want)) fail('SHEET', `the rows are ${JSON.stringify(rows.map((r) => r.text))}, expected ${JSON.stringify(want)}`)
      for (const r of rows) if (r.h < 43.5) fail('SHEET', `"${r.text}" is ${r.h}px tall`)
    },
  },
  {
    // PR5a, D34: For You's first card (101) with its 300-character
    // preview, clamped to 2 lines on a phone, at the widths C11 and C14
    // run at.
    name: 'card-preview-long',
    widths: [390, 320, 312],
    run: async ({ page, fail }) => {
      const p = await page.evaluate((sel) => {
        const el = document.querySelector(`${sel} article[data-card-id="101"] [data-preview]`)
        if (!el) return null
        const cs = getComputedStyle(el)
        return { clamp: cs.webkitLineClamp, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, lineHeight: parseFloat(cs.lineHeight) }
      }, MOBILE_MAIN)
      if (!p) return fail('PREVIEW', 'story 101 shows no preview')
      if (p.clamp !== '2') fail('PREVIEW', `the line clamp is ${JSON.stringify(p.clamp)}, expected 2`)
      if (p.clientHeight > p.lineHeight * 2 + 1) fail('PREVIEW', `the preview is ${p.clientHeight}px tall, more than 2 lines`)
      if (p.scrollHeight <= p.clientHeight) fail('PREVIEW', 'the long blurb was not clamped')
    },
  },
  {
    // PR5a: the News tab, whose story 103 has no preview: the card looks
    // as it did (title row, then the action bar).
    name: 'card-no-preview',
    widths: [390, 320, 312],
    setup: async ({ page }) => {
      await page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"]').filter({ hasText: /^News/ }).first().click()
      await page.locator(`${MOBILE_MAIN} article[data-card-id="103"]`).waitFor({ state: 'visible' })
    },
    run: async ({ page, fail }) => {
      if (await page.locator(`${MOBILE_MAIN} article[data-card-id="103"] [data-preview]`).count()) fail('PREVIEW', 'story 103 shows a preview although the server sent none')
      if (!(await page.locator(`${MOBILE_MAIN} article[data-card-id="101"] [data-preview]`).count())) fail('PREVIEW', 'story 101 shows no preview on News')
    },
  },
  {
    name: 'card-summary',
    setup: async ({ page }) => {
      // Card 101: 180-character title, thumbnail, 60-character source name.
      // Card 102: 90-character unbroken token in the title and summary.
      for (const id of [101, 102]) {
        await page.locator(`${MOBILE_MAIN} article[data-card-id="${id}"] button[aria-label="show summary"]`).click()
      }
      await page.waitForFunction(
        ([sel, a, b]) => {
          const t = (id) => (document.querySelector(`${sel} article[data-card-id="${id}"]`) || {}).textContent || ''
          return t(101).includes(a) && t(102).includes(b)
        },
        [MOBILE_MAIN, 'needs-weighted formula', 'migration guide'],
        { timeout: 10000 },
      )
    },
  },
  { name: 'search-x', setup: async ({ page }) => openSearch(page, 'x') },
  { name: 'search-60', setup: async ({ page }) => openSearch(page, F.SEARCH_QUERY_60) },
  {
    name: 'menu',
    widths: [390, 320, 312],
    setup: async ({ page }) => {
      await page.locator('button[aria-label="open menu"]').click()
      // Wait for the slide-in transition to finish.
      await page.waitForFunction(() => {
        const a = document.querySelector('aside[aria-label="menu"]')
        if (!a) return false
        const r = a.getBoundingClientRect()
        return r.right <= innerWidth + 0.5 && r.left < innerWidth && a.getAnimations().every((x) => x.playState !== 'running')
      }, null, { timeout: 5000 })
    },
  },
  {
    name: 'settings-feeds',
    url: '/?view=settings&tab=feeds',
    setup: async ({ page }) => {
      await waitForSettingsTab(page)
      await page.locator(`${SETTINGS} button[aria-expanded]`).first().waitFor({ state: 'visible' })
    },
  },
  {
    name: 'settings-add-youtube',
    url: '/?view=settings&tab=feeds',
    setup: async ({ page }) => {
      await waitForSettingsTab(page)
      // Tabs by position: "My feeds", "Suggested"/"Recommended", "Add"/"Add custom".
      await page.locator(`${FEED_TABS} [role="tab"]`).nth(2).click()
      await page.locator('#fm-name').waitFor({ state: 'visible' })
      await page.locator('input[type="radio"][value="youtube_channel"]').check({ force: true })
      await page.fill('#fm-name', 'veritasium_clips_and_extras_channel_uploads')
      await page.fill('#fm-url', 'https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA/videos?view=0&sort=dd&shelf_id=0')
      await page.locator('#fm-url').blur()
    },
  },
  {
    // A Test that failed with YouTube's longest message.
    name: 'settings-add-error',
    url: '/?view=settings&tab=feeds',
    before: async ({ page }) => {
      await page.route((u) => new URL(u).pathname === '/api/sources/test', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: false,
            status_code: null,
            item_count: null,
            sample_titles: [],
            error_kind: 'invalid_url',
            error: F.YOUTUBE_BLOCKED_MESSAGE,
            resolved_url: null,
          }),
        }),
      )
    },
    setup: async ({ page }) => {
      await openFeedTab(page, 2)
      await page.locator('input[type="radio"][value="youtube_channel"]').check({ force: true })
      await page.fill('#fm-url', '@veritasium')
      await page.getByRole('button', { name: 'Test', exact: true }).click()
      const result = page.locator('[data-test-result="err"]')
      await result.waitFor({ state: 'visible' })
      await result.scrollIntoViewIfNeeded()
    },
  },
  {
    // The category picker in New-category mode with a long name.
    name: 'settings-add-newcat',
    url: '/?view=settings&tab=feeds',
    setup: async ({ page }) => {
      await openFeedTab(page, 2)
      await page.getByRole('button', { name: 'New category', exact: true }).click()
      await page.fill('#fm-category', 'Ice hockey and other winter sports')
      await page.getByText('Shows as', { exact: false }).waitFor({ state: 'visible' })
      await page.locator('#fm-category').scrollIntoViewIfNeeded()
    },
  },
  {
    // Advanced headers open: a browser identity and a refused header.
    name: 'settings-add-headers',
    url: '/?view=settings&tab=feeds',
    setup: async ({ page }) => {
      await openFeedTab(page, 2)
      await page.locator('button[aria-controls="fm-headers"]').click()
      await page.getByRole('button', { name: 'Use a browser identity', exact: true }).click()
      await page.getByRole('button', { name: 'Add header', exact: true }).click()
      await page.locator('#fm-headers input[aria-label="header name"]').last().fill('Cookie')
      await page.locator('#fm-headers input[placeholder="Value"]').last().fill('session=abc')
      await page.locator('#fm-url').focus()
      await page.getByText("Popping can't send a Cookie header.").waitFor({ state: 'visible' })
      await page.locator('#fm-headers').scrollIntoViewIfNeeded()
    },
  },
  {
    name: 'settings-suggested',
    url: '/?view=settings&tab=feeds',
    setup: async ({ page }) => {
      await openFeedTab(page, 1)
      await page.locator(`${SETTINGS} button[aria-label^="add "]`).first().waitFor({ state: 'visible' })
    },
  },
  // PR6: the feed editor. A custom feed with a long name and headers, a
  // built-in, one stopped after failures, a website feed with its
  // sitemap fields, a blocked one with its Last problem box, and one with
  // a stored header the rules now refuse.
  editorState('custom', 3),
  editorState('builtin', 1),
  editorState('stopped', 12),
  editorState('website', 15),
  editorState('blocked', 4),
  editorState('legacy-header', 17),
  editorState('newcat', 11, {
    // New-category mode with a 45-character name (over the limit).
    setup: async ({ page }) => {
      await page.locator('#feed-11').getByRole('button', { name: 'New category', exact: true }).click()
      await page.fill('#fe-category-11', 'Regional ice hockey leagues and junior scores')
      await page.locator('#feed-11').getByText('Keep it to 40 characters or fewer.').waitFor({ state: 'visible' })
      await page.locator('#fe-category-11').scrollIntoViewIfNeeded()
    },
  }),
  editorState('error', 6, {
    // A refused address: the long value stays and the reason shows with Undo.
    expectedFailedLoads: ['/api/sources/6'],
    before: async ({ page }) => {
      await page.route((u) => new URL(u).pathname === '/api/sources/6', (route) =>
        route.request().method() === 'PATCH'
          ? route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: 'url host is not reachable from this server' }) })
          : route.fallback(),
      )
    },
    setup: async ({ page }) => {
      const addr = page.locator('#feed-6').getByLabel('Address', { exact: true })
      await addr.fill('https://intranet.example.internal/feeds/very-long-path/that-keeps-going/and-going/rss.xml?token=abcdef')
      await addr.press('Enter')
      await page.locator('#feed-6').getByText("That address isn't reachable from Popping.").waitFor({ state: 'visible' })
      await addr.scrollIntoViewIfNeeded()
    },
  }),
  {
    // PR7: a column's ⋯ popover on the phone (Newest | Best from PR1c,
    // Show stories from, the 4-way window control, from PR4d).
    name: 'column-popover',
    setup: async ({ page }) => {
      await page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"]').filter({ hasText: /^News/ }).first().click()
      await page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`).click()
      await page.locator(`${MOBILE_MAIN} [data-column-popover]`).waitFor({ state: 'visible' })
    },
    run: async ({ page, fail }) => {
      const POPOVER = `${MOBILE_MAIN} [data-column-popover]`
      const more = page.locator(`${MOBILE_MAIN} button[aria-label="column preferences"]`)
      // The window control (PR4d): four choices, 2 days chosen, each a
      // 44px target that fits the popover at 320.
      const windows = await page.$$eval(`${POPOVER} button[data-window]`, (els) =>
        els.map((el) => ({ w: el.getAttribute('data-window'), pressed: el.getAttribute('aria-pressed'), h: el.getBoundingClientRect().height, right: el.getBoundingClientRect().right, clipped: el.scrollWidth > el.clientWidth + 1 })),
      )
      if (windows.map((x) => x.w).join() !== '1d,2d,7d,all') fail('POPOVER', `Show stories from offers ${JSON.stringify(windows.map((x) => x.w))}`)
      if (!windows.some((x) => x.w === '2d' && x.pressed === 'true')) fail('POPOVER', '2 days is not shown as chosen')
      const vw = await page.evaluate(() => window.innerWidth)
      for (const x of windows) {
        if (x.h < 43.5) fail('POPOVER', `the ${x.w} window button is ${x.h}px tall`)
        if (x.right > vw + 0.5) fail('POPOVER', `the ${x.w} window button ends at ${x.right}px, past the ${vw}px viewport`)
        if (x.clipped) fail('POPOVER', `the ${x.w} window button's label is clipped`)
      }
      // iOS sends pointer events but no mousedown for a tap on empty
      // space, so the popover must close on pointerdown.
      await page.evaluate(() => document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })))
      await page.waitForTimeout(150)
      if (await page.locator(POPOVER).count()) fail('POPOVER', 'a pointerdown outside left the popover open')
      // Tapping ⋯ again closes it instead of closing and reopening.
      await more.click()
      await page.locator(POPOVER).waitFor({ state: 'visible' })
      await more.click()
      await page.waitForTimeout(150)
      if (await page.locator(POPOVER).count()) fail('POPOVER', 'tapping ⋯ while open did not close the popover')
      // Saved has no ⋯: nothing in it would apply (no read state, sort
      // or window), and an empty popover is a control that does nothing.
      const strip = page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"]')
      await strip.filter({ hasText: /^Saved/ }).first().click()
      await page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"][aria-selected="true"]').filter({ hasText: /^Saved/ }).first().waitFor({ state: 'visible' })
      await page.waitForTimeout(150)
      if (await page.locator(`${MOBILE_MAIN} [data-column-action="prefs"]`).count()) fail('POPOVER', 'Saved has a ⋯ (column preferences) button')
      await strip.filter({ hasText: /^News/ }).first().click()
    },
  },
  {
    // PR7: the sign-in page with the local form and a refused password.
    name: 'login',
    wait: 'input[autocomplete="username"]',
    expectedFailedLoads: ['/auth/local'],
    before: async ({ page }) => {
      const reply = (status, body) => (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      await page.route((u) => new URL(u).pathname === '/auth/me', reply(401, { detail: 'not logged in' }))
      await page.route((u) => new URL(u).pathname === '/auth/local/availability', reply(200, { enabled: true }))
      await page.route((u) => new URL(u).pathname === '/auth/local', reply(401, { detail: 'invalid credentials' }))
    },
    setup: async ({ page }) => {
      await page.fill('input[autocomplete="username"]', 'someone')
      await page.fill('input[autocomplete="current-password"]', 'not-the-password')
      await page.getByRole('button', { name: 'Sign in', exact: true }).click()
      await page.getByText("That username and password don't match.").waitFor({ state: 'visible' })
    },
    run: async ({ page, fail }) => {
      // iOS must not capitalise the first letter of a lowercase username.
      const attrs = await page
        .locator('input[autocomplete="username"]')
        .evaluate((el) => ({ cap: el.getAttribute('autocapitalize'), correct: el.getAttribute('autocorrect'), spell: el.getAttribute('spellcheck') }))
      if (attrs.cap !== 'none' || attrs.correct !== 'off' || attrs.spell !== 'false') fail('LOGIN', `username field attributes are ${JSON.stringify(attrs)}`)
    },
  },
  settingsState('llm'), // "AI"
  settingsState('notifications'),
  settingsState('history'),
  settingsState('hidden'),
  settingsState('starred'), // "Saved"
  settingsState('voted'), // "Downvoted"
  settingsState('reset'),
  {
    name: 'brief-empty',
    widths: [320],
    before: async ({ page }) => {
      await page.route((u) => new URL(u).pathname === '/api/brief/latest', (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
      )
    },
    setup: async ({ page }) => {
      // The phone's first dashboard tab is the Brief.
      await page.locator('[role="tablist"][aria-label="dashboard sections"] [role="tab"]').first().click()
      await page.locator(`${MOBILE_MAIN} [data-brief-action="generate"]`).waitFor({ state: 'visible' })
    },
  },
  // PR3b: the end of a tab. The NHL tab (3 stories) with every story
  // seen: ✓ "You're all caught up", C8's row (Podcast, the next tab with
  // new stories) and Earlier (3) folded; with none seen: the 3 cards and
  // "3 not seen · Mark as read"; with two seen and Earlier open: one card,
  // the row, then the two dimmed.
  readModelState('caught-up', [601, 602, 603], false),
  readModelState('not-seen-row', [], false),
  readModelState('earlier-open', [602, 603], true),
  // PR1c: no AI provider. The phone's Brief opens expanded with no
  // brief; the second state has a brief and is collapsed. The 44px
  // "Brief needs an AI provider · Set up" must stay on one line.
  briefNeedsAiState('brief-needs-ai', { brief: false, collapse: false }),
  briefNeedsAiState('brief-needs-ai-compact', { brief: true, collapse: true }),
]

// Behaviour flows (flows.cjs, from PR4) use the same shape as STATES, usually
// with `checks: false` and a `run`. They load only if the file exists.
const FLOWS_FILE = path.join(__dirname, 'flows.cjs')
const FLOWS = fs.existsSync(FLOWS_FILE) ? require(FLOWS_FILE) : []

// ---------------------------------------------------------------------------
// In-page checks. Self-contained: page.evaluate serialises this function.
// cfg: { rules: [...ids], c7Scopes: [...], c12Roots: [...] }
// Returns [{ rule, path, rect, text, detail }].
// ---------------------------------------------------------------------------
function pageChecks(cfg) {
  const W = window.innerWidth
  const H = window.innerHeight
  const on = new Set(cfg.rules)
  const out = []

  const csCache = new Map()
  const cs = (el) => {
    let s = csCache.get(el)
    if (!s) {
      s = getComputedStyle(el)
      csCache.set(el, s)
    }
    return s
  }
  const R = (r) =>
    r ? { l: +r.left.toFixed(1), t: +r.top.toFixed(1), r: +r.right.toFixed(1), b: +r.bottom.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1) } : null
  const short = (el) => {
    if (!el || el.nodeType !== 1) return ''
    let s = el.tagName.toLowerCase()
    if (el.id) s += '#' + el.id
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean)
    if (cls.length) s += '.' + cls.slice(0, 4).join('.') + (cls.length > 4 ? '…' : '')
    for (const a of ['role', 'aria-label', 'data-card-id', 'type']) {
      const v = el.getAttribute(a)
      if (v != null) s += `[${a}="${v.slice(0, 40)}"]`
    }
    return s
  }
  const LANDMARKS = '[role="dialog"][aria-label], aside[aria-label], header, main, article[data-card-id]'
  const pathOf = (el) => {
    const parts = []
    let cur = el
    for (let d = 0; cur && cur.nodeType === 1 && cur !== document.documentElement && d < 6; d++) {
      parts.unshift(short(cur))
      if (cur === document.body) return parts.join(' > ')
      cur = cur.parentElement
    }
    if (cur && cur !== document.documentElement && cur !== document.body) {
      // Deeper than six levels: start with the nearest landmark instead.
      const lm = cur.closest(LANDMARKS)
      parts.unshift(lm ? `${short(lm)} …` : '…')
      return parts.join(' > ')
    }
    return parts.join(' > ')
  }
  const textOf = (el) => ((el && el.textContent) || '').replace(/\s+/g, ' ').trim().slice(0, 100)

  // ---- visibility ----
  const transparent = new Map()
  const isTransparent = (el) => {
    if (!el || el.nodeType !== 1) return false
    if (transparent.has(el)) return transparent.get(el)
    const v = parseFloat(cs(el).opacity) === 0 || isTransparent(el.parentElement)
    transparent.set(el, v)
    return v
  }
  const closedDrawer = (el) => {
    const a = el.closest('aside[aria-label="menu"]')
    return !!a && a.getBoundingClientRect().left >= W - 1
  }
  const baseVisible = (el) => {
    if (!el.getClientRects().length) return false
    const v = cs(el).visibility
    if (v === 'hidden' || v === 'collapse') return false
    if (el.closest('[inert], [aria-hidden="true"]')) return false
    if (isTransparent(el)) return false
    if (closedDrawer(el)) return false
    return true
  }
  // An open aria-modal dialog covers the page behind it (Settings and the
  // Menu both sit on a full-screen backdrop), so content outside it is
  // treated as hidden, except toasts ([role=status]) which sit above.
  const modals = [...document.querySelectorAll('[aria-modal="true"]')].filter(baseVisible)
  const visCache = new Map()
  const isVisible = (el) => {
    if (visCache.has(el)) return visCache.get(el)
    let v = baseVisible(el)
    if (v && modals.length && !modals.some((m) => m.contains(el)) && !el.closest('[role="status"]')) v = false
    visCache.set(el, v)
    return v
  }

  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT', 'LINK', 'META'])
  const els = []
  for (const el of document.body.querySelectorAll('*')) {
    if (SKIP_TAGS.has(el.tagName)) continue
    if (el instanceof SVGElement && !(el instanceof SVGSVGElement)) continue
    if (isVisible(el)) els.push(el)
  }

  const texts = []
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.nodeValue || !n.nodeValue.trim()) continue
    const p = n.parentElement
    if (!p || p.closest('script, style, noscript, template, svg, textarea, select, option')) continue
    if (!isVisible(p)) continue
    const range = document.createRange()
    range.selectNodeContents(n)
    const rects = [...range.getClientRects()].filter((r) => r.width > 0.5 && r.height > 0.5)
    if (!rects.length) continue
    texts.push({ node: n, parent: p, rects, text: n.nodeValue.replace(/\s+/g, ' ').trim().slice(0, 100) })
  }
  const union = (rects) => {
    const l = Math.min(...rects.map((r) => r.left))
    const t = Math.min(...rects.map((r) => r.top))
    const r = Math.max(...rects.map((x) => x.right))
    const b = Math.max(...rects.map((x) => x.bottom))
    return { left: l, top: t, right: r, bottom: b, width: r - l, height: b - t }
  }
  const lineCount = (rects) => {
    const tops = rects.map((r) => r.top).sort((a, b) => a - b)
    let n = 0
    let last = -Infinity
    for (const t of tops) {
      if (t - last > 2) n++
      last = t
    }
    return n
  }

  // Report only the outermost offender per rule: skip an element whose
  // ancestor already failed the same rule.
  const reported = {}
  const covered = (rule, el) => {
    const set = reported[rule]
    if (!set) return false
    for (let a = el; a; a = a.parentElement) if (set.has(a)) return true
    return false
  }
  const fail = (rule, el, detail, rect, text) => {
    ;(reported[rule] = reported[rule] || new Set()).add(el)
    out.push({ rule, path: el ? pathOf(el) : '(page)', rect: R(rect || (el && el.getBoundingClientRect())), text: text != null ? text : textOf(el), detail })
  }
  const px = (n) => `${+n.toFixed(1)}px`

  // C1 harness
  if (on.has('C1') && !matchMedia('(pointer: coarse)').matches) {
    fail('C1', document.documentElement, 'matchMedia("(pointer: coarse)") is false: the context is not emulating a touch phone', null, '')
  }

  // C2 page width. html alone misses overflow because of overflow-x: clip.
  if (on.has('C2')) {
    const root = document.getElementById('root')
    for (const el of [document.body, root]) {
      if (!el || el.scrollWidth <= W) continue
      // Name the widest box that is not inside a scroller (scroller content
      // does not widen the page).
      let widest = null
      let right = W
      for (const e of els) {
        const r = e.getBoundingClientRect()
        if (r.right <= right + 0.5) continue
        let inScroller = false
        for (let a = e.parentElement; a && a !== document.body; a = a.parentElement) {
          if (cs(a).overflowX !== 'visible') {
            inScroller = true
            break
          }
        }
        if (!inScroller) {
          widest = e
          right = r.right
        }
      }
      fail('C2', el, `${el === root ? '#root' : 'body'}.scrollWidth ${el.scrollWidth} > innerWidth ${W}` + (widest ? `; widest box: ${pathOf(widest)} (right ${px(right)})` : ''), null, widest ? textOf(widest) : '')
    }
  }

  // C2b page height: Chromium's env(safe-area-inset-bottom) is 0, so add the
  // iPhone's 34px by hand (inline style, so a CSP can never block it).
  // #root is border-box (Tailwind preflight), so in Chromium a child that is
  // too tall overlaps the padding instead of growing the document; iOS turns
  // the same overlap into a scroll range. So also assert directly that
  // #root's in-flow children end above the 34px padding (R9).
  if (on.has('C2b')) {
    const root = document.getElementById('root')
    const prev = root.style.paddingBottom
    root.style.paddingBottom = '34px'
    const se = document.scrollingElement || document.documentElement
    const sh = se.scrollHeight
    const rr = root.getBoundingClientRect()
    const contentBottom = rr.bottom - parseFloat(getComputedStyle(root).paddingBottom) - parseFloat(getComputedStyle(root).borderBottomWidth)
    const over = []
    for (const c of root.children) {
      const p = getComputedStyle(c).position
      if (p === 'fixed' || p === 'absolute' || !c.getClientRects().length) continue
      const r = c.getBoundingClientRect()
      if (r.bottom > contentBottom + 1) over.push({ c, r, minH: getComputedStyle(c).minHeight })
    }
    root.style.paddingBottom = prev
    if (sh > H + 1) fail('C2b', se, `with #root{padding-bottom:34px} document.scrollingElement.scrollHeight ${sh} > innerHeight ${H} + 1: the whole UI can be dragged`, null, '')
    for (const o of over) {
      fail('C2b', o.c, `with #root{padding-bottom:34px} this child of #root ends at ${px(o.r.bottom)}, below #root's content box (${px(contentBottom)}); min-height ${o.minH}. On an iPhone the document then scrolls by the overlap`, o.r, '')
    }
  }

  // C3 viewport escape. Only descendants of [data-hscroll] are skipped.
  if (on.has('C3')) {
    for (const el of els) {
      if (el.parentElement && el.parentElement.closest('[data-hscroll]')) continue
      const r = el.getBoundingClientRect()
      if ((r.left < -1 || r.right > W + 1) && !covered('C3', el)) {
        fail('C3', el, `box outside the viewport: left ${px(r.left)}, right ${px(r.right)}, innerWidth ${W}`)
      }
    }
  }

  // C4 vertical scrollers. [data-hscroll] strips compute overflow-y:auto too
  // and are exempt; form controls (textarea) are not page scrollers.
  if (on.has('C4')) {
    for (const el of els) {
      if (el.matches('[data-hscroll], textarea, select, input')) continue
      const s = cs(el)
      if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') continue
      const problems = []
      if (el.scrollWidth > el.clientWidth + 1) problems.push(`scrollWidth ${el.scrollWidth} > clientWidth ${el.clientWidth}`)
      const ob = s.getPropertyValue('overscroll-behavior-y')
      if (ob !== 'contain') problems.push(`overscroll-behavior-y is ${ob || 'auto'}, not contain`)
      if (problems.length) fail('C4', el, `vertical scroller: ${problems.join('; ')}`, null, '')
    }
  }

  // C5 clipped text: each line box's left and right lie inside the nearest
  // ancestor whose overflow-x is not visible (or the viewport).
  if (on.has('C5')) {
    for (const t of texts) {
      let clipper = null
      let exempt = false
      for (let a = t.parent; a && a !== document.documentElement; a = a.parentElement) {
        const s = cs(a)
        if (s.textOverflow === 'ellipsis') exempt = true
        const lc = s.getPropertyValue('-webkit-line-clamp')
        if (lc && lc !== 'none') exempt = true
        if (a.hasAttribute('data-hscroll')) exempt = true
        if (s.overflowX !== 'visible') {
          clipper = a
          break
        }
      }
      if (exempt) continue
      let box = { left: 0, right: W }
      if (clipper) {
        const cr = clipper.getBoundingClientRect()
        if (cr.width <= 1 || cr.height <= 1 || cs(clipper).clip === 'rect(0px, 0px, 0px, 0px)') continue
        box = cr
      }
      const bad = t.rects.find((r) => r.left < box.left - 1 || r.right > box.right + 1)
      if (bad && !covered('C5', t.parent)) {
        fail(
          'C5',
          t.parent,
          `text line ${px(bad.left)}..${px(bad.right)} runs past ${clipper ? 'its clipping box ' + short(clipper) : 'the viewport'} (${px(box.left)}..${px(box.right)})`,
          bad,
          t.text,
        )
      }
    }
  }

  // C7 tap targets (warn until --strict).
  if (on.has('C7')) {
    const scopes = cfg.c7Scopes.join(', ')
    const TARGETS = 'button, a[href], select, [role="button"], [role="tab"], [role="switch"], input[type="checkbox"], input[type="radio"]'
    const seen = new Set()
    for (const el of els) {
      if (!el.matches(TARGETS)) continue
      const inScope = el.closest(scopes) || (el.tagName === 'BUTTON' && el.closest('[role="status"]'))
      if (!inScope || el.closest('[data-tap-exempt]')) continue
      const box = el.matches('input[type="checkbox"], input[type="radio"]') ? el.closest('label') || el : el
      if (seen.has(box)) continue
      seen.add(box)
      const r = box.getBoundingClientRect()
      const hasText = textOf(box).length > 0
      const problems = []
      if (r.height < 43.5) problems.push(`height ${px(r.height)} < 43.5px`)
      if (!hasText && r.width < 43.5) problems.push(`width ${px(r.width)} < 43.5px (no text)`)
      if (problems.length) fail('C7', box, `tap target: ${problems.join('; ')}`, r)
    }
  }

  // C11 one line.
  if (on.has('C11')) {
    for (const t of texts) {
      let inScope = !!t.parent.closest('button, [role="tab"], time, [data-one-line]')
      if (!inScope) {
        const a = t.parent.closest('a')
        inScope = !!a && cs(a).whiteSpace === 'nowrap'
      }
      if (!inScope) continue
      const lines = lineCount(t.rects)
      if (lines > 1 && !covered('C11', t.parent)) {
        const host = t.parent.closest('button, [role="tab"], time, [data-one-line], a') || t.parent
        fail('C11', host, `text wraps onto ${lines} lines: "${t.text}"`, union(t.rects), t.text)
      }
    }
  }

  // C12 gutter: 8px from both screen edges unless data-bleed/data-hscroll.
  if (on.has('C12')) {
    const roots = cfg.c12Roots.join(', ')
    const MIN = 8
    const off = (r) => r.left < MIN || r.right > W - MIN
    for (const el of els) {
      if (!el.matches('input, select, button')) continue
      if (!el.closest(roots) || el.closest('[data-bleed], [data-hscroll]')) continue
      const r = el.getBoundingClientRect()
      if (off(r) && !covered('C12', el)) fail('C12', el, `control at ${px(r.left)}..${px(r.right)}, gutter is ${MIN}..${W - MIN}`)
    }
    // A text node's Range rect is its unclipped extent; measure only what is
    // painted: intersect with every ancestor whose overflow-x clips
    // (truncate, scrollers), and skip text that is clipped away (sr-only).
    const clippers = (el) => {
      const list = []
      for (let a = el; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
        const s = cs(a)
        if (s.clip === 'rect(0px, 0px, 0px, 0px)') return null
        if (s.overflowX !== 'visible') list.push(a.getBoundingClientRect())
      }
      return list
    }
    for (const t of texts) {
      if (!t.parent.closest(roots) || t.parent.closest('[data-bleed], [data-hscroll]')) continue
      const clips = clippers(t.parent)
      if (!clips) continue
      let bad = null
      for (const r of t.rects) {
        let l = r.left
        let rt = r.right
        for (const c of clips) {
          l = Math.max(l, c.left)
          rt = Math.min(rt, c.right)
        }
        if (rt - l <= 1) continue
        const seen = { left: l, right: rt, top: r.top, bottom: r.bottom, width: rt - l, height: r.height }
        if (off(seen)) {
          bad = seen
          break
        }
      }
      if (bad && !covered('C12', t.parent)) fail('C12', t.parent, `text at ${px(bad.left)}..${px(bad.right)}, gutter is ${MIN}..${W - MIN}`, bad, t.text)
    }
  }

  // C13 measure.
  if (on.has('C13')) {
    for (const a of els) {
      if (!a.matches('article a[aria-label="open in new tab"]')) continue
      const art = a.closest('article')
      const s = cs(art)
      const content = art.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight)
      const w = a.getBoundingClientRect().width
      if (content > 0 && w < 0.55 * content) {
        fail('C13', a, `headline ${px(w)} is ${Math.round((w / content) * 100)}% of the card's ${px(content)} content box (minimum 55%)`)
      }
    }
    for (const t of texts) {
      const lines = lineCount(t.rects)
      if (lines <= 8) continue
      const u = union(t.rects)
      if (u.width < 160 && !covered('C13', t.parent)) fail('C13', t.parent, `text block of ${lines} lines in ${px(u.width)} (more than 8 lines narrower than 160px)`, u, t.text)
    }
  }

  // C14 ellipsis floor.
  if (on.has('C14')) {
    for (const el of els) {
      const s = cs(el)
      if (s.textOverflow !== 'ellipsis' || s.display === 'inline' || s.display === 'contents') continue
      const floor = Math.min(96, el.scrollWidth)
      if (el.clientWidth < floor) fail('C14', el, `truncated box is ${el.clientWidth}px wide, needs min(96, scrollWidth ${el.scrollWidth}) = ${floor}px`)
    }
  }

  // C15 no native search field.
  if (on.has('C15')) {
    for (const el of els) {
      if (el.matches('input[type="search" i]')) fail('C15', el, 'input[type=search]: its native clear button and search styling are invisible to the other checks; use type="text" inputMode="search"')
    }
  }

  // C10 no Save: Settings fields save on their own (PR6, PR8).
  if (on.has('C10')) {
    const dialog = document.querySelector('[role="dialog"][aria-label="settings"]')
    for (const b of dialog ? dialog.querySelectorAll('button') : []) {
      const name = (b.getAttribute('aria-label') || b.textContent || '').trim()
      if (/^save$/i.test(name) && b.getClientRects().length) fail('C10', b, 'a Save button in Settings; fields save on their own')
    }
  }

  // C16 title fits: the large title in the top bar shows in full.
  if (on.has('C16')) {
    const h1 = document.querySelector('header h1')
    if (h1 && h1.getClientRects().length && h1.scrollWidth > h1.clientWidth + 0.5) {
      fail('C16', h1, `title is cut off: scrollWidth ${h1.scrollWidth} > clientWidth ${h1.clientWidth}`)
    }
  }

  // C6 field font: drop the coarse-pointer 16px !important rule through
  // CSSOM, measure, then put it back.
  if (on.has('C6')) {
    const removed = []
    const walk = (owner) => {
      let rules
      try {
        rules = owner.cssRules
      } catch {
        return
      }
      for (let i = rules.length - 1; i >= 0; i--) {
        const r = rules[i]
        const cond = r.conditionText || (r.media && r.media.mediaText) || ''
        if (r.type === CSSRule.MEDIA_RULE && /pointer:\s*coarse/.test(cond) && /font-size/.test(r.cssText)) {
          removed.push({ owner, i, text: r.cssText })
          owner.deleteRule(i)
        } else if (r.cssRules) {
          walk(r)
        }
      }
    }
    for (const sh of document.styleSheets) walk(sh)
    for (const el of els) {
      if (!el.matches('input, select, textarea')) continue
      if (el.matches('input[type="checkbox" i], input[type="radio" i], input[type="range" i], input[type="hidden" i]')) continue
      const size = parseFloat(getComputedStyle(el).fontSize)
      if (size < 16) fail('C6', el, `field font-size ${size}px < 16px once the (pointer: coarse) !important rule is gone (iOS zooms on focus)`, null, el.value ? String(el.value).slice(0, 60) : el.getAttribute('placeholder') || '')
    }
    for (const r of removed.reverse()) r.owner.insertRule(r.text, r.i)
    if (!removed.length) {
      // After PR9 the rule no longer exists; nothing to restore.
    }
  }

  return out
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------
function usage(msg) {
  if (msg) console.error(`smoke.cjs: ${msg}`)
  console.error('usage: node smoke.cjs <base> [--csp] [--strict] [--browser chromium|webkit] [--out <dir>] [--only <state>[,<state>...]] [--states-only]')
  process.exit(2)
}

function parseArgs(argv) {
  const opts = { base: null, csp: false, strict: false, browser: 'chromium', out: null, only: null, statesOnly: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const val = () => {
      const v = argv[++i]
      if (v == null || v.startsWith('--')) usage(`${a} needs a value`)
      return v
    }
    if (a === '--csp') opts.csp = true
    else if (a === '--strict') opts.strict = true
    else if (a === '--browser') opts.browser = val()
    else if (a === '--out') opts.out = val()
    else if (a === '--only') opts.only = val().split(',').map((s) => s.trim()).filter(Boolean)
    else if (a === '--states-only') opts.statesOnly = true
    else if (a === '-h' || a === '--help') usage()
    else if (a.startsWith('--')) usage(`unknown option ${a}`)
    else if (!opts.base) opts.base = a
    else usage(`unexpected argument ${a}`)
  }
  if (!opts.base) usage('missing <base> (e.g. http://127.0.0.1:4173)')
  opts.base = opts.base.replace(/\/+$/, '')
  if (!['chromium', 'webkit'].includes(opts.browser)) usage(`--browser must be chromium or webkit, not ${opts.browser}`)
  return opts
}

// The production CSP, read from nginx.conf.template with one regex.
function readCsp() {
  const file = path.join(__dirname, '..', '..', 'nginx.conf.template')
  let text = ''
  try {
    text = fs.readFileSync(file, 'utf8')
  } catch (e) {
    return { file, csp: null, error: e.message }
  }
  const m = text.match(/Content-Security-Policy "([^"]+)"/)
  return { file, csp: m ? m[1] : null, error: m ? null : 'no Content-Security-Policy header found' }
}

// Safari on a Mac, for the desktop width.
const DESKTOP_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15'

function contextOptions(browserName, w) {
  const vp = { width: w.width, height: w.height }
  const common = {
    viewport: vp,
    screen: vp,
    deviceScaleFactor: w.deviceScaleFactor,
    isMobile: !w.desktop,
    hasTouch: !w.desktop,
    colorScheme: 'dark',
    locale: 'en-CA',
    timezoneId: 'America/Vancouver',
  }
  // A desktop is a mouse and a wide window: (hover: hover) matches and
  // (pointer: coarse) does not.
  if (w.desktop) return { ...common, userAgent: DESKTOP_UA }
  if (browserName === 'webkit') {
    // The iPhone 13 descriptor's viewport is 390x664 (Safari's toolbars);
    // the viewport and screen are set explicitly to the sizes under test.
    const { defaultBrowserType, ...iphone } = playwright.devices['iPhone 13']
    return { ...iphone, ...common }
  }
  return { ...common, userAgent: IOS_UA }
}

// Resolves once no request has been in flight for `quietMs`.
function requestTracker(page) {
  let inflight = 0
  let last = Date.now()
  page.on('request', () => {
    inflight++
    last = Date.now()
  })
  const done = () => {
    inflight = Math.max(0, inflight - 1)
    last = Date.now()
  }
  page.on('requestfinished', done)
  page.on('requestfailed', done)
  return async (quietMs = 300, maxMs = 6000) => {
    const t0 = Date.now()
    while (Date.now() - t0 < maxMs) {
      if (inflight === 0 && Date.now() - last >= quietMs) return
      await sleep(50)
    }
  }
}

async function runState(browser, state, width, opts) {
  const w = WIDTHS[width]
  const failures = []
  const push = (rule, f) => failures.push({ width, state: state.name, rule, path: '(page)', rect: null, text: '', ...f })

  const context = await browser.newContext(contextOptions(opts.browser, w))
  try {
    await context.clock.setFixedTime(F.NOW)
    await mockApi(context, {
      onUnmocked: ({ method, path: p }) => push('C8', { detail: `unmocked request ${method} ${p}` }),
      onSetKeyPut: ({ key }) => push('C17', { detail: `PUT /api/preferences/${key}: a set key is written as a change (PATCH), never as a whole list` }),
    })
    const page = await context.newPage()
    const settle = requestTracker(page)

    // C8 / C9 runtime listeners.
    page.on('pageerror', (e) => push('C8', { detail: `pageerror: ${String((e && e.stack) || e).split('\n').slice(0, 3).join(' | ')}` }))
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return
      const text = msg.text()
      const loc = msg.location() || {}
      let pathname = ''
      try {
        pathname = new URL(loc.url).pathname
      } catch {
        pathname = ''
      }
      if (/Refused to/.test(text)) {
        push(opts.cspActive ? 'C9' : 'C8', { path: loc.url || '(console)', detail: `console: ${text.slice(0, 400)}` })
        return
      }
      if (pathname === '/auth/me') return // the expected 404 when OIDC is off
      if ((state.expectedFailedLoads || []).includes(pathname) && /^Failed to load resource/.test(text)) return
      push('C8', { path: loc.url || '(console)', detail: `console.error: ${text.slice(0, 400)}` })
    })
    page.on('dialog', (d) => {
      push('C8', { detail: `unexpected ${d.type()} dialog: ${d.message().slice(0, 200)}` })
      d.dismiss().catch(() => {})
    })

    if (opts.cspActive) {
      await page.route('**/*', async (route) => {
        if (route.request().resourceType() === 'document') {
          const response = await route.fetch()
          await route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': opts.csp } })
        } else {
          await route.fallback()
        }
      })
    }
    if (state.before) await state.before({ page, context, width })

    let shotTaken = false
    const shot = async () => {
      if (!opts.out || shotTaken) return
      shotTaken = true
      await page.screenshot({ path: path.join(opts.out, `${width}-${state.name}.png`) }).catch(() => {})
    }
    // A flow's own screenshots (its end state, a step in between): the
    // one above runs before `run`, so it shows only where a flow starts.
    const labelled = async (label) => {
      if (!opts.out) return
      await page.screenshot({ path: path.join(opts.out, `${width}-${state.name}-${label}.png`) }).catch(() => {})
    }

    try {
      await page.goto(opts.base + (state.url || '/'), { waitUntil: 'load', timeout: 30000 })
      await page.locator(state.wait || PHONE_CARD).first().waitFor({ state: 'visible', timeout: 20000 })
      await settle()
      // Let any load-time toast go so every state starts clean.
      await page.waitForFunction(() => !document.querySelector('[role="status"]'), null, { timeout: 8000 }).catch(() => {})
      if (state.setup) await state.setup({ page, context, width, settle })
      await settle()
      await sleep(400)

      if (state.checks !== false) {
        // C1 asserts a touch phone, so a desktop width skips it.
        const rules = [...PAGE_RULES, ...(state.extra || [])].filter((r) => !(w.desktop && r === 'C1'))
        const cfg = { c7Scopes: C7_SCOPES, c12Roots: C12_ROOTS }
        const first = await page.evaluate(pageChecks, { ...cfg, rules: rules.filter((r) => !MUTATING_RULES.includes(r)) })
        await shot()
        const second = await page.evaluate(pageChecks, { ...cfg, rules: rules.filter((r) => MUTATING_RULES.includes(r)) })
        for (const f of [...first, ...second]) failures.push({ width, state: state.name, ...f })
      }
      await shot()
      if (state.run) {
        await state.run({ page, context, width, settle, shot: labelled, fail: (rule, detail, extra = {}) => push(rule, { detail, ...extra }) })
      }
    } catch (e) {
      push('SETUP', { detail: `state could not be reached: ${String((e && e.message) || e).split('\n')[0]}` })
      await shot()
    }
  } finally {
    await context.close().catch(() => {})
  }
  return failures
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------
const ghEscape = (s) => String(s).replace(/%/g, '%25').replace(/\r/g, '%0D').replace(/\n/g, '%0A')
const ghProp = (s) => ghEscape(s).replace(/:/g, '%3A').replace(/,/g, '%2C')
const fmtRect = (r) => (r ? `l=${r.l} t=${r.t} r=${r.r} b=${r.b} (${r.w}x${r.h})` : '-')

function printFailure(f, label) {
  console.log(`${label} ${f.width} ${f.state} ${f.rule}: ${f.detail}`)
  console.log(`    at   ${f.path}`)
  console.log(`    rect ${fmtRect(f.rect)}`)
  console.log(`    text ${JSON.stringify(f.text || '')}`)
}

async function main() {
  const opts = parseArgs(process.argv.slice(2))

  opts.cspActive = false
  if (opts.csp) {
    if (opts.browser !== 'chromium') {
      console.log('note: --csp (C9) runs on chromium only; skipped for webkit')
    } else {
      const { file, csp, error } = readCsp()
      if (!csp) {
        console.error(`smoke.cjs: --csp: ${error} in ${file}`)
        process.exit(1)
      }
      opts.csp = csp
      opts.cspActive = true
    }
  }
  if (opts.out) fs.mkdirSync(opts.out, { recursive: true })

  const all = [...STATES, ...FLOWS]
  const names = new Set(all.map((s) => s.name))
  if (names.size !== all.length) usage('duplicate state names')
  for (const n of opts.only || []) if (!names.has(n)) usage(`unknown state "${n}"; states: ${[...names].join(', ')}`)
  const flowNames = new Set(FLOWS.map((s) => s.name))
  const pool = opts.statesOnly ? STATES : all
  const selected = opts.only ? pool.filter((s) => opts.only.includes(s.name)) : pool

  const browser = await playwright[opts.browser].launch({ headless: true })
  const results = []
  const flowTimes = []
  const t0 = Date.now()
  try {
    for (const width of DEFAULT_WIDTHS.concat(Object.keys(WIDTHS).map(Number).filter((x) => !DEFAULT_WIDTHS.includes(x)))) {
      for (const state of selected) {
        if (!(state.widths || DEFAULT_WIDTHS).includes(width)) continue
        const t = Date.now()
        const fs_ = await runState(browser, state, width, opts)
        for (const f of fs_) {
          const warn = RULES[f.rule] && RULES[f.rule].warn && !opts.strict
          f.level = warn ? 'warn' : 'error'
        }
        results.push(...fs_)
        if (flowNames.has(state.name)) flowTimes.push({ name: `${width} ${state.name}`, ms: Date.now() - t })
        const e = fs_.filter((f) => f.level === 'error').length
        const wn = fs_.length - e
        console.log(`[${width}] ${state.name}: ${e ? `${e} failure${e === 1 ? '' : 's'}` : 'ok'}${wn ? `, ${wn} warning${wn === 1 ? '' : 's'}` : ''} (${((Date.now() - t) / 1000).toFixed(1)}s)`)
      }
    }
  } finally {
    await browser.close()
  }

  const errors = results.filter((f) => f.level === 'error')
  const warns = results.filter((f) => f.level === 'warn')
  const gh = !!process.env.GITHUB_ACTIONS

  if (errors.length) {
    console.log(`\n==== ${errors.length} failure${errors.length === 1 ? '' : 's'} ====`)
    for (const f of errors) printFailure(f, 'FAIL')
    if (gh) {
      // GitHub shows only the first few annotations of a step, so emit one
      // ::error:: per distinct problem (rule, element, text) and list every
      // width and state it failed in.
      const groups = new Map()
      for (const f of errors) {
        const key = `${f.rule}|${f.path}|${f.text}`
        if (!groups.has(key)) groups.set(key, { f, where: [] })
        groups.get(key).where.push(`${f.width} ${f.state}`)
      }
      for (const { f, where } of groups.values()) {
        console.log(`::error title=${ghProp(`iPhone smoke ${f.rule} at ${f.width}px in ${f.state}`)}::${ghEscape(`${f.detail} | at ${f.path} | rect ${fmtRect(f.rect)} | text ${JSON.stringify(f.text || '')} | failed in: ${where.join(', ')}`)}`)
      }
    }
  }

  if (warns.length) {
    // Warnings are grouped: one line per (width, rule, element), with the
    // states it appeared in. --strict turns them into failures.
    const groups = new Map()
    for (const f of warns) {
      const key = `${f.width}|${f.rule}|${f.path}|${f.detail}|${f.text}`
      if (!groups.has(key)) groups.set(key, { ...f, states: [] })
      groups.get(key).states.push(f.state)
    }
    console.log(`\n==== ${warns.length} warning${warns.length === 1 ? '' : 's'} (${[...new Set(warns.map((f) => f.rule))].join(', ')}; failures with --strict) ====`)
    for (const g of groups.values()) {
      console.log(`WARN ${g.width} ${g.rule}: ${g.detail} | ${JSON.stringify(g.text || '')} | ${g.path} | in ${g.states.join(', ')}`)
    }
    if (gh) {
      for (const rule of [...new Set(warns.map((f) => f.rule))]) {
        const n = warns.filter((f) => f.rule === rule).length
        console.log(`::warning title=${ghProp(`iPhone smoke ${rule} (warning)`)}::${ghEscape(`${n} ${rule} warnings: ${RULES[rule].title}. They fail the run once --strict is on (PR9).`)}`)
      }
    }
  }

  // Summary by width / state / rule.
  console.log('\n==== summary ====')
  const by = new Map()
  for (const f of results) {
    const key = `${f.width} ${f.state}`
    if (!by.has(key)) by.set(key, new Map())
    const m = by.get(key)
    const rk = f.level === 'warn' ? `${f.rule}(warn)` : f.rule
    m.set(rk, (m.get(rk) || 0) + 1)
  }
  for (const [k, m] of by) console.log(`${k}: ${[...m].map(([r, n]) => `${r} x${n}`).join(', ')}`)
  if (flowTimes.length) {
    // Where CI time goes as flows are added.
    console.log('\n==== slowest flows ====')
    for (const f of flowTimes.sort((a, b) => b.ms - a.ms).slice(0, 10)) console.log(`${(f.ms / 1000).toFixed(1)}s ${f.name}`)
  }
  console.log(`${errors.length} failure(s), ${warns.length} warning(s), browser ${opts.browser}${opts.cspActive ? ' with the production CSP' : ''}, ${((Date.now() - t0) / 1000).toFixed(0)}s`)
  process.exit(errors.length ? 1 : 0)
}

module.exports = { STATES, RULES, WIDTHS, PAGE_RULES, pageChecks, runState, readCsp, contextOptions }

if (require.main === module) {
  main().catch((e) => {
    console.error(e)
    process.exit(1)
  })
}
