import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  checkName,
  classifyPaste,
  isBuiltIn,
  isYouTubeVideoLink,
  normalizeFeedInput,
  normalizeName,
  refreshLabel,
  suggestName,
} from '../../src/lib/feedRules.ts'

const ID = 'UCabcdefghijklmnopqrstuv'
const FEED = `https://www.youtube.com/feeds/videos.xml?channel_id=${ID}`

test('normalizeFeedInput', () => {
  assert.equal(normalizeFeedInput('youtube_channel', '  @mkbhd '), 'https://www.youtube.com/@mkbhd')
  assert.equal(normalizeFeedInput('youtube_channel', '@ゆっくり実況'), 'https://www.youtube.com/@%E3%82%86%E3%81%A3%E3%81%8F%E3%82%8A%E5%AE%9F%E6%B3%81')
  assert.equal(normalizeFeedInput('youtube_channel', ` ${ID} `), `https://www.youtube.com/channel/${ID}`)
  assert.equal(normalizeFeedInput('youtube_channel', 'youtube.com/@mkbhd'), 'https://youtube.com/@mkbhd')
  assert.equal(normalizeFeedInput('youtube_channel', '@ab'), '@ab')
  assert.equal(normalizeFeedInput('rss', 'example.com/feed.xml'), 'https://example.com/feed.xml')
  assert.equal(normalizeFeedInput('rss', '@mkbhd'), '@mkbhd')
  assert.equal(normalizeFeedInput('rss', ID), ID)
  assert.equal(normalizeFeedInput('reddit', 'r/python'), 'r/python')
  assert.equal(normalizeFeedInput('reddit', 'reddit.com/r/python'), 'reddit.com/r/python')
  assert.equal(normalizeFeedInput('generic_scrape', 'http://example.com'), 'http://example.com')
})

test('suggestName mirrors the backend YouTube slugs', () => {
  const none: string[] = []
  assert.equal(suggestName('youtube_channel', 'https://www.youtube.com/@Some.Body', none), 'some_body_yt')
  assert.equal(suggestName('youtube_channel', '@Some.Body', none), 'some_body_yt')
  assert.equal(suggestName('youtube_channel', 'https://www.youtube.com/c/Name', none), 'name_yt')
  assert.equal(suggestName('youtube_channel', 'https://www.youtube.com/user/LegacyName', none), 'legacyname_yt')
  assert.equal(suggestName('youtube_channel', 'https://www.youtube.com/feeds/videos.xml?user=LegacyName', none), 'legacyname_yt')
  assert.equal(suggestName('youtube_channel', 'https://www.youtube.com/playlist?list=PLabcdefghij123456', none), 'playlist_123456_yt')
  assert.equal(
    suggestName('youtube_channel', 'https://www.youtube.com/playlist?list=PLabcdefghij123456', none, 'https://www.youtube.com/feeds/videos.xml?playlist_id=PLabcdefghij123456'),
    'playlist_123456_yt',
  )
  assert.equal(suggestName('youtube_channel', `https://www.youtube.com/playlist?list=UU${ID.slice(2)}`, none), 'channel_qrstuv_yt')
  assert.equal(suggestName('youtube_channel', ID, none), 'channel_qrstuv_yt')
  assert.equal(suggestName('youtube_channel', `https://www.youtube.com/channel/${ID}/videos`, none), 'channel_qrstuv_yt')
  assert.equal(suggestName('youtube_channel', FEED, none), 'channel_qrstuv_yt')
  // The channel is only known after Test for video links and non-Latin handles.
  assert.equal(suggestName('youtube_channel', 'https://youtu.be/abcdefghijk', none), null)
  assert.equal(suggestName('youtube_channel', 'https://youtu.be/abcdefghijk', none, FEED), 'channel_qrstuv_yt')
  assert.equal(suggestName('youtube_channel', '@ゆっくり実況', none), null)
  assert.equal(suggestName('youtube_channel', '@ゆっくり実況', none, FEED), 'channel_qrstuv_yt')
  const dashed = 'https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnop-r_t-v'
  assert.equal(suggestName('youtube_channel', 'https://youtu.be/abcdefghijk', none, dashed), 'channel__r_t_v_yt')
})

test('suggestName for other types, de-duplicated', () => {
  assert.equal(suggestName('reddit', 'r/Python', []), 'r_python')
  assert.equal(suggestName('reddit', 'https://www.reddit.com/r/python/top', []), 'r_python')
  assert.equal(suggestName('reddit', 'python', ['r_python']), 'r_python_2')
  assert.equal(suggestName('rss', 'https://www.theverge.com/rss/index.xml', []), 'theverge_com')
  assert.equal(suggestName('rss', 'theverge.com/rss', ['theverge_com', 'theverge_com_2']), 'theverge_com_3')
  assert.equal(suggestName('podcast', 'not a url', []), null)
  assert.equal(suggestName('youtube_channel', 'https://example.com/feed', []), 'example_com')
  assert.equal(suggestName('rss', 'https://bbc.news/feed', ['bbc_news']), 'bbc_news_2')
})

test('isYouTubeVideoLink', () => {
  assert.equal(isYouTubeVideoLink('https://www.youtube.com/watch?v=abcdefghijk&t=1'), true)
  assert.equal(isYouTubeVideoLink('https://youtu.be/abcdefghijk?si=x'), true)
  assert.equal(isYouTubeVideoLink('https://m.youtube.com/shorts/abcdefghijk'), true)
  assert.equal(isYouTubeVideoLink('https://www.youtube.com/live/abcdefghijk'), true)
  assert.equal(isYouTubeVideoLink('https://www.youtube.com/@mkbhd'), false)
  assert.equal(isYouTubeVideoLink('https://example.com/watch?v=abcdefghijk'), false)
})

test('classifyPaste', () => {
  assert.equal(classifyPaste('@mkbhd'), 'youtube')
  assert.equal(classifyPaste(ID), 'youtube')
  assert.equal(classifyPaste('youtube.com/@mkbhd'), 'youtube')
  assert.equal(classifyPaste('https://youtu.be/abcdefghijk'), 'youtube')
  assert.equal(classifyPaste('r/python'), 'reddit')
  assert.equal(classifyPaste('https://www.reddit.com/r/python/'), 'reddit')
  assert.equal(classifyPaste('old.reddit.com/r/python'), 'reddit')
  assert.equal(classifyPaste('https://www.reddit.com/user/someone'), 'other')
  assert.equal(classifyPaste('https://podcasts.apple.com/ca/podcast/x/id123'), 'podcast')
  assert.equal(classifyPaste('theverge.com'), 'other')
  assert.equal(classifyPaste('youtube.com.evil.example/@x'), 'other')
})

test('normalizeName and checkName', () => {
  assert.equal(normalizeName('  The Verge-Tech! '), 'the_verge_tech')
  assert.deepEqual(checkName('My Feed'), { ok: true, value: 'my_feed' })
  assert.deepEqual(checkName('!!!'), { ok: false, message: 'Type a name.' })
  assert.deepEqual(checkName('a'.repeat(121)), { ok: false, message: 'Keep it to 120 characters or fewer.' })
  assert.deepEqual(checkName('BBC News'), { ok: false, message: 'That name belongs to a built-in feed. Pick another.' })
})

test('built-ins and refresh labels', () => {
  assert.equal(isBuiltIn({ name: 'rfd_hot_deals' }), true)
  assert.equal(isBuiltIn({ name: 'the_verge' }), false)
  assert.equal(refreshLabel(3600), '1 hour')
  assert.equal(refreshLabel(7200), '2 h')
  assert.equal(refreshLabel(300), '5 min')
})
