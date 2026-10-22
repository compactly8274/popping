// Which build a page runs (PR1r, D22). A build is named by the files its
// index.html loads from /static/: the entry script (type="module") and
// the stylesheets. Vite puts a content hash in each of those names, and a
// CSS-only change can leave the script's name alone, so all of them
// count. useUpdateCheck.ts reads the live document's names and the served
// index.html's; this module only compares them. No imports (pure, so it
// runs under node --experimental-strip-types --test).

const STATIC = /^\/static\//

/** The build id for a page's module-script srcs and stylesheet hrefs:
 *  the /static/ ones, sorted and joined. '' when there are none (the
 *  dev server serves /src/main.tsx). */
export function buildIdFromUrls(urls: readonly string[]): string {
  return Array.from(new Set(urls.filter((u) => STATIC.test(u)))).sort().join(' ')
}

type Tag = { name: string; attrs: Map<string, string> }

// The <script> and <link> start tags of an HTML document, outside
// comments, with their attributes (names lowercased, values unquoted).
function tags(html: string): Tag[] {
  const out: Tag[] = []
  const body = html.replace(/<!--[\s\S]*?-->/g, '')
  for (const m of body.matchAll(/<(script|link)\b([^>]*)>/gi)) {
    const attrs = new Map<string, string>()
    for (const a of m[2].matchAll(/([^\s"'=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
      attrs.set(a[1].toLowerCase(), a[2] ?? a[3] ?? a[4] ?? '')
    }
    out.push({ name: m[1].toLowerCase(), attrs })
  }
  return out
}

const isModuleScript = (t: Tag) => t.name === 'script' && t.attrs.get('type')?.toLowerCase() === 'module' && !!t.attrs.get('src')
const isStylesheet = (t: Tag) =>
  t.name === 'link' && (t.attrs.get('rel') || '').toLowerCase().split(/\s+/).includes('stylesheet') && !!t.attrs.get('href')

/** The URLs a document's build id is made of, the same ones the hook
 *  reads from script[type=module] and link[rel~=stylesheet]. */
export function buildUrlsFromHtml(html: string): string[] {
  const urls: string[] = []
  for (const t of tags(html)) {
    if (isModuleScript(t)) urls.push(t.attrs.get('src')!)
    else if (isStylesheet(t)) urls.push(t.attrs.get('href')!)
  }
  return urls
}

/** The build id of a served index.html. */
export function buildIdFromHtml(html: string): string {
  return buildIdFromUrls(buildUrlsFromHtml(html))
}

/** The served index.html's entry script under /static/, or null. The
 *  update check asks for it before reloading, so a reload never lands on
 *  a page whose script isn't there. */
export function entryScriptFromHtml(html: string): string | null {
  for (const t of tags(html)) {
    if (isModuleScript(t) && STATIC.test(t.attrs.get('src')!)) return t.attrs.get('src')!
  }
  return null
}

/** The 8-character id Settings shows: a 32-bit FNV-1a hash of the id,
 *  in hex. '' for ''. */
export function shortId(id: string): string {
  if (!id) return ''
  let h = 0x811c9dc5
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16).padStart(8, '0')
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "19 Oct, 14:02" in local time; '' for a time that doesn't parse. */
export function formatBuildTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
