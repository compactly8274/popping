#!/usr/bin/env node
// Targeted measurements for the suspects found by capture.cjs.
// node probe.cjs  (vite preview must be running on 127.0.0.1:4173)
'use strict'
const { chromium } = require('playwright')
const { mockApi, IOS_UA, BASE } = require('./capture.cjs')
const F = require('./fixtures.cjs')

const rect = (el) => {
  const r = el.getBoundingClientRect()
  return { l: +r.left.toFixed(1), r: +r.right.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1), sw: el.scrollWidth, cw: el.clientWidth }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function run(width, height, dpr) {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: dpr, isMobile: true, hasTouch: true, userAgent: IOS_UA })
  await mockApi(ctx)
  const page = await ctx.newPage()
  await page.goto(BASE + '/', { waitUntil: 'networkidle' })
  await page.locator('main.md\\:hidden article[data-card-id]').first().waitFor()
  await sleep(1500)
  const out = { width }

  // 1. Header at rest and with search open.
  out.headerRest = await page.evaluate(() => {
    const row = document.querySelector('header > div.flex')
    const h1 = row.querySelector('h1')
    const btns = row.querySelector('div.flex.items-center')
    const r = (el) => { const b = el.getBoundingClientRect(); return { l: +b.left.toFixed(1), r: +b.right.toFixed(1), w: +b.width.toFixed(1) } }
    return { row: r(row), h1: r(h1), h1Text: h1.textContent, h1ScrollW: h1.scrollWidth, h1ClientW: h1.clientWidth, buttons: r(btns), buttonsMinContent: (() => { const c = btns.cloneNode(true); c.style.cssText = 'position:absolute;width:max-content;visibility:hidden'; document.body.appendChild(c); const w = c.getBoundingClientRect().width; c.remove(); return +w.toFixed(1) })() }
  })
  await page.locator('button[aria-label="open search"]').click()
  await page.fill('#app-search', 'infrastructure')
  await sleep(700)
  out.headerSearch = await page.evaluate(() => {
    const row = document.querySelector('header > div.flex')
    const h1 = row.querySelector('h1')
    const btns = row.querySelector('div.flex.items-center')
    const box = row.querySelector('div.bg-bg-elevated')
    const input = document.getElementById('app-search')
    const r = (el) => { const b = el.getBoundingClientRect(); return { l: +b.left.toFixed(1), r: +b.right.toFixed(1), w: +b.width.toFixed(1) } }
    return { row: r(row), h1: r(h1), h1ScrollW: h1.scrollWidth, h1ClientW: h1.clientWidth, buttons: r(btns), searchBox: r(box), searchBoxClass: box.className, input: r(input), inputFont: getComputedStyle(input).fontSize, inputType: input.type, docScrollW: document.documentElement.scrollWidth, rootScrollW: document.getElementById('root').scrollWidth, hamburger: r(document.querySelector('button[aria-label="open menu"]')) }
  })
  await page.fill('#app-search', '')
  await page.locator('#app-search').blur()
  await sleep(300)

  // 2. Card 101 header row (title + thumbnail) and meta row; reddit footer on 104.
  out.card = await page.evaluate(() => {
    const main = document.querySelector('main.md\\:hidden')
    const c = main.querySelector('article[data-card-id="101"]')
    const r = (el) => { const b = el.getBoundingClientRect(); return { l: +b.left.toFixed(1), r: +b.right.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1) } }
    const head = c.querySelector(':scope > div.flex.items-center.justify-between')
    const a = head.querySelector('a')
    const titleSpan = a.querySelector('span.min-w-0')
    const thumb = head.querySelector('[role="button"]')
    const meta = c.querySelector(':scope > div.flex.items-center.gap-2')
    const src = meta.querySelector('span.font-semibold')
    const time = meta.querySelector('time')
    const lines = (el) => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))
    const c104 = main.querySelector('article[data-card-id="104"]')
    const foot = c104.querySelector('a[href*="reddit"]').parentElement
    const footItems = [...foot.children].map((el) => ({ tag: el.tagName, text: el.textContent.trim().slice(0, 30), ...r(el), lines: lines(el), cls: el.className.slice(0, 60) }))
    // find any text node in the title that broke mid-word: compare words vs line boxes
    const range = document.createRange(); range.selectNodeContents(titleSpan)
    const rects = [...range.getClientRects()].map((b) => ({ l: +b.left.toFixed(1), r: +b.right.toFixed(1), w: +b.width.toFixed(1) }))
    return { card: r(c), headRow: r(head), titleLink: r(a), titleSpan: r(titleSpan), titleLines: lines(titleSpan), titleFont: getComputedStyle(titleSpan).fontSize, titleLineRects: rects.length, narrowestLine: Math.min(...rects.map((x) => x.w)), thumb: thumb ? r(thumb) : null, thumbClass: thumb ? thumb.className.slice(0, 80) : null, meta: r(meta), metaSrc: { ...r(src), lines: lines(src), text: src.textContent }, metaTime: { ...r(time), lines: lines(time), text: time.textContent, minWidth: getComputedStyle(time).minWidth, flexShrink: getComputedStyle(time).flexShrink }, redditFooterRow: { ...r(foot), cls: foot.className, flexWrap: getComputedStyle(foot).flexWrap }, redditFooterItems: footItems }
  })

  // 3. Settings -> feeds: tab strip, rows with chips, auto-feed row.
  // Menu → Settings: the top bar's one button since PR5.
  await page.locator('button[aria-label="open menu"]').click()
  await page.locator('aside[aria-label="menu"]').getByRole('button', { name: /^Settings/ }).click()
  await page.waitForSelector('[role="tablist"][aria-label="feed manager tabs"]')
  await sleep(300)
  out.feeds = await page.evaluate(() => {
    const r = (el) => { const b = el.getBoundingClientRect(); return { l: +b.left.toFixed(1), r: +b.right.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1), sw: el.scrollWidth, cw: el.clientWidth } }
    const dlg = document.querySelector('[role="dialog"][aria-label="settings"]')
    const body = dlg.querySelector('div.overflow-y-auto')
    const wrapper = body.firstElementChild
    const strip = dlg.querySelector('[role="tablist"][aria-label="feed manager tabs"]')
    const tabs = [...strip.children].map((b) => ({ text: b.textContent, ...r(b), lines: Math.round(b.getBoundingClientRect().height / 22), pad: getComputedStyle(b).paddingLeft, ws: getComputedStyle(b).whiteSpace }))
    const rows = [...dlg.querySelectorAll('button[aria-expanded]')].map((b) => {
      const kids = [...b.children].map((k) => ({ tag: k.tagName, text: k.textContent.trim().slice(0, 24), ...r(k), shrink: getComputedStyle(k).flexShrink, ov: getComputedStyle(k).overflowX, font: getComputedStyle(k).fontSize }))
      return { text: b.textContent.slice(0, 40), ...r(b), overflow: b.scrollWidth - b.clientWidth, kids }
    }).filter((x) => x.overflow > 1)
    const h2 = dlg.querySelector('h2')
    const done = dlg.querySelector('button[aria-label="close settings"]')
    return { wrapperClass: wrapper.className, wrapperPadding: getComputedStyle(wrapper).paddingLeft, stripRect: r(strip), tabs, overflowingRows: rows, h2: { ...r(h2), font: getComputedStyle(h2).fontSize, lines: Math.round(h2.getBoundingClientRect().height / parseFloat(getComputedStyle(h2).lineHeight)) }, done: { ...r(done), textW: +done.firstElementChild.getBoundingClientRect().width.toFixed(1), shrink: getComputedStyle(done).flexShrink } }
  })
  await page.locator('[role="tablist"][aria-label="feed manager tabs"] button:has-text("Add custom")').click()
  await page.waitForSelector('#fm-name')
  await sleep(300)
  out.addCustom = await page.evaluate(() => {
    const r = (el) => { const b = el.getBoundingClientRect(); return { l: +b.left.toFixed(1), r: +b.right.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1), sw: el.scrollWidth, cw: el.clientWidth } }
    const dlg = document.querySelector('[role="dialog"][aria-label="settings"]')
    const auto = dlg.querySelector('input[placeholder="https://example.com"]')
    const row = auto.parentElement
    const btn = row.querySelector('button')
    const block = row.parentElement
    const cs = getComputedStyle(auto)
    // intrinsic (min-content) width of the input: clone with width:auto in a zero-width container
    const probe = document.createElement('div'); probe.style.cssText = 'position:absolute;width:0;visibility:hidden'
    const clone = auto.cloneNode(); clone.className = auto.className; clone.style.width = 'auto'; clone.style.flex = 'none'; probe.appendChild(clone); dlg.appendChild(probe)
    const intrinsic = clone.getBoundingClientRect().width; probe.remove()
    const fm = ['fm-name', 'fm-url', 'fm-category', 'fm-refresh'].map((id) => { const el = document.getElementById(id); return { id, ...r(el), font: getComputedStyle(el).fontSize, lh: getComputedStyle(el).lineHeight } })
    const form = document.querySelector('form')
    return { block: r(block), row: r(row), input: { ...r(auto), font: cs.fontSize, minWidth: cs.minWidth, flex: cs.flex, intrinsicWidth: +intrinsic.toFixed(1), cls: auto.className }, button: { ...r(btn), text: btn.textContent, font: getComputedStyle(btn).fontSize }, form: r(form), formLeft: form.getBoundingClientRect().left, fm, sheetWidth: dlg.firstElementChild.getBoundingClientRect().width }
  })

  // 4. Feed edit form: textarea line-height vs rows.
  await page.locator('[role="tablist"][aria-label="feed manager tabs"] button:has-text("My feeds")').click()
  const rowBtn = page.locator('[role="dialog"][aria-label="settings"] button[aria-expanded]:has-text("The Extraordinarily Long Name")').first()
  await rowBtn.click()
  await page.locator(`button[aria-label="edit ${F.LONG_SOURCE_NAME}"]`).click()
  await page.waitForSelector('[role="dialog"][aria-label="settings"] textarea')
  out.editForm = await page.evaluate(() => {
    const ta = document.querySelector('[role="dialog"][aria-label="settings"] textarea')
    const cs = getComputedStyle(ta)
    const inputs = [...document.querySelectorAll('[role="dialog"][aria-label="settings"] label input')].map((i) => ({ type: i.type, font: getComputedStyle(i).fontSize, lh: getComputedStyle(i).lineHeight, h: +i.getBoundingClientRect().height.toFixed(1), cls: i.className.slice(0, 70) }))
    return { textarea: { font: cs.fontSize, lineHeight: cs.lineHeight, fontFamily: cs.fontFamily.slice(0, 40), rows: ta.rows, h: +ta.getBoundingClientRect().height.toFixed(1), scrollH: ta.scrollHeight, cls: ta.className.slice(0, 90) }, inputs }
  })
  await browser.close()
  return out
}

;(async () => {
  for (const [w, h, d] of [[390, 844, 3], [320, 568, 2]]) {
    const o = await run(w, h, d)
    console.log(`\n################ ${w}px ################`)
    console.log(JSON.stringify(o, null, 1))
  }
})().catch((e) => { console.error(e); process.exit(1) })
