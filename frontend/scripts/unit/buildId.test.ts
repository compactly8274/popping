import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildIdFromHtml,
  buildIdFromUrls,
  buildUrlsFromHtml,
  entryScriptFromHtml,
  formatBuildTime,
  shortId,
} from '../../src/lib/buildId.ts'

// The shape vite build writes to dist/index.html.
const page = ({ js = '/static/index-AAAA1111.js', css = ['/static/index-BBBB2222.css'] as string[] } = {}) => `<!doctype html>
<html lang="en">
  <head>
    <link rel="manifest" href="/manifest.webmanifest" />
    <link rel="icon" href="/icon.svg" type="image/svg+xml" />
    <!-- a comment with <script type="module" src="/static/old.js"></script> in it -->
    <script type="module" crossorigin src="${js}"></script>
    ${css.map((h) => `<link rel="stylesheet" crossorigin href="${h}">`).join('\n    ')}
  </head>
  <body><div id="root"></div><noscript>Popping needs JavaScript.</noscript></body>
</html>`

test('the id is the /static/ module script and stylesheets, sorted', () => {
  const html = page({ css: ['/static/z-1.css', '/static/a-2.css'] })
  assert.deepEqual(buildUrlsFromHtml(html), ['/static/index-AAAA1111.js', '/static/z-1.css', '/static/a-2.css'])
  assert.equal(buildIdFromHtml(html), '/static/a-2.css /static/index-AAAA1111.js /static/z-1.css')
  // The order the tags come in doesn't matter.
  assert.equal(buildIdFromHtml(page({ css: ['/static/a-2.css', '/static/z-1.css'] })), buildIdFromHtml(html))
})

test('icons, the manifest, commented-out tags and classic scripts are not part of it', () => {
  const html = page() + '<script src="/static/classic.js"></script><link rel="preload" href="/static/font.woff2">'
  assert.equal(buildIdFromHtml(html), '/static/index-AAAA1111.js /static/index-BBBB2222.css')
})

test('a CSS-only change changes the id', () => {
  assert.notEqual(buildIdFromHtml(page()), buildIdFromHtml(page({ css: ['/static/index-CCCC3333.css'] })))
  assert.notEqual(shortId(buildIdFromHtml(page())), shortId(buildIdFromHtml(page({ css: ['/static/index-CCCC3333.css'] }))))
})

test('a missing stylesheet still gives an id, and a different one', () => {
  const noCss = page({ css: [] })
  assert.equal(buildIdFromHtml(noCss), '/static/index-AAAA1111.js')
  assert.notEqual(buildIdFromHtml(noCss), buildIdFromHtml(page()))
  assert.equal(entryScriptFromHtml(noCss), '/static/index-AAAA1111.js')
})

test('the dev server and pages that are not Popping have no id and no entry script', () => {
  const dev = '<script type="module" src="/src/main.tsx"></script>'
  assert.equal(buildIdFromHtml(dev), '')
  assert.equal(entryScriptFromHtml(dev), null)
  assert.equal(buildIdFromHtml('<html><body>Sign in</body></html>'), '')
  assert.equal(entryScriptFromHtml(page({ js: 'https://elsewhere.example/static/x.js' })), null)
})

test('the live side gives the same id from the same urls', () => {
  // useUpdateCheck reads script[type=module] src and link[rel~=stylesheet]
  // href; the dev server's /src/ paths drop out.
  assert.equal(buildIdFromUrls(['/static/index-BBBB2222.css', '/static/index-AAAA1111.js']), buildIdFromHtml(page()))
  assert.equal(buildIdFromUrls(['/src/main.tsx']), '')
  assert.equal(buildIdFromUrls(['/static/a.js', '/static/a.js']), '/static/a.js')
})

test('attribute quoting and case do not matter', () => {
  const html = "<SCRIPT TYPE=module SRC=/static/e.js></SCRIPT><link REL='Stylesheet preload' HREF='/static/s.css'>"
  assert.equal(buildIdFromHtml(html), '/static/e.js /static/s.css')
  assert.equal(entryScriptFromHtml(html), '/static/e.js')
})

test('the short id is 8 hex characters and follows the id', () => {
  const id = buildIdFromHtml(page())
  assert.match(shortId(id), /^[0-9a-f]{8}$/)
  assert.equal(shortId(id), shortId(id))
  assert.notEqual(shortId(id), shortId(buildIdFromHtml(page({ js: '/static/index-DDDD4444.js' }))))
  assert.equal(shortId(''), '')
  // 32-bit FNV-1a of "a".
  assert.equal(shortId('a'), 'e40c292c')
})

test('the built time reads like "19 Oct, 14:02", in local time', () => {
  assert.equal(formatBuildTime(new Date(2026, 9, 19, 14, 2).toISOString()), '19 Oct, 14:02')
  assert.equal(formatBuildTime(new Date(2026, 0, 5, 9, 7).toISOString()), '5 Jan, 09:07')
  assert.equal(formatBuildTime('not a time'), '')
})
