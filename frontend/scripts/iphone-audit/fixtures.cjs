// Realistic API fixtures for the Popping frontend (see frontend/src/api.ts).
// Everything is deterministic except timestamps, which are relative to "now"
// so timeAgo() renders sensible values.
'use strict'

const NOW = Date.now()
const minsAgo = (m) => new Date(NOW - m * 60000).toISOString()

// ---- helpers to hit exact lengths -----------------------------------------
function padToLength(s, n, filler = 'x') {
  if (s.length === n) return s
  if (s.length > n) return s.slice(0, n)
  return s + filler.repeat(n - s.length)
}

// Exactly 60 characters (valid source-name regex: [a-z0-9_]).
const LONG_SOURCE_NAME = padToLength(
  'the_extraordinarily_long_name_of_a_regional_newspaper_feed_x',
  60,
  '_',
)
// Exactly 120 characters.
const LONG_SOURCE_URL = padToLength(
  'https://www.example-regional-newspaper-network.co.uk/sections/local/politics/council-meetings/feeds/latest-articles-',
  120,
  'a',
).replace(/a+$/, (m) => m.replace(/a/g, 'a')) + ''
// Exactly 180 characters.
const TITLE_180 =
  'Federal government announces changes to the infrastructure funding formula affecting municipalities across British Columbia, Alberta and Saskatchewan in a move critics call rushed.'
if (TITLE_180.length !== 180) throw new Error(`TITLE_180 is ${TITLE_180.length} chars, expected 180`)
// A single unbroken token of exactly 90 characters (no spaces/hyphens).
const TOKEN_90 = padToLength(
  'https://github.com/example_org/very_long_repository_name/releases/tag/v2026.10.18_rc1_build',
  90,
  '4',
)
const TITLE_WITH_TOKEN = `Release notes posted at ${TOKEN_90} for the October drop`
if (LONG_SOURCE_NAME.length !== 60) throw new Error('LONG_SOURCE_NAME must be 60 chars')
if (LONG_SOURCE_URL.length !== 120) throw new Error('LONG_SOURCE_URL must be 120 chars')
if (TOKEN_90.length !== 90 || /\s|-/.test(TOKEN_90)) throw new Error('TOKEN_90 must be 90 unbroken chars')
// A 60-character search query: one unbroken snake_case token (the shape of a
// source name), so nothing in the results header can wrap it.
const SEARCH_QUERY_60 = padToLength('federal_infrastructure_funding_formula_needs_weighted_annex', 60, '_')
// A 40-character category (the backend's maximum, routes/sources.py _CATEGORY_MAX).
const CATEGORY_40 = 'regional politics and municipal councils'
// A 300-character fetch error for the auto-disabled row (id 12).
const LAST_ERROR_300 = padToLength(
  'httpx.ConnectTimeout: timed out after 30.0s fetching https://risky.biz/feeds/risky-business/ (attempt 12 of 12). ' +
    'The upstream CDN accepted the TCP connection but sent no bytes before the read deadline; the previous eleven ' +
    'attempts failed the same way, so the scheduler auto-disabled this source until it is resumed by hand.',
  300,
  '.',
)
// the_verge (id 4): blocked by the site's bot filter.
const VERGE_BLOCKED_ERROR = "[blocked] HTTPStatusError: Client error '403 Forbidden' for url 'https://www.theverge.com/rss/index.xml'"
// The longest message the YouTube lookup can answer with (backend/app/youtube.py MSG_BLOCKED).
const YOUTUBE_BLOCKED_MESSAGE =
  "YouTube didn't let Popping look this channel up. Paste the channel ID instead: open the channel in YouTube, " +
  'tap \u201cmore about this channel\u201d, then Share channel, then Copy channel ID. It starts with UC.'
if (SEARCH_QUERY_60.length !== 60 || /\s|-/.test(SEARCH_QUERY_60)) throw new Error('SEARCH_QUERY_60 must be 60 unbroken chars')
if (CATEGORY_40.length !== 40) throw new Error(`CATEGORY_40 is ${CATEGORY_40.length} chars, expected 40`)
if (LAST_ERROR_300.length !== 300) throw new Error('LAST_ERROR_300 must be 300 chars')

// ---- sources ---------------------------------------------------------------
// 17 sources, 7 categories (news, deals, security, podcast, video, nhl and the
// 40-character CATEGORY_40). Ids 1-14 are the original set; keep them stable.
const SOURCES = [
  { id: 1, name: 'bbc_news', type: 'rss', category: 'news', url: 'https://feeds.bbci.co.uk/news/rss.xml', refresh_interval_seconds: 900, last_fetch_at: minsAgo(4), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/1.ico', custom_headers: null, auto_disabled: false, net_vote_score: 2 },
  { id: 2, name: 'hn_top', type: 'hn', category: 'news', url: 'https://hacker-news.firebaseio.com/v0/topstories.json', refresh_interval_seconds: 300, last_fetch_at: minsAgo(2), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/2.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  { id: 3, name: LONG_SOURCE_NAME, type: 'rss', category: 'news', url: LONG_SOURCE_URL, refresh_interval_seconds: 21600, last_fetch_at: minsAgo(190), last_error: null, error_count: 0, active: true, favicon_path: null, custom_headers: { 'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1' }, auto_disabled: false, net_vote_score: -3 },
  // Custom rss without a User-Agent header, blocked by the site (403).
  { id: 4, name: 'the_verge', type: 'rss', category: 'news', url: 'https://www.theverge.com/rss/index.xml', refresh_interval_seconds: 3600, last_fetch_at: minsAgo(35), last_error: VERGE_BLOCKED_ERROR, error_count: 2, active: true, favicon_path: 'favicons/4.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  { id: 5, name: 'rfd_hot_deals', type: 'rss', category: 'deals', url: 'https://forums.redflagdeals.com/feed/forum/9', refresh_interval_seconds: 900, last_fetch_at: minsAgo(7), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/5.ico', custom_headers: null, auto_disabled: false, net_vote_score: 5 },
  { id: 6, name: 'slickdeals_frontpage', type: 'rss', category: 'deals', url: 'https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1', refresh_interval_seconds: 3600, last_fetch_at: minsAgo(50), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/6.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  { id: 7, name: 'r_bapcsalescanada', type: 'reddit', category: 'deals', url: 'https://www.reddit.com/r/bapcsalescanada', refresh_interval_seconds: 900, last_fetch_at: minsAgo(9), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/7.ico', custom_headers: null, auto_disabled: false, net_vote_score: 1 },
  { id: 8, name: 'nvd_recent', type: 'nvd', category: 'security', url: 'https://services.nvd.nist.gov/rest/json/cves/2.0', refresh_interval_seconds: 3600, last_fetch_at: minsAgo(20), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/8.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  // Fetch error (still active).
  { id: 9, name: 'cisa_kev', type: 'json', category: 'security', url: 'https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json', refresh_interval_seconds: 21600, last_fetch_at: minsAgo(1500), last_error: 'HTTP 503 Service Unavailable after 3 retries (upstream CDN returned HTML instead of JSON)', error_count: 3, active: true, favicon_path: 'favicons/9.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  // Manually paused.
  { id: 10, name: 'krebs_on_security', type: 'rss', category: 'security', url: 'https://krebsonsecurity.com/feed/', refresh_interval_seconds: 3600, last_fetch_at: minsAgo(2880), last_error: null, error_count: 0, active: false, favicon_path: 'favicons/10.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  { id: 11, name: 'darknet_diaries', type: 'podcast', category: 'podcast', url: 'https://feeds.megaphone.fm/darknetdiaries', refresh_interval_seconds: 21600, last_fetch_at: minsAgo(120), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/11.ico', custom_headers: null, auto_disabled: false, net_vote_score: 4 },
  // Auto-disabled after repeated failures.
  { id: 12, name: 'risky_business_podcast', type: 'podcast', category: 'podcast', url: 'https://risky.biz/feeds/risky-business/', refresh_interval_seconds: 21600, last_fetch_at: minsAgo(4000), last_error: LAST_ERROR_300, error_count: 12, active: false, favicon_path: 'favicons/12.ico', custom_headers: null, auto_disabled: true, net_vote_score: -1 },
  { id: 13, name: 'anthropic_yt', type: 'youtube_channel', category: 'video', url: 'https://www.youtube.com/feeds/videos.xml?channel_id=UCrDwWp7EBBv4NwvScIpBDOA', refresh_interval_seconds: 21600, last_fetch_at: minsAgo(200), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/13.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  { id: 14, name: 'veritasium_yt', type: 'youtube_channel', category: 'video', url: 'https://www.youtube.com/@veritasium', refresh_interval_seconds: 21600, last_fetch_at: minsAgo(260), last_error: null, error_count: 1, active: true, favicon_path: null, custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  // generic_scrape with both discovery hints, in the 40-character category.
  { id: 15, name: 'ollama_library', type: 'generic_scrape', category: CATEGORY_40, url: 'https://ollama.com/library', sitemap_url: 'https://ollama.com/sitemap.xml', link_pattern: '/library/', refresh_interval_seconds: 21600, last_fetch_at: minsAgo(300), last_error: null, error_count: 0, active: true, favicon_path: null, custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  // The only source in category nhl (3 entries below).
  { id: 16, name: 'nhl_news', type: 'rss', category: 'nhl', url: 'https://www.nhl.com/rss/news.xml', refresh_interval_seconds: 3600, last_fetch_at: minsAgo(18), last_error: null, error_count: 0, active: true, favicon_path: 'favicons/16.ico', custom_headers: null, auto_disabled: false, net_vote_score: 0 },
  // Custom rss with a legacy header name the editor must flag (space in the name).
  { id: 17, name: 'legacy_header_feed', type: 'rss', category: 'news', url: 'https://feeds.example.org/legacy/rss.xml', refresh_interval_seconds: 3600, last_fetch_at: minsAgo(65), last_error: null, error_count: 0, active: true, favicon_path: null, custom_headers: { 'Bad Header': 'x' }, auto_disabled: false, net_vote_score: 0 },
].map((s) => ({ favicon_url: s.favicon_path ? `https://example.invalid/${s.favicon_path}` : null, sitemap_url: null, link_pattern: null, ...s }))

// ---- entries ---------------------------------------------------------------
const LONG_SUMMARY_A =
  'The federal infrastructure minister confirmed on Friday that the per-capita allocation model used since 2014 will be replaced by a needs-weighted formula that scores each municipality on asset age, climate exposure and population growth. Municipal associations in all three provinces said they were given eleven days to respond to a 212-page consultation document, and several mid-sized cities warned that capital plans already approved by council assume the old numbers. The ministry says transition funding will cover shortfalls for two fiscal years; opposition critics say the two-year window is "a cliff dressed up as a bridge". Full consultation text: https://www.infrastructure.gc.ca/consultations/2026/needs-weighted-allocation-formula/technical-annex-b-methodology.pdf'
const LONG_SUMMARY_B =
  'A critical authentication bypass in the appliance\'s management interface allows an unauthenticated remote attacker to obtain an administrative session by sending a crafted request to the /api/v1/session endpoint. The vendor has released firmware 9.4.2 which closes the hole; devices that cannot be upgraded should have the management interface removed from the internet immediately. CISA added the CVE to the Known Exploited Vulnerabilities catalogue with a remediation deadline of November 7. Proof-of-concept code circulated publicly within 36 hours of the advisory.'
const LONG_SUMMARY_C =
  'In this episode we sit down with the incident commander who ran the response when a ransomware crew got into a regional hospital network through a forgotten VPN concentrator. We talk about the first 48 hours, why the backups were technically fine but operationally useless, and what the team would do differently. This episode contains frank language.'
const SHORT_SUMMARY = 'A short one-line summary.'

// The preview under the headline (PR5a, D34), as the server computes
// it: the cleaned feed blurb cut to 300 characters on a word boundary.
// ``preview: null`` in ``extra`` is a story the server found nothing
// for (a blurb that only repeats the title).
function previewOf(summary) {
  const text = String(summary || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
  if (!text) return null
  if (text.length <= 300) return text
  let cut = text.slice(0, 300)
  if (cut.includes(' ')) cut = cut.slice(0, cut.lastIndexOf(' '))
  return cut.trimEnd() + '…'
}

function mkEntry(id, source_id, title, extra = {}) {
  const summary = extra.summary ?? LONG_SUMMARY_A
  return {
    id,
    source_id,
    title,
    url: extra.url || `https://example.com/${source_id}/article-${id}`,
    published_at: minsAgo(extra.ago ?? id * 7),
    fetched_at: minsAgo((extra.ago ?? id * 7) - 1),
    composite_score: extra.score ?? ((id * 37) % 100),
    personal_score: extra.score ?? ((id * 37) % 100),
    raw_score: extra.score ?? ((id * 37) % 100),
    meta: { summary },
    preview: extra.preview !== undefined ? extra.preview : previewOf(summary),
    image_url: extra.image ? `https://example.invalid/thumbs/${id}.jpg` : null,
    image_path: extra.image ? `thumbs/${id}.jpg` : null,
    reddit_thread_url: extra.reddit ? `https://www.reddit.com/r/technology/comments/abc${id}/thread/` : null,
    reddit_comment_count: extra.reddit ? 100 + id : null,
    audio_url: extra.audio ? `https://cdn.example.com/episodes/${id}.mp3` : null,
    duration_seconds: extra.audio ? 3600 + id * 13 : null,
    transcript_url: extra.transcript ? `https://cdn.example.com/episodes/${id}.vtt` : null,
    story_cluster_id: extra.cluster ?? null,
  }
}

const ENTRIES = [
  // --- news (bbc=1, hn=2, long=3, verge=4) ---
  mkEntry(101, 3, TITLE_180, { image: true, score: 92, summary: LONG_SUMMARY_A, cluster: 7, ago: 12 }),
  mkEntry(102, 2, TITLE_WITH_TOKEN, { score: 81, summary: 'Changelog: ' + TOKEN_90 + ' and the migration guide.', ago: 25 }),
  // No preview (PR5a): the server found nothing worth showing.
  mkEntry(103, 1, 'BC wildfire season officially over, says province', { image: true, score: 64, summary: SHORT_SUMMARY, ago: 40, preview: null }),
  mkEntry(104, 1, 'Interest rate decision: Bank of Canada holds at 2.75% as inflation cools but housing starts slip for a third straight month', { score: 70, reddit: true, ago: 55 }),
  mkEntry(105, 4, 'Apple quietly confirms the iPhone 17 Air will not get a second camera, and that is the point', { image: true, score: 58, ago: 70 }),
  mkEntry(106, 4, 'The Verge review: a $1,999 laptop that is somehow both too much and not enough', { score: 44, ago: 90 }),
  mkEntry(107, 2, 'Show HN: I built a 4-bit CPU out of relays and it runs Tetris at 0.3 FPS', { score: 77, reddit: true, ago: 110 }),
  mkEntry(108, 2, 'Ask HN: Who is hiring? (October 2026)', { score: 33, ago: 130 }),
  mkEntry(109, 3, 'Council approves 2027 capital plan after marathon eleven-hour meeting', { score: 51, ago: 150, summary: LONG_SUMMARY_A }),
  mkEntry(110, 3, 'Regional transit authority delays fare integration to Q3', { score: 22, ago: 170 }),
  mkEntry(111, 1, 'UK unveils plan to cut energy bills', { score: 12, ago: 190 }),
  mkEntry(112, 4, 'Microsoft is bringing Copilot to the Windows lock screen whether you like it or not', { image: true, score: 48, ago: 210 }),
  // --- deals (rfd=5, slick=6, r_bapc=7) ---
  mkEntry(201, 5, '[Amazon.ca] Anker 737 Power Bank (PowerCore 24K) 140W - $99.99 (was $179.99) lowest ever', { image: true, score: 88, ago: 15 }),
  mkEntry(202, 5, '[Costco] Dyson V15 Detect Absolute $649.99 in-store only, YMMV, ends Sunday', { score: 61, ago: 33 }),
  mkEntry(203, 6, 'Samsung 49" Odyssey OLED G9 Curved Gaming Monitor $899.99 + Free Shipping', { image: true, score: 73, ago: 48 }),
  mkEntry(204, 6, 'Sony WH-1000XM6 $298', { score: 55, ago: 66 }),
  mkEntry(205, 7, '[Canada Computers] 9800X3D $589.99', { score: 90, reddit: true, ago: 80, summary: SHORT_SUMMARY }),
  mkEntry(206, 7, '[Memory Express] Price match thread for the week of Oct 13, post your finds and the store will honour them for 7 days', { score: 29, ago: 100 }),
  mkEntry(207, 5, '[Best Buy] LG C5 65" OLED $1,999.99 + 4 year warranty', { image: true, score: 67, ago: 125 }),
  mkEntry(208, 6, 'Logitech MX Master 4 $79', { score: 40, ago: 140 }),
  mkEntry(209, 7, '[Amazon.ca] WD_BLACK SN850X 4TB NVMe $289 (Prime)', { score: 83, ago: 160 }),
  mkEntry(210, 5, 'Free: Google One 2TB for 6 months with Pixel purchase', { score: 18, ago: 180 }),
  // --- security (nvd=8, cisa=9, krebs=10) ---
  mkEntry(301, 8, 'CVE-2026-41877: Authentication bypass in FortiOS SSL-VPN web portal (CVSS 9.8)', { score: 97, summary: LONG_SUMMARY_B, cluster: 7, ago: 5 }),
  mkEntry(302, 8, 'CVE-2026-41902: Heap overflow in libwebp lossless decoder', { score: 85, summary: LONG_SUMMARY_B, ago: 30 }),
  mkEntry(303, 9, 'CISA adds three Ivanti Connect Secure flaws to KEV catalog, federal agencies have until Nov 7', { score: 91, reddit: true, ago: 45 }),
  mkEntry(304, 10, 'Patch Tuesday, October 2026 Edition', { image: true, score: 60, ago: 60 }),
  mkEntry(305, 10, 'The Great Un-Jailbreaking: how one SIM-swap gang got caught by their own Telegram backups', { score: 52, ago: 95, summary: LONG_SUMMARY_B }),
  mkEntry(306, 8, 'CVE-2026-42001: Improper input validation in OpenSSH agent forwarding', { score: 71, ago: 120 }),
  mkEntry(307, 9, 'Emergency directive 26-03: disconnect end-of-life Cisco ASA devices', { score: 94, ago: 135 }),
  mkEntry(308, 8, 'CVE-2026-42110: SQL injection in WordPress plugin "Forminator" < 1.44.3', { score: 36, ago: 155 }),
  mkEntry(309, 10, 'Why your password manager\'s browser extension is the weakest link', { score: 47, ago: 175 }),
  // --- podcast (darknet=11, riskybiz=12) ---
  mkEntry(401, 11, 'EP 160: The Hospital', { audio: true, transcript: true, score: 79, summary: LONG_SUMMARY_C, ago: 100 }),
  mkEntry(402, 11, 'EP 159: Shadow Brokers, Revisited — What We Know Now, a Decade Later, About the Leak That Changed Everything', { audio: true, score: 66, summary: LONG_SUMMARY_C, ago: 1500 }),
  mkEntry(403, 12, 'Risky Business #790 — Ivanti, again', { audio: true, score: 54, ago: 3000, summary: LONG_SUMMARY_C }),
  mkEntry(404, 12, 'Risky Bulletin: Microsoft fixes 117 bugs', { audio: true, transcript: true, score: 41, ago: 4100 }),
  mkEntry(405, 11, 'EP 158: The Pig Butcher', { audio: true, score: 69, ago: 6000 }),
  // --- video (anthropic=13, veritasium=14) ---
  mkEntry(501, 13, 'Claude Code: building a full app from a single prompt (live demo)', { image: true, score: 76, ago: 220 }),
  mkEntry(502, 14, 'The Most Misunderstood Concept in Physics', { image: true, score: 82, ago: 300 }),
  mkEntry(503, 14, 'Why Nobody Can Explain How This Bridge Is Still Standing After 2,000 Years of Earthquakes and Floods', { image: true, score: 63, ago: 2000 }),
  mkEntry(504, 13, 'Prompt caching explained in 4 minutes', { score: 38, ago: 2500 }),
  mkEntry(505, 14, 'I Spent $10,000 On A Single Photo', { image: true, score: 57, ago: 4000 }),
  mkEntry(506, 13, 'Model context protocol (MCP) deep dive with the team that built it, part two of the three part series', { score: 49, ago: 5000 }),
  // --- nhl (nhl_news=16): exactly 3 entries in the last 2 days (the tab
  // every tab loads, PR4d), one 3 days old (604: in the 7-day window)
  // and one 10 days old (605: only in All). Their scores put them among
  // the 2-day ones under Best (601, 605, 602, 604, 603), so a wider row
  // appended at the end instead of at its place shows. NHL's cards have
  // no preview (PR5a): the 2-day tab, with its end row, fits on one
  // 390x844 screen, which the read-model flows lean on ---
  mkEntry(601, 16, 'Canucks sign Pettersson to an eight-year extension', { image: true, score: 72, ago: 22, summary: SHORT_SUMMARY, preview: null }),
  mkEntry(602, 16, 'Oilers beat Flames 4-3 in overtime', { score: 46, ago: 140, summary: SHORT_SUMMARY, preview: null }),
  mkEntry(603, 16, 'Trade deadline primer: every team\'s cap space, needs and likely sellers', { score: 39, ago: 400, preview: null }),
  // (604's title doesn't name Pettersson: a search for him finds 601
  // alone, which flow-card-sheet's "Hide on the only result" leans on.)
  mkEntry(604, 16, 'Three days on: what the long-term deal means for the cap', { score: 45, ago: 3 * 24 * 60, summary: SHORT_SUMMARY, preview: null }),
  mkEntry(605, 16, 'Preseason power rankings, ten days out', { score: 50, ago: 10 * 24 * 60, summary: SHORT_SUMMARY, preview: null }),
]

// The ranking's order. 304 is from the paused krebs_on_security (10): the
// server leaves it out since PR4b (and so does the mock, which serves
// FOR_YOU_RANKED); FOR_YOU is what For You shows.
const FOR_YOU_IDS = [101, 301, 102, 201, 401, 304, 104, 303, 502]
const FOR_YOU_RANKED = FOR_YOU_IDS.map((id) => ENTRIES.find((e) => e.id === id))
const FOR_YOU = FOR_YOU_RANKED.filter((e) => SOURCES.find((s) => s.id === e.source_id).active)

const sourceName = (id) => (SOURCES.find((s) => s.id === id) || {}).name || 'unknown'

const SETTINGS = {
  llm_provider: 'ollama_cloud',
  llm_model_brief: 'gpt-oss:120b-cloud',
  llm_model_scoring: 'qwen3:30b-a3b-cloud',
  anthropic_api_key_set: true,
  openai_api_key_set: false,
  groq_api_key_set: false,
  ollama_cloud_api_key_set: true,
}

const LLM_TAGS = {
  models: [
    { name: 'gpt-oss:120b-cloud', size: null, family: 'gptoss', parameter_size: '120B', quantization_level: null, recommended: true, recommended_note: null },
    { name: 'qwen3:30b-a3b-cloud', size: null, family: 'qwen3', parameter_size: '30B', quantization_level: null, recommended: true, recommended_note: 'thinking' },
    { name: 'deepseek-v3.1:671b-cloud', size: null, family: 'deepseek', parameter_size: '671B', quantization_level: null, recommended: false, recommended_note: null },
    { name: 'kimi-k2:1t-cloud', size: null, family: 'kimi', parameter_size: '1T', quantization_level: null, recommended: false, recommended_note: null },
  ],
  cached_at: new Date(NOW - 60000).toISOString(),
  ttl_seconds: 3600,
}

module.exports = {
  NOW,
  SOURCES,
  ENTRIES,
  mkEntry,
  FOR_YOU,
  FOR_YOU_RANKED,
  SETTINGS,
  LLM_TAGS,
  sourceName,
  LONG_SOURCE_NAME,
  LONG_SOURCE_URL,
  TITLE_180,
  TOKEN_90,
  TITLE_WITH_TOKEN,
  LONG_SUMMARY_A,
  SEARCH_QUERY_60,
  CATEGORY_40,
  LAST_ERROR_300,
  VERGE_BLOCKED_ERROR,
  YOUTUBE_BLOCKED_MESSAGE,
}

if (require.main === module) {
  // Sanity-print the exact lengths so the report can cite them.
  console.log('LONG_SOURCE_NAME', LONG_SOURCE_NAME.length, LONG_SOURCE_NAME)
  console.log('LONG_SOURCE_URL ', LONG_SOURCE_URL.length, LONG_SOURCE_URL)
  console.log('TITLE_180       ', TITLE_180.length, TITLE_180)
  console.log('TOKEN_90        ', TOKEN_90.length, TOKEN_90)
  console.log('SEARCH_QUERY_60 ', SEARCH_QUERY_60.length, SEARCH_QUERY_60)
  console.log('CATEGORY_40     ', CATEGORY_40.length, CATEGORY_40)
  console.log('LAST_ERROR_300  ', LAST_ERROR_300.length)
  console.log('sources', SOURCES.length, 'categories', [...new Set(SOURCES.map((s) => s.category))])
  console.log('entries', ENTRIES.length)
}
